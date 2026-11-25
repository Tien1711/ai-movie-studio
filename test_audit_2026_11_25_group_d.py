# -*- coding: utf-8 -*-
"""
test_audit_2026_11_25_group_d.py — Behaviour tests for audit group D
(TTS / Shot Desk), main ce8782f, 2026-11-25.

  #7  Edge-TTS / VieNeu / Omni khong co timeout -> scene/job ket vinh vien.
  #8  Shot Desk nuot loi TTS -> long audio cu sai thoai.
  #9  Compose loi -> shot ket vinh vien o "rendering".
  #10 _timeline_tmp chi don khi thanh cong + race compose song song.

Cac test dung timeout tuong minh (nho) de chay nhanh; production dung
timeout mac dinh tinh theo do dai text (max(60, len(text)/5) moi lan goi
engine, max(180, len(text)/5*2) cho toan bo buoc TTS).

Do tren code cu (truoc khi sua):
  - #7: cac ham chua co tham so timeout -> TypeError, va repro tay
    /tmp/repro_tts_hang.py cho thay generate_tts_with_status ket that
    khi Edge-TTS treo (timeout 15s giet, exit 124).
  - #8/#9/#10: cac assert ve file cu / trang thai shot / tmp dir that bai.
"""
import asyncio
import json
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

import batch_engine
import shot_desk
import tts_manager


# ── Fixtures & helpers ────────────────────────────────────────────────────────

@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Shot Desk workspace rieng tren tmp_path (khong dung workspace that)."""
    monkeypatch.setattr(shot_desk, "SHOT_DESK_DIR", tmp_path)
    return tmp_path


def _make_project(ws, project_id="test-proj", shots=None):
    proj = {
        "id": project_id,
        "title": "Test Project",
        "aspect_ratio": "9:16",
        "shots": shots
        if shots is not None
        else [
            {
                "index": 1,
                "prompt": "a test scene",
                "voiceover": "xin chao cac ban",
                "voice": "vi-VN-HoaiMyNeural",
                "duration": 10,
                "status": "ready",
            }
        ],
    }
    shot_desk.save_shot_desk_project(proj)
    # Thu muc runtime ma render_single_shot_desk ghi vao (audio/clips).
    (ws / project_id / "audio").mkdir(parents=True, exist_ok=True)
    (ws / project_id / "clips").mkdir(parents=True, exist_ok=True)
    return proj


def _fake_edge_tts_module(monkeypatch, hang_seconds=30.0):
    """Gia lap module edge_tts ma Communicate.save treo hang_seconds."""

    class _FakeCommunicate:
        def __init__(self, *a, **k):
            pass

        async def save(self, output_path):
            await asyncio.sleep(hang_seconds)

    fake = types.SimpleNamespace(Communicate=_FakeCommunicate)
    monkeypatch.setitem(sys.modules, "edge_tts", fake)
    return fake


class _FakeStreamResp:
    status_code = 200
    headers = {"content-length": "256"}

    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self, chunk_size=1024 * 1024):
        yield self._data


class _FakePostResp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": "task-123"}


class _FakeAsyncClient:
    """Gia lap httpx.AsyncClient cho render_single_shot_desk (buoc 2)."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakePostResp()

    def stream(self, *a, **k):
        return _FakeStreamResp(b"V" * 256)


@pytest.fixture()
def video_harness(monkeypatch):
    """Mock duong render video + compose de render_single_shot_desk chay
    het ma khong goi Muse2API that. Tra ve dict ghi lai cac cuoc goi."""
    calls = {"compose": [], "poll": []}

    async def _fake_poll(*a, **k):
        calls["poll"].append(a)
        return "http://example.invalid/fake.mp4"

    def _fake_compose(**kw):
        calls["compose"].append(kw)
        return kw.get("output")

    monkeypatch.setattr(shot_desk.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(shot_desk, "poll_video_task", _fake_poll)
    monkeypatch.setattr(shot_desk, "compose_scene_video", _fake_compose)
    return calls


# ── #7 — Edge-TTS / TTS timeout ──────────────────────────────────────────────

def test_edge_tts_hang_raises_timeout(tmp_path, monkeypatch):
    """Edge-TTS treo -> synthesize_edge_tts phai raise TimeoutError trong
    khoang timeout chu khong doi mai (audit #7)."""
    async def _run():
        _fake_edge_tts_module(monkeypatch, hang_seconds=30.0)
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(
                tts_manager.synthesize_edge_tts(
                    "xin chao", "vi-VN-HoaiMyNeural", str(tmp_path / "o.mp3"),
                    timeout=1.0,
                ),
                timeout=10,
            )
    asyncio.run(_run())

def test_generate_tts_hang_returns_tts_failed(tmp_path, monkeypatch):
    """synthesize_voice treo -> generate_tts_with_status phai tra ve
    tts_failed=True trong thoi gian gioi han (audit #7)."""
    async def _run():
        unique_text = f"xin chao {uuid.uuid4().hex[:8]}"  # tranh cache hit

        async def _hang(*a, **k):
            await asyncio.sleep(30)

        monkeypatch.setattr(tts_manager, "synthesize_voice", _hang)
        out = tmp_path / "tts.mp3"
        t0 = time.monotonic()
        dur, failed, err = await batch_engine.generate_tts_with_status(
            unique_text, "vi-VN-HoaiMyNeural", str(out), timeout=1.0
        )
        assert time.monotonic() - t0 < 20, "generate_tts_with_status ket qua lau"
        assert failed is True
        assert err, "phai co thong bao loi timeout"
        assert "timeout" in err.lower() or "quá thời gian" in err.lower()
    asyncio.run(_run())

def test_generate_tts_fast_path_still_works(tmp_path, monkeypatch):
    """Guardrail: TTS nhanh binh thuong khong bi timeout oan (audit #7)."""
    async def _run():
        unique_text = f"xin chao {uuid.uuid4().hex[:8]}"

        async def _fast(*a, **k):
            return 3.5

        monkeypatch.setattr(tts_manager, "synthesize_voice", _fast)
        dur, failed, err = await batch_engine.generate_tts_with_status(
            unique_text, "vi-VN-HoaiMyNeural", str(tmp_path / "tts.mp3"),
            timeout=5.0,
        )
        assert dur == 3.5
        assert failed is False
        assert err == ""
    asyncio.run(_run())

def test_vieneu_hang_times_out(monkeypatch):
    """VieNeu (to_thread) treo -> synthesize_voice phai timeout tung lan goi
    engine chu khong ket trong thread (audit #7)."""
    async def _run():
        import time as _time

        def _hanging_synthesize(*a, **k):
            _time.sleep(5)
            return 3.5

        async def _no_edge(*a, **k):
            raise RuntimeError("no edge in test")

        monkeypatch.setattr(
            tts_manager.VieNeuEngine, "synthesize", _hanging_synthesize
        )
        monkeypatch.setattr(tts_manager, "synthesize_edge_tts", _no_edge)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                tts_manager.synthesize_voice(
                    "xin chao", "vieneu:DucTri", "/tmp/x.mp3",
                    allow_silent_fallback=False, timeout=1.0,
                ),
                timeout=10,
            )
    asyncio.run(_run())

# ── #8 — Shot Desk nuot loi TTS / audio cu ───────────────────────────────────

def test_shotdesk_tts_failure_raises_and_removes_stale_audio(ws, monkeypatch):
    """TTS loi o Shot Desk -> raise ro rang, file audio cu/partial bi xoa,
    shot ve ready kem error (audit #8, chinh sach 4d8a563)."""
    async def _run():
        _make_project(ws)
        stale = ws / "test-proj" / "audio" / "shot_001_tts.mp3"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"STALE-AUDIO" * 100)  # >500 bytes

        async def _failed_tts(text, voice, output_path, timeout=None):
            return (5.0, True, "mock TTS boom")

        monkeypatch.setattr(shot_desk, "generate_tts_with_status", _failed_tts)
        with pytest.raises(RuntimeError, match="TTS"):
            await shot_desk.render_single_shot_desk("test-proj", 1, api_key="test-key")
        assert not stale.exists(), "file audio cu phai bi xoa khi TTS loi"
        proj_after = shot_desk.get_shot_desk_project("test-proj")
        shot = proj_after["shots"][0]
        assert shot["status"] == "ready"
        assert shot.get("error") and "TTS" in shot["error"]
    asyncio.run(_run())

def test_shotdesk_compose_uses_fresh_audio_only(ws, monkeypatch, video_harness):
    """TTS thanh cong lan nay -> compose dung dung file audio moi (audit #8)."""
    async def _run():
        _make_project(ws)

        async def _ok_tts(text, voice, output_path, timeout=None):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"FRESH-AUDIO" * 100)
            return (5.0, False, "")

        monkeypatch.setattr(shot_desk, "generate_tts_with_status", _ok_tts)
        await shot_desk.render_single_shot_desk("test-proj", 1, api_key="test-key")
        assert len(video_harness["compose"]) == 1
        audio_out = str(ws / "test-proj" / "audio" / "shot_001_tts.mp3")
        assert video_harness["compose"][0]["scene_audio"] == audio_out
        proj_after = shot_desk.get_shot_desk_project("test-proj")
        assert proj_after["shots"][0]["status"] == "review"
    asyncio.run(_run())

def test_shotdesk_no_voiceover_ignores_stale_audio(ws, monkeypatch, video_harness):
    """Khong co voiceover (khong sinh TTS) ma file audio cu van nam tren dia
    -> compose KHONG duoc long tieng cu, file cu bi don (audit #8)."""
    async def _run():
        shots = [
            {
                "index": 1,
                "prompt": "a test scene",
                "voiceover": "",
                "voice": "vi-VN-HoaiMyNeural",
                "duration": 10,
                "status": "ready",
            }
        ]
        _make_project(ws, shots=shots)
        stale = ws / "test-proj" / "audio" / "shot_001_tts.mp3"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"STALE-AUDIO" * 100)
        await shot_desk.render_single_shot_desk("test-proj", 1, api_key="test-key")
        assert len(video_harness["compose"]) == 1
        assert video_harness["compose"][0]["scene_audio"] is None
        assert not stale.exists(), "file audio cu phai bi don khi khong co TTS moi"
    asyncio.run(_run())

# ── #9 — Compose loi khong ket shot o "rendering" ────────────────────────────

def test_shotdesk_compose_error_resets_shot_state(ws, monkeypatch, video_harness):
    """compose_scene_video loi -> shot ve ready kem error, khong ket o
    rendering; exception van duoc raise (audit #9)."""
    async def _run():
        _make_project(ws)

        async def _ok_tts(text, voice, output_path, timeout=None):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"FRESH-AUDIO" * 100)
            return (5.0, False, "")

        def _boom_compose(**kw):
            video_harness["compose"].append(kw)
            raise RuntimeError("ffmpeg boom")

        monkeypatch.setattr(shot_desk, "generate_tts_with_status", _ok_tts)
        monkeypatch.setattr(shot_desk, "compose_scene_video", _boom_compose)
        with pytest.raises(RuntimeError, match="ffmpeg boom"):
            await shot_desk.render_single_shot_desk("test-proj", 1, api_key="test-key")
        proj_after = shot_desk.get_shot_desk_project("test-proj")
        shot = proj_after["shots"][0]
        assert shot["status"] == "ready", "shot khong duoc ket o rendering"
        assert shot.get("error") and "ffmpeg boom" in shot["error"]
    asyncio.run(_run())

# ── #10 — _timeline_tmp: don sach + tmp rieng moi lan compose ───────────────

def _timeline_project(ws, monkeypatch):
    """Project 1 shot approved + clip gia; mock cac buoc nang de compose
    chay nhanh ma khong can ffmpeg that."""
    shots = [
        {
            "index": 1,
            "prompt": "a test scene",
            "voiceover": "xin chao",
            "voice": "vi-VN-HoaiMyNeural",
            "duration": 10,
            "status": "approved",
        }
    ]
    _make_project(ws, shots=shots)
    clip = ws / "test-proj" / "clips" / "shot_001_clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"FAKE-CLIP" * 100)

    def _fake_sfx(tag, dur, path, custom_sfx_dir=None):
        Path(path).write_bytes(b"SFX" * 100)

    monkeypatch.setattr(shot_desk, "generate_sfx_audio", _fake_sfx)
    monkeypatch.setattr(
        shot_desk, "merge_audio_with_transitions", lambda **k: None
    )
    monkeypatch.setattr(
        shot_desk, "mix_multitrack_timeline", lambda **k: k.get("output_path")
    )


def test_timeline_tmp_removed_on_failure(ws, monkeypatch):
    """Compose loi giua chung -> khong de lai _timeline_tmp* (audit #10)."""
    _timeline_project(ws, monkeypatch)

    def _boom_merge(**k):
        raise RuntimeError("merge boom")

    monkeypatch.setattr(shot_desk, "merge_clips_advanced", _boom_merge)
    with pytest.raises(RuntimeError, match="merge boom"):
        shot_desk.compose_shot_desk_timeline("test-proj", subtitle_mode="none")
    leftovers = [
        p for p in (ws / "test-proj").iterdir() if p.name.startswith("_timeline_tmp")
    ]
    assert leftovers == [], f"tmp con sot lai: {leftovers}"


def test_timeline_tmp_unique_across_parallel_composes(ws, monkeypatch):
    """2 lan compose cung project chay song song -> tmp rieng, khong ghi de
    file tam cua nhau; ca 2 deu xong (audit #10)."""
    _timeline_project(ws, monkeypatch)
    seen_dirs = {}
    seen_contents = {}
    barrier = threading.Barrier(2)

    def _fake_sfx(tag, dur, path, custom_sfx_dir=None):
        name = threading.current_thread().name
        Path(path).write_bytes(f"SFX-{name}".encode() * 50)
        seen_dirs.setdefault(name, set()).add(str(Path(path).parent))
        barrier.wait(timeout=15)

    def _fake_merge_audio(audio_clips, transitions, sound_bridge_duration, output_path):
        name = threading.current_thread().name
        for c in audio_clips:
            seen_contents.setdefault(name, []).append(Path(c).read_bytes()[:20])
        Path(output_path).write_bytes(b"M" * 100)

    monkeypatch.setattr(shot_desk, "generate_sfx_audio", _fake_sfx)
    monkeypatch.setattr(
        shot_desk, "merge_audio_with_transitions", _fake_merge_audio
    )
    monkeypatch.setattr(
        shot_desk, "merge_clips_advanced", lambda **k: None
    )

    errors = []

    def _run():
        try:
            shot_desk.compose_shot_desk_timeline("test-proj", subtitle_mode="none")
        except Exception as e:  # noqa: BLE001 - ghi lai de assert sau
            errors.append(e)

    t1 = threading.Thread(target=_run, name="worker-A")
    t2 = threading.Thread(target=_run, name="worker-B")
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)
    assert not t1.is_alive() and not t2.is_alive(), "compose treo"
    assert not errors, f"compose loi: {errors}"
    dirs_a = seen_dirs.get("worker-A", set())
    dirs_b = seen_dirs.get("worker-B", set())
    assert dirs_a and dirs_b, "khong ghi duoc tmp dir"
    assert dirs_a.isdisjoint(dirs_b), f"2 compose dung chung tmp: {dirs_a} vs {dirs_b}"
    assert seen_contents["worker-A"][0].startswith(b"SFX-worker-A")
    assert seen_contents["worker-B"][0].startswith(b"SFX-worker-B")
    leftovers = [
        p for p in (ws / "test-proj").iterdir() if p.name.startswith("_timeline_tmp")
    ]
    assert leftovers == [], f"tmp con sot lai: {leftovers}"
