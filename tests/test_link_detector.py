from bandwidtharr.link_detector import BACKUP, PRIMARY, LinkStateTracker, classify_isp


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
