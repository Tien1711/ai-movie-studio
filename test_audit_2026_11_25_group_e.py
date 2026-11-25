# -*- coding: utf-8 -*-
"""
test_audit_2026_11_25_group_e.py — Behaviour tests for audit group E
(MEDIUM #11-#14 + 6 diem phu Tien chi ra + cac ghi nhan them), main 62d7b4a.

  #11 get_media_duration tra 5.0 gia -> cat cut phim / lech timeline.
  #12 _generate_fallback_clip: -vf dung truoc -i thu hai -> ffmpeg loi 100%.
  #13 audio_duration uoc luong len/3.0 (sai ~5 lan so voi chuan len/15.0).
  #14 _dramatic_score khop chuoi con "ma" (va _TWIST_WORDS / moral check).
  #5a auth/rate-limit: bucket IP ro ri, IP tu scope["client"], SSE mien
      auth, so key bang !=.
  #5b mix_multitrack_timeline map "preview" ve 720p (compose_scene_video: draft).
  #5c race _DURATION_CACHE (dict tran, check-then-act).
  #5d burn_subtitles: timeout 300s chi ap sau vong doc stdout.
  #5e _topic_hashtags khu trung lap hong.
  #5f _cache_path khong loc ten file.
  #6  chim nao cung thanh phuong hoang vang; giong VieNeu la lang le doi
      sang Edge; _download_validated de lai file do; loc anh chi can 1
      tu khoa; merge_clips_advanced khong dung quality/aspect_ratio;
      convert_to_shot_desk ghi de project; SFX loi hong ca export + khong
      cache; XSS voiceSelectHtml; concat list khong escape ' / update_video
      / get_video khong kiem is_safe_video_id / add_batch extra ghi de
      id-status; import_shot_desk_zip duong dan CWD + khong don proj do.

Do tren code cu (truoc khi sua): cac test duoc danh dau [RED-OLD] fail;
test guardrail (ghi [GREEN-OLD]) phai xanh ca truoc va sau khi sua.
"""
import asyncio
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

import app as app_module
import affiliate_engine
import auto_factory
import batch_engine
import batch_video_engine
import composer
import shot_desk
import story_engine
import tts_manager


FFMPEG = composer.FFMPEG


# ── Helpers ─────────────────────────────────────────────────────────────────

def _run_ffmpeg(args, timeout=60):
    res = subprocess.run([FFMPEG, "-y", *args],
                         capture_output=True, timeout=timeout)
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-500:]
    return res


def _make_clip(path, dur=2.0, w=720, h=1280, with_audio=True):
    """Clip mp4 that (color + sine) de test merge / SFX / timeline."""
    src = ["-f", "lavfi", "-i", f"color=c=0x223344:s={w}x{h}:d={dur}:r=30"]
    if with_audio:
        src += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"]
    _run_ffmpeg([*src, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", str(path)])


def _make_audio(path, dur=2.0):
    _run_ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=660:duration={dur}",
                 "-c:a", "libmp3lame", "-b:a", "128k", str(path)])


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Workspace tam cho cac test dung file that (khong dong workspace that)."""
    monkeypatch.setattr(batch_video_engine, "WORKSPACE_DIR", tmp_path / "ws")
    monkeypatch.setattr(batch_video_engine, "BATCH_DIR", tmp_path / "ws" / "_batch_videos")
    monkeypatch.setattr(batch_video_engine, "QUEUE_FILE",
                        tmp_path / "ws" / "_batch_videos" / "batch_queue.json")
    monkeypatch.setattr(batch_video_engine, "QUEUE_BAK_FILE",
                        tmp_path / "ws" / "_batch_videos" / "batch_queue.json.bak")
    monkeypatch.setattr(shot_desk, "SHOT_DESK_DIR", tmp_path / "ws" / "_shot_desk_projects")
    (tmp_path / "ws" / "_batch_videos").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "_shot_desk_projects").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ── #11 — get_media_duration tra 5.0 gia ────────────────────────────────────

def test_get_media_duration_missing_returns_zero_not_fake_five():  # [RED-OLD]
    assert composer.get_media_duration("/khong/ton/tai/file.mp4") == 0.0


def test_get_media_duration_corrupt_returns_zero(tmp_path):  # [RED-OLD]
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"day khong phai video" * 10)
    assert composer.get_media_duration(str(bad)) == 0.0


def test_get_media_duration_real_file(tmp_path):  # [GREEN-OLD]
    clip = tmp_path / "real.mp4"
    _make_clip(clip, dur=2.0)
    assert abs(composer.get_media_duration(str(clip)) - 2.0) < 0.3


def test_get_media_duration_batch_engine_duplicate_also_zero():  # [RED-OLD]
    # batch_engine co ban copy rieng cua get_media_duration (cung loi 5.0 gia).
    assert batch_engine.get_media_duration("/khong/ton/tai/file.mp4") == 0.0


def test_mix_multitrack_omits_t_when_duration_unknown(tmp_path, monkeypatch):  # [RED-OLD]
    captured = {}

    def fake_run(cmd, label="", timeout=120):
        captured["cmd"] = list(cmd)
        return None

    monkeypatch.setattr(composer, "_run", fake_run)
    monkeypatch.setattr(composer, "get_media_duration", lambda p: 0.0)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    composer.mix_multitrack_timeline(str(vid), output_path=str(tmp_path / "out.mp4"))
    assert "-t" not in captured["cmd"], "khong duoc truyen -t khi khong biet thoi luong"


def test_mix_multitrack_passes_t_when_duration_known(tmp_path, monkeypatch):  # [GREEN-OLD]
    captured = {}

    def fake_run(cmd, label="", timeout=120):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr(composer, "_run", fake_run)
    monkeypatch.setattr(composer, "get_media_duration", lambda p: 8.0)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    composer.mix_multitrack_timeline(str(vid), output_path=str(tmp_path / "out.mp4"))
    assert "-t" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-t") + 1] == "8.000"


def test_add_background_music_omits_t_when_duration_unknown(tmp_path, monkeypatch):  # [RED-OLD]
    captured = {}

    def fake_run(cmd, label="", timeout=120):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr(composer, "_run", fake_run)
    monkeypatch.setattr(composer, "get_media_duration", lambda p: 0.0)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    composer.add_background_music(str(vid), str(tmp_path / "m.mp3"),
                                  str(tmp_path / "out.mp4"))
    assert "-t" not in captured["cmd"], "khong duoc truyen -t 5.0 gia"
    assert "-shortest" in captured["cmd"]


# ── #12 — _generate_fallback_clip: -vf truoc -i thu hai ─────────────────────

def test_generate_fallback_clip_no_audio_real_ffmpeg(tmp_path):  # [RED-OLD]
    out = tmp_path / "fallback_noaudio.mp4"
    ok = batch_video_engine._generate_fallback_clip(
        out, None, 2.0, "9:16", "Tieu de test", 1, 3)
    assert ok, "nhanh khong-audio phai chay duoc bang ffmpeg that"
    assert out.exists() and out.stat().st_size > 1000
    assert abs(composer.get_media_duration(str(out)) - 2.0) < 0.5


def test_generate_fallback_clip_with_audio_real_ffmpeg(tmp_path):  # [GREEN-OLD]
    audio = tmp_path / "tts.mp3"
    _make_audio(audio, dur=2.0)
    out = tmp_path / "fallback_audio.mp4"
    ok = batch_video_engine._generate_fallback_clip(
        out, audio, 2.0, "9:16", "Tieu de test", 1, 3)
    assert ok
    assert out.exists() and out.stat().st_size > 1000


# ── #13 — uoc luong audio_duration len/3.0 -> len/15.0 ──────────────────────

def test_audio_duration_estimator_uses_15_chars_per_second():  # [RED-OLD]
    # Helper uoc luong dung chung cho moi fallback trong process_scene.
    assert hasattr(batch_engine, "_estimate_audio_duration")
    assert batch_engine._estimate_audio_duration("x" * 150) == pytest.approx(10.0)
    assert batch_engine._estimate_audio_duration("x" * 30) == pytest.approx(2.0)
    assert batch_engine._estimate_audio_duration("") == pytest.approx(2.0)


def test_no_len_over_3_estimator_left_in_batch_engine():  # [RED-OLD]
    src = Path(batch_engine.__file__).read_text(encoding="utf-8")
    assert "/ 3.0" not in src, "khong con fallback len/3.0 nao trong batch_engine.py"


# ── #14 — _dramatic_score khop chuoi con ─────────────────────────────────────

def test_dramatic_score_no_substring_ma():  # [RED-OLD]
    # "May man" / "mang" chua "ma" nhung khong co tu "ma" nao.
    assert story_engine._dramatic_score("May mắn mang về nhiều niềm vui.") == 1  # chi +1 do dai
    assert story_engine._dramatic_score("Mang về nhà.") == 0
    # "toan" chua "oan" nhung khong co tu "oan".
    assert story_engine._dramatic_score("Toan tính kỹ lưỡng.") == 0


def test_dramatic_score_real_ghost_word_still_scores():  # [GREEN-OLD]
    assert story_engine._dramatic_score("Con ma xuất hiện.") == 2
    assert story_engine._dramatic_score("Máu chảy khắp nơi!") >= 2


def test_contains_word_helper_boundaries():  # [RED-OLD]
    assert hasattr(story_engine, "_contains_word")
    assert story_engine._contains_word("Đột nhiên, cửa mở.", "đột nhiên")
    assert not story_engine._contains_word("Chuyện xảy ra đột ngột.", "đột nhiên")
    assert story_engine._contains_word("Con ma xuất hiện.", "ma")
    assert not story_engine._contains_word("May mắn quá.", "ma")


def test_magic_bird_only_for_phoenix_not_any_bird():  # [RED-OLD]
    sparrow = story_engine._scene_prompt(
        "Con chim sẻ hót líu lo trên cành.", "cinematic", "9:16")
    assert "bird" in sparrow
    assert story_engine.MAGIC_BIRD_BLOCK not in sparrow
    phoenix = story_engine._scene_prompt(
        "Chim thần phượng hoàng bay lên trời.", "cinematic", "9:16")
    assert story_engine.MAGIC_BIRD_BLOCK in phoenix
    phoenix2 = story_engine._scene_prompt(
        "Phượng hoàng vàng xuất hiện.", "cinematic", "9:16")
    assert story_engine.MAGIC_BIRD_BLOCK in phoenix2


# ── #5a — auth / rate-limit ─────────────────────────────────────────────────

def _dummy_asgi(status=200):
    async def _app(scope, receive, send):
        await send({"type": "http.response.start", "status": status,
                    "headers": [[b"content-type", b"text/plain"]]})
        await send({"type": "http.response.body", "body": b"ok"})
    return _app


async def _call_middleware(mw, path, query=b"", headers=None, client=("127.0.0.1", 5000)):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "http_version": "1.1", "method": "GET",
             "path": path, "query_string": query,
             "headers": headers or [(b"host", b"test")], "client": client}
    await mw(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def test_rate_limit_bucket_purge(monkeypatch):  # [RED-OLD]
    monkeypatch.setattr(app_module, "_rate_limit_buckets", {})
    stale = {f"10.0.0.{i}": [0.0, 1] for i in range(10001)}
    monkeypatch.setattr(app_module, "_rate_limit_buckets", stale)
    mw = app_module._RateLimitMiddleware(_dummy_asgi())
    status, _ = asyncio.run(_call_middleware(mw, "/api/health"))
    assert status == 200
    # Bucket het han (window 60s, start=0.0) phai duoc don, khong ro ri RAM.
    assert len(app_module._rate_limit_buckets) < 1000


def test_rate_limit_xff_trusted_peer():  # [RED-OLD]
    app_module._rate_limit_buckets.clear()
    mw = app_module._RateLimitMiddleware(_dummy_asgi())
    headers = [(b"host", b"test"), (b"x-forwarded-for", b"203.0.113.7")]
    status, _ = asyncio.run(_call_middleware(
        mw, "/api/health", headers=headers, client=("127.0.0.1", 5000)))
    assert status == 200
    assert "203.0.113.7" in app_module._rate_limit_buckets
    assert "127.0.0.1" not in app_module._rate_limit_buckets


def test_rate_limit_xff_ignored_for_public_peer():  # [RED-OLD]
    app_module._rate_limit_buckets.clear()
    mw = app_module._RateLimitMiddleware(_dummy_asgi())
    headers = [(b"host", b"test"), (b"x-forwarded-for", b"203.0.113.7")]
    status, _ = asyncio.run(_call_middleware(
        mw, "/api/health", headers=headers, client=("8.8.8.8", 5000)))
    assert status == 200
    # Peer truc tiep la IP cong khai -> khong tin XFF (chong gia mao IP de
    # ne rate limit); bucket phai theo peer that.
    assert "8.8.8.8" in app_module._rate_limit_buckets
    assert "203.0.113.7" not in app_module._rate_limit_buckets


def test_sse_requires_key_when_configured(monkeypatch):  # [RED-OLD]
    monkeypatch.setenv("STUDIO_API_KEY", "test-secret-123")
    mw = app_module._ApiKeyAuthMiddleware(_dummy_asgi())
    status, _ = asyncio.run(_call_middleware(mw, "/api/jobs/bv_abc123/sse"))
    assert status == 401
    status, _ = asyncio.run(_call_middleware(
        mw, "/api/jobs/bv_abc123/sse", query=b"api_key=wrong"))
    assert status == 401
    status, _ = asyncio.run(_call_middleware(
        mw, "/api/jobs/bv_abc123/sse", query=b"api_key=test-secret-123"))
    assert status == 401
    status, body = asyncio.run(_call_middleware(
        mw, "/api/jobs/bv_abc123/sse", headers=[(b"x-api-key", b"test-secret-123")]))
    assert status == 200 and body == b"ok"


def test_sse_open_when_no_key_configured(monkeypatch):  # [GREEN-OLD]
    monkeypatch.delenv("STUDIO_API_KEY", raising=False)
    mw = app_module._ApiKeyAuthMiddleware(_dummy_asgi())
    status, _ = asyncio.run(_call_middleware(mw, "/api/jobs/bv_abc123/sse"))
    assert status == 200


def test_api_key_compared_with_compare_digest(monkeypatch):  # [RED-OLD]
    import hmac
    monkeypatch.setenv("STUDIO_API_KEY", "test-secret-123")
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    mw = app_module._ApiKeyAuthMiddleware(_dummy_asgi())
    headers = [(b"host", b"test"), (b"x-api-key", b"test-secret-123")]
    status, _ = asyncio.run(_call_middleware(mw, "/api/jobs", headers=headers))
    assert status == 200
    assert calls, "phai so key bang hmac.compare_digest, khong phai !="
    assert calls[0] == (b"test-secret-123", b"test-secret-123")


# ── #5b — mix preview map ve draft ──────────────────────────────────────────

def test_mix_preview_maps_to_draft(tmp_path, monkeypatch):  # [RED-OLD]
    captured = {}

    def fake_run(cmd, label="", timeout=120):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr(composer, "_run", fake_run)
    monkeypatch.setattr(composer, "get_media_duration", lambda p: 8.0)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    composer.mix_multitrack_timeline(str(vid), output_path=str(tmp_path / "out.mp4"),
                                     quality="preview", aspect_ratio="9:16")
    fc = " ".join(captured["cmd"])
    assert "scale=w=480:h=854" in fc, "preview phai dung draft 480p nhu compose_scene_video"
    assert "scale=w=720:h=1280" not in fc


# ── #5c — race _DURATION_CACHE ──────────────────────────────────────────────

def test_duration_cache_has_lock_and_survives_threads(tmp_path):  # [RED-OLD]
    import threading as _th
    assert hasattr(composer, "_DURATION_CACHE_LOCK")
    assert isinstance(composer._DURATION_CACHE_LOCK, type(_th.Lock()))
    clip = tmp_path / "c.mp4"
    _make_clip(clip, dur=1.0, w=320, h=568)
    results = []

    def _measure():
        for _ in range(10):
            results.append(composer.get_media_duration(str(clip)))

    threads = [_th.Thread(target=_measure) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results and all(abs(r - 1.0) < 0.3 for r in results)
    assert len(composer._DURATION_CACHE) <= composer._DURATION_CACHE_MAX_SIZE


# ── #5d — burn_subtitles timeout bao vong doc stdout ────────────────────────

class _HangingProc:
    """Gia lap ffmpeg treo giua chung: ghi 1 dong progress roi dung im
    mai, khong bao gio EOF stdout va khong tu thoat."""

    def __init__(self):
        self.stdout = self._gen()
        self.returncode = None
        self.killed = False
        self._stop = threading.Event()

    def _gen(self):
        yield "out_time=00:00:01.00\n"
        self._stop.wait(timeout=3600)
        return

    def poll(self):
        return None if not self.killed else 0

    def wait(self, timeout=None):
        if self.killed:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired("ffmpeg", timeout)

    def kill(self):
        self.killed = True
        self._stop.set()


def test_burn_subtitles_timeout_covers_read_loop(tmp_path, monkeypatch):  # [RED-OLD]
    vid = tmp_path / "in.mp4"
    _make_clip(vid, dur=2.0, w=320, h=568)
    proc = _HangingProc()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n\n", encoding="utf-8")
    out = tmp_path / "out.mp4"
    t0 = time.monotonic()
    composer.burn_subtitles(str(vid), str(srt), str(out), progress_cb=lambda *a: None,
                            total_duration=2.0, timeout=2.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 20, f"timeout phai co hieu luc trong vong doc, mat {elapsed:.1f}s"
    assert proc.killed, "ffmpeg treo phai bi kill khi het timeout"
    assert out.exists(), "het timeout thi fallback ve video goc"


def test_burn_subtitles_timeout_param_defaults_300():  # [RED-OLD]
    import inspect
    sig = inspect.signature(composer.burn_subtitles)
    assert "timeout" in sig.parameters
    assert sig.parameters["timeout"].default == 300


# ── #5e — _topic_hashtags ───────────────────────────────────────────────────

def test_topic_hashtags_no_duplicates():  # [RED-OLD]
    # "da" chi 2 ky tu nen bi loc boi len>=3 tu truoc; quan trong la
    # "bong" khong lap lai lan 2 nhu code cu.
    tags = auto_factory._topic_hashtags("Bóng đá hay, bóng đá đẹp")
    assert len(tags) == len(set(tags)), f"tag lap lai: {tags}"
    assert tags == ["#bóng", "#hay", "#đẹp"]
    tags2 = auto_factory._topic_hashtags("Phở bò ngon, phở bò nóng")
    assert tags2 == ["#phở", "#ngon", "#nóng"]


# ── #5f — _cache_path ───────────────────────────────────────────────────────

def test_cache_path_sanitizes_traversal():  # [RED-OLD]
    p = auto_factory._cache_path("../evil")
    assert p.resolve().is_relative_to(auto_factory.FACTORY_CACHE_DIR.resolve())
    assert ".." not in p.name
    assert auto_factory._cache_path("trending").name == "trending.json"


# ── #6 — giong VieNeu la lang le doi sang Edge ─────────────────────────────

def test_synthesize_voice_unknown_vieneu_raises(tmp_path, monkeypatch):  # [RED-OLD]
    async def _run():
        return await tts_manager.synthesize_voice(
            "Xin chào", "vieneu:KhongTonTai", str(tmp_path / "o.mp3"),
            allow_silent_fallback=False)

    with pytest.raises(ValueError, match="không có trong catalog|khong co trong catalog"):
        asyncio.run(_run())


def test_synthesize_voice_unknown_omnivoice_raises(tmp_path):  # [RED-OLD]
    async def _run():
        return await tts_manager.synthesize_voice(
            "Xin chào", "omnivoice:KhongTonTai", str(tmp_path / "o.mp3"),
            allow_silent_fallback=False)

    with pytest.raises(ValueError, match="không có trong catalog|khong co trong catalog"):
        asyncio.run(_run())


def test_synthesize_voice_known_vieneu_still_works(tmp_path, monkeypatch):  # [GREEN-OLD]
    def fake_synthesize(**kwargs):
        out = Path(kwargs["output_path"])
        out.write_bytes(b"x" * 100)
        return 2.5

    monkeypatch.setattr(tts_manager.VieNeuEngine, "synthesize",
                        staticmethod(fake_synthesize))

    async def _run():
        return await tts_manager.synthesize_voice(
            "Xin chào", "vieneu:TrucLy", str(tmp_path / "o.mp3"))

    assert asyncio.run(_run()) == pytest.approx(2.5)


# ── #6 — _download_validated de lai file do ────────────────────────────────

class _FakeResp:
    def __init__(self, headers, chunks, status=200):
        self.headers = headers
        self._chunks = chunks
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self, chunk_size=1024 * 1024):
        for c in self._chunks:
            yield c


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def stream(self, method, url, timeout=None):
        return _FakeStreamCtx(self._resp)


def _dl(client, out, expected="video", max_bytes=100):
    return asyncio.run(batch_engine._download_validated(
        client, "http://example.com/f.mp4", out, expected=expected,
        max_bytes=max_bytes, timeout=10.0))


def test_download_validated_removes_partial_on_size_limit(tmp_path):  # [RED-OLD]
    resp = _FakeResp({"content-type": "video/mp4", "content-length": "50"},
                     [b"\x00\x00\x00\x18ftypisom" + b"x" * 60,
                      b"y" * 60])  # tong 140 > max 100
    out = str(tmp_path / "v.mp4")
    with pytest.raises(RuntimeError, match="vượt giới hạn|quá lớn"):
        _dl(_FakeClient(resp), out, max_bytes=100)
    assert not os.path.exists(out), "file tai do phai bi xoa khi raise"


def test_download_validated_bad_content_length_not_crash_and_removes(tmp_path):  # [RED-OLD]
    resp = _FakeResp({"content-type": "video/mp4", "content-length": "abc"},
                     [b"hello world, khong phai video" * 8])  # magic bytes la
    out = str(tmp_path / "v.mp4")
    with pytest.raises(RuntimeError, match="không đúng định dạng|magic"):
        _dl(_FakeClient(resp), out, max_bytes=10000)
    assert not os.path.exists(out)


def test_download_validated_valid_keeps_file(tmp_path):  # [GREEN-OLD]
    chunk = b"\x00\x00\x00\x18ftypisom" + b"x" * 200
    resp = _FakeResp({"content-type": "video/mp4"}, [chunk])
    out = str(tmp_path / "v.mp4")
    assert _dl(_FakeClient(resp), out, max_bytes=10000) == out
    assert os.path.exists(out)


# ── #6 — loc anh affiliate: >=2 tu khoa hoac tu khoa thuong hieu ───────────

def test_image_filter_needs_two_keywords_or_brand():  # [RED-OLD]
    kws = affiliate_engine._product_search_keywords("SỮA ENSURE GOLD")
    assert "ensure" in kws and "gold" in kws
    # Chi "gold" (vd gold-necklace.jpg) khong du.
    assert not affiliate_engine._image_url_matches_product(
        "https://cdn.x.com/gold-necklace.jpg", kws)
    # >= 2 tu khoa thi du.
    assert affiliate_engine._image_url_matches_product(
        "https://cdn.x.com/ensure-gold-milk.jpg", kws)
    # Tu khoa thuong hieu (tu dai nhat) thi du du chi 1 tu.
    assert affiliate_engine._image_url_matches_product(
        "https://cdn.x.com/ensure.jpg", kws)
    # Chi "milk" (tu chung) thi khong du.
    assert not affiliate_engine._image_url_matches_product(
        "https://cdn.x.com/milk.jpg", kws)


def test_clean_product_search_query_expanded_tokens():  # [RED-OLD]
    q = affiliate_engine._clean_product_search_query(
        "COMBO 4 LON SỮA ENSURE GOLD DẠNG BỘT HƯƠNG VANI 800G")
    assert q == "SỮA ENSURE GOLD"
    q2 = affiliate_engine._clean_product_search_query("BỘ 2 LON SỮA ENSURE GOLD")
    assert "BỘ" not in q2 and "LON" not in q2
    assert "ENSURE" in q2 and "GOLD" in q2
    q3 = affiliate_engine._clean_product_search_query("SET 3 HỘP SỮA X")
    assert "SET" not in q3 and "HỘP" not in q3


# ── #6 — merge_clips_advanced dung quality/aspect_ratio that ───────────────

def test_merge_clips_advanced_uses_quality_profile(tmp_path):  # [RED-OLD]
    c1 = tmp_path / "c1.mp4"
    c2 = tmp_path / "c2.mp4"
    _make_clip(c1, dur=2.0, w=720, h=1280)
    _make_clip(c2, dur=2.0, w=720, h=1280)
    out = tmp_path / "merged_draft.mp4"
    composer.merge_clips_advanced(
        [str(c1), str(c2)], transitions=["dissolve"],
        sound_bridge_duration=0.5, output_path=str(out),
        quality="draft", aspect_ratio="9:16")
    assert out.exists() and out.stat().st_size > 1000
    info = subprocess.run(
        [FFMPEG, "-i", str(out)], capture_output=True, text=True)
    m = re.search(r"Video:.* (\d+)x(\d+)", info.stderr)
    assert m, info.stderr[-500:]
    assert (int(m.group(1)), int(m.group(2))) == (480, 854)


def test_merge_clips_advanced_720p_keeps_720(tmp_path):  # [GREEN-OLD]
    c1 = tmp_path / "c1.mp4"
    c2 = tmp_path / "c2.mp4"
    _make_clip(c1, dur=2.0, w=720, h=1280)
    _make_clip(c2, dur=2.0, w=720, h=1280)
    out = tmp_path / "merged_720.mp4"
    composer.merge_clips_advanced(
        [str(c1), str(c2)], transitions=["dissolve"],
        sound_bridge_duration=0.5, output_path=str(out),
        quality="720p", aspect_ratio="9:16")
    info = subprocess.run(
        [FFMPEG, "-i", str(out)], capture_output=True, text=True)
    m = re.search(r"Video:.* (\d+)x(\d+)", info.stderr)
    assert m and (int(m.group(1)), int(m.group(2))) == (720, 1280)


# ── #6 — convert_to_shot_desk khong ghi de project ─────────────────────────

def test_convert_to_shot_desk_does_not_overwrite(ws):  # [RED-OLD]
    mgr = batch_video_engine.BATCH_MANAGER
    items = mgr.add_batch(
        ["Video test ghi de"], auto_run=False,
        scripts=[{"title": "Video test ghi de", "viral_title": "Video test ghi de",
                  "caption": "", "hashtags": [],
                  "scenes": [{"index": 1, "role": "hook", "prompt": "PROMPT GOC",
                              "voiceover": "Thoai goc", "duration": 6}],
                  "character": None}])
    vid = items[0]["id"]
    r1 = mgr.convert_to_shot_desk(vid)
    r2 = mgr.convert_to_shot_desk(vid)
    assert r1["project_id"] != r2["project_id"], "lan convert 2 phai tao project moi"
    p1 = json.loads((shot_desk.SHOT_DESK_DIR / r1["project_id"] / "project.json").read_text(encoding="utf-8"))
    assert p1["shots"][0]["prompt"] == "PROMPT GOC", "project cu phai giu nguyen prompt"


# ── #6 — SFX loi khong hong export + cache theo tag+duration ───────────────

def _shot_desk_project_with_clips(tmp_path, n=2):
    proj_id = "test_sfx_proj"
    proj_dir = shot_desk.SHOT_DESK_DIR / proj_id
    (proj_dir / "clips").mkdir(parents=True, exist_ok=True)
    (proj_dir / "audio").mkdir(parents=True, exist_ok=True)
    (proj_dir / "shots").mkdir(parents=True, exist_ok=True)
    (proj_dir / "final").mkdir(parents=True, exist_ok=True)
    shots = []
    for i in range(1, n + 1):
        clip = proj_dir / "clips" / f"shot_{i:03d}_clip.mp4"
        _make_clip(clip, dur=2.0, w=720, h=1280)
        shots.append({
            "index": i, "title": f"Cảnh {i}", "prompt": f"prompt {i}",
            "voiceover": f"Thoại cảnh {i}", "voice_mode": "narration",
            "voice": "vi-VN-HoaiMyNeural", "duration": 2,
            "status": "approved", "clip_path": f"clips/shot_{i:03d}_clip.mp4",
            "sfx": "rain" if i == 1 else "wind",
            "transition_out": "dissolve",
        })
    project = {"id": proj_id, "title": "Test SFX", "aspect_ratio": "9:16",
               "shots": shots, "characters": [], "default_voice": "vi-VN-HoaiMyNeural"}
    shot_desk.save_shot_desk_project(project)
    return proj_id


def test_timeline_sfx_error_does_not_fail_export(ws, tmp_path, monkeypatch):  # [RED-OLD]
    proj_id = _shot_desk_project_with_clips(tmp_path)

    def flaky_sfx(tag, dur, out, custom_sfx_dir=None):
        if tag == "rain":
            raise RuntimeError("SFX engine hong")
        return composer.generate_sfx_audio(tag, dur, out, custom_sfx_dir=custom_sfx_dir)

    monkeypatch.setattr(shot_desk, "generate_sfx_audio", flaky_sfx)
    result = shot_desk.compose_shot_desk_timeline(
        proj_id, subtitle_mode="none", quality="720p")
    assert result.get("final_video") or result.get("final_dir"), result
    final_dir = Path(result["final_dir"])
    assert list(final_dir.glob("*.mp4")), "phai co video cuoi du SFX 1 shot loi"


def test_timeline_sfx_cached_by_tag_and_duration(ws, tmp_path, monkeypatch):  # [RED-OLD]
    proj_id = _shot_desk_project_with_clips(tmp_path)
    calls = []
    real = composer.generate_sfx_audio

    def counting_sfx(tag, dur, out, custom_sfx_dir=None):
        calls.append((tag, round(float(dur), 1)))
        return real(tag, dur, out, custom_sfx_dir=custom_sfx_dir)

    monkeypatch.setattr(shot_desk, "generate_sfx_audio", counting_sfx)
    shot_desk.compose_shot_desk_timeline(proj_id, subtitle_mode="none", quality="720p")
    first_calls = len(calls)
    assert first_calls == 2
    calls.clear()
    shot_desk.compose_shot_desk_timeline(proj_id, subtitle_mode="none", quality="720p")
    assert len(calls) == 0, f"lan export 2 phai dung cache SFX, van goi {calls}"


# ── #6 — XSS voiceSelectHtml ────────────────────────────────────────────────

def test_voice_select_html_escapes_ids_and_labels(tmp_path):  # [RED-OLD]
    src = Path("static/js/app.js").read_text(encoding="utf-8")
    m = re.search(r"function voiceSelectHtml\(.*?^}", src, re.S | re.M)
    assert m, "khong tim thay voiceSelectHtml"
    esc_src = re.search(r"function esc\(s\).*?^}", src, re.S | re.M).group(0)
    harness = (
        esc_src + "\n" + m.group(0) + "\n"
        "const VOICES = [{id: 'x\" onmouseover=\"alert(1)', label: '<img src=x onerror=alert(2)>', lang: 'vi', gender: 'female'}];\n"
        "console.log(voiceSelectHtml(''));\n"
    )
    js = tmp_path / "harness.js"
    js.write_text(harness, encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert 'onmouseover="alert(1)' not in out
    assert "<img src=x" not in out
    assert "&quot;" in out and "&lt;img" in out


# ── #6 — concat list / get_video / update_video / add_batch ─────────────────

def test_concat_list_line_escapes_apostrophe(tmp_path):
    path = tmp_path / "O'Brien" / "clip.mp4"
    expected = path.as_posix().replace("'", "'\\''")
    assert composer._concat_list_line(str(path)) == f"file '{expected}'\n"


def test_batch_concat_uses_escaped_concat_line():  # [RED-OLD]
    src = Path(batch_video_engine.__file__).read_text(encoding="utf-8")
    assert "_concat_list_line" in src, (
        "batch_video_engine phai dung _concat_list_line (da escape ') "
        "thay vi f-string tho cho concat.txt")


def test_add_batch_extra_cannot_override_id_status(ws):  # [RED-OLD]
    mgr = batch_video_engine.BATCH_MANAGER
    items = mgr.add_batch(
        ["Video extra test"], auto_run=False,
        extra={"id": "hacked", "status": "done", "story_source": "test-src"})
    assert items[0]["id"].startswith("bv_"), "extra khong duoc ghi de id"
    assert items[0]["id"] != "hacked"
    assert items[0]["status"] == "draft", "extra khong duoc ghi de status"
    assert items[0].get("story_source") == "test-src"


def test_update_video_rejects_unsafe_id(ws):  # [RED-OLD]
    mgr = batch_video_engine.BATCH_MANAGER
    # Gieo 1 video co id doc hai vao hang doi (mo phong queue file bi sua).
    mgr._write_queue([{"id": "../evil", "title": "x", "status": "queued"}])
    assert mgr.update_video("../evil", {"status": "done"}) is None
    assert not (ws / "ws" / "evil" / "project.json").exists()
    assert not (ws / "evil").exists()


def test_get_video_rejects_unsafe_id(ws):  # [RED-OLD]
    mgr = batch_video_engine.BATCH_MANAGER
    mgr._write_queue([{"id": "../evil", "title": "x", "status": "queued"}])
    assert mgr.get_video("../evil") is None


# ── #6 — import_shot_desk_zip: duong dan CWD + don proj do + lock ──────────

def _make_shot_desk_zip(manifest, extra_files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project.json", json.dumps(manifest, ensure_ascii=False))
        for name, data in (extra_files or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def _repo_characters_path():
    return Path(shot_desk.__file__).parent / "workspace" / "_characters.json"


@pytest.fixture()
def backup_characters():
    """Sao luu + khoi phuc _characters.json that cua repo quanh test."""
    p = _repo_characters_path()
    had = p.exists()
    orig = p.read_bytes() if had else None
    yield p
    if had:
        p.write_bytes(orig)
    elif p.exists():
        p.unlink()


def test_import_zip_characters_go_to_repo_not_cwd(ws, tmp_path, monkeypatch,
                                                  backup_characters):  # [RED-OLD]
    monkeypatch.chdir(tmp_path)  # doi CWD sang cho khac
    manifest = {"title": "Test Import", "shots": [],
                "characters": [{"id": "char_e_test",
                                "name": "Nhan Vat E Test",
                                "character_block": "block e test",
                                "voice": "vi-VN-HoaiMyNeural"}]}
    proj = shot_desk.import_shot_desk_zip(_make_shot_desk_zip(manifest))
    assert (shot_desk.SHOT_DESK_DIR / proj["id"]).exists()
    assert not (tmp_path / "workspace" / "_characters.json").exists(), (
        "khong duoc ghi _characters.json theo CWD")
    chars = json.loads(backup_characters.read_text(encoding="utf-8"))
    assert any(c.get("name") == "Nhan Vat E Test" for c in chars)


def test_import_zip_cleans_up_project_on_bad_entry(ws):  # [RED-OLD]
    before = set(p.name for p in shot_desk.SHOT_DESK_DIR.iterdir())
    manifest = {"title": "Test Bad Zip", "shots": [], "characters": []}
    bad_zip = _make_shot_desk_zip(manifest, {"../evil.txt": b"x"})
    with pytest.raises(ValueError, match="không an toàn|path traversal"):
        shot_desk.import_shot_desk_zip(bad_zip)
    after = set(p.name for p in shot_desk.SHOT_DESK_DIR.iterdir())
    assert after == before, "proj_dir do dang phai bi xoa khi giai nen loi"


def test_import_zip_characters_write_is_locked(ws, backup_characters):  # [RED-OLD]
    src = Path(shot_desk.__file__).read_text(encoding="utf-8")
    assert "_CHARACTERS" in src and "Lock" in src, (
        "doc-ghi _characters.json phai co lock rieng trong shot_desk.py")
