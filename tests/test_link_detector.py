import json
import time

from bandwidtharr.link_detector import (
    BACKUP,
    PRIMARY,
    DnsBaselineDetector,
    LinkStateTracker,
    classify_isp,
    next_link_check_decision,
)


def test_classify_isp_backup_match_wins():
    assert classify_isp("Starlink Internet Services", "Comcast", "Starlink") == BACKUP


def test_classify_isp_no_backup_match_falls_through_to_primary_match():
    assert classify_isp("Comcast Cable", "Comcast", "Starlink") == PRIMARY


def test_classify_isp_primary_set_but_no_match_is_backup():
    # different ISP than configured primary, no backup list configured -> treat as backup
    assert classify_isp("Verizon Wireless", "Comcast", "") == BACKUP


def test_classify_isp_neither_configured_is_primary():
    assert classify_isp("Anything At All", "", "") == PRIMARY


def test_classify_isp_only_backup_configured_no_match_is_primary():
    assert classify_isp("Comcast Cable", "", "Starlink") == PRIMARY


def test_classify_isp_case_insensitive_and_multi_value():
    assert classify_isp("T-Mobile 5G Home Internet", "", "starlink, t-mobile") == BACKUP


def test_link_state_tracker_requires_confirm_count_before_flipping():
    tracker = LinkStateTracker(confirm_count=3)
    assert tracker.observe(BACKUP) == PRIMARY
    assert tracker.observe(BACKUP) == PRIMARY
    assert tracker.observe(BACKUP) == BACKUP


def test_link_state_tracker_resets_pending_on_alternating_readings():
    tracker = LinkStateTracker(confirm_count=2)
    assert tracker.observe(BACKUP) == PRIMARY
    assert tracker.observe(PRIMARY) == PRIMARY  # resets the pending backup count
    assert tracker.observe(BACKUP) == PRIMARY  # back to 1, not 2
    assert tracker.observe(BACKUP) == BACKUP


def test_link_state_tracker_flips_back_with_same_confirm_count():
    tracker = LinkStateTracker(confirm_count=2, initial=BACKUP)
    assert tracker.observe(PRIMARY) == BACKUP
    assert tracker.observe(PRIMARY) == PRIMARY


def test_link_state_tracker_matching_reading_clears_pending_immediately():
    tracker = LinkStateTracker(confirm_count=5)
    assert tracker.observe(PRIMARY) == PRIMARY
    assert tracker.observe(BACKUP) == PRIMARY
    assert tracker.observe(PRIMARY) == PRIMARY


def test_next_link_check_not_due_before_schedule():
    due, interval = next_link_check_decision(
        now=100, next_link_check=110, is_active=False, active_interval=30, idle_interval=900,
    )
    assert due is False


def test_next_link_check_due_when_schedule_reached_idle():
    due, interval = next_link_check_decision(
        now=110, next_link_check=110, is_active=False, active_interval=30, idle_interval=900,
    )
    assert due is True
    assert interval == 900


def test_next_link_check_due_when_schedule_reached_active():
    due, interval = next_link_check_decision(
        now=110, next_link_check=110, is_active=True, active_interval=30, idle_interval=900,
    )
    assert due is True
    assert interval == 30


def test_next_link_check_pulls_forward_stale_idle_schedule_when_active_resumes():
    # Regression test: a check made while idle can schedule the next one
    # minutes out (idle_interval). If downloads resume before then, don't
    # wait out the stale schedule -- it should become due immediately, on
    # the active (not idle) cadence.
    due, interval = next_link_check_decision(
        now=100, next_link_check=980, is_active=True, active_interval=30, idle_interval=900,
    )
    assert due is True
    assert interval == 30


def test_next_link_check_not_pulled_forward_while_still_idle():
    due, interval = next_link_check_decision(
        now=100, next_link_check=980, is_active=False, active_interval=30, idle_interval=900,
    )
    assert due is False


def test_dns_baseline_detector_persists_and_reloads(tmp_path):
    state_file = str(tmp_path / "baseline.json")
    first = DnsBaselineDetector("host", "resolver", state_file=state_file)
    first._save_baseline("1.2.3.4")

    second = DnsBaselineDetector("host", "resolver", state_file=state_file)
    assert second._baseline_ip == "1.2.3.4"


def test_dns_baseline_detector_ignores_stale_persisted_file(tmp_path):
    state_file = tmp_path / "baseline.json"
    state_file.write_text(json.dumps({"ip": "9.9.9.9", "saved_at": time.time() - 999_999}))

    detector = DnsBaselineDetector("host", "resolver", state_file=str(state_file), max_persisted_age=86400)
    assert detector._baseline_ip is None


def test_dns_baseline_detector_no_state_file_is_pure_in_memory():
    detector = DnsBaselineDetector("host", "resolver")
    assert detector._baseline_ip is None
    assert detector.state_file is None


def test_dns_baseline_detector_missing_file_starts_fresh(tmp_path):
    state_file = str(tmp_path / "does_not_exist.json")
    detector = DnsBaselineDetector("host", "resolver", state_file=state_file)
    assert detector._baseline_ip is None
