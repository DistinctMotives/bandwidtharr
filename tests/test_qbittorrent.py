import pytest

from bandwidtharr.qbittorrent import QBittorrentClient


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def test_login_rejected_credentials_raise_clear_error(monkeypatch):
    client = QBittorrentClient("http://qbit.example", username="admin", password="wrong")
    monkeypatch.setattr(client.session, "post", lambda *a, **k: FakeResponse(200, "Fails."))

    with pytest.raises(RuntimeError, match="login failed"):
        client.get_download_speed()
    assert client._logged_in is False


def test_login_success_then_request(monkeypatch):
    client = QBittorrentClient("http://qbit.example", username="admin", password="right")
    monkeypatch.setattr(client.session, "post", lambda *a, **k: FakeResponse(200, "Ok."))
    monkeypatch.setattr(
        client.session, "request",
        lambda *a, **k: FakeResponse(200, json_data={"dl_info_speed": 1234}),
    )

    assert client.get_download_speed() == 1234.0
    assert client._logged_in is True


def test_expired_session_relogs_in_and_retries(monkeypatch):
    client = QBittorrentClient("http://qbit.example", username="admin", password="right")
    logins = []
    monkeypatch.setattr(client.session, "post", lambda *a, **k: logins.append(1) or FakeResponse(200, "Ok."))
    responses = iter([FakeResponse(403), FakeResponse(200, json_data={"dl_info_speed": 5})])
    monkeypatch.setattr(client.session, "request", lambda *a, **k: next(responses))
    client._logged_in = True  # a previously valid session cookie that has since expired

    assert client.get_download_speed() == 5.0
    assert len(logins) == 1


def test_no_credentials_never_logs_in(monkeypatch):
    client = QBittorrentClient("http://qbit.example")

    def fail_post(*a, **k):
        raise AssertionError("login should not be attempted without credentials")

    monkeypatch.setattr(client.session, "post", fail_post)
    monkeypatch.setattr(
        client.session, "request",
        lambda *a, **k: FakeResponse(200, json_data={"dl_info_speed": 0}),
    )

    assert client.get_download_speed() == 0.0
