"""Hồi quy: nhóm audit vừa + nhỏ 2026-11-08 (#10–#17).

- #10 /api/reference-images là tên chính, /api/character-library còn lại
  làm alias deprecated (trả cùng dữ liệu).
- #11 sidebar thu gọn thật 64px (CSS).
- #12 voice đặc biệt (ai_voice/native/none/no_dub) không được nghe thử:
  backend trả 400, frontend chặn trong testPlayVoice + disable nút.
- #13 nhãn âm lượng dạng ×N, slider nhạc max 2.0.
- #14 update-prompt + /api/library đã xóa; last-frame GIỮ LẠI (backend
  tự sinh last_frame_url trỏ tới nó).
- #15 _voice_previews prune LRU tối đa 200 file.
- #16 fontSize UI 8–72 khớp backend.
- #17 hint concurrent không hứa chia tài khoản.
Chạy bằng TestClient, không cần Muse key, không gọi API video thật.
"""
import inspect
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import (
    MovieRequest,
    _voice_preview_prune,
    app,
    preview_voice,
)

client = TestClient(app)
ROOT = Path(__file__).resolve().parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_CSS = (ROOT / "static" / "css" / "ui.css").read_text(encoding="utf-8")

SPECIAL_VOICES = ["ai_voice", "native", "none", "no_dub"]


# ── #10 ─────────────────────────────────────────────────────────────────
def test_reference_images_endpoint_and_deprecated_alias():
    r_new = client.get("/api/reference-images")
    assert r_new.status_code == 200
    assert isinstance(r_new.json(), list)
    r_old = client.get("/api/character-library")
    assert r_old.status_code == 200
    assert r_old.json() == r_new.json()
    assert "/api/reference-images" in APP_JS
    assert "/api/character-library" not in APP_JS


# ── #12 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("voice", SPECIAL_VOICES)
def test_preview_special_voice_rejected(voice):
    r = client.get("/api/voices/preview", params={"voice": voice})
    assert r.status_code == 400
    assert "không có bản nghe thử" in r.json()["detail"]


def test_preview_special_voice_no_longer_substitutes_hoaimy():
    src = inspect.getsource(preview_voice)
    # Không còn gán voice = HoaiMy để phát thay (default param của hàm vẫn
    # là HoaiMy cho voice TTS thường — đó không phải substitution).
    assert 'voice = "vi-VN-HoaiMyNeural"' not in src
    assert "400" in src


def test_frontend_blocks_special_voice_preview():
    for voice in SPECIAL_VOICES:
        assert f"'{voice}'" in APP_JS
    assert "isSpecialVoice(voiceId)" in APP_JS
    assert "specialVoicePreviewAttrs(seg.voice)" in APP_JS
    assert "specialVoicePreviewAttrs(s.voice" in APP_JS
    assert "specialVoicePreviewAttrs(c.voice)" in APP_JS
    assert "specialVoicePreviewAttrs(voice)" in APP_JS
    assert "specialVoicePreviewAttrs(defaultVoice)" in APP_JS


# ── #13 ─────────────────────────────────────────────────────────────────
def test_volume_labels_use_multiplier_not_percent():
    assert "formatVolumeLabel" in APP_JS
    assert 'id="lblMusicVol">×0.25<' in INDEX_HTML
    assert 'id="lblVoiceVol">×1.0<' in INDEX_HTML
    assert 'id="sdMusicVolVal">×0.25<' in INDEX_HTML
    assert 'id="sdVoiceVolVal">×1.0<' in INDEX_HTML
    assert "Math.round(this.value*100)" not in INDEX_HTML
    assert "Math.round(n * 100)" not in APP_JS
    assert 'id="musicVolume" min="0" max="2"' in INDEX_HTML
    assert 'id="voiceVolume" min="0" max="2"' in INDEX_HTML
    assert 'id="sdMusicVol" min="0" max="2"' in INDEX_HTML
    assert 'id="sdVoiceVol" min="0.5" max="2"' in INDEX_HTML


# ── #14 ─────────────────────────────────────────────────────────────────
def test_dead_endpoints_removed():
    r = client.post(
        "/api/jobs/nope/scenes/update-prompt",
        json={"scene_index": 1, "new_prompt": "x"},
    )
    assert r.status_code == 404
    assert client.get("/api/library").status_code == 404


def test_last_frame_route_kept():
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/api/jobs/{job_id}/last-frame/{scene_idx}" in paths
    # Job không tồn tại -> 404 từ handler (chứ không phải 404 vì mất route).
    r = client.get("/api/jobs/nope/last-frame/1")
    assert r.status_code == 404


# ── #15 ─────────────────────────────────────────────────────────────────
def test_voice_preview_prune_lru_max_200(tmp_path):
    now = time.time()
    for i in range(205):
        p = tmp_path / f"voice_{i:03d}.mp3"
        p.write_bytes(b"x" * 10)
        os.utime(p, (now - (205 - i), now - (205 - i)))
    _voice_preview_prune(tmp_path)
    remaining = sorted(p.name for p in tmp_path.glob("*.mp3"))
    assert len(remaining) == 200
    assert "voice_000.mp3" not in remaining  # cũ nhất bị xóa
    assert "voice_204.mp3" in remaining  # mới nhất còn lại


# ── #16 ─────────────────────────────────────────────────────────────────
def test_font_size_ui_matches_backend():
    assert 'id="fontSize" value="22" min="8" max="72"' in INDEX_HTML
    req = MovieRequest(
        font_size=72,
        music_volume=2.0,
        voice_volume=2.0,
        scenes=[{"index": 1, "prompt": "x"}],
    )
    assert req.font_size == 72
    assert req.music_volume == 2.0
    with pytest.raises(ValidationError):
        MovieRequest(font_size=73, scenes=[{"index": 1, "prompt": "x"}])


# ── #11 ─────────────────────────────────────────────────────────────────
def test_sidebar_collapsed_really_collapses():
    assert "body.sidebar-collapsed .shell { grid-template-columns: 64px" in UI_CSS
    assert "body.sidebar-collapsed .sidebar .nav-label" in UI_CSS
    assert "body.sidebar-collapsed .sidebar-footer" in UI_CSS


# ── #17 ─────────────────────────────────────────────────────────────────
def test_concurrent_hint_no_longer_promises_account_split():
    assert "Tối đa ${data.accounts} luồng song song (đặt ở ô Concurrent)" in APP_JS
    assert "chia đều chạy song song" not in APP_JS
    assert "Tối đa N luồng song song (đặt ở ô Concurrent)" in INDEX_HTML
