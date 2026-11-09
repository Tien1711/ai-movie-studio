"""
test_perfect_g2_characters.py — Hồi quy NHÓM 2 / nhiệm vụ 1: nối snapshot
nhân vật CỦA PHIM (job["characters"]) vào pipeline render.

- apply_film_characters: scene nhắc tên ai thì dán đúng Character Block
  NGUYÊN VĂN của người đó, không dán block nhân vật còn lại, không dán
  trùng khi block đã có, khớp cả alias, không khớp nhầm biên từ.
- process_scene (mock network/TTS/compose — KHÔNG gọi API video thật):
  prompt cuối (used_prompt) chứa đúng block của nhân vật được nhắc.
- run_batch: chuyển tiếp snapshot characters xuống process_scene
  (mock process_scene + mock health-check).

Chạy: python -m pytest test_perfect_g2_characters.py -v
"""
import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

import batch_engine
from batch_engine import (
    apply_film_characters,
    character_block_of,
    match_film_characters,
    process_scene,
    run_batch,
)

LAN_BLOCK = "Nữ hiệp áo trắng, tóc dài buộc cao, kiếm bạc bên hông."
PHONG_BLOCK = "Kiếm khách áo đen, mặt lạnh, sẹo dài trên má trái."
LAN = {
    "id": "char-lan", "name": "Lan", "aliases": ["Lan Nữ"],
    "voice": "vi-VN-HoaiMyNeural", "avatar": "lan.png",
    "description": LAN_BLOCK,
    "character_sheet": {"front": "lan_front.png"},
}
PHONG = {
    "id": "char-phong", "name": "Phong", "aliases": [],
    "voice": "vi-VN-NamMinhNeural", "avatar": "phong.png",
    "description": PHONG_BLOCK,
    "character_sheet": {"front": "phong_front.png"},
}
CHARS = [LAN, PHONG]


# ── 1. Helper thuần ────────────────────────────────────────────────────────

def test_character_block_of_prefers_block_fields_over_sheet_dict():
    # character_sheet dạng dict (URL ảnh) không phải block text.
    assert character_block_of(LAN) == LAN_BLOCK
    assert character_block_of(PHONG) == PHONG_BLOCK
    # Snapshot cũ: character_sheet dạng chuỗi vẫn dùng được.
    assert character_block_of(
        {"name": "X", "character_sheet": "X: mô tả cũ."}) == "X: mô tả cũ."
    assert character_block_of({"name": "Y"}) == ""
    # character_block thắng description.
    assert character_block_of(
        {"character_block": "BLOCK CHUẨN", "description": "mô tả khác"}) == "BLOCK CHUẨN"


def test_only_mentioned_character_block_appended_verbatim():
    out = apply_film_characters(
        "Lan đứng trên đồi buổi sáng",
        {"prompt": "Lan đứng trên đồi buổi sáng", "voiceover": ""}, CHARS)
    assert LAN_BLOCK in out
    assert f"Character [Lan]: {LAN_BLOCK}" in out
    assert PHONG_BLOCK not in out
    assert "Phong" not in out


def test_alias_matches_and_longer_name_wins():
    assert [c["name"] for c in match_film_characters(
        "Lan Nữ rút kiếm", CHARS)] == ["Lan"]
    out = apply_film_characters(
        "Lan Nữ rút kiếm", {"prompt": "Lan Nữ rút kiếm"}, CHARS)
    assert LAN_BLOCK in out and PHONG_BLOCK not in out


def test_no_mention_no_block_and_no_false_word_boundary_match():
    # "Phongba"/"Lankhông" dính liền thì không khớp (biên từ). Lưu ý giới
    # hạn: "Phong" đứng riêng như một từ (vd "phong ba") vẫn khớp — giống
    # nhận diện phía frontend (substring) — không phân biệt được danh từ
    # chung và tên riêng khi trùng chữ.
    base = "Phongba bão táp ngoài khơi, Lankhông phải tên ai"
    out = apply_film_characters(base, {"prompt": base}, CHARS)
    assert out == base


def test_block_already_present_is_not_duplicated():
    prompt = f"Lan đứng trên đồi, Character [Lan]: {LAN_BLOCK}"
    out = apply_film_characters(prompt, {"prompt": prompt}, CHARS)
    assert out.count(LAN_BLOCK) == 1


def test_voice_segment_mention_counts():
    scene = {"prompt": "Hai người đối thoại trong rừng",
             "voice_segments": [{"character": "Phong", "text": "Đi thôi."}]}
    out = apply_film_characters(scene["prompt"], scene, CHARS)
    assert PHONG_BLOCK in out and LAN_BLOCK not in out


def test_empty_snapshot_or_no_block_leaves_prompt_untouched():
    assert apply_film_characters("Lan cười", {"prompt": "Lan cười"}, []) == "Lan cười"
    assert apply_film_characters(
        "Lan cười", {"prompt": "Lan cười"},
        [{"name": "Lan"}]) == "Lan cười"


# ── 2. process_scene thật (mock network/TTS/compose) ───────────────────────

class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    async def post(self, url, json=None, headers=None, timeout=None):
        return _FakeResp(200, {"id": "task-fake-1"})


@pytest.fixture()
def _mock_scene_pipeline(monkeypatch, tmp_path):
    """Mock mọi bước chạm mạng/ffmpeg/TTS của process_scene."""
    async def _fake_poll(client, task_id, base_url, headers, idx,
                         progress_cb=None, cancel_event=None):
        return "http://example.invalid/video.mp4"

    async def _fake_download(client, url, dest, expected=None,
                             max_bytes=None, timeout=None):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"FAKEVIDEO" + b"\x00" * 1024)

    async def _fake_tts(text, voice, output_path):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"FAKEMP3" + b"\x00" * 512)
        return (5.0, False, "")

    async def _fake_duration(path):
        return 5.0

    def _fake_compose(scene_video, scene_audio, output, audio_duration,
                      target_duration=None, aspect_ratio="16:9", **kw):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        data = Path(scene_video).read_bytes()
        Path(output).write_bytes(data)

    def _fake_last_frame(video_path, output_image_path):
        Path(output_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_image_path).write_bytes(b"FAKEJPEG" + b"\x00" * 512)
        return output_image_path

    monkeypatch.setattr(batch_engine, "poll_video_task", _fake_poll)
    monkeypatch.setattr(batch_engine, "_download_validated", _fake_download)
    monkeypatch.setattr(batch_engine, "generate_tts_with_status", _fake_tts)
    monkeypatch.setattr(batch_engine, "get_media_duration_async", _fake_duration)
    monkeypatch.setattr(batch_engine, "extract_last_frame", _fake_last_frame)
    import composer
    monkeypatch.setattr(composer, "compose_scene_video", _fake_compose)
    return tmp_path


def test_process_scene_used_prompt_has_only_mentioned_block(_mock_scene_pipeline):
    tmp_path = _mock_scene_pipeline
    scene = {"index": 1, "prompt": "Lan đứng trên đồi buổi sáng",
             "voiceover": "", "duration": 5, "size": "16:9"}
    res = asyncio.run(process_scene(
        _FakeClient(), scene, asyncio.Semaphore(1),
        base_url="http://example.invalid", api_key="fake-key",
        output_dir=str(tmp_path), voice="vi-VN-HoaiMyNeural",
        characters=CHARS))
    assert res["status"] == "done", res
    used = res["used_prompt"]
    assert LAN_BLOCK in used
    assert PHONG_BLOCK not in used


def test_process_scene_without_characters_unchanged(_mock_scene_pipeline):
    tmp_path = _mock_scene_pipeline
    scene = {"index": 1, "prompt": "Lan đứng trên đồi buổi sáng",
             "voiceover": "", "duration": 5, "size": "16:9"}
    res = asyncio.run(process_scene(
        _FakeClient(), scene, asyncio.Semaphore(1),
        base_url="http://example.invalid", api_key="fake-key",
        output_dir=str(tmp_path), voice="vi-VN-HoaiMyNeural",
        characters=None))
    assert res["status"] == "done", res
    assert LAN_BLOCK not in res["used_prompt"]


# ── 3. run_batch chuyển tiếp snapshot ──────────────────────────────────────

class _FakeHealthResp:
    status_code = 200


class _FakeHealthClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, timeout=None):
        return _FakeHealthResp()


def test_run_batch_forwards_characters_to_process_scene(monkeypatch, tmp_path):
    seen = {}

    async def _fake_process_scene(client, scene, sem, base_url, api_key,
                                  output_dir, voice, progress_cb=None,
                                  **kw):
        seen["characters"] = kw.get("characters")
        return {"index": scene["index"], "status": "done",
                "video_path": None, "audio_path": None,
                "voiceover": "", "audio_duration": 5.0}

    monkeypatch.setattr(batch_engine, "process_scene", _fake_process_scene)
    monkeypatch.setattr(batch_engine.httpx, "AsyncClient", _FakeHealthClient)
    results = asyncio.run(run_batch(
        scenes=[{"index": 1, "prompt": "Phong rút kiếm", "voiceover": "",
                 "duration": 5, "size": "16:9"}],
        base_url="http://example.invalid", api_key="fake-key",
        output_dir=str(tmp_path), voice="vi-VN-HoaiMyNeural",
        max_concurrent=1, chain_mode="none", characters=CHARS))
    assert results[0]["status"] == "done"
    assert seen["characters"] == CHARS
