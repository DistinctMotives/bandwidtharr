import traceback

import pytest
import requests

from bandwidtharr.sabnzbd import SabnzbdClient


def test_call_redacts_api_key_on_connection_error(monkeypatch):
    client = SabnzbdClient("http://sab.example", api_key="supersecretkey123")

    def raise_connection_error(*args, **kwargs):
        raise requests.exceptions.ConnectionError(
            "Max retries exceeded with url: /sabnzbd/api?mode=queue&apikey=supersecretkey123&output=json"
        )

    monkeypatch.setattr(client.session, "get", raise_connection_error)

    with pytest.raises(RuntimeError) as exc_info:
        client._call({"mode": "queue"})

    assert "supersecretkey123" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_call_redacts_api_key_on_http_error(monkeypatch):
    client = SabnzbdClient("http://sab.example", api_key="supersecretkey123")

    class FakeResponse:
        def raise_for_status(self):
            raise requests.exceptions.HTTPError(
                "403 Client Error: Forbidden for url: "
                "http://sab.example/sabnzbd/api?mode=queue&apikey=supersecretkey123&output=json"
            )

    monkeypatch.setattr(client.session, "get", lambda *a, **k: FakeResponse())

    with pytest.raises(RuntimeError) as exc_info:
        client._call({"mode": "queue"})

    assert "supersecretkey123" not in str(exc_info.value)


def test_call_full_traceback_never_contains_the_key(monkeypatch):
    # A full traceback dump (if one were ever added later, e.g. log.exception())
    # walks the chained cause/context and re-stringifies the original
    # exception -- `from None` suppresses that chaining so a traceback of the
    # redacted RuntimeError can't reintroduce the unredacted key.
    client = SabnzbdClient("http://sab.example", api_key="supersecretkey123")

    def raise_connection_error(*args, **kwargs):
        raise requests.exceptions.ConnectionError("url has apikey=supersecretkey123 in it")

    monkeypatch.setattr(client.session, "get", raise_connection_error)

    try:
        client._call({"mode": "queue"})
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        formatted = "".join(traceback.format_exception(type(e), e, e.__traceback__))

    assert "supersecretkey123" not in formatted
