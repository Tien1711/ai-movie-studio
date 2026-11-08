"""Hồi quy nhóm NẶNG audit 2026-11-08 (nhóm 1: #5, #6, #8, #9).

- #5: duration cảnh ngoài [5,6,8,10] (vd 7s) không được âm thầm sửa thành 5s.
- #6: max_concurrent 13-30 phải bị chặn (UI kẹp <= 12; API 422 rõ ràng).
- #8: cancel phải dừng render thật (event kiểm tra trước mỗi cảnh + trong
  vòng poll; CancelledError cập nhật timing/log/trạng thái).
- #9: TTS lỗi vẫn fallback im lặng nhưng scene phải bị đánh dấu tts_failed
  và job phải cảnh báo rõ trong Monitor/log.
Chạy bằng TestClient / gọi trực tiếp, không cần Muse key, không gọi API video thật.
"""
import asyncio
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_mod
import batch_engine
from app import JOBS, JOB_CANCEL_EVENTS, PROJECTS_DIR, WORKSPACE, app

client = TestClient(app)

PROJ_DUR = "test_audit_severe_duration"


def _cleanup_project(name):
    p = Path(PROJECTS_DIR) / f"{name}.json"
    p.unlink(missing_ok=True)


def _cleanup_job(job_id):
    JOBS.pop(job_id, None)
    JOB_CANCEL_EVENTS.pop(job_id, None)
    job_dir = Path(WORKSPACE) / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


def _movie_payload(**overrides):
    payload = {
        "title": "Phim test audit severe",
        "aspect_ratio": "16:9",
        "voice": "vi-VN-HoaiMyNeural",
        "voice_mode": "tts",
        "export_mode": "full",
        "max_concurrent": 6,
        "chain_mode": "none",
        "chain_fallback_mode": "independent",
        "subtitle_mode": "burn_in",
        "quality": "720p",
        "transition": "none",
        "bg_music_path": "",
        "music_volume": 0.25,
        "voice_volume": 1.0,
        "consistency_mode": "standard",
        "scenes": [
            {"index": 1, "prompt": "Canh 1", "voiceover": "Mot",
             "duration": 7, "size": "16:9"},
        ],
        "characters": [],
        "style_locks": {"style": "", "lighting": "", "color": "",
                        "camera": "", "character": ""},
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _clean():
    yield
    _cleanup_project(PROJ_DUR)


# ── #5 Duration ───────────────────────────────────────────────────────────

def test_05_duration_7_survives_project_roundtrip():
    """Cảnh 7s (AI breakdown / project cũ) lưu project rồi mở lại vẫn 7s."""
    payload = {
        "name": PROJ_DUR,
        "title": "Phim duration test",
        "script_text": "",
        "aspect_ratio": "16:9",
        "voice": "vi-VN-HoaiMyNeural",
        "voice_mode": "tts",
        "consistency_mode": "standard",
        "export_mode": "full",
        "chain_mode": "none",
        "chain_fallback_mode": "independent",
        "subtitle_mode": "burn_in",
        "quality": "720p",
        "transition": "none",
        "bg_music_path": "",
        "music_volume": 0.25,
        "voice_volume": 1.0,
        "scenes": [
            {"index": 1, "prompt": "Canh 7 giay", "voiceover": "",
             "duration": 7, "size": "16:9"},
            {"index": 2, "prompt": "Canh 3 giay", "voiceover": "",
             "duration": 3, "size": "16:9"},
            {"index": 3, "prompt": "Canh 12 giay", "voiceover": "",
             "duration": 12, "size": "16:9"},
        ],
        "characters": [],
        "style_locks": {"style": "", "lighting": "", "color": "",
                        "camera": "", "character": ""},
    }
    r = client.post("/api/projects/save", json=payload)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/projects/{PROJ_DUR}")
    assert r.status_code == 200, r.text
    durations = [s["duration"] for s in r.json()["scenes"]]
    assert durations == [7, 3, 12], durations


def test_05_duration_7_accepted_by_movie_api():
    """POST /api/movies với cảnh 7s: backend nhận đúng 7s trong job req."""
    r = client.post("/api/movies", json=_movie_payload())
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    try:
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["req"]["scenes"][0]["duration"] == 7
    finally:
        _cleanup_job(job_id)


def test_05_duration_bounds_still_enforced():
    """Backend vẫn chặn duration ngoài 1-60 (0 và 61 -> 422)."""
    for bad in (0, 61):
        payload = _movie_payload(scenes=[
            {"index": 1, "prompt": "x", "voiceover": "",
             "duration": bad, "size": "16:9"}])
        r = client.post("/api/movies", json=payload)
        assert r.status_code == 422, (bad, r.text)


# ── #6 Concurrent ─────────────────────────────────────────────────────────

def test_06_max_concurrent_20_rejected_with_clear_detail():
    """max_concurrent=20 -> 422, detail nêu rõ field + giới hạn, không tạo job."""
    r = client.post("/api/movies", json=_movie_payload(max_concurrent=20))
    assert r.status_code == 422, r.text
    assert "max_concurrent" in r.text
    assert "job_id" not in r.json()


def test_06_max_concurrent_12_accepted():
    """max_concurrent=12 (giới hạn backend) vẫn được chấp nhận."""
    r = client.post("/api/movies", json=_movie_payload(max_concurrent=12))
    assert r.status_code == 200, r.text
    try:
        job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        assert job["req"]["max_concurrent"] == 12
    finally:
        _cleanup_job(r.json()["job_id"])


def test_06_max_concurrent_13_rejected():
    """13 (vừa vượt giới hạn) cũng 422 — ranh giới đúng ở 12."""
    r = client.post("/api/movies", json=_movie_payload(max_concurrent=13))
    assert r.status_code == 422, r.text


# ── #8 Cancel ─────────────────────────────────────────────────────────────

def test_08_run_batch_preset_cancel_returns_cancelled_without_network(tmp_path):
    """Event đã set trước khi batch chạy: không gọi API, mọi cảnh cancelled."""
    ev = asyncio.Event()
    ev.set()
    scenes = [
        {"index": 1, "prompt": "a", "voiceover": "", "duration": 5},
        {"index": 2, "prompt": "b", "voiceover": "", "duration": 5},
    ]
    results = asyncio.run(batch_engine.run_batch(
        scenes=scenes, base_url="http://127.0.0.1:9", api_key="x",
        output_dir=str(tmp_path), cancel_event=ev))
    assert [r["status"] for r in results] == ["cancelled", "cancelled"]
    assert all(r["index"] in (1, 2) for r in results)


def test_08_run_batch_stops_before_next_scene_sequential(monkeypatch, tmp_path):
    """Sequential: hủy sau cảnh 1 -> cảnh 2 không được render (cancelled)."""
    ev = asyncio.Event()

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _FakeResp()

    async def _fake_process_scene(client, s, sem, *a, **kw):
        if s["index"] == 1:
            ev.set()
            return {"index": 1, "status": "done", "error": None,
                    "video_path": "/tmp/x.mp4", "audio_path": None,
                    "voiceover": "", "audio_duration": 5}
        raise AssertionError("canh 2 khong duoc render sau khi huy")

    monkeypatch.setattr(batch_engine.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(batch_engine, "process_scene", _fake_process_scene)
    scenes = [
        {"index": 1, "prompt": "a", "voiceover": "", "duration": 5},
        {"index": 2, "prompt": "b", "voiceover": "", "duration": 5},
    ]
    results = asyncio.run(batch_engine.run_batch(
        scenes=scenes, base_url="http://127.0.0.1:9", api_key="x",
        output_dir=str(tmp_path), chain_mode="sequential", cancel_event=ev))
    by_idx = {r["index"]: r["status"] for r in results}
    assert by_idx == {1: "done", 2: "cancelled"}, by_idx


def test_08_poll_video_task_raises_on_cancel():
    """Vòng poll kiểm tra event mỗi lượt: event set -> CancelledError ngay."""
    ev = asyncio.Event()
    ev.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(batch_engine.poll_video_task(
            client=None, task_id="t1", base_url="http://127.0.0.1:9",
            headers={}, scene_idx=1, cancel_event=ev))


def test_08_film_pipeline_cancelled_error_updates_timing_and_status(monkeypatch):
    """CancelledError trong pipeline: job cancelled + timing.total_s + log."""
    job_id = "test_audit_cancel_pipe"
    job = {"id": job_id, "logs": [], "scenes": {}, "status": "running",
           "cancelled": False, "progress": 0}

    async def _raise_cancel(**kw):
        raise asyncio.CancelledError("cancel test")

    monkeypatch.setattr(app_mod, "MUSE_API_KEY", "test-key")
    monkeypatch.setattr(app_mod, "run_batch", _raise_cancel)
    req = app_mod.MovieRequest(**_movie_payload())
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(app_mod.film_pipeline(job, req))
        assert job["status"] == "cancelled"
        assert job["timing"]["total_s"] >= 0
        assert any("đã bị hủy" in line for line in job["logs"])
        assert job_id not in JOB_CANCEL_EVENTS  # finally dọn event
    finally:
        _cleanup_job(job_id)


def test_08_api_cancel_job_sets_cancel_event():
    """POST /api/jobs/{id}/cancel set cả cờ job lẫn event cho run_batch."""
    job_id = "test_audit_cancel_api"
    ev = asyncio.Event()
    job = {"id": job_id, "logs": [], "scenes": {}, "status": "running",
           "cancelled": False, "progress": 0}
    JOBS[job_id] = job
    JOB_CANCEL_EVENTS[job_id] = ev
    try:
        r = client.post(f"/api/jobs/{job_id}/cancel")
        assert r.status_code == 200, r.text
        assert ev.is_set()
        assert job["cancelled"] is True
        assert job["status"] == "cancelled"
    finally:
        _cleanup_job(job_id)


# ── #9 TTS failed ─────────────────────────────────────────────────────────

def test_09_generate_tts_marks_failure_but_keeps_silent_fallback(monkeypatch, tmp_path):
    """synthesize lỗi -> (dur > 0, failed=True, error rõ), vẫn có fallback."""
    import tts_manager

    def _boom(*a, **kw):
        raise RuntimeError("TTS boom: engine sập")

    def _fake_silent(path, dur):
        Path(path).write_bytes(b"x" * 100)
        return dur

    monkeypatch.setattr(tts_manager, "synthesize_voice", _boom)
    monkeypatch.setattr(tts_manager, "create_silent_audio_fallback", _fake_silent)
    out = str(tmp_path / "scene_001.mp3")
    # Text duy nhất mỗi lần chạy để không dính cache TTS từ lần chạy trước.
    import uuid as _uuid
    _text = f"Audit severe TTS loi {_uuid.uuid4().hex[:8]}"
    dur, failed, err = asyncio.run(
        batch_engine.generate_tts_with_status(_text, "vi-VN-HoaiMyNeural", out))
    assert dur > 0
    assert failed is True
    assert "TTS boom" in err
    # generate_tts cũ vẫn chỉ trả float (tương thích ngược).
    dur2 = asyncio.run(batch_engine.generate_tts(_text, "vi-VN-HoaiMyNeural", out))
    assert isinstance(dur2, float) and dur2 > 0


def test_09_generate_tts_success_not_marked(monkeypatch, tmp_path):
    """synthesize OK -> failed=False, error rỗng."""
    import tts_manager

    async def _ok(*a, **kw):
        Path(kw["output_path"]).write_bytes(b"x" * 100)
        return 3.5

    monkeypatch.setattr(tts_manager, "synthesize_voice", _ok)
    out = str(tmp_path / "ok.mp3")
    # Text duy nhất mỗi lần chạy để không dính cache TTS từ lần chạy trước.
    import uuid as _uuid
    dur, failed, err = asyncio.run(
        batch_engine.generate_tts_with_status(f"Audit severe TTS ok {_uuid.uuid4().hex[:8]}", "vi-VN-HoaiMyNeural", out))
    assert failed is False
    assert err == ""
    assert dur == 3.5


def test_09_multi_voice_marks_failed_segment(monkeypatch, tmp_path):
    """Đa giọng: 1 đoạn lỗi -> failed=True, error nêu tên nhân vật đoạn lỗi."""
    import tts_manager

    async def _sometimes(*a, **kw):
        # Đoạn tốt: tạo mp3 im lặng THẬT bằng ffmpeg để bước concat sau đó
        # chạy được; đoạn xấu: raise để đi qua silent fallback thật.
        if kw.get("voice") == "bad-voice":
            raise RuntimeError("giọng hỏng")
        return tts_manager.create_silent_audio_fallback(kw["output_path"], 2.0)

    monkeypatch.setattr(tts_manager, "synthesize_voice", _sometimes)
    import uuid as _uuid
    _tag = _uuid.uuid4().hex[:8]
    segs = [
        {"character": "Lan", "text": f"Chào {_tag}", "voice": "good-voice"},
        {"character": "Phong", "text": f"Ừ {_tag}", "voice": "bad-voice"},
    ]
    total, failed, err = asyncio.run(
        batch_engine.generate_multi_voice_tts_with_status(
            segs, str(tmp_path / "multi.mp3"), str(tmp_path / "segs")))
    assert failed is True
    assert "Phong" in err


def test_09_film_pipeline_flags_tts_failed_scenes_and_warns(monkeypatch):
    """run_batch trả scene 2 tts_failed -> job tts_failed_scenes=[2],
    scene state có cờ, log có cảnh báo rõ (dù compose sau đó lỗi)."""
    job_id = "test_audit_tts_pipe"
    job = {"id": job_id, "logs": [], "scenes": {}, "status": "running",
           "cancelled": False, "progress": 0}

    async def _fake_batch(**kw):
        return [
            {"index": 1, "status": "done", "video_path": "/tmp/nope1.mp4",
             "audio_path": "/tmp/a1.mp3", "voiceover": "Mot",
             "audio_duration": 5, "last_frame_url": None,
             "tts_failed": False, "tts_error": None},
            {"index": 2, "status": "done", "video_path": "/tmp/nope2.mp4",
             "audio_path": "/tmp/a2.mp3", "voiceover": "Hai",
             "audio_duration": 5, "last_frame_url": None,
             "tts_failed": True, "tts_error": "TTS boom: engine sập"},
        ]

    monkeypatch.setattr(app_mod, "MUSE_API_KEY", "test-key")
    monkeypatch.setattr(app_mod, "run_batch", _fake_batch)
    req = app_mod.MovieRequest(**_movie_payload(
        allow_partial=True,
        scenes=[
            {"index": 1, "prompt": "Canh 1", "voiceover": "Mot",
             "duration": 5, "size": "16:9"},
            {"index": 2, "prompt": "Canh 2", "voiceover": "Hai",
             "duration": 5, "size": "16:9"},
        ]))
    try:
        asyncio.run(app_mod.film_pipeline(job, req))
        assert job["tts_failed_scenes"] == [2]
        assert job["scenes"]["2"]["tts_failed"] is True
        assert "TTS lỗi" in job["scenes"]["2"]["msg"]
        assert job["scenes"]["1"]["tts_failed"] is False
        warn = [line for line in job["logs"] if "Cảnh báo TTS" in line]
        assert len(warn) == 1 and "#2" in warn[0]
    finally:
        _cleanup_job(job_id)
