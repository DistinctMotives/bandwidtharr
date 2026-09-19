import requests


class SabnzbdClient:
    """
    SABnzbd's `Downloader.limit_speed(value)` (used by both the `mode=config`
    and `mode=queue` speedlimit API actions -- they're the same handler)
    disambiguates its `value` param itself: a bare number in 1-100 (or one
    ending in '%') is a PERCENTAGE of bandwidth_max; anything else is an
    absolute speed in literal bytes/sec (no suffix = bytes, confirmed by
    reading sabnzbd/downloader.py and misc.py:from_units directly). Sending
    "KB/s" numbers here was the actual bug: they were silently read as literal
    bytes, capping every "800 Mbps" attempt at ~95 KB/s. Always send raw
    bytes/sec, which matches what `speedlimit_abs` already reports on read.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()

    def _call(self, params: dict) -> dict:
        params = {**params, "apikey": self.api_key, "output": "json"}
        resp = self.session.get(f"{self.base_url}/sabnzbd/api", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_download_speed(self) -> float:
        """Current download speed in bytes/sec."""
        data = self._call({"mode": "queue"})
        return float(data["queue"]["kbpersec"]) * 1024

    def set_download_limit(self, limit_bytes_per_sec: int) -> None:
        # A value that lands in 1-100 would be misread as a percentage instead
        # of an absolute speed, so nudge it just outside that band -- 101
        # bytes/sec is negligible next to any speed we'd realistically set.
        value = max(101, int(limit_bytes_per_sec))
        self._call({"mode": "config", "name": "speedlimit", "value": value})
