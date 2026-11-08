"""Hồi quy: nhân vật + trạng thái phim theo từng phim (scope per-film).

Nhóm lỗi: lưu project nhưng không khôi phục / khôi phục nửa vời —
nhân vật, voice_mode, script_text, lock/nhạc của phim cũ rò sang phim sau.
- Phim A (2 nhân vật + lock + voice_mode ai_voice + script_text) lưu →
  mở lại phải đủ.
- Phim B (không nhân vật, lock rỗng) lưu → mở B phải rỗng, mở lại A
  vẫn đủ (không lây chéo).
- POST /api/movies kèm characters → job lưu snapshot nhân vật của đúng
  phim đó (không phải toàn bộ thư viện).
- Project cũ (file JSON không có key "characters") vẫn mở được qua API
  (frontend xử lý legacy: tạo entry "Nhân vật khóa" từ lock text nếu có).
Chạy bằng TestClient, không cần Muse key, không gọi API video thật.
Dọn project + job test sau mỗi test.
"""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import JOBS, PROJECTS_DIR, WORKSPACE, app

client = TestClient(app)

PROJ_A = "test_scope_film_a"
PROJ_B = "test_scope_film_b"
PROJ_LEGACY = "test_scope_legacy"

CHAR_1 = {
    "id": "char-lan",
    "name": "Lan",
    "aliases": ["Lan Nữ"],
    "voice": "vi-VN-HoaiMyNeural",
    "avatar": "lan.png",
    "description": "Nữ hiệp áo trắng, tóc dài.",
    "character_sheet": "Lan: nữ hiệp áo trắng, tóc dài buộc cao.",
}
CHAR_2 = {
    "id": "char-phong",
    "name": "Phong",
    "aliases": [],
    "voice": "vi-VN-NamMinhNeural",
    "avatar": "phong.png",
    "description": "Kiếm khách áo đen.",
    "character_sheet": "Phong: kiếm khách áo đen, mặt lạnh.",
}

LOCK_A = "Lan: nữ hiệp áo trắng, tóc dài buộc cao."


def _project_payload(name, *, characters, lock_character, voice_mode,
                     script_text, bg_music_path=""):
    return {
        "name": name,
        "title": f"Phim {name}",
        "script_text": script_text,
        "aspect_ratio": "9:16",
        "voice": "vi-VN-HoaiMyNeural",
        "voice_mode": voice_mode,
        "export_mode": "full",
        "chain_mode": "sequential",
        "chain_fallback_mode": "independent",
        "subtitle_mode": "burn_in",
        "quality": "720p",
        "transition": "dissolve",
        "bg_music_path": bg_music_path,
        "music_volume": 0.25,
        "voice_volume": 1.0,
        "consistency_mode": "standard",
        "scenes": [
            {"index": 1, "prompt": "Lan đứng trên đồi", "voiceover": "Lan nói.",
             "duration": 5, "size": "9:16"},
        ],
        "characters": characters,
        "style_locks": {
            "style": "anime", "lighting": "", "color": "",
            "camera": "", "character": lock_character,
        },
    }


def _cleanup_project(name):
    p = Path(PROJECTS_DIR) / f"{name}.json"
    p.unlink(missing_ok=True)


def _cleanup_job(job_id):
    JOBS.pop(job_id, None)
    job_dir = Path(WORKSPACE) / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean():
    yield
    for n in (PROJ_A, PROJ_B, PROJ_LEGACY):
        _cleanup_project(n)


def _save_a():
    r = client.post("/api/projects/save", json=_project_payload(
        PROJ_A, characters=[CHAR_1, CHAR_2], lock_character=LOCK_A,
        voice_mode="ai_voice", script_text="Kịch bản phim A: Lan gặp Phong.",
        bg_music_path="music_a.mp3"))
    assert r.status_code == 200, r.text
    return r.json()


def _save_b():
    r = client.post("/api/projects/save", json=_project_payload(
        PROJ_B, characters=[], lock_character="",
        voice_mode="tts", script_text=""))
    assert r.status_code == 200, r.text
    return r.json()


# ── Round-trip phim A ─────────────────────────────────────────────────────

def test_project_a_roundtrip_keeps_characters_voice_mode_script_text():
    _save_a()
    r = client.get(f"/api/projects/{PROJ_A}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["characters"] == [CHAR_1, CHAR_2]
    assert d["style_locks"]["character"] == LOCK_A
    assert d["voice_mode"] == "ai_voice"
    assert d["script_text"] == "Kịch bản phim A: Lan gặp Phong."
    assert d["bg_music_path"] == "music_a.mp3"


# ── Mở phim B rỗng → không rò từ phim A; mở lại A vẫn đủ ────────────────

def test_project_b_loads_empty_then_a_intact():
    _save_a()
    _save_b()
    rb = client.get(f"/api/projects/{PROJ_B}")
    assert rb.status_code == 200, rb.text
    db = rb.json()
    assert db["characters"] == []
    assert db["style_locks"]["character"] == ""
    assert db["voice_mode"] == "tts"
    assert db["script_text"] == ""
    assert db["bg_music_path"] == ""

    ra = client.get(f"/api/projects/{PROJ_A}")
    assert ra.status_code == 200, ra.text
    da = ra.json()
    assert da["characters"] == [CHAR_1, CHAR_2]
    assert da["style_locks"]["character"] == LOCK_A
    assert da["voice_mode"] == "ai_voice"


# ── Project cũ không có key characters vẫn mở được (backend passthrough) ──

def test_legacy_project_without_characters_key_still_loads():
    legacy = _project_payload(
        PROJ_LEGACY, characters=[], lock_character=LOCK_A,
        voice_mode="tts", script_text="Kịch bản cũ.")
    del legacy["characters"]  # mô phỏng file lưu trước bản scope-per-film
    Path(PROJECTS_DIR).mkdir(parents=True, exist_ok=True)
    with open(Path(PROJECTS_DIR) / f"{PROJ_LEGACY}.json", "w",
              encoding="utf-8") as f:
        json.dump(legacy, f, ensure_ascii=False)
    r = client.get(f"/api/projects/{PROJ_LEGACY}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert "characters" not in d
    assert d["style_locks"]["character"] == LOCK_A
    assert d["script_text"] == "Kịch bản cũ."


# ── Payload tạo phim snapshot đúng nhân vật của phim ──────────────────────

def test_create_movie_snapshots_film_characters():
    payload = {
        "title": "Phim test snapshot",
        "script_text": "Lan gặp Phong.",
        "aspect_ratio": "9:16",
        "voice": "vi-VN-HoaiMyNeural",
        "voice_mode": "tts",
        "export_mode": "full",
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
            {"index": 1, "prompt": "Lan đứng trên đồi", "voiceover": "Lan nói.",
             "duration": 5, "size": "9:16"},
        ],
        "characters": [CHAR_1],
        "style_locks": {"style": "", "lighting": "", "color": "",
                        "camera": "", "character": LOCK_A},
    }
    r = client.post("/api/movies", json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    try:
        job = client.get(f"/api/jobs/{job_id}")
        assert job.status_code == 200, job.text
        assert job.json()["characters"] == [CHAR_1]
    finally:
        _cleanup_job(job_id)


def test_create_movie_without_characters_snapshots_empty():
    payload = {
        "title": "Phim test khong nhan vat",
        "script_text": "",
        "aspect_ratio": "16:9",
        "voice": "vi-VN-HoaiMyNeural",
        "voice_mode": "tts",
        "export_mode": "full",
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
            {"index": 1, "prompt": "Biển xanh", "voiceover": "",
             "duration": 5, "size": "16:9"},
        ],
        "characters": [],
        "style_locks": {"style": "", "lighting": "", "color": "",
                        "camera": "", "character": ""},
    }
    r = client.post("/api/movies", json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    try:
        job = client.get(f"/api/jobs/{job_id}")
        assert job.status_code == 200, job.text
        assert job.json()["characters"] == []
    finally:
        _cleanup_job(job_id)
