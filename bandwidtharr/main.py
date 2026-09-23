import logging
import os
import time
from datetime import datetime, timezone

from bandwidtharr import webserver
from bandwidtharr.allocator import Arbitrator
from bandwidtharr.link_detector import BACKUP, LinkStateTracker, build_link_detector, next_link_check_decision
from bandwidtharr.qbittorrent import QBittorrentClient
from bandwidtharr.sabnzbd import SabnzbdClient
from bandwidtharr.slack import build_slack_notifier
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


# How long one app's API must stay continuously unreachable before the
# outage is treated as real (not a VPN reconnect blip) and the full
# effective budget is handed to the app that IS still reachable, instead
# of arbitration sitting frozen at a split sized for two apps while only
# one is actually being served.
PEER_HANDOVER_SECONDS = 60.0


def should_hand_out_budget(now: float, peer_unreachable_since: float | None, threshold_seconds: float) -> bool:
    """True once the peer app has been continuously unreachable (its first
    failure timestamp is `peer_unreachable_since`, None while reachable)
    for at least `threshold_seconds`."""
    if peer_unreachable_since is None:
        return False
    return now - peer_unreachable_since >= threshold_seconds


def main() -> None:
    total = mbps_to_bytes(float(os.environ.get("TOTAL_LIMIT_MBPS", "800")))
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "3"))
    active_threshold = mbps_to_bytes(float(os.environ.get("ACTIVE_THRESHOLD_MBPS", "2")))
    reallocation_settle_seconds = float(os.environ.get("REALLOCATION_SETTLE_SECONDS", "30"))
    overshoot_settle_seconds = float(os.environ.get("OVERSHOOT_SETTLE_SECONDS", "15"))

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
    slack_notifier = build_slack_notifier(os.environ)
    next_link_check = 0.0
    link_ok = True
    link_error = None
    last_link_check_at = None
    qbit_fail_count = 0
    sab_fail_count = 0
    link_fail_count = 0
    qbit_upload_fail_count = 0
    qbit_unreachable_since = None
    sab_unreachable_since = None
    last_qbit_handover_limit = None
    last_sab_handover_limit = None
    pending_link_changed = False

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
    state = SharedState(link_events_file="/app/state/failover_log.json")
    web_port = int(os.environ.get("WEB_PORT", "80"))
    webserver.start(state, web_port)

    arbitrator = Arbitrator(total)

    log.info(
        "starting: total=%.0fMbps poll=%ss web_port=%s link_detector=%s%s%s",
        total * 8 / 1_000_000, poll_interval, web_port,
        link_detector_kind,
        f" backup_total={backup_total * 8 / 1_000_000:.0f}Mbps" if backup_total else "",
        f" qbit_upload_limit={qbit_upload_limit * 8 / 1_000_000:.0f}Mbps" if qbit_upload_limit else "",
    )

    while True:
        qbit_ok, sab_ok = True, True
        qbit_error, sab_error = None, None
        qbit_speed, sab_speed = 0.0, 0.0
        link_event = None

        try:
            qbit_speed = qbit.get_download_speed()
            qbit_fail_count = 0
            qbit_unreachable_since = None
        except Exception as e:
            qbit_ok = False
            qbit_error = str(e)
            qbit_fail_count += 1
            if qbit_fail_count == 1:
                qbit_unreachable_since = time.time()
            if should_log_repeated_failure(qbit_fail_count):
                log.warning("qbit unreachable (%dx): %s", qbit_fail_count, e)

        try:
            sab_speed = sab.get_download_speed()
            sab_fail_count = 0
            sab_unreachable_since = None
        except Exception as e:
            sab_ok = False
            sab_error = str(e)
            sab_fail_count += 1
            if sab_fail_count == 1:
                sab_unreachable_since = time.time()
            if should_log_repeated_failure(sab_fail_count):
                log.warning("sab unreachable (%dx): %s", sab_fail_count, e)

        now = time.time()
        combined_speed = qbit_speed + sab_speed
        # Also use the fast cadence whenever currently on the backup link,
        # regardless of download activity -- otherwise a check made while
        # idle right as the router fails back can fall onto the slow idle
        # cadence, and a single transient failure right then (a dropped
        # DNS query, brief routing flux during the switch itself) plus the
        # confirm-count's second reading can each independently land on
        # that same slow cadence -- worst case, tens of minutes before a
        # "recovered" event ever fires.
        is_active = combined_speed >= link_check_min_speed or link_tracker.confirmed == BACKUP
        check_due, link_check_interval_to_use = next_link_check_decision(
            now, next_link_check, is_active, link_check_interval, link_check_idle_interval,
        )
        if check_due:
            last_link_check_at = now
            try:
                reading, detail = link_detector.check()
                link_fail_count = 0
                log.debug("link check: %s (%s)", reading, detail)
                previous_link = link_tracker.confirmed
                confirmed_link = link_tracker.observe(reading)
                link_ok = True
                link_error = None
                if confirmed_link != previous_link:
                    # Sticky: if arbitration happens to be skipped this cycle
                    # (one app's API down), the reset still must reach the
                    # Arbitrator on the first resumed cycle instead of being
                    # silently lost -- hence pending_link_changed, consumed
                    # at the step() call below, rather than a per-cycle flag.
                    pending_link_changed = True
                    old_total_mbps = (backup_total if previous_link == BACKUP else total) * 8 / 1_000_000
                    new_total_mbps = (backup_total if confirmed_link == BACKUP else total) * 8 / 1_000_000
                    log.info(
                        "link %s: %s -> %s (budget %.0f -> %.0f Mbps) [%s]",
                        "failed over" if confirmed_link == BACKUP else "recovered",
                        previous_link, confirmed_link, old_total_mbps, new_total_mbps, detail,
                    )
                    link_event = (now, previous_link, confirmed_link, old_total_mbps, new_total_mbps)
                    if slack_notifier is not None:
                        # UTC explicitly, not the container's local time
                        # (often just whatever the base image defaults to,
                        # e.g. UTC regardless of where it's hosted) -- avoids
                        # ambiguity for anyone reading the message regardless
                        # of their own timezone.
                        event_time = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        # notify() sends in a background thread with its own
                        # retries and never raises, so no try/except needed
                        # here -- a slow/unreachable Slack API can't block
                        # this loop.
                        slack_notifier.notify(
                            "bandwidtharr: link %s (%s -> %s, budget %.0f -> %.0f Mbps) at %s" % (
                                "failed over" if confirmed_link == BACKUP else "recovered",
                                previous_link, confirmed_link, old_total_mbps, new_total_mbps, event_time,
                            )
                        )
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

        # Peer-outage handover: arbitration only runs when both apps are
        # reachable, so during a long outage of one app's API the surviving
        # app would otherwise sit throttled at a share sized for a two-way
        # split of a budget nobody is competing for. Deliberately touches
        # only the app's own applied limit, never the Arbitrator's
        # bookkeeping: the handover value (full effective budget) is
        # exactly what allocate() itself assigns while one side is idle, so
        # the first resumed cycle re-syncs naturally through the existing
        # paths. Re-checked every cycle, so a mid-outage link flip re-hands
        # over at the new budget; state clears as soon as both are reachable.
        if qbit_ok and should_hand_out_budget(now, sab_unreachable_since, PEER_HANDOVER_SECONDS):
            if last_qbit_handover_limit != effective_total:
                try:
                    qbit.set_download_limit(int(effective_total))
                    log.info(
                        "handing qbit full %.0f Mbps budget (sab unreachable > %.0fs)",
                        effective_total * 8 / 1_000_000, PEER_HANDOVER_SECONDS,
                    )
                    last_qbit_handover_limit = effective_total
                except Exception as e:
                    log.warning("failed to apply qbit handover limit: %s", e)
        else:
            last_qbit_handover_limit = None

        if sab_ok and should_hand_out_budget(now, qbit_unreachable_since, PEER_HANDOVER_SECONDS):
            if last_sab_handover_limit != effective_total:
                try:
                    sab.set_download_limit(int(effective_total))
                    log.info(
                        "handing sab full %.0f Mbps budget (qbit unreachable > %.0fs)",
                        effective_total * 8 / 1_000_000, PEER_HANDOVER_SECONDS,
                    )
                    last_sab_handover_limit = effective_total
                except Exception as e:
                    log.warning("failed to apply sab handover limit: %s", e)
        else:
            last_sab_handover_limit = None

        # Only arbitrate once both are reachable -- Arbitrator needs both
        # sides' real speed to mean anything, and there's nothing useful to
        # do with just one.
        if qbit_ok and sab_ok:
            new_qbit_limit, qbit_should_apply, new_sab_limit, sab_should_apply, overshoot_penalty = arbitrator.step(
                now=now,
                qbit_speed=qbit_speed,
                sab_speed=sab_speed,
                total=effective_total,
                active_threshold=active_threshold,
                reallocation_settle_seconds=reallocation_settle_seconds,
                link_changed=pending_link_changed,
                overshoot_settle_seconds=overshoot_settle_seconds,
            )
            pending_link_changed = False

            if qbit_should_apply:
                try:
                    qbit.set_download_limit(int(new_qbit_limit))
                    log.info(
                        "qbit limit %.0f -> %.0f Mbps%s",
                        arbitrator.qbit_limit * 8 / 1_000_000, new_qbit_limit * 8 / 1_000_000,
                        f" (overshoot -{overshoot_penalty * 8 / 1_000_000:.0f}Mbps)" if overshoot_penalty > 0 else "",
                    )
                    arbitrator.qbit_limit = new_qbit_limit
                except Exception as e:
                    qbit_ok = False
                    qbit_error = str(e)
                    log.warning("failed to set qbit limit: %s", e)

            if sab_should_apply:
                try:
                    sab.set_download_limit(int(new_sab_limit))
                    log.info(
                        "sab limit %.0f -> %.0f Mbps",
                        arbitrator.sab_limit * 8 / 1_000_000, new_sab_limit * 8 / 1_000_000,
                    )
                    arbitrator.sab_limit = new_sab_limit
                except Exception as e:
                    sab_ok = False
                    sab_error = str(e)
                    log.warning("failed to set sab limit: %s", e)

        state.update(
            effective_total, qbit_speed, arbitrator.qbit_limit, sab_speed, arbitrator.sab_limit,
            qbit_ok=qbit_ok, sab_ok=sab_ok, qbit_error=qbit_error, sab_error=sab_error,
            link_enabled=link_enabled, active_link=link_tracker.confirmed,
            link_ok=link_ok, link_error=link_error,
            last_link_check_at=last_link_check_at, next_link_check=next_link_check,
            downloading=is_active, link_event=link_event,
        )

        log.debug(
            "qbit speed=%.1fMbps limit=%.1fMbps fair_share=%.1fMbps ok=%s | "
            "sab speed=%.1fMbps limit=%.1fMbps ok=%s | overshoot_penalty=%.1fMbps",
            qbit_speed * 8 / 1_000_000, arbitrator.qbit_limit * 8 / 1_000_000,
            arbitrator.qbit_fair_share * 8 / 1_000_000, qbit_ok,
            sab_speed * 8 / 1_000_000, arbitrator.sab_limit * 8 / 1_000_000, sab_ok,
            arbitrator.overshoot_compensator.penalty * 8 / 1_000_000,
        )

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
