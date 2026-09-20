from bandwidtharr.slack import SlackNotifier, build_slack_notifier


def test_build_slack_notifier_none_when_unset():
    assert build_slack_notifier({}) is None


def test_build_slack_notifier_none_when_blank():
    assert build_slack_notifier({"SLACK_WEBHOOK_URL": "   "}) is None


def test_build_slack_notifier_returns_notifier_when_set():
    notifier = build_slack_notifier({"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/xxx"})
    assert isinstance(notifier, SlackNotifier)
    assert notifier.webhook_url == "https://hooks.slack.com/services/xxx"
