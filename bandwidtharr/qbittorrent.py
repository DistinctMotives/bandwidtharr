import requests


class QBittorrentClient:
    def __init__(self, base_url: str, username: str | None = None, password: str | None = None, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self._logged_in = False

    def _login(self) -> None:
        if not (self.username and self.password):
            return
        resp = self.session.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        self._logged_in = True

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        if self.username and not self._logged_in:
            self._login()
        resp = self.session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        if resp.status_code == 403 and self.username:
            self._logged_in = False
            self._login()
            resp = self.session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        return resp

    def get_download_speed(self) -> float:
        """Current download speed in bytes/sec."""
        info = self._request("GET", "/api/v2/transfer/info").json()
        return float(info["dl_info_speed"])

    def get_download_limit(self) -> float:
        """Current configured limit in bytes/sec, or 0 if unlimited."""
        info = self._request("GET", "/api/v2/transfer/info").json()
        return float(info["dl_rate_limit"])

    def set_download_limit(self, limit_bytes_per_sec: int) -> None:
        self._request(
            "POST",
            "/api/v2/transfer/setDownloadLimit",
            data={"limit": int(limit_bytes_per_sec)},
        )

    def set_upload_limit(self, limit_bytes_per_sec: int) -> None:
        self._request(
            "POST",
            "/api/v2/transfer/setUploadLimit",
            data={"limit": int(limit_bytes_per_sec)},
        )
