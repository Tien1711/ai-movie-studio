import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

import comic_pipeline as comic
import studio_workflow as wf


def panel(box, lines=()):
    return {
        "box": box,
        "action": "Linh holds up the lantern.",
        "narration": "",
        "setting": "Harbor",
        "characters": ["Linh"],
        "lines": list(lines),
    }


def page(panels, colors, setting_colors):
    return {
        "panels": panels,
        "characters": [{"name": "Linh", "appearance": "Short dark hair", "colors": colors, "voice": "female"}],
        "settings": [{"name": "Harbor", "colors": setting_colors}],
    }


def test_comic_reading_order_dialogue_and_palette_precedence():
    source = Image.new("RGB", (200, 300), "gray")
    dialogue = [
        {"speaker": "linh", "text": "Khoan đã!"},
        {"speaker": None, "text": "Có ai ở đó không?"},
        {"speaker": "Linh", "text": "Là tôi đây."},
    ]
    data = page([panel([.5, 0, 1, 1], dialogue), panel([0, 0, .5, 1])], "yellow coat", "gray stone")
    shots, cast, settings = comic.parse_page(data, source, 1, [], {}, "NarratorVoice")
    assert [shot["comic_panel_box"][0] for _, shot in shots] == [.5, 0]
    assert not shots[0][1]["comic_source_color"]
    assert shots[0][1]["dialogue"] == "Linh: Khoan đã!\nLinh: Là tôi đây."
    voices = wf.comic_speech_segments({"cast": cast, "voice": "NarratorVoice"}, shots[0][1])
    assert [line["text"] for line in voices] == [line["text"] for line in dialogue]
    assert [line["voice"] for line in voices] == ["vi-VN-HoaiMyNeural", "NarratorVoice", "vi-VN-HoaiMyNeural"]

    colored = Image.new("RGB", (200, 300), "navy")
    corrected = page([panel([0, 0, 1, 1])], "blue coat", "red stone")
    corrected["characters"][0]["colors"] = {"coat": "blue coat"}
    corrected["settings"][0]["colors"] = ["red stone"]
    shots, cast, settings = comic.parse_page(corrected, colored, 2, cast, settings, "NarratorVoice")
    assert shots[0][1]["comic_source_color"]
    assert "blue coat" in cast[0]["description"]
    assert "red stone" in settings["harbor"]["colors"]
    _, cast, settings = comic.parse_page(data, source, 3, cast, settings, "NarratorVoice")
    assert "blue coat" in cast[0]["description"]
    assert "red stone" in settings["harbor"]["colors"]


def test_chat_preserves_unescaped_dialogue_quotes_without_accepting_partial_pages():
    expected = [{"lines": [{"text": 'Chữ "HAKI" trong lời thoại.', "speaker": None}]}, {"lines": []}]
    reply = json.dumps(expected, ensure_ascii=False).replace('\\"HAKI\\"', '"HAKI"')
    assert comic.parse_vision_rows(reply) == expected
    with pytest.raises(ValueError, match="chưa hoàn chỉnh"):
        comic.parse_vision_rows(reply.rsplit(",", 1)[0])


def test_comic_worker_resumes_page_checkpoints_then_renders_every_panel(tmp_path, monkeypatch):
    vid = "bv_12345678"
    monkeypatch.setattr(wf.batch, "BATCH_DIR", tmp_path)
    (tmp_path / vid).mkdir()
    video = {
        "id": vid, "title": "A chapter", "voice": "NarratorVoice", "comic_pages": ["first", "second"],
        "comic_pages_done": 0, "scenes": [{"index": 1}, {"index": 2}],
    }
    updates = []

    def update(_, data):
        updates.append(data)
        return True

    monkeypatch.setattr(wf.batch, "BATCH_MANAGER", SimpleNamespace(update_video=update))
    receipts = []

    def download(url):
        receipts.append(url)
        return comic.image_uri([Image.new("RGB", (200, 200), "gray")])

    monkeypatch.setattr(wf.media, "_browser_image_from_url", download)
    first = page([panel([.5, 0, 1, 1]), panel([0, 0, .5, 1])], "red coat", "gray stone")
    second = page([panel([0, 0, 1, 1])], "red coat", "gray stone")
    profiles = [
        *(dict(profile, kind="character") for profile in first["characters"]),
        *(dict(profile, kind="setting") for profile in first["settings"]),
    ]
    provider = AsyncMock(side_effect=[first["panels"], profiles, ValueError("Vision timed out"), second["panels"]])
    monkeypatch.setattr(wf, "ask_json", provider)
    progress = lambda *_: None
    with pytest.raises(ValueError, match="timed out"):
        asyncio.run(wf.analyze_comic_pages(video, progress))
    assert video["comic_pages_done"] == 1
    assert len(video["scenes"]) == 2

    async def color(_, shot, *args):
        assert wf.asset_path(vid, shot["comic_panel"]).is_file()
        shot.update(frame_approved=True, frame=shot["comic_panel"])

    async def render(_, shot, action, *args):
        assert action == "clip"
        shot.update(clip="new.mp4", qc={"passed": True})

    monkeypatch.setattr(wf, "color_comic_panel", color)
    monkeypatch.setattr(wf, "produce", render)
    export = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(wf, "export", export)
    assert asyncio.run(wf.render_comic(video, {}, tmp_path, progress)) == {"ok": True}
    assert receipts == ["first", "second"]
    assert [(s["comic_page"], s["comic_panel_number"]) for s in video["scenes"]] == [(1, 1), (1, 2), (2, 1)]
    assert all(s["approved"] for s in video["scenes"])
    assert export.await_args.args[1]["action"] == "narrated"
    video["clip_audio_mode"] = "native"
    assert asyncio.run(wf.render_comic(video, {}, tmp_path, progress)) == {"ok": True}
    assert export.await_args.args[1]["action"] == "compose"


def test_comic_keyframe_keeps_original_panel_and_rejects_color_drift(tmp_path, monkeypatch):
    vid = "bv_12345678"
    monkeypatch.setattr(wf.batch, "BATCH_DIR", tmp_path)
    (tmp_path / vid).mkdir()
    source, previous = [wf.new_asset(vid, "webp") for _ in range(2)]
    Image.new("RGB", (80, 120), "gray").save(source)
    Image.new("RGB", (80, 120), "red").save(previous)
    current = {
        "index": 2, "comic_panel": source.name, "comic_source_color": False,
        "prompt": "Linh holds a lantern", "setting": "Harbor", "characters": ["Linh"],
    }
    video = {
        "id": vid, "aspect_ratio": "9:16",
        "cast": [{"name": "Linh", "description": "Red coat with gold trim"}],
        "comic_settings": {"harbor": {"name": "Harbor", "colors": "Blue brick walls"}},
        "scenes": [
            {"index": 1, "comic_panel": source.name, "frame": previous.name, "frame_approved": True, "characters": ["Linh"]},
            current,
        ],
    }
    monkeypatch.setattr(wf.batch, "muse_connection", lambda _: ("browser", "http://muse.test", "test-key"))
    updates = {}
    monkeypatch.setattr(wf.batch, "BATCH_MANAGER", SimpleNamespace(update_video=lambda _, data: updates.update(data)))

    async def generate(prompt, url, key, output, aspect, reference):
        assert "Red coat with gold trim" in prompt and "Blue brick walls" in prompt
        assert "identity/color guides only" in prompt
        assert reference.startswith("data:image/jpeg;base64,")
        Image.new("RGB", (80, 120), "blue").save(output, format="PNG")

    monkeypatch.setattr(wf.media, "generate_keyframe_image", generate)
    monkeypatch.setattr(wf, "ask_json", AsyncMock(return_value={"score": 55, "issues": ["Áo đổi màu"]}))
    with pytest.raises(ValueError, match="Áo đổi màu"):
        asyncio.run(wf.color_comic_panel(video, current, tmp_path, lambda *_: None))
    assert current["frame_approved"] is False
    assert current["comic_panel"] == source.name
    assert wf.asset_path(vid, updates["scenes"][1]["frame"]).is_file()
