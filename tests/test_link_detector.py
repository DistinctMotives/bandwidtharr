import pytest

from bandwidtharr.link_detector import (
    BACKUP,
    PRIMARY,
    AsnMatchDetector,
    LinkStateTracker,
    NullDetector,
    build_link_detector,
    classify_isp,
    next_link_check_decision,
)


def test_classify_isp_backup_match_is_backup():
    assert classify_isp("Starlink Internet Services", "Starlink") == BACKUP


def test_classify_isp_no_match_is_primary():
    # unrecognized ISP with no backup match -> fails safe toward primary,
    # not toward backup
    assert classify_isp("Comcast Cable", "Starlink") == PRIMARY


def test_classify_isp_no_backup_configured_is_primary():
    assert classify_isp("Anything At All", "") == PRIMARY


def test_classify_isp_case_insensitive_and_multi_value():
    assert classify_isp("T-Mobile 5G Home Internet", "starlink, t-mobile") == BACKUP


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


def test_build_link_detector_none_by_default():
    assert isinstance(build_link_detector({}), NullDetector)


def test_build_link_detector_explicit_none():
    assert isinstance(build_link_detector({"LINK_DETECTOR": "none"}), NullDetector)


def test_build_link_detector_unknown_kind_raises():
    with pytest.raises(ValueError):
        build_link_detector({"LINK_DETECTOR": "bogus"})


def test_build_link_detector_requires_backup_isp_match():
    with pytest.raises(ValueError):
        build_link_detector({"LINK_DETECTOR": "public_ip"})


def test_build_link_detector_defaults_to_asn_match():
    detector = build_link_detector({"LINK_DETECTOR": "public_ip", "BACKUP_ISP_MATCH": "Starlink"})
    assert isinstance(detector, AsnMatchDetector)


