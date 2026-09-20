import threading
import time

import requests

from bandwidtharr.slack import SlackNotifier, build_slack_notifier


def test_build_slack_notifier_none_when_unset():
    assert build_slack_notifier({}) is None


def test_build_slack_notifier_none_when_blank():
    assert build_slack_notifier({"SLACK_WEBHOOK_URL": "   "}) is None


def test_build_slack_notifier_returns_notifier_when_set():
    notifier = build_slack_notifier({"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/xxx"})
    assert isinstance(notifier, SlackNotifier)
    assert notifier.webhook_url == "https://hooks.slack.com/services/xxx"


class FakeResponse:
    def raise_for_status(self):
        pass


def test_send_with_retries_succeeds_on_first_attempt(monkeypatch):
    notifier = SlackNotifier("https://hooks.slack.com/services/xxx")
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse()

    monkeypatch.setattr(notifier.session, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))

    notifier._send_with_retries("test")
    assert len(calls) == 1


def test_send_with_retries_succeeds_after_failures(monkeypatch, caplog):
    notifier = SlackNotifier("https://hooks.slack.com/services/xxx")
    calls = []
    sleeps = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError("boom")
        return FakeResponse()

    monkeypatch.setattr(notifier.session, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    notifier._send_with_retries("test")
    assert len(calls) == 3
    assert sleeps == [SlackNotifier.BACKOFF_SECONDS * 1, SlackNotifier.BACKOFF_SECONDS * 2]
    assert "failed to send Slack notification" not in caplog.text


def test_send_with_retries_gives_up_and_redacts_webhook_url(monkeypatch, caplog):
    webhook_url = "https://hooks.slack.com/services/T000/B000/SECRETTOKEN123"
    notifier = SlackNotifier(webhook_url)
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        raise requests.exceptions.ConnectionError(f"Failed to connect: {webhook_url}")

    monkeypatch.setattr(notifier.session, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    import logging
    with caplog.at_level(logging.WARNING):
        notifier._send_with_retries("test")  # must not raise

    assert len(calls) == SlackNotifier.MAX_ATTEMPTS
    assert "failed to send Slack notification" in caplog.text
    assert "SECRETTOKEN123" not in caplog.text
    assert "***" in caplog.text


def test_notify_returns_without_blocking(monkeypatch):
    notifier = SlackNotifier("https://hooks.slack.com/services/xxx")
    started = threading.Event()
    release = threading.Event()

    def slow_send(text):
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(notifier, "_send_with_retries", slow_send)

    notifier.notify("test")
    assert started.wait(timeout=1), "background thread never started"
    release.set()  # let the background thread finish so it doesn't linger
