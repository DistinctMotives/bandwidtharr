import logging
import os
import time

from speedarr import webserver
from speedarr.allocator import allocate
from speedarr.qbittorrent import QBittorrentClient
from speedarr.sabnzbd import SabnzbdClient
from speedarr.state import SharedState

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("speedarr")


def mbps_to_bytes(mbps: float) -> float:
    return mbps * 1_000_000 / 8


def main() -> None:
    total = mbps_to_bytes(float(os.environ.get("TOTAL_LIMIT_MBPS", "800")))
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "3"))
    min_floor = mbps_to_bytes(float(os.environ.get("MIN_FLOOR_MBPS", "40")))
    active_threshold = mbps_to_bytes(float(os.environ.get("ACTIVE_THRESHOLD_MBPS", "2")))
    probe_step = mbps_to_bytes(float(os.environ.get("PROBE_STEP_MBPS", "40")))
    change_threshold = float(os.environ.get("CHANGE_THRESHOLD_FRACTION", "0.05"))

    qbit = QBittorrentClient(
        base_url=os.environ["QBIT_URL"],
        username=os.environ.get("QBIT_USER") or None,
        password=os.environ.get("QBIT_PASS") or None,
    )
    sab = SabnzbdClient(
        base_url=os.environ["SAB_URL"],
        api_key=os.environ["SAB_API_KEY"],
    )

    qbit_limit = qbit.get_download_limit() or total
    sab_limit = sab.get_download_limit() or total

    state = SharedState()
    web_port = int(os.environ.get("WEB_PORT", "80"))
    webserver.start(state, web_port)

    log.info(
        "starting: total=%.0fMbps floor=%.0fMbps poll=%ss web_port=%s",
        total * 8 / 1_000_000, min_floor * 8 / 1_000_000, poll_interval, web_port,
    )

    first_cycle = True
    while True:
        try:
            qbit_speed = qbit.get_download_speed()
            sab_speed = sab.get_download_speed()

            new_qbit_limit, new_sab_limit = allocate(
                qbit_speed=qbit_speed,
                sab_speed=sab_speed,
                qbit_limit=qbit_limit,
                sab_limit=sab_limit,
                total=total,
                min_floor=min_floor,
                active_threshold=active_threshold,
                probe_step=probe_step,
            )

            # On the first cycle, force-apply regardless of the change threshold so a
            # stale pre-existing limit (set manually, or from a previous speedarr run
            # with different settings) doesn't linger just because it happens to fall
            # within the normal hysteresis band.
            if first_cycle or abs(new_qbit_limit - qbit_limit) >= total * change_threshold:
                qbit.set_download_limit(int(new_qbit_limit))
                log.info("qbit limit %.0f -> %.0f Mbps", qbit_limit * 8 / 1_000_000, new_qbit_limit * 8 / 1_000_000)
                qbit_limit = new_qbit_limit
            if first_cycle or abs(new_sab_limit - sab_limit) >= total * change_threshold:
                sab.set_download_limit(int(new_sab_limit))
                log.info("sab limit %.0f -> %.0f Mbps", sab_limit * 8 / 1_000_000, new_sab_limit * 8 / 1_000_000)
                sab_limit = new_sab_limit
            first_cycle = False

            state.update(total, qbit_speed, qbit_limit, sab_speed, sab_limit)

            log.debug(
                "qbit speed=%.1fMbps limit=%.1fMbps | sab speed=%.1fMbps limit=%.1fMbps",
                qbit_speed * 8 / 1_000_000, qbit_limit * 8 / 1_000_000,
                sab_speed * 8 / 1_000_000, sab_limit * 8 / 1_000_000,
            )
        except Exception:
            log.exception("poll cycle failed, will retry")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
