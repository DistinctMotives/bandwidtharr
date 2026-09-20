import logging
import threading
import time

import requests

log = logging.getLogger("bandwidtharr.slack")


class SlackNotifier:
    MAX_ATTEMPTS = 3
    BACKOFF_SECONDS = 2.0

    def __init__(self, webhook_url: str, timeout: float = 5.0):
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.session = requests.Session()

    def notify(self, text: str) -> None:
        """Sends in a background thread with retries, so a slow or
        unreachable Slack API can never block the main poll loop. Never
        raises -- a failure after exhausting all retries is only logged."""
        threading.Thread(target=self._send_with_retries, args=(text,), daemon=True).start()

    def _send_with_retries(self, text: str) -> None:
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                resp = self.session.post(self.webhook_url, json={"text": text}, timeout=self.timeout)
                resp.raise_for_status()
                return
            except requests.exceptions.RequestException as e:
                # The webhook URL is itself a bearer credential (anyone who
                # has it can post to the channel) -- same reasoning as
                # sabnzbd.py's API-key redaction.
                redacted = str(e).replace(self.webhook_url, "***")
                if attempt == self.MAX_ATTEMPTS:
                    log.warning("failed to send Slack notification after %d attempts: %s", attempt, redacted)
                    return
                time.sleep(self.BACKOFF_SECONDS * attempt)


def build_slack_notifier(env: dict) -> SlackNotifier | None:
    webhook_url = env.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return None
    return SlackNotifier(webhook_url)
