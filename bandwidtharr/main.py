import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from bandwidtharr import webserver
from bandwidtharr.allocator import Arbitrator
from bandwidtharr.link_detector import BACKUP, LinkStateTracker, build_link_detector, next_link_check_decision
from bandwidtharr.qbittorrent import QBittorrentClient
from bandwidtharr.sabnzbd import SabnzbdClient
from bandwidtharr.slack import build_slack_notifier
from bandwidtharr.state import SharedState

log = logging.getLogger("bandwidtharr")


def mbps_to_bytes(mbps: float) -> float:
    return mbps * 1_000_000 / 8


def optional_mbps_env(name: str, env=None) -> float | None:
    """Like os.environ.get(name), but treats an unset OR blank value (e.g.
    `FOO=` in .env, which docker compose still passes through as an empty
    string rather than omitting the var) as "not configured", converted to
    bytes/sec. Distinguishes "not configured" from an explicit 0."""
    raw = (os.environ if env is None else env).get(name, "").strip()
    return mbps_to_bytes(float(raw)) if raw else None


def should_log_repeated_failure(count: int) -> bool:
    """Log the 1st occurrence, then every 20th (~once/min at the default 3s
    poll interval) -- avoids a warning every single poll cycle for the
    duration of a sustained outage."""
    return count == 1 or count % 20 == 0


# How long one app's API must stay continuously unreachable before the
# outage is treated as real (not a VPN reconnect blip). Until then
# arbitration just pauses; after it, the unreachable app is arbitrated as
# idle, so the app that IS reachable gets the full budget instead of
# sitting at a split sized for two.
PEER_OUTAGE_CONFIRM_SECONDS = 60.0

# Match the binhex/arch-qbittorrentvpn and binhex/arch-sabnzbdvpn images'
# container names and ports, as documented in the README. Used when the
# variable is unset or blank.
DEFAULT_QBIT_URL = "http://binhex-qbittorrentvpn:8080"
DEFAULT_SAB_URL = "http://binhex-sabnzbdvpn:8080"


def outage_confirmed(now: float, unreachable_since: float | None, threshold_seconds: float) -> bool:
    """True once an app has been continuously unreachable (its first
    failure timestamp is `unreachable_since`, None while reachable) for at
    least `threshold_seconds`."""
    if unreachable_since is None:
        return False
    return now - unreachable_since >= threshold_seconds


@dataclass
class Config:
    """Everything main() reads from the environment, in bytes/sec and
    seconds. Built via from_env() in production; tests construct it
    directly."""

    total: float
    poll_interval: float = 3.0
    active_threshold: float = mbps_to_bytes(2)
    reallocation_settle_seconds: float = 30.0
    overshoot_settle_seconds: float = 15.0
    link_detector_kind: str = "none"
    backup_total: float | None = None
    qbit_upload_limit: float | None = None
    qbit_upload_limit_backup: float | None = None
    link_check_interval: float = 30.0
    link_check_idle_interval: float = 900.0
    link_check_min_speed: float = mbps_to_bytes(5)
    link_confirm_count: int = 2
    web_port: int = 80

    @property
    def link_enabled(self) -> bool:
        return self.link_detector_kind not in ("", "none")

    def __post_init__(self):
        if self.link_enabled and self.backup_total is None:
            raise RuntimeError("BACKUP_TOTAL_LIMIT_MBPS must be set when LINK_DETECTOR is enabled")
        if self.qbit_upload_limit_backup is not None and self.qbit_upload_limit is None:
            raise RuntimeError("QBIT_UPLOAD_LIMIT_MBPS must be set when QBIT_UPLOAD_LIMIT_BACKUP_MBPS is set")

    @classmethod
    def from_env(cls, env) -> "Config":
        return cls(
            total=mbps_to_bytes(float(env.get("TOTAL_LIMIT_MBPS", "800"))),
            poll_interval=float(env.get("POLL_INTERVAL_SECONDS", "3")),
            active_threshold=mbps_to_bytes(float(env.get("ACTIVE_THRESHOLD_MBPS", "2"))),
            reallocation_settle_seconds=float(env.get("REALLOCATION_SETTLE_SECONDS", "30")),
            overshoot_settle_seconds=float(env.get("OVERSHOOT_SETTLE_SECONDS", "15")),
            link_detector_kind=env.get("LINK_DETECTOR", "none").strip().lower(),
            backup_total=optional_mbps_env("BACKUP_TOTAL_LIMIT_MBPS", env),
            # Static qBittorrent upload cap, independent of the download
            # arbitration -- optional, .env-only, off (untouched) unless
            # configured.
            qbit_upload_limit=optional_mbps_env("QBIT_UPLOAD_LIMIT_MBPS", env),
            qbit_upload_limit_backup=optional_mbps_env("QBIT_UPLOAD_LIMIT_BACKUP_MBPS", env),
            link_check_interval=float(env.get("LINK_CHECK_INTERVAL_SECONDS", "30")),
            link_check_idle_interval=float(env.get("LINK_CHECK_IDLE_INTERVAL_SECONDS", "900")),
            link_check_min_speed=mbps_to_bytes(float(env.get("LINK_CHECK_MIN_SPEED_MBPS", "5"))),
            link_confirm_count=int(env.get("LINK_FAILOVER_CONFIRM_COUNT", "2")),
            web_port=int(env.get("WEB_PORT", "80")),
        )


class AppHealth:
    """Per-app reachability bookkeeping: consecutive-failure count (for log
    throttling) and when the current outage started. Outage timestamps are
    monotonic (not wall-clock) so an NTP step -- e.g. the host booting with
    a stale clock and syncing minutes later -- can't turn a 5s blip into a
    60s "outage" or defer a real one."""

    def __init__(self, name: str):
        self.name = name
        self.fail_count = 0
        self.unreachable_since: float | None = None

    def succeeded(self, mono_now: float) -> bool:
        """Record a successful read. Returns True if this ends a confirmed
        outage, i.e. the shares need re-baselining."""
        returning = outage_confirmed(mono_now, self.unreachable_since, PEER_OUTAGE_CONFIRM_SECONDS)
        if returning:
            log.info("%s back after a confirmed outage -- re-baselining shares", self.name)
        self.fail_count = 0
        self.unreachable_since = None
        return returning

    def failed(self, mono_now: float, error: Exception) -> None:
        self.fail_count += 1
        if self.fail_count == 1:
            self.unreachable_since = mono_now
        if should_log_repeated_failure(self.fail_count):
            log.warning("%s unreachable (%dx): %s", self.name, self.fail_count, error)

    def outage_confirmed(self, mono_now: float) -> bool:
        return outage_confirmed(mono_now, self.unreachable_since, PEER_OUTAGE_CONFIRM_SECONDS)


class Controller:
    """One poll cycle of the main loop -- read both apps, check the WAN
    link, apply the upload cap, arbitrate download limits, publish state --
    with every I/O dependency and both clocks injected, so the whole cycle
    can be driven by a test against fakes."""

    def __init__(
        self,
        config: Config,
        qbit,
        sab,
        link_detector,
        state: SharedState,
        slack_notifier=None,
        clock=time.time,
        mono_clock=time.monotonic,
    ):
        self.config = config
        self.qbit = qbit
        self.sab = sab
        self.link_detector = link_detector
        self.state = state
        self.slack_notifier = slack_notifier
        self.clock = clock
        self.mono_clock = mono_clock

        self.arbitrator = Arbitrator(config.total)
        self.link_tracker = LinkStateTracker(confirm_count=config.link_confirm_count)
        self.qbit_health = AppHealth("qbit")
        self.sab_health = AppHealth("sab")
        self.next_link_check = 0.0
        self.link_ok = True
        self.link_error: str | None = None
        self.last_link_check_at: float | None = None
        self.link_fail_count = 0
        self.last_applied_upload_limit: float | None = None
        self.qbit_upload_fail_count = 0
        self.qbit_set_fail_count = 0
        self.sab_set_fail_count = 0
        # Sticky "the current shares are stale, start both apps from a
        # neutral baseline" request for the Arbitrator -- raised by a
        # confirmed link flip or by an app returning from a confirmed
        # outage, and kept until a step() actually consumes it, since
        # arbitration may be skipped in the cycle it's raised (one app's
        # API down).
        self.pending_rebaseline = False

    @property
    def effective_total(self) -> float:
        return self.config.backup_total if self.link_tracker.confirmed == BACKUP else self.config.total

    def cycle(self) -> None:
        cfg = self.config
        mono_now = self.mono_clock()
        qbit_speed, qbit_error = self._read_speed(self.qbit, self.qbit_health, mono_now)
        sab_speed, sab_error = self._read_speed(self.sab, self.sab_health, mono_now)
        qbit_ok, sab_ok = qbit_error is None, sab_error is None

        now = self.clock()
        # Also use the fast cadence whenever currently on the backup link,
        # regardless of download activity -- otherwise a check made while
        # idle right as the router fails back can fall onto the slow idle
        # cadence, and a single transient failure right then (a dropped
        # DNS query, brief routing flux during the switch itself) plus the
        # confirm-count's second reading can each independently land on
        # that same slow cadence -- worst case, tens of minutes before a
        # "recovered" event ever fires.
        is_active = qbit_speed + sab_speed >= cfg.link_check_min_speed or self.link_tracker.confirmed == BACKUP
        link_event = self._maybe_check_link(now, is_active)

        effective_total = self.effective_total
        if qbit_ok:
            self._apply_upload_limit()

        # Arbitration pauses while an app's API is briefly unreachable (a
        # VPN reconnect blip) -- a missing speed reading means nothing yet.
        # Once the outage is confirmed, the unreachable app is arbitrated as
        # idle (its speed already reads 0.0 -- the GET failed), so
        # allocate()'s lone-downloader rule gives the survivor the full
        # budget through the normal path. Nothing is ever applied to the
        # unreachable app, so its tracked limit stays exactly what's really
        # set in it; when it returns, pending_rebaseline (raised at the GET
        # above) restarts both apps from a neutral half/half baseline.
        qbit_out = self.qbit_health.outage_confirmed(mono_now)
        sab_out = self.sab_health.outage_confirmed(mono_now)
        if (qbit_ok or qbit_out) and (sab_ok or sab_out):
            qbit_set_error, sab_set_error = self._arbitrate(
                now, qbit_speed, sab_speed, effective_total, qbit_ok, sab_ok,
            )
            if qbit_set_error is not None:
                qbit_ok, qbit_error = False, qbit_set_error
            if sab_set_error is not None:
                sab_ok, sab_error = False, sab_set_error

        arbitrator = self.arbitrator
        self.state.update(
            effective_total, qbit_speed, arbitrator.qbit_limit, sab_speed, arbitrator.sab_limit,
            qbit_ok=qbit_ok, sab_ok=sab_ok, qbit_error=qbit_error, sab_error=sab_error,
            link_enabled=cfg.link_enabled, active_link=self.link_tracker.confirmed,
            link_ok=self.link_ok, link_error=self.link_error,
            last_link_check_at=self.last_link_check_at, next_link_check=self.next_link_check,
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

    def _read_speed(self, client, health: AppHealth, mono_now: float) -> tuple[float, str | None]:
        """Returns (speed, error): speed is 0.0 and error the failure
        message when the app's API is unreachable."""
        try:
            speed = client.get_download_speed()
        except Exception as e:
            health.failed(mono_now, e)
            return 0.0, str(e)
        if health.succeeded(mono_now):
            self.pending_rebaseline = True
        return speed, None

    def _maybe_check_link(self, now: float, is_active: bool) -> tuple | None:
        """Runs a link check if one is due. Returns the dashboard's
        link_event tuple on a confirmed flip, else None."""
        cfg = self.config
        check_due, interval = next_link_check_decision(
            now, self.next_link_check, is_active, cfg.link_check_interval, cfg.link_check_idle_interval,
        )
        if not check_due:
            return None

        link_event = None
        self.last_link_check_at = now
        try:
            reading, detail = self.link_detector.check()
            self.link_fail_count = 0
            log.debug("link check: %s (%s)", reading, detail)
            previous_link = self.link_tracker.confirmed
            confirmed_link = self.link_tracker.observe(reading)
            self.link_ok = True
            self.link_error = None
            if confirmed_link != previous_link:
                link_event = self._on_link_flip(now, previous_link, confirmed_link, detail)
        except Exception as e:
            self.link_ok = False
            self.link_error = str(e)
            self.link_fail_count += 1
            if should_log_repeated_failure(self.link_fail_count):
                log.warning("link detector check failed (%dx): %s", self.link_fail_count, e)
        self.next_link_check = now + interval
        return link_event

    def _on_link_flip(self, now: float, previous_link: str, confirmed_link: str, detail: str) -> tuple:
        cfg = self.config
        self.pending_rebaseline = True
        old_total_mbps = (cfg.backup_total if previous_link == BACKUP else cfg.total) * 8 / 1_000_000
        new_total_mbps = (cfg.backup_total if confirmed_link == BACKUP else cfg.total) * 8 / 1_000_000
        verb = "failed over" if confirmed_link == BACKUP else "recovered"
        log.info(
            "link %s: %s -> %s (budget %.0f -> %.0f Mbps) [%s]",
            verb, previous_link, confirmed_link, old_total_mbps, new_total_mbps, detail,
        )
        if self.slack_notifier is not None:
            # UTC explicitly, not the container's local time (often just
            # whatever the base image defaults to, e.g. UTC regardless of
            # where it's hosted) -- avoids ambiguity for anyone reading the
            # message regardless of their own timezone.
            event_time = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            # notify() sends in a background thread with its own retries and
            # never raises, so no try/except needed here -- a slow/unreachable
            # Slack API can't block this loop.
            self.slack_notifier.notify(
                "bandwidtharr: link %s (%s -> %s, budget %.0f -> %.0f Mbps) at %s" % (
                    verb, previous_link, confirmed_link, old_total_mbps, new_total_mbps, event_time,
                )
            )
        return (now, previous_link, confirmed_link, old_total_mbps, new_total_mbps)

    def _apply_upload_limit(self) -> None:
        """Static upload cap, independent of download arbitration -- only
        touches qBittorrent when configured, and only re-applies when the
        value that should be in effect actually changes (link failover
        swaps it, or this is the first time we've been able to set it)."""
        cfg = self.config
        if cfg.qbit_upload_limit is None:
            return
        effective_upload_limit = (
            cfg.qbit_upload_limit_backup
            if self.link_tracker.confirmed == BACKUP and cfg.qbit_upload_limit_backup is not None
            else cfg.qbit_upload_limit
        )
        if effective_upload_limit == self.last_applied_upload_limit:
            return
        try:
            self.qbit.set_upload_limit(int(effective_upload_limit))
        except Exception as e:
            self.qbit_upload_fail_count += 1
            if should_log_repeated_failure(self.qbit_upload_fail_count):
                log.warning("failed to set qbit upload limit (%dx): %s", self.qbit_upload_fail_count, e)
            return
        prev_str = (
            f"{self.last_applied_upload_limit * 8 / 1_000_000:.0f}"
            if self.last_applied_upload_limit is not None
            else "unset"
        )
        log.info("qbit upload limit %s -> %.0f Mbps", prev_str, effective_upload_limit * 8 / 1_000_000)
        self.last_applied_upload_limit = effective_upload_limit
        self.qbit_upload_fail_count = 0

    def _arbitrate(
        self, now: float, qbit_speed: float, sab_speed: float, effective_total: float, qbit_ok: bool, sab_ok: bool,
    ) -> tuple[str | None, str | None]:
        """Steps the Arbitrator and applies its decision to each reachable
        app. Returns (qbit_set_error, sab_set_error), None where the apply
        succeeded or wasn't needed."""
        cfg = self.config
        arbitrator = self.arbitrator
        new_qbit_limit, qbit_should_apply, new_sab_limit, sab_should_apply, overshoot_penalty = arbitrator.step(
            now=now,
            qbit_speed=qbit_speed,
            sab_speed=sab_speed,
            total=effective_total,
            active_threshold=cfg.active_threshold,
            reallocation_settle_seconds=cfg.reallocation_settle_seconds,
            rebaseline=self.pending_rebaseline,
            overshoot_settle_seconds=cfg.overshoot_settle_seconds,
        )
        self.pending_rebaseline = False
        qbit_set_error, sab_set_error = None, None

        if qbit_should_apply and qbit_ok:
            try:
                self.qbit.set_download_limit(int(new_qbit_limit))
                log.info(
                    "qbit limit %.0f -> %.0f Mbps%s",
                    arbitrator.qbit_limit * 8 / 1_000_000, new_qbit_limit * 8 / 1_000_000,
                    f" (overshoot -{overshoot_penalty * 8 / 1_000_000:.0f}Mbps)" if overshoot_penalty > 0 else "",
                )
                arbitrator.qbit_limit = new_qbit_limit
                self.qbit_set_fail_count = 0
            except Exception as e:
                qbit_set_error = str(e)
                # Throttled like the read failures: an app whose limit isn't
                # known yet is retried every cycle (see Arbitrator.step()).
                self.qbit_set_fail_count += 1
                if should_log_repeated_failure(self.qbit_set_fail_count):
                    log.warning("failed to set qbit limit (%dx): %s", self.qbit_set_fail_count, e)

        if sab_should_apply and sab_ok:
            try:
                self.sab.set_download_limit(int(new_sab_limit))
                log.info(
                    "sab limit %.0f -> %.0f Mbps",
                    arbitrator.sab_limit * 8 / 1_000_000, new_sab_limit * 8 / 1_000_000,
                )
                arbitrator.sab_limit = new_sab_limit
                self.sab_set_fail_count = 0
            except Exception as e:
                sab_set_error = str(e)
                self.sab_set_fail_count += 1
                if should_log_repeated_failure(self.sab_set_fail_count):
                    log.warning("failed to set sab limit (%dx): %s", self.sab_set_fail_count, e)

        return qbit_set_error, sab_set_error


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env(os.environ)
    link_detector = build_link_detector(os.environ)
    slack_notifier = build_slack_notifier(os.environ)

    qbit = QBittorrentClient(
        base_url=os.environ.get("QBIT_URL") or DEFAULT_QBIT_URL,
        username=os.environ.get("QBIT_USER") or None,
        password=os.environ.get("QBIT_PASS") or None,
    )
    sab = SabnzbdClient(
        base_url=os.environ.get("SAB_URL") or DEFAULT_SAB_URL,
        api_key=os.environ["SAB_API_KEY"],
    )

    # Start the dashboard before touching either app's API, so it's reachable
    # (and can show a disconnected status) even if qBittorrent/SABnzbd aren't
    # up yet -- there's no guaranteed startup ordering between containers.
    state = SharedState(link_events_file="/app/state/failover_log.json")
    webserver.start(state, config.web_port)

    controller = Controller(config, qbit, sab, link_detector, state, slack_notifier)

    log.info(
        "starting: total=%.0fMbps poll=%ss web_port=%s link_detector=%s%s%s",
        config.total * 8 / 1_000_000, config.poll_interval, config.web_port,
        config.link_detector_kind,
        f" backup_total={config.backup_total * 8 / 1_000_000:.0f}Mbps" if config.backup_total else "",
        f" qbit_upload_limit={config.qbit_upload_limit * 8 / 1_000_000:.0f}Mbps" if config.qbit_upload_limit else "",
    )

    while True:
        controller.cycle()
        time.sleep(config.poll_interval)


if __name__ == "__main__":
    main()
