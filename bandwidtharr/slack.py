import requests


class SlackNotifier:
    def __init__(self, webhook_url: str, timeout: float = 5.0):
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.session = requests.Session()

    def notify(self, text: str) -> None:
        try:
            resp = self.session.post(self.webhook_url, json={"text": text}, timeout=self.timeout)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            # The webhook URL is itself a bearer credential (anyone who has
            # it can post to the channel) -- same reasoning as sabnzbd.py's
            # API-key redaction. requests embeds the full URL in these
            # exceptions' str(), which is exactly what would end up in logs
            # on a connection hiccup otherwise.
            raise RuntimeError(str(e).replace(self.webhook_url, "***")) from None


def build_slack_notifier(env: dict) -> SlackNotifier | None:
    webhook_url = env.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return None
    return SlackNotifier(webhook_url)
