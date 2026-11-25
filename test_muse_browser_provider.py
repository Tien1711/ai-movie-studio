import asyncio

import pytest

import app as studio
from muse_connection import muse_connection


def test_muse_browser_uses_its_own_endpoint_and_key(monkeypatch):
    monkeypatch.delenv("MUSE_PROVIDER", raising=False)
    monkeypatch.setenv("MUSE2API_URL", "http://gateway:18610")
    monkeypatch.setenv("MUSE2API_KEY", "gateway-key")
    assert muse_connection() == ("muse2api", "http://gateway:18610", "gateway-key")

    monkeypatch.setenv("MUSE_PROVIDER", "browser")
    monkeypatch.setenv("MUSE_BROWSER_PORT", "18788")
    monkeypatch.delenv("MUSE_BROWSER_KEY", raising=False)
    assert muse_connection() == ("browser", "http://127.0.0.1:18788", None)
    monkeypatch.setenv("MUSE_BROWSER_KEY", "browser-key")
    assert muse_connection() == ("browser", "http://127.0.0.1:18788", "browser-key")
    monkeypatch.setenv("MUSE_BROWSER_PORT", "65536")
    with pytest.raises(ValueError, match="MUSE_BROWSER_PORT"):
        muse_connection()


@pytest.mark.parametrize("ready", [False, True])
def test_studio_reports_browser_readiness(monkeypatch, ready):
    class Response:
        status_code = 200

        def json(self):
            return {"ready": ready, "accounts": [{"id": "browser"}] if ready else []}

    class Client:
        async def get(self, url, headers, timeout):
            assert url == "http://127.0.0.1:18787/admin/accounts"
            assert headers["Authorization"] == "Bearer browser-key"
            return Response()

    monkeypatch.setattr(studio, "MUSE_PROVIDER", "browser")
    monkeypatch.setattr(studio, "MUSE_API_URL", "http://127.0.0.1:18787")
    monkeypatch.setattr(studio, "MUSE_API_KEY", "browser-key")
    monkeypatch.setattr(studio, "get_http_client", lambda: Client())
    result = asyncio.run(studio.health_check())
    assert result["muse_provider"] == "browser"
    assert result["muse2api"] is ready
    assert result["accounts"] == int(ready)
