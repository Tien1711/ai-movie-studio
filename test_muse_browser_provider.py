import asyncio

import pytest
from fastapi.testclient import TestClient

import muse_connection as muse_config

import app as studio
from muse_connection import muse_connection


@pytest.mark.parametrize("ready", [True, False])
def test_browser_batch_checks_gateway_and_attaches_remote_image(monkeypatch, tmp_path, ready):
    import json

    import batch_engine
    import httpx
    import story_engine

    reference = "https://images.example.org/product.webp"
    image = b"RIFF\x00\x00\x00\x00WEBPfixture"
    monkeypatch.setattr(story_engine, "_pinned_fetch", lambda *args: (200, {}, image))
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/admin/accounts":
            return httpx.Response(200, json={"ready": ready, "error": "Mở tab Muse."})
        return httpx.Response(422)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    results = asyncio.run(batch_engine.run_batch(
        scenes=[{"index": 1, "prompt": "A product demonstration", "duration": 8,
                 "reference_image": reference}],
        base_url="http://127.0.0.1:18787", api_key="fixture", output_dir=str(tmp_path),
        voice_mode="ai_voice", chain_mode="sequential", muse_provider="browser",
    ))
    assert all(call.headers["Authorization"] == "Bearer fixture" for call in calls)
    paths = [call.url.path for call in calls]
    if ready:
        assert paths == ["/admin/accounts", "/v1/videos"]
        payload = json.loads(calls[1].content)
        assert payload["duration"] == 8
        assert payload["reference_image"] == "data:image/webp;base64,UklGRgAAAABXRUJQZml4dHVyZQ=="
    else:
        assert paths == ["/admin/accounts"]
        assert results[0]["error"] == "Mở tab Muse."


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

def test_settings_applies_provider_without_restart(tmp_path, monkeypatch):
    import batch_video_engine

    queue_file = tmp_path / "batch_queue.json"
    queue_file.write_text('{"videos":[],"updated_at":0}', encoding="utf-8")
    monkeypatch.setattr(batch_video_engine, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(studio, "RENDER_QUEUE", [])
    monkeypatch.setattr(studio, "JOBS", {})
    monkeypatch.setattr(studio, "ACTIVE_SHOT_TASKS", {})
    monkeypatch.setattr(muse_config, "MUSE_SETTINGS_FILE", tmp_path / "settings" / "muse.json")
    monkeypatch.setattr(studio, "MUSE_PROVIDER", "muse2api")
    monkeypatch.delenv("STUDIO_API_KEY", raising=False)
    monkeypatch.setenv("MUSE_PROVIDER", "muse2api")

    client = TestClient(studio.app)
    response = client.get("/api/settings/muse")
    assert response.json() == {
        "provider": "muse2api",
        "active_provider": "muse2api",
        "restart_required": False,
    }

    response = client.put("/api/settings/muse", json={"provider": "browser"})
    assert response.status_code == 200
    assert response.json() == {
        "provider": "browser",
        "active_provider": "browser",
        "restart_required": False,
    }
    assert client.get("/api/settings/muse").json() == response.json()
    assert muse_config.active_muse_provider() == "browser"

    invalid = client.put("/api/settings/muse", json={"provider": "remote"})
    assert invalid.status_code == 422
    assert muse_config.saved_muse_provider() == "browser"

    switched_back = client.put("/api/settings/muse", json={"provider": "muse2api"})
    assert switched_back.json()["restart_required"] is False


def test_settings_can_check_browser_while_muse2api_is_active(monkeypatch):
    monkeypatch.setattr(studio, "MUSE_PROVIDER", "muse2api")
    monkeypatch.delenv("STUDIO_API_KEY", raising=False)
    monkeypatch.setenv("MUSE_BROWSER_KEY", "browser-test-key")
    monkeypatch.setenv("MUSE_BROWSER_PORT", "18789")

    async def check(provider, url, key):
        assert (provider, url, key) == ("browser", "http://127.0.0.1:18789", "browser-test-key")
        return {"ready": True, "accounts": 1, "error": ""}

    monkeypatch.setattr(studio, "_check_muse_gateway", check)
    response = TestClient(studio.app).get("/api/settings/muse/health", params={"provider": "browser"})
    assert response.json() == {"ready": True, "accounts": 1, "error": ""}

def test_studio_proxies_chrome_profile_management_to_loopback(monkeypatch):
    profile_id = "a1e5c195-4465-45b4-a739-085d55d73576"
    calls = []
    monkeypatch.delenv("STUDIO_API_KEY", raising=False)
    monkeypatch.setenv("MUSE_BROWSER_PORT", "18787")
    monkeypatch.setenv("MUSE_BROWSER_KEY", "browser-fixture")

    class Response:
        is_success = True

        def json(self):
            return {"selected": profile_id, "profiles": [{"id": profile_id, "name": "1230"}]}

    class Client:
        async def request(self, method, url, headers, timeout, **kwargs):
            assert headers == {"Authorization": "Bearer browser-fixture"}
            calls.append((method, url, kwargs))
            return Response()

    monkeypatch.setattr(studio, "get_http_client", lambda: Client())
    client = TestClient(studio.app)
    result = client.put("/api/settings/muse/chrome-profiles/selection", json={"id": profile_id})
    assert result.json()["selected"] == profile_id
    assert calls == [("PUT", "http://127.0.0.1:18787/admin/chrome-profiles/selection", {"json": {"id": profile_id}})]
    assert "browser-fixture" not in result.text
    invalid = client.put("/api/settings/muse/chrome-profiles/selection", json={"id": "invalid"})
    assert invalid.status_code == 422

    pool_res = client.put("/api/settings/muse/chrome-profiles/pool", json={"enabled": True})
    assert pool_res.status_code == 200
    assert calls[-1] == ("PUT", "http://127.0.0.1:18787/admin/chrome-profiles/pool", {"json": {"enabled": True}})


def test_multi_film_production_workers_run_concurrently_without_double_claim(monkeypatch):
    import threading
    import batch_video_engine as batch

    jobs = [
        {"id": "bv_film_1", "status": "queued", "guided": True, "operation": {"action": "write"}},
        {"id": "bv_film_2", "status": "queued", "guided": True, "operation": {"action": "write"}},
    ]
    threads = {}
    claimed = set()
    both_started = threading.Event()
    release = threading.Event()
    seen = []

    def process(video_id):
        with batch._QUEUE_LOCK:
            seen.append(video_id)
            job = next(j for j in jobs if j["id"] == video_id)
            job["status"] = "running"
            if len(seen) == 2:
                both_started.set()
        release.wait(timeout=5)
        with batch._QUEUE_LOCK:
            job["status"] = "completed"

    def read_queue():
        with batch._QUEUE_LOCK:
            return [j.copy() for j in jobs]

    monkeypatch.setattr(batch, "_worker_threads", threads)
    monkeypatch.setattr(batch, "_claimed_video_ids", claimed)
    monkeypatch.setattr(batch, "_production_worker_limit", lambda: 2)
    monkeypatch.setattr(batch.BATCH_MANAGER, "_read_queue", read_queue)
    monkeypatch.setattr(batch, "_process_single_video", process)
    workers = set()
    try:
        batch.start_batch_worker()
        with batch._worker_lock:
            workers.update(threads.values())
        assert both_started.wait(timeout=5), "Both films must run concurrently on 2 browser lanes"
        assert sorted(seen) == ["bv_film_1", "bv_film_2"]
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)
    assert not threads
    assert not claimed

def test_quick_video_proxy_authenticates_private_browser_video_and_forwards_range(monkeypatch):
    video_path = "/v1/browser-media/12345678-1234-1234-1234-123456789abc.mp4"
    monkeypatch.delenv("STUDIO_API_KEY", raising=False)
    monkeypatch.setattr(studio, "MUSE_API_KEY", "browser-fixture-key")
    monkeypatch.setattr(studio, "MUSE_API_URL", "http://127.0.0.1:18787")

    class StatusClient:
        async def get(self, url, **kwargs):
            return studio.httpx.Response(200, json={"status": "completed", "video_url": video_path})

    class VideoBytes(studio.httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ftyp"

    async def video_request(request):
        assert request.url.path == video_path
        assert request.headers["Authorization"] == "Bearer browser-fixture-key"
        assert request.headers["Range"] == "bytes=4-7"
        return studio.httpx.Response(206, headers={
            "Content-Type": "video/mp4", "Content-Length": "4",
            "Content-Range": "bytes 4-7/1200", "Accept-Ranges": "bytes",
        }, stream=VideoBytes())

    real_client = studio.httpx.AsyncClient
    monkeypatch.setattr(studio, "get_http_client", lambda: StatusClient())
    monkeypatch.setattr(studio.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=studio.httpx.MockTransport(video_request), **kwargs))
    response = TestClient(studio.app).get("/api/quick-video/example/stream", headers={"Range": "bytes=4-7"})
    assert response.status_code == 206
    assert response.content == b"ftyp"
    assert response.headers["content-range"] == "bytes 4-7/1200"
    assert muse_config.browser_media_headers("https://muse.ai" + video_path, studio.MUSE_API_URL, studio.MUSE_API_KEY) == {}
