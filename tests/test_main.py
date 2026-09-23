import pytest

from bandwidtharr.main import (
    mbps_to_bytes,
    optional_mbps_env,
    should_hand_out_budget,
    should_log_repeated_failure,
)


def test_should_hand_out_budget_no_outage():
    assert should_hand_out_budget(now=1000.0, peer_unreachable_since=None, threshold_seconds=60.0) is False


def test_should_hand_out_budget_blip_under_threshold():
    assert should_hand_out_budget(now=1059.0, peer_unreachable_since=1000.0, threshold_seconds=60.0) is False


def test_should_hand_out_budget_at_threshold():
    assert should_hand_out_budget(now=1060.0, peer_unreachable_since=1000.0, threshold_seconds=60.0) is True


def test_should_hand_out_budget_past_threshold():
    assert should_hand_out_budget(now=5000.0, peer_unreachable_since=1000.0, threshold_seconds=60.0) is True


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
