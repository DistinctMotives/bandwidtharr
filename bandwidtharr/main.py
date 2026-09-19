import logging
import os
import time

from bandwidtharr import webserver
from bandwidtharr.allocator import allocate
from bandwidtharr.link_detector import BACKUP, LinkStateTracker, build_link_detector
from bandwidtharr.qbittorrent import QBittorrentClient
from bandwidtharr.sabnzbd import SabnzbdClient
from bandwidtharr.state import SharedState

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bandwidtharr")


def mbps_to_bytes(mbps: float) -> float:
    return mbps * 1_000_000 / 8


def main() -> None:
    total = mbps_to_bytes(float(os.environ.get("TOTAL_LIMIT_MBPS", "800")))
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "3"))
    min_floor = mbps_to_bytes(float(os.environ.get("MIN_FLOOR_MBPS", "40")))
    active_threshold = mbps_to_bytes(float(os.environ.get("ACTIVE_THRESHOLD_MBPS", "2")))
    probe_step = mbps_to_bytes(float(os.environ.get("PROBE_STEP_MBPS", "40")))
    change_threshold = float(os.environ.get("CHANGE_THRESHOLD_FRACTION", "0.05"))

    link_detector_kind = os.environ.get("LINK_DETECTOR", "none").strip().lower()
    link_enabled = link_detector_kind not in ("", "none")
    if link_enabled and "BACKUP_TOTAL_LIMIT_MBPS" not in os.environ:
        raise RuntimeError("BACKUP_TOTAL_LIMIT_MBPS must be set when LINK_DETECTOR is enabled")
    backup_total = (
        mbps_to_bytes(float(os.environ["BACKUP_TOTAL_LIMIT_MBPS"]))
        if "BACKUP_TOTAL_LIMIT_MBPS" in os.environ
        else None
    )
    link_check_interval = float(os.environ.get("LINK_CHECK_INTERVAL_SECONDS", "30"))
    link_confirm_count = int(os.environ.get("LINK_FAILOVER_CONFIRM_COUNT", "2"))
    link_detector = build_link_detector(os.environ)
    link_tracker = LinkStateTracker(confirm_count=link_confirm_count)
    next_link_check = 0.0
    link_ok = True
    link_error = None

    qbit = QBittorrentClient(
        base_url=os.environ["QBIT_URL"],
        username=os.environ.get("QBIT_USER") or None,
        password=os.environ.get("QBIT_PASS") or None,
    )
    sab = SabnzbdClient(
        base_url=os.environ["SAB_URL"],
        api_key=os.environ["SAB_API_KEY"],
    )

    # Start the dashboard before touching either app's API, so it's reachable
    # (and can show a disconnected status) even if qBittorrent/SABnzbd aren't
    # up yet -- there's no guaranteed startup ordering between containers.
    state = SharedState()
    web_port = int(os.environ.get("WEB_PORT", "80"))
    webserver.start(state, web_port)

    qbit_limit = total
    sab_limit = total

    log.info(
        "starting: total=%.0fMbps floor=%.0fMbps poll=%ss web_port=%s link_detector=%s%s",
        total * 8 / 1_000_000, min_floor * 8 / 1_000_000, poll_interval, web_port,
        link_detector_kind,
        f" backup_total={backup_total * 8 / 1_000_000:.0f}Mbps" if backup_total else "",
    )

    first_cycle = True
    while True:
        qbit_ok, sab_ok = True, True
        qbit_error, sab_error = None, None
        qbit_speed, sab_speed = 0.0, 0.0
        link_changed = False

        now = time.time()
        if now >= next_link_check:
            try:
                reading = link_detector.check()
                previous_link = link_tracker.confirmed
                confirmed_link = link_tracker.observe(reading)
                link_ok = True
                link_error = None
                if confirmed_link != previous_link:
                    link_changed = True
                    old_total_mbps = (backup_total if previous_link == BACKUP else total) * 8 / 1_000_000
                    new_total_mbps = (backup_total if confirmed_link == BACKUP else total) * 8 / 1_000_000
                    log.info(
                        "link %s: %s -> %s (budget %.0f -> %.0f Mbps)",
                        "failed over" if confirmed_link == BACKUP else "recovered",
                        previous_link, confirmed_link, old_total_mbps, new_total_mbps,
                    )
            except Exception as e:
                link_ok = False
                link_error = str(e)
                log.warning("link detector check failed: %s", e)
            next_link_check = now + link_check_interval

        effective_total = backup_total if link_tracker.confirmed == BACKUP else total

        try:
            qbit_speed = qbit.get_download_speed()
        except Exception as e:
            qbit_ok = False
            qbit_error = str(e)
            log.warning("qbit unreachable: %s", e)

        try:
            sab_speed = sab.get_download_speed()
        except Exception as e:
            sab_ok = False
            sab_error = str(e)
            log.warning("sab unreachable: %s", e)

        # Only arbitrate once both are reachable -- allocate() needs both
        # sides' real speed to mean anything, and there's nothing useful to
        # do with just one.
        if qbit_ok and sab_ok:
            new_qbit_limit, new_sab_limit = allocate(
                qbit_speed=qbit_speed,
                sab_speed=sab_speed,
                qbit_limit=qbit_limit,
                sab_limit=sab_limit,
                total=effective_total,
                min_floor=min_floor,
                active_threshold=active_threshold,
                probe_step=probe_step,
            )

            # On the first successful cycle, force-apply regardless of the change
            # threshold so a stale pre-existing limit (set manually, or from a
            # previous bandwidtharr run with different settings) doesn't linger
            # just because it happens to fall within the normal hysteresis band.
            if first_cycle or link_changed or abs(new_qbit_limit - qbit_limit) >= effective_total * change_threshold:
                try:
                    qbit.set_download_limit(int(new_qbit_limit))
                    log.info(
                        "qbit limit %.0f -> %.0f Mbps",
                        qbit_limit * 8 / 1_000_000, new_qbit_limit * 8 / 1_000_000,
                    )
                    qbit_limit = new_qbit_limit
                except Exception as e:
                    qbit_ok = False
                    qbit_error = str(e)
                    log.warning("failed to set qbit limit: %s", e)

            if first_cycle or link_changed or abs(new_sab_limit - sab_limit) >= effective_total * change_threshold:
                try:
                    sab.set_download_limit(int(new_sab_limit))
                    log.info(
                        "sab limit %.0f -> %.0f Mbps",
                        sab_limit * 8 / 1_000_000, new_sab_limit * 8 / 1_000_000,
                    )
                    sab_limit = new_sab_limit
                except Exception as e:
                    sab_ok = False
                    sab_error = str(e)
                    log.warning("failed to set sab limit: %s", e)

            first_cycle = False

        state.update(
            effective_total, qbit_speed, qbit_limit, sab_speed, sab_limit,
            qbit_ok=qbit_ok, sab_ok=sab_ok, qbit_error=qbit_error, sab_error=sab_error,
            link_enabled=link_enabled, active_link=link_tracker.confirmed,
            link_ok=link_ok, link_error=link_error,
        )

        log.debug(
            "qbit speed=%.1fMbps limit=%.1fMbps ok=%s | sab speed=%.1fMbps limit=%.1fMbps ok=%s",
            qbit_speed * 8 / 1_000_000, qbit_limit * 8 / 1_000_000, qbit_ok,
            sab_speed * 8 / 1_000_000, sab_limit * 8 / 1_000_000, sab_ok,
        )

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
