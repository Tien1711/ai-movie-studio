"""
test_consistency_mode.py — Tests cho chế độ "Đồng nhất tối đa" (2026-11-07).

- consistency.py: hàm thuần, test trực tiếp không cần server.
- Endpoint: POST /api/consistency-check (không tạo job),
  POST /api/jobs/{job_id}/consistency-check,
  POST /api/movies với consistency_mode=max (TestClient, mock render:
  không có Muse key nên pipeline chỉ đánh dấu job error, không gọi
  API video thật).
- Neo lại nhân vật: test helper consistency_anchor_reference + hằng số.

Chạy từ thư mục gốc repo:  python -m pytest test_consistency_mode.py -v
"""

import shutil
import sys
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from consistency import (
    API_CLIP_SECONDS,
    CONSISTENCY_ANCHOR_EVERY,
    consistency_anchor_reference,
    run_consistency_checks,
)

CHARACTER_LOCK = (
    "solitary swordsman in dark hanfu robe and bamboo conical hat, "
    "long black hair tied low, calm sharp eyes"
)


def _scene(idx, prompt="A swordsman walks through the forest at dawn.",
           voiceover="Lời thoại mẫu.", duration=10, **extra):
    s = {"index": idx, "prompt": prompt, "voiceover": voiceover,
         "duration": duration, "voice_segments": []}
    s.update(extra)
    return s


def _req(scenes, character_lock="", voice="vi-VN-HoaiMyNeural",
         chain_mode="none", consistency_mode="max"):
    return {
        "title": "Test Consistency",
        "voice": voice,
        "style_locks": {"character": character_lock},
        "chain_mode": chain_mode,
        "consistency_mode": consistency_mode,
        "scenes": scenes,
    }


def _codes(result):
    return {c["code"] for c in result["checks"]}


def _by_code(result, code):
    return [c for c in result["checks"] if c["code"] == code]


# ── 1. consistency.py thuần ───────────────────────────────────────────────

def test_lock_missing_is_error():
    req = _req([_scene(1), _scene(2)], character_lock="")
    res = run_consistency_checks(req, consistency_mode="max")
    assert res["passed"] is False
    assert res["errors"] >= 1
    errs = _by_code(res, "lock_present")
    assert len(errs) == 1 and errs[0]["level"] == "error"
    assert "chưa khóa nhân vật" in errs[0]["message"]


def test_lock_present_passes():
    req = _req([_scene(1), _scene(2)], character_lock=CHARACTER_LOCK)
    res = run_consistency_checks(req, consistency_mode="max")
    assert res["passed"] is True
    assert res["errors"] == 0
    assert _by_code(res, "lock_present")[0]["level"] == "info"


def test_lock_via_reference_image_passes():
    req = _req([_scene(1, reference_image="/api/uploads/hero.png")],
               character_lock="")
    res = run_consistency_checks(req, consistency_mode="max")
    assert res["passed"] is True
    assert res["errors"] == 0


def test_no_dialogue_no_characters_needs_no_lock():
    req = _req([_scene(1, voiceover=""), _scene(2, voiceover="")],
               character_lock="")
    req["scenes"] = [
        {"index": 1, "prompt": "Waves crash on the beach.", "voiceover": "",
         "duration": 10, "voice_segments": []},
    ]
    res = run_consistency_checks(req, consistency_mode="max")
    assert res["errors"] == 0  # phim không người/không thoại: không bắt lock


def test_voice_locked_multi_character_default_voice_warns():
    segs = [
        {"character": "Thỏ Trắng", "text": "Chào bạn!", "voice": "vi-VN-HoaiMyNeural"},
        {"character": "Cáo Nhỏ", "text": "Chào!", "voice": "vi-VN-HoaiMyNeural"},
    ]
    req = _req([_scene(1, voice_segments=segs)], character_lock=CHARACTER_LOCK)
    res = run_consistency_checks(req, consistency_mode="max")
    warns = _by_code(res, "voice_locked")
    assert len(warns) == 1 and warns[0]["level"] == "warning"
    assert warns[0]["scene_index"] == 1


def test_voice_locked_distinct_voices_ok():
    segs = [
        {"character": "Thỏ Trắng", "text": "Chào bạn!", "voice": "voice-A"},
        {"character": "Cáo Nhỏ", "text": "Chào!", "voice": "voice-B"},
    ]
    req = _req([_scene(1, voice_segments=segs)], character_lock=CHARACTER_LOCK)
    res = run_consistency_checks(req, consistency_mode="max")
    assert _by_code(res, "voice_locked") == []


def test_bridge_day_to_night_warns_with_suggestion():
    req = _req([
        _scene(1, prompt="A swordsman trains in the village at dawn."),
        _scene(2, prompt="The swordsman fights in the forest at night."),
    ], character_lock=CHARACTER_LOCK)
    res = run_consistency_checks(req, consistency_mode="max")
    warns = _by_code(res, "bridge")
    assert len(warns) == 1 and warns[0]["level"] == "warning"
    assert warns[0]["scene_index"] == 2
    assert "cầu nối" in warns[0]["suggestion"]
    assert "Wide establishing shot" in warns[0]["suggestion"]


def test_bridge_same_time_place_no_warning():
    req = _req([
        _scene(1, prompt="A swordsman walks in the forest at dawn."),
        _scene(2, prompt="The swordsman rests in the forest at dawn."),
    ], character_lock=CHARACTER_LOCK)
    res = run_consistency_checks(req, consistency_mode="max")
    assert _by_code(res, "bridge") == []


def test_duration_over_10_warns():
    req = _req([_scene(1, duration=14)], character_lock=CHARACTER_LOCK)
    res = run_consistency_checks(req, consistency_mode="max")
    warns = _by_code(res, "duration")
    assert len(warns) == 1 and warns[0]["level"] == "warning"
    assert str(API_CLIP_SECONDS) in warns[0]["message"]


def test_duration_10_no_warning():
    req = _req([_scene(1, duration=10)], character_lock=CHARACTER_LOCK)
    res = run_consistency_checks(req, consistency_mode="max")
    assert _by_code(res, "duration") == []


def test_chain_info_reports_max_mode():
    req = _req([_scene(1)], character_lock=CHARACTER_LOCK)
    res = run_consistency_checks(req, consistency_mode="max")
    chain = _by_code(res, "chain")
    assert len(chain) == 1 and "sequential" in chain[0]["message"]


# ── 2. Neo lại nhân vật ───────────────────────────────────────────────────

def test_anchor_constant_is_8():
    assert CONSISTENCY_ANCHOR_EVERY == 8


def test_anchor_reference_prefers_keyframe_then_character():
    assert consistency_anchor_reference(
        {"keyframe_path": "/tmp/k.png",
         "character_reference": "/tmp/c.png"}) == "/tmp/k.png"
    assert consistency_anchor_reference(
        {"character_reference": "/tmp/c.png"}) == "/tmp/c.png"
    assert consistency_anchor_reference({}) is None


# ── 3. Endpoint (TestClient, không gọi API video thật) ────────────────────

from app import app, JOBS, WORKSPACE  # noqa: E402

client = TestClient(app)


def _cleanup_job(job_id):
    JOBS.pop(job_id, None)
    job_dir = Path(WORKSPACE) / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


def test_endpoint_check_no_job_needed():
    payload = _req([_scene(1), _scene(2)], character_lock="")
    resp = client.post("/api/consistency-check", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["passed"] is False
    assert data["errors"] >= 1
    assert any(c["code"] == "lock_present" for c in data["checks"])


def test_create_movie_max_missing_lock_422():
    payload = _req([_scene(1), _scene(2)], character_lock="",
                   chain_mode="none")
    resp = client.post("/api/movies", json=payload)
    assert resp.status_code == 422
    data = resp.json()
    assert "checks" in data
    assert any(c["code"] == "lock_present" and c["level"] == "error"
               for c in data["checks"])


def test_create_movie_max_forces_sequential():
    payload = _req([_scene(1), _scene(2)], character_lock=CHARACTER_LOCK,
                   chain_mode="none")
    resp = client.post("/api/movies", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    job_id = data["job_id"]
    try:
        assert data.get("consistency_mode") == "max"
        assert data.get("chain_mode") == "sequential"
        meta = client.get(f"/api/jobs/{job_id}/metadata")
        assert meta.status_code == 200
        req_saved = meta.json()["req"]
        assert req_saved["chain_mode"] == "sequential"
        assert req_saved["consistency_mode"] == "max"

        # Endpoint check trên job đã lưu
        chk = client.post(f"/api/jobs/{job_id}/consistency-check")
        assert chk.status_code == 200
        chk_data = chk.json()
        assert chk_data["consistency_mode"] == "max"
        assert chk_data["chain_mode"] == "sequential"
        assert chk_data["passed"] is True
    finally:
        _cleanup_job(job_id)


def test_create_movie_standard_keeps_chain_none():
    payload = _req([_scene(1), _scene(2)], character_lock="",
                   chain_mode="none", consistency_mode="standard")
    resp = client.post("/api/movies", json=payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    try:
        meta = client.get(f"/api/jobs/{job_id}/metadata")
        assert meta.json()["req"]["chain_mode"] == "none"
    finally:
        _cleanup_job(job_id)


def test_create_movie_invalid_mode_422():
    payload = _req([_scene(1)], character_lock=CHARACTER_LOCK,
                   consistency_mode="ultra")
    resp = client.post("/api/movies", json=payload)
    assert resp.status_code == 422
