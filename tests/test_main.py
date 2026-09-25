import pytest

from bandwidtharr.link_detector import BACKUP, PRIMARY
from bandwidtharr.main import (
    PEER_OUTAGE_CONFIRM_SECONDS,
    Config,
    Controller,
    mbps_to_bytes,
    optional_mbps_env,
    outage_confirmed,
    should_log_repeated_failure,
)
from bandwidtharr.state import SharedState


def test_outage_confirmed_no_outage():
    assert outage_confirmed(now=1000.0, unreachable_since=None, threshold_seconds=60.0) is False


def test_outage_confirmed_blip_under_threshold():
    assert outage_confirmed(now=1059.0, unreachable_since=1000.0, threshold_seconds=60.0) is False


def test_outage_confirmed_at_threshold():
    assert outage_confirmed(now=1060.0, unreachable_since=1000.0, threshold_seconds=60.0) is True


def test_outage_confirmed_past_threshold():
    assert outage_confirmed(now=5000.0, unreachable_since=1000.0, threshold_seconds=60.0) is True


def test_optional_mbps_env_unset(monkeypatch):
    monkeypatch.delenv("FOO_MBPS", raising=False)
    assert optional_mbps_env("FOO_MBPS") is None


def test_optional_mbps_env_blank(monkeypatch):
    monkeypatch.setenv("FOO_MBPS", "")
    assert optional_mbps_env("FOO_MBPS") is None


def test_optional_mbps_env_whitespace_only(monkeypatch):
    monkeypatch.setenv("FOO_MBPS", "   ")
    assert optional_mbps_env("FOO_MBPS") is None


def test_optional_mbps_env_valid_integer(monkeypatch):
    monkeypatch.setenv("FOO_MBPS", "5")
    assert optional_mbps_env("FOO_MBPS") == mbps_to_bytes(5.0)


def test_optional_mbps_env_valid_decimal(monkeypatch):
    monkeypatch.setenv("FOO_MBPS", "2.5")
    assert optional_mbps_env("FOO_MBPS") == mbps_to_bytes(2.5)


def test_optional_mbps_env_explicit_zero_is_not_none(monkeypatch):
    # 0 is a valid configured value, distinct from "not configured"
    monkeypatch.setenv("FOO_MBPS", "0")
    assert optional_mbps_env("FOO_MBPS") == 0.0


def test_optional_mbps_env_invalid_raises(monkeypatch):
    monkeypatch.setenv("FOO_MBPS", "not-a-number")
    with pytest.raises(ValueError):
        optional_mbps_env("FOO_MBPS")


def test_should_log_repeated_failure_first_occurrence():
    assert should_log_repeated_failure(1) is True


def test_should_log_repeated_failure_suppresses_in_between():
    assert should_log_repeated_failure(2) is False
    assert should_log_repeated_failure(19) is False


def test_should_log_repeated_failure_every_twentieth():
    assert should_log_repeated_failure(20) is True
    assert should_log_repeated_failure(40) is True


# --- Config ---------------------------------------------------------------


def test_config_from_env_defaults():
    config = Config.from_env({})
    assert config.total == mbps_to_bytes(800)
    assert config.link_enabled is False
    assert config.backup_total is None
    assert config.qbit_upload_limit is None


def test_config_requires_backup_total_when_link_detector_enabled():
    with pytest.raises(RuntimeError, match="BACKUP_TOTAL_LIMIT_MBPS"):
        Config.from_env({"LINK_DETECTOR": "public_ip"})


def test_config_backup_upload_limit_requires_primary_upload_limit():
    with pytest.raises(RuntimeError, match="QBIT_UPLOAD_LIMIT_MBPS"):
        Config.from_env({"QBIT_UPLOAD_LIMIT_BACKUP_MBPS": "10"})


# --- Controller -----------------------------------------------------------
#
# Drives full poll cycles against fake apps/detector and fake clocks, with
# the real Arbitrator and SharedState in the loop.

TOTAL = mbps_to_bytes(800)
BACKUP_TOTAL = mbps_to_bytes(50)
POLL = 3.0


class FakeApp:
    def __init__(self, speed=0.0):
        self.speed = speed
        self.reachable = True
        self.fail_sets = False
        self.download_limits = []
        self.upload_limits = []

    def get_download_speed(self):
        if not self.reachable:
            raise ConnectionError("connection refused")
        return self.speed

    def set_download_limit(self, limit):
        if not self.reachable or self.fail_sets:
            raise ConnectionError("set failed")
        self.download_limits.append(limit)

    def set_upload_limit(self, limit):
        if not self.reachable or self.fail_sets:
            raise ConnectionError("set failed")
        self.upload_limits.append(limit)


class FakeDetector:
    def __init__(self):
        self.reading = PRIMARY
        self.fail = False
        self.checks = 0

    def check(self):
        self.checks += 1
        if self.fail:
            raise TimeoutError("dns timeout")
        return self.reading, "detail"


class FakeSlack:
    def __init__(self):
        self.messages = []

    def notify(self, text):
        self.messages.append(text)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Harness:
    def __init__(self, **config_overrides):
        overrides = {"total": TOTAL, **config_overrides}
        self.config = Config(**overrides)
        self.qbit = FakeApp()
        self.sab = FakeApp()
        self.detector = FakeDetector()
        self.state = SharedState()
        self.slack = FakeSlack()
        self.clock = Clock()
        self.controller = Controller(
            self.config, self.qbit, self.sab, self.detector, self.state, self.slack,
            clock=self.clock, mono_clock=self.clock,
        )

    def run(self, cycles=1):
        for _ in range(cycles):
            self.controller.cycle()
            self.clock.t += POLL
        return self.state.snapshot()

    def run_for(self, seconds):
        return self.run(int(seconds // POLL))

    def clear_applied(self):
        self.qbit.download_limits.clear()
        self.sab.download_limits.clear()


def test_first_cycle_overwrites_both_apps_limits():
    h = Harness()
    snap = h.run()
    assert h.qbit.download_limits == [round(TOTAL)]
    assert h.sab.download_limits == [round(TOTAL)]
    assert snap["qbit_ok"] and snap["sab_ok"]


def test_both_active_splits_evenly():
    h = Harness()
    h.qbit.speed = h.sab.speed = mbps_to_bytes(390)
    snap = h.run()
    assert snap["qbit_limit"] == snap["sab_limit"] == TOTAL / 2


def test_brief_outage_pauses_arbitration():
    h = Harness()
    h.qbit.speed = h.sab.speed = mbps_to_bytes(390)
    h.run()
    h.clear_applied()

    h.sab.reachable = False
    h.qbit.speed = mbps_to_bytes(390)
    snap = h.run_for(PEER_OUTAGE_CONFIRM_SECONDS - POLL)

    assert h.qbit.download_limits == []
    assert snap["sab_ok"] is False
    assert snap["sab_error"] == "connection refused"
    assert snap["qbit_limit"] == TOTAL / 2


def test_confirmed_outage_hands_survivor_full_budget_then_rebaselines():
    h = Harness()
    h.qbit.speed = h.sab.speed = mbps_to_bytes(390)
    h.run()

    h.sab.reachable = False
    snap = h.run_for(PEER_OUTAGE_CONFIRM_SECONDS + 2 * POLL)
    assert snap["qbit_limit"] == round(TOTAL)
    # nothing was applied to the unreachable app, so its tracked limit
    # still matches what's really set in it
    assert snap["sab_limit"] == TOTAL / 2

    h.sab.reachable = True
    h.clear_applied()
    snap = h.run()
    assert h.controller.pending_rebaseline is False
    assert snap["qbit_limit"] == snap["sab_limit"] == TOTAL / 2
    assert h.qbit.download_limits == [round(TOTAL / 2)]


def test_rebaseline_survives_a_cycle_where_arbitration_is_skipped():
    h = Harness()
    h.qbit.speed = h.sab.speed = mbps_to_bytes(390)
    h.run()
    h.sab.reachable = False
    h.run_for(PEER_OUTAGE_CONFIRM_SECONDS + 2 * POLL)

    # sab returns in the same cycle qbit blips: the rebaseline is raised
    # but arbitration can't run yet, so it must stay pending
    h.sab.reachable = True
    h.qbit.reachable = False
    h.run()
    assert h.controller.pending_rebaseline is True

    h.qbit.reachable = True
    snap = h.run()
    assert h.controller.pending_rebaseline is False
    assert snap["qbit_limit"] == snap["sab_limit"] == TOTAL / 2


def test_failed_limit_set_keeps_tracked_limit_and_reports_error():
    h = Harness()
    h.run()
    h.qbit.speed = h.sab.speed = mbps_to_bytes(390)
    h.qbit.fail_sets = True
    snap = h.run()
    assert snap["qbit_ok"] is False
    assert snap["qbit_error"] == "set failed"
    assert snap["qbit_limit"] == TOTAL  # unchanged: the apply never landed
    assert snap["sab_limit"] == TOTAL / 2


def test_failed_first_apply_is_retried_next_cycle():
    h = Harness()
    h.qbit.fail_sets = True
    snap = h.run()
    assert h.qbit.download_limits == []
    assert snap["qbit_ok"] is False

    h.qbit.fail_sets = False
    snap = h.run()
    assert h.qbit.download_limits == [round(TOTAL)]
    assert snap["qbit_ok"] is True

    h.run(3)
    assert h.qbit.download_limits == [round(TOTAL)]  # applied once, not every cycle


def test_app_unreachable_at_startup_gets_its_limit_applied_once_it_appears():
    h = Harness()
    h.sab.reachable = False
    h.run_for(PEER_OUTAGE_CONFIRM_SECONDS + 2 * POLL)
    assert h.sab.download_limits == []

    h.sab.reachable = True
    h.run()
    assert h.sab.download_limits == [round(TOTAL)]


def link_harness(**overrides):
    return Harness(
        link_detector_kind="public_ip", backup_total=BACKUP_TOTAL,
        link_check_interval=POLL, link_check_idle_interval=POLL, **overrides,
    )


def test_link_failover_swaps_budget_notifies_and_logs_event():
    h = link_harness()
    h.qbit.speed = mbps_to_bytes(100)
    h.run()

    h.detector.reading = BACKUP
    h.run()  # first backup reading: not confirmed yet
    assert h.state.snapshot()["active_link"] == PRIMARY
    snap = h.run()

    assert snap["active_link"] == BACKUP
    assert snap["total"] == BACKUP_TOTAL
    assert snap["qbit_limit"] == round(BACKUP_TOTAL)
    assert len(snap["link_events"]) == 1
    assert snap["link_events"][0][1:] == (PRIMARY, BACKUP, 800, 50)
    assert len(h.slack.messages) == 1
    assert "failed over" in h.slack.messages[0]

    h.detector.reading = PRIMARY
    snap = h.run(2)
    assert snap["active_link"] == PRIMARY
    assert snap["total"] == TOTAL
    assert "recovered" in h.slack.messages[1]


def test_link_check_failure_keeps_current_link_state():
    h = link_harness()
    h.detector.reading = BACKUP
    h.run(2)
    assert h.state.snapshot()["active_link"] == BACKUP

    h.detector.fail = True
    snap = h.run(5)
    assert snap["active_link"] == BACKUP
    assert snap["total"] == BACKUP_TOTAL
    assert snap["link_ok"] is False
    assert snap["link_error"] == "dns timeout"


def test_link_checks_follow_idle_cadence_when_idle():
    h = Harness(link_check_interval=30, link_check_idle_interval=900)
    h.run_for(600)
    assert h.detector.checks == 1

    # activity resuming pulls the stale idle schedule forward
    h.qbit.speed = mbps_to_bytes(100)
    h.run()
    assert h.detector.checks == 2


def test_upload_limit_applied_once_and_swapped_on_failover():
    h = link_harness(
        qbit_upload_limit=mbps_to_bytes(40), qbit_upload_limit_backup=mbps_to_bytes(5),
    )
    h.run(3)
    assert h.qbit.upload_limits == [int(mbps_to_bytes(40))]

    h.detector.reading = BACKUP
    h.run(3)
    assert h.qbit.upload_limits == [int(mbps_to_bytes(40)), int(mbps_to_bytes(5))]


def test_upload_limit_retried_after_failure():
    h = Harness(qbit_upload_limit=mbps_to_bytes(40))
    h.qbit.fail_sets = True
    h.run()
    assert h.qbit.upload_limits == []
    h.qbit.fail_sets = False
    h.run()
    assert h.qbit.upload_limits == [int(mbps_to_bytes(40))]


def test_upload_limit_untouched_when_not_configured():
    h = Harness()
    h.run(3)
    assert h.qbit.upload_limits == []
