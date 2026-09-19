import logging
import os
import time

from bandwidtharr import webserver
from bandwidtharr.allocator import allocate
from bandwidtharr.link_detector import BACKUP, LinkStateTracker, build_link_detector, next_link_check_decision
from bandwidtharr.qbittorrent import QBittorrentClient
from bandwidtharr.sabnzbd import SabnzbdClient
from bandwidtharr.state import SharedState

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bandwidtharr")


def mbps_to_bytes(mbps: float) -> float:
    return mbps * 1_000_000 / 8


def optional_mbps_env(name: str) -> float | None:
    """Like os.environ.get(name), but treats an unset OR blank value (e.g.
    `FOO=` in .env, which docker compose still passes through as an empty
    string rather than omitting the var) as "not configured", converted to
    bytes/sec. Distinguishes "not configured" from an explicit 0."""
    raw = os.environ.get(name, "").strip()
    return mbps_to_bytes(float(raw)) if raw else None


def should_log_repeated_failure(count: int) -> bool:
    """Log the 1st occurrence, then every 20th (~once/min at the default 3s
    poll interval) -- avoids a warning every single poll cycle for the
    duration of a sustained outage."""
    return count == 1 or count % 20 == 0


def main() -> None:
    total = mbps_to_bytes(float(os.environ.get("TOTAL_LIMIT_MBPS", "800")))
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "3"))
    min_floor = mbps_to_bytes(float(os.environ.get("MIN_FLOOR_MBPS", "40")))
    active_threshold = mbps_to_bytes(float(os.environ.get("ACTIVE_THRESHOLD_MBPS", "2")))
    probe_step = mbps_to_bytes(float(os.environ.get("PROBE_STEP_MBPS", "40")))
    change_threshold = float(os.environ.get("CHANGE_THRESHOLD_FRACTION", "0.05"))

    link_detector_kind = os.environ.get("LINK_DETECTOR", "none").strip().lower()
    link_enabled = link_detector_kind not in ("", "none")
    backup_total = optional_mbps_env("BACKUP_TOTAL_LIMIT_MBPS")
    if link_enabled and backup_total is None:
        raise RuntimeError("BACKUP_TOTAL_LIMIT_MBPS must be set when LINK_DETECTOR is enabled")

    # Static qBittorrent upload cap, independent of the download arbitration
    # above -- optional, .env-only, off (untouched) unless configured.
    qbit_upload_limit = optional_mbps_env("QBIT_UPLOAD_LIMIT_MBPS")
    qbit_upload_limit_backup = optional_mbps_env("QBIT_UPLOAD_LIMIT_BACKUP_MBPS")
    if qbit_upload_limit_backup is not None and qbit_upload_limit is None:
        raise RuntimeError("QBIT_UPLOAD_LIMIT_MBPS must be set when QBIT_UPLOAD_LIMIT_BACKUP_MBPS is set")
    last_applied_upload_limit = None

    link_check_interval = float(os.environ.get("LINK_CHECK_INTERVAL_SECONDS", "30"))
    link_check_idle_interval = float(os.environ.get("LINK_CHECK_IDLE_INTERVAL_SECONDS", "900"))
    link_check_min_speed = mbps_to_bytes(float(os.environ.get("LINK_CHECK_MIN_SPEED_MBPS", "5")))
    link_confirm_count = int(os.environ.get("LINK_FAILOVER_CONFIRM_COUNT", "2"))
    link_detector = build_link_detector(os.environ)
    link_tracker = LinkStateTracker(confirm_count=link_confirm_count)
    next_link_check = 0.0
    link_ok = True
    link_error = None
    last_link_check_at = None
    qbit_fail_count = 0
    sab_fail_count = 0
    link_fail_count = 0
    qbit_upload_fail_count = 0

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
        "starting: total=%.0fMbps floor=%.0fMbps poll=%ss web_port=%s link_detector=%s%s%s",
        total * 8 / 1_000_000, min_floor * 8 / 1_000_000, poll_interval, web_port,
        link_detector_kind,
        f" backup_total={backup_total * 8 / 1_000_000:.0f}Mbps" if backup_total else "",
        f" qbit_upload_limit={qbit_upload_limit * 8 / 1_000_000:.0f}Mbps" if qbit_upload_limit else "",
    )

    first_cycle = True
    while True:
        qbit_ok, sab_ok = True, True
        qbit_error, sab_error = None, None
        qbit_speed, sab_speed = 0.0, 0.0
        link_changed = False
        link_event = None

        try:
            qbit_speed = qbit.get_download_speed()
            qbit_fail_count = 0
        except Exception as e:
            qbit_ok = False
            qbit_error = str(e)
            qbit_fail_count += 1
            if should_log_repeated_failure(qbit_fail_count):
                log.warning("qbit unreachable (%dx): %s", qbit_fail_count, e)

        try:
            sab_speed = sab.get_download_speed()
            sab_fail_count = 0
        except Exception as e:
            sab_ok = False
            sab_error = str(e)
            sab_fail_count += 1
            if should_log_repeated_failure(sab_fail_count):
                log.warning("sab unreachable (%dx): %s", sab_fail_count, e)

        now = time.time()
        combined_speed = qbit_speed + sab_speed
        is_active = combined_speed >= link_check_min_speed
        check_due, link_check_interval_to_use = next_link_check_decision(
            now, next_link_check, is_active, link_check_interval, link_check_idle_interval,
        )
        if check_due:
            last_link_check_at = now
            try:
                reading, _detail = link_detector.check()
                link_fail_count = 0
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
                    link_event = (now, previous_link, confirmed_link, old_total_mbps, new_total_mbps)
            except Exception as e:
                link_ok = False
                link_error = str(e)
                link_fail_count += 1
                if should_log_repeated_failure(link_fail_count):
                    log.warning("link detector check failed (%dx): %s", link_fail_count, e)
            next_link_check = now + link_check_interval_to_use

        effective_total = backup_total if link_tracker.confirmed == BACKUP else total

        # Static upload cap, independent of the download arbitration below --
        # only touches qBittorrent when configured, and only re-applies when
        # the value that should be in effect actually changes (link failover
        # swaps it, or this is the first time we've been able to set it).
        if qbit_ok and qbit_upload_limit is not None:
            effective_upload_limit = (
                qbit_upload_limit_backup
                if link_tracker.confirmed == BACKUP and qbit_upload_limit_backup is not None
                else qbit_upload_limit
            )
            if effective_upload_limit != last_applied_upload_limit:
                try:
                    qbit.set_upload_limit(int(effective_upload_limit))
                    prev_str = (
                        f"{last_applied_upload_limit * 8 / 1_000_000:.0f}"
                        if last_applied_upload_limit is not None
                        else "unset"
                    )
                    log.info(
                        "qbit upload limit %s -> %.0f Mbps",
                        prev_str, effective_upload_limit * 8 / 1_000_000,
                    )
                    last_applied_upload_limit = effective_upload_limit
                    qbit_upload_fail_count = 0
                except Exception as e:
                    qbit_upload_fail_count += 1
                    if should_log_repeated_failure(qbit_upload_fail_count):
                        log.warning("failed to set qbit upload limit (%dx): %s", qbit_upload_fail_count, e)

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
            last_link_check_at=last_link_check_at, next_link_check=next_link_check,
            downloading=is_active, link_event=link_event,
        )

        log.debug(
            "qbit speed=%.1fMbps limit=%.1fMbps ok=%s | sab speed=%.1fMbps limit=%.1fMbps ok=%s",
            qbit_speed * 8 / 1_000_000, qbit_limit * 8 / 1_000_000, qbit_ok,
            sab_speed * 8 / 1_000_000, sab_limit * 8 / 1_000_000, sab_ok,
        )

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
