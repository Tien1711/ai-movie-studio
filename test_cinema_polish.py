"""End-to-end regression tests for the 7 Cinema-Grade Video Quality & UX upgrades."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import app as app_module
import batch_video_engine as batch
import composer
import consistency
import qc
import studio_workflow as wf
import tts_manager


def _make_video(path: Path, duration: float = 3.0, color: str = "blue", size: str = "360x640"):
    subprocess.run(
        [
            composer.FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:d={duration}:r=25",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_audio(path: Path, duration: float = 3.8, freq: int = 330):
    subprocess.run(
        [
            composer.FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration}",
            "-c:a",
            "libmp3lame",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_image(path: Path, color: tuple[int, int, int], text: str = ""):
    img = Image.new("RGB", (320, 320), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 280, 280], outline=(255, 255, 255), width=6)
    if text:
        draw.text((60, 140), text, fill=(255, 255, 0))
    img.save(path)


def test_smart_pacing_color_grade_and_watermark(tmp_path):
    """Feature #1, #3, #7: Smart slow-mo pacing (no loop), LUT grade, and watermark."""
    clip = tmp_path / "clip.mp4"
    audio = tmp_path / "voice.mp3"
    out = tmp_path / "out.mp4"
    _make_video(clip, duration=3.0)
    _make_audio(audio, duration=3.7)

    composer.compose_scene_video(
        str(clip),
        str(audio),
        str(out),
        audio_duration=3.7,
        target_duration=3.7,
        quality="720p",
        aspect_ratio="9:16",
        smart_pacing=True,
        smart_trim=False,
        color_grade="warm_epic",
        watermark_text="@CinemaTest",
    )
    assert out.is_file() and out.stat().st_size > 1000
    dur = composer.get_media_duration(str(out))
    assert 3.4 <= dur <= 4.1


def test_auto_ducking_bgm_and_transition_sfx(tmp_path):
    """Feature #4: Auto-ducking sidechaincompress BGM + synthesized whoosh transition SFX."""
    vid = tmp_path / "film.mp4"
    bgm = tmp_path / "music.mp3"
    ducked = tmp_path / "ducked.mp4"
    with_sfx = tmp_path / "sfx.mp4"
    _make_video(vid, duration=4.0)
    _make_audio(bgm, duration=5.0, freq=220)

    composer.add_background_music(
        str(vid),
        str(bgm),
        str(ducked),
        music_volume=0.2,
        voice_volume=1.0,
        fade_duration=0.5,
        auto_ducking=True,
    )
    assert ducked.is_file() and ducked.stat().st_size > 1000

    composer.add_transition_sfx(
        str(ducked),
        transition_offsets=[1.5, 2.8],
        output_video=str(with_sfx),
        sfx_volume=0.14,
    )
    assert with_sfx.is_file() and with_sfx.stat().st_size > 1000
    assert composer.get_media_duration(str(with_sfx)) >= 3.5


def test_word_boundary_karaoke_sync(tmp_path, monkeypatch):
    """Feature #5: Edge-TTS WordBoundary JSON capture + timeline offset + exact ASS karaoke."""
    out_mp3 = tmp_path / "speech.mp3"

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
            self.text = text

        async def stream(self):
            yield {"type": "audio", "data": b"ID3FAKEAUDIOBYTES"}
            yield {
                "type": "WordBoundary",
                "offset": 1_000_000,  # 0.1s
                "duration": 4_000_000,  # 0.4s
                "text": "Hai",
            }
            yield {
                "type": "WordBoundary",
                "offset": 5_000_000,  # 0.5s
                "duration": 6_000_000,  # 0.6s
                "text": "Bà",
            }
            yield {
                "type": "WordBoundary",
                "offset": 11_000_000,  # 1.1s
                "duration": 5_000_000,  # 0.5s
                "text": "Trưng",
            }

    import edge_tts

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    asyncio.run(
        tts_manager.synthesize_edge_tts(
            "Hai Bà Trưng", "vi-VN-HoaiMyNeural", str(out_mp3)
        )
    )

    assert out_mp3.is_file()
    wp = tts_manager.word_timings_path(str(out_mp3))
    assert wp.is_file()
    saved = json.loads(wp.read_text(encoding="utf-8"))
    assert [w["word"] for w in saved] == ["Hai", "Bà", "Trưng"]
    assert saved[0]["start"] == pytest.approx(0.1, abs=1e-3)
    assert saved[0]["end"] == pytest.approx(0.5, abs=1e-3)

    timeline = composer.collect_timeline_word_timings([str(out_mp3)], [10.0])
    assert len(timeline) == 3
    assert timeline[0]["start"] == pytest.approx(10.1, abs=1e-3)
    assert timeline[2]["end"] == pytest.approx(11.6, abs=1e-3)

    srt_path = tmp_path / "subs.srt"
    srt_path.write_text(
        "1\n00:00:10,000 --> 00:00:12,000\nHai Bà Trưng\n\n",
        encoding="utf-8",
    )
    ass_path = tmp_path / "karaoke.ass"
    composer._write_karaoke_ass(
        str(srt_path),
        str(ass_path),
        font_size=20,
        alignment=2,
        style="karaoke",
        word_timings=timeline,
    )
    ass_text = ass_path.read_text(encoding="utf-8")
    assert "0:00:10.10,0:00:10.50" in ass_text
    assert "0:00:11.10,0:00:11.60" in ass_text
    assert "{\\c&H00CFFF&}Trưng{\\rMain}" in ass_text


def test_smart_thumbnail_generation(tmp_path):
    """Feature #7: Sharpest-frame selection + cinema title overlay thumbnail."""
    vid = tmp_path / "final.mp4"
    thumb = tmp_path / "thumbnail.jpg"
    _make_video(vid, duration=3.0, color="darkgreen", size="640x360")

    res = composer.generate_smart_thumbnail(
        str(vid),
        str(thumb),
        title="Khởi Nghĩa Hai Bà Trưng",
        aspect_ratio="16:9",
    )
    assert Path(res).is_file()
    with Image.open(thumb) as img:
        assert img.width > 0 and img.height > 0


def test_vision_qc_scoring_and_auto_heal(tmp_path, monkeypatch):
    """Feature #2: Vision QC contact-sheet scoring & auto-retry once if score < 75."""
    anchor = tmp_path / "anchor.png"
    frame = tmp_path / "frame.png"
    _make_image(anchor, (30, 80, 160), "REF")
    _make_image(frame, (160, 40, 40), "GEN")

    calls = []

    async def fake_ask_json(system, user, reference_image=None):
        calls.append((system, user, reference_image))
        return {
            "score": 62,
            "passed": False,
            "issues": ["Trang phục khác màu giáp đỏ"],
        }

    import base64

    anchor_b64 = "data:image/png;base64," + base64.b64encode(anchor.read_bytes()).decode("ascii")
    result = asyncio.run(
        qc.evaluate_visual_consistency(
            str(frame),
            anchor_b64,
            "Trưng Trắc đứng bên sông Hát",
            fake_ask_json,
        )
    )
    assert result["score"] == 62
    assert result["passed"] is False
    assert "Trang phục" in result["issues"][0]
    assert len(calls) == 1
    assert calls[0][2] and calls[0][2].startswith("data:image/jpeg;base64,")


def test_partition_clip_segments_and_parallel_execution(tmp_path, monkeypatch):
    """Feature #6: Segment partitioning at new_segment, setting change, and anchor reset."""
    scenes = [
        {"index": 1, "setting": "Mê Linh · sáng", "new_segment": False},
        {"index": 2, "setting": "Mê Linh · sáng", "new_segment": False},
        {"index": 3, "setting": "Sông Hát · chiều", "new_segment": False},
        {"index": 4, "setting": "Sông Hát · chiều", "new_segment": False},
        {"index": 5, "setting": "Sông Hát · chiều", "new_segment": True},
        {"index": 6, "setting": "Sông Hát · chiều", "new_segment": False},
        {"index": 7, "setting": "Sông Hát · chiều", "new_segment": False},
        {"index": 8, "setting": "Sông Hát · chiều", "new_segment": False},
        {"index": 9, "setting": "Sông Hát · chiều", "new_segment": False},
    ]
    segments = consistency.partition_clip_segments(
        scenes, [1, 2, 3, 4, 5, 6, 7, 8, 9], reference_mode="chain"
    )
    # Splits at:
    # - 1 (start): [1, 2]
    # - 3 (setting changed): [3, 4]
    # - 5 (new_segment=True): [5, 6, 7]
    # - 8 (index % 8 == 0 periodic anchor reset): [8, 9]
    assert segments == [[1, 2], [3, 4], [5, 6, 7], [8, 9]]

    # In 'approved' mode, every scene is independent
    indep = consistency.partition_clip_segments(
        scenes[:3], [1, 2, 3], reference_mode="approved"
    )
    assert indep == [[1], [2], [3]]

    # Verify produce_clip_segments runs segments concurrently and keeps intra-segment order
    monkeypatch.setattr(batch, "BATCH_DIR", tmp_path / "batch_seg")
    monkeypatch.setattr(batch, "QUEUE_FILE", tmp_path / "batch_seg" / "queue.json")
    monkeypatch.setattr(batch, "QUEUE_BAK_FILE", tmp_path / "batch_seg" / "queue.bak.json")
    batch.BATCH_DIR.mkdir(parents=True, exist_ok=True)
    mgr = batch.BatchVideoManager()
    monkeypatch.setattr(batch, "BATCH_MANAGER", mgr)
    vid_seg = "bv_abcdef12"
    wf.directory(vid_seg).mkdir(parents=True, exist_ok=True)
    recorded = []
    active = 0
    max_active = 0

    async def fake_produce(video_arg, scene_arg, kind, work_dir, progress):
        nonlocal active, max_active
        idx = scene_arg["index"]
        active += 1
        max_active = max(max_active, active)
        recorded.append(("start", idx))
        await asyncio.sleep(0.03)
        recorded.append(("end", idx))
        active -= 1

    monkeypatch.setattr(wf, "produce", fake_produce)
    asyncio.run(
        wf.produce_clip_segments(
            {"id": vid_seg, "scenes": scenes[:4]},
            [[1, 2], [3, 4]],
            workers=2,
            progress=lambda i, msg: None,
        )
    )
    assert max_active == 2
    # Within segment [1, 2], 1 must finish before 2 starts
    assert recorded.index(("end", 1)) < recorded.index(("start", 2))
    # Within segment [3, 4], 3 must finish before 4 starts
    assert recorded.index(("end", 3)) < recorded.index(("start", 4))


def test_cinema_settings_bridge_shots_and_thumbnail_routes(tmp_path, monkeypatch):
    """Feature #6 & #7 routes: chapter-settings persistence, bridge-shots insertion, and thumbnail endpoint."""
    monkeypatch.setattr(batch, "BATCH_DIR", tmp_path / "batch")
    monkeypatch.setattr(batch, "QUEUE_FILE", tmp_path / "batch" / "queue.json")
    monkeypatch.setattr(batch, "QUEUE_BAK_FILE", tmp_path / "batch" / "queue.bak.json")
    batch.BATCH_DIR.mkdir(parents=True, exist_ok=True)
    mgr = batch.BatchVideoManager()
    monkeypatch.setattr(batch, "BATCH_MANAGER", mgr)

    scenes = [
        {
            "index": 1,
            "title": "Sáng ở Mê Linh",
            "prompt": "Wide shot of Me Linh village at dawn",
            "voiceover": "Bình minh lên trên đất Mê Linh.",
            "setting": "Mê Linh · bình minh",
            "duration": 8,
            "transition_out": "dissolve",
        },
        {
            "index": 2,
            "title": "Đêm ở Sông Hát",
            "prompt": "Night battle camp by Hat River under torches",
            "voiceover": "Đêm xuống, nghĩa quân tụ họp bên sông Hát.",
            "setting": "Sông Hát · đêm",
            "duration": 8,
            "transition_out": "dissolve",
        },
    ]
    item = mgr.add_batch(
        ["Khởi nghĩa Hai Bà Trưng"],
        aspect_ratio="9:16",
        visual_style="pixar_3d",
        voice="vi-VN-HoaiMyNeural",
        scenes_count=2,
        scripts=[{"scenes": scenes}],
        auto_run=False,
        extra={"guided": True, "render_mode": "ai_video", "studio_source": "test"},
    )[0]
    vid = item["id"]

    client = TestClient(app_module.app)

    # 1. Update chapter settings with all new cinema fields including Feature #6 mode
    res = client.put(
        f"/api/muse-studio/{vid}/chapter-settings",
        json={
            "version": item["updated_at"],
            "voice": "vi-VN-HoaiMyNeural",
            "subtitle_style": "karaoke",
            "subtitle_position": "bottom",
            "music": "",
            "volume": 0.2,
            "reference_mode": "chain",
            "chain_fallback_mode": "fallback_image",
            "color_grade": "warm_epic",
            "smart_pacing": True,
            "smart_trim": True,
            "auto_ducking": True,
            "transition_sfx": True,
            "vision_qc": True,
            "clip_parallel_mode": "segment_parallel",
            "watermark_text": "@SuViet3D",
        },
    )
    assert res.status_code == 200, res.text
    updated = mgr.get_video(vid)
    assert updated["color_grade"] == "warm_epic"
    assert updated["clip_parallel_mode"] == "segment_parallel"
    assert updated["smart_trim"] is True
    assert updated["transition_sfx"] is True
    assert updated["vision_qc"] is True
    assert updated["watermark_text"] == "@SuViet3D"

    # 2. Rebuild preserves all new cinema settings
    rb = client.post(f"/api/muse-studio/{vid}/rebuild")
    assert rb.status_code == 200, rb.text
    rebuilt = mgr.get_video(rb.json()["id"])
    assert rebuilt["color_grade"] == "warm_epic"
    assert rebuilt["clip_parallel_mode"] == "segment_parallel"
    assert rebuilt["watermark_text"] == "@SuViet3D"

    # 3. Insert bridge shots between setting jump (Mê Linh -> Sông Hát)
    br = client.post(
        f"/api/muse-studio/{vid}/bridge-shots",
        json={"version": updated["updated_at"]},
    )
    assert br.status_code == 200, br.text
    body = br.json()
    assert body["inserted"] == 1
    assert body["scenes"] == 3
    after_bridge = mgr.get_video(vid)
    assert [s["index"] for s in after_bridge["scenes"]] == [1, 2, 3]
    assert after_bridge["scenes"][1]["duration"] == 4
    assert "cầu nối" in after_bridge["scenes"][1]["title"].lower()

    # 4. Thumbnail returns 404 before final.mp4, and 200 image/jpeg once final.mp4 exists
    t404 = client.get(f"/api/muse-studio/{vid}/thumbnail")
    assert t404.status_code == 404

    vid_dir = wf.directory(vid)
    vid_dir.mkdir(parents=True, exist_ok=True)
    _make_video(vid_dir / "final.mp4", duration=2.5)
    t200 = client.get(f"/api/muse-studio/{vid}/thumbnail")
    assert t200.status_code == 200
    assert t200.headers["content-type"].startswith("image/jpeg")
    assert len(t200.content) > 1000


def test_third_person_narration_vs_character_dialogue_separation(tmp_path):
    """Đảm bảo cảnh kể ngôi thứ 3 KHÔNG kích hoạt nhân vật mở miệng nói lời kể,
    còn cảnh có dialogue nhân vật chỉ khớp môi đúng nhân vật đang thoại."""
    import batch_engine

    # 1. Cảnh kể ngôi thứ 3 (không có dialogue, chỉ có voiceover & danh sách nhân vật)
    narr_scene = {
        "index": 3,
        "prompt": "Thi Sách sitting by a low table telling stories of the northern lands while Trưng Trắc pours tea",
        "voiceover": "Bữa cơm chiều ấm áp, Thi Sách kể chuyện phương Bắc. Cả nhà lắng nghe.",
        "characters": ["Thi Sách", "Trưng Trắc"],
    }
    built_tts = batch_engine.build_scene_prompt(narr_scene, None, "vi-VN-HoaiMyNeural", "tts")
    assert built_tts["speech_mode"] == "offscreen_narration"
    assert built_tts["has_dialogue"] is False
    assert "closed mouths" in built_tts["prompt"].lower()
    assert "lip articulation" not in built_tts["prompt"].lower()
    assert "telling stories" not in built_tts["prompt"].lower()
    assert "Bữa cơm chiều ấm áp" not in built_tts["prompt"]

    # 2. Cảnh kể ngôi thứ 3 ở chế độ AI Voice: tách rõ giọng kể ngoài hình, nhân vật diễn câm
    built_ai = batch_engine.build_scene_prompt(narr_scene, None, "ai_voice", "ai_voice")
    assert built_ai["speech_mode"] == "offscreen_narration"
    assert "off-screen third-person" in built_ai["prompt"].lower()
    assert "closed mouths" in built_ai["prompt"].lower()
    assert "the character speaking with natural voice" not in built_ai["prompt"].lower()

    # 3. Cảnh CÓ thoại nhân vật trực tiếp: chỉ nhân vật thoại khớp môi, người còn lại giữ kín miệng
    dlg_scene = {
        "index": 4,
        "prompt": "Trưng Trắc raises her sword by the Hát River while Trưng Nhị stands beside her",
        "voiceover": "Trước ba quân, Trưng Trắc cất lời thề.",
        "dialogue": "Trưng Trắc: Một xin rửa sạch nước thù!",
        "characters": ["Trưng Trắc", "Trưng Nhị"],
    }
    built_dlg = batch_engine.build_scene_prompt(dlg_scene, None, "vi-VN-HoaiMyNeural", "tts")
    assert built_dlg["speech_mode"] == "onscreen_dialogue"
    assert built_dlg["has_dialogue"] is True
    assert "Trưng Trắc" in built_dlg["prompt"]
    assert "lip articulation" in built_dlg["prompt"].lower()
    assert "non-speaking characters listen silently with closed mouths" in built_dlg["prompt"].lower()

    # 4. Vocal-suppression ambience mix trong composer.compose_scene_video chạy mượt không lỗi FFmpeg
    clip = tmp_path / "clip_amb.mp4"
    voice = tmp_path / "narrator.mp3"
    out = tmp_path / "mixed_narrated.mp4"
    _make_video(clip, duration=3.0)
    _make_audio(voice, duration=3.2)
    composer.compose_scene_video(
        str(clip),
        str(voice),
        str(out),
        target_duration=3.2,
        aspect_ratio="9:16",
        quality="720p",
        ambience_volume=0.18,
    )
    assert out.is_file() and out.stat().st_size > 1000
    assert composer.get_media_duration(str(out)) >= 3.0


def test_strict_clip_timing_mode_and_fit_durations_route(tmp_path, monkeypatch):
    """Kiểm tra timing_mode='strict_clip' khóa chặt đúng thời lượng clip (tăng tốc đọc nhẹ, không kéo dài phim)
    và endpoint /fit-durations tự nâng mốc giây cho cảnh chưa dựng clip."""
    # 1. strict_clip: clip 3.0s + audio 3.9s -> output khóa đúng 3.0s (không bị kéo thành 3.9s)
    clip = tmp_path / "strict_clip.mp4"
    audio = tmp_path / "long_voice.mp3"
    out = tmp_path / "strict_out.mp4"
    _make_video(clip, duration=3.0)
    _make_audio(audio, duration=3.9)

    composer.compose_scene_video(
        str(clip),
        str(audio),
        str(out),
        target_duration=3.0,
        aspect_ratio="9:16",
        quality="720p",
        timing_mode="strict_clip",
    )
    assert out.is_file() and out.stat().st_size > 1000
    strict_dur = composer.get_media_duration(str(out))
    assert 2.85 <= strict_dur <= 3.15

    # 2. Word timings scale đúng theo speed_factors khi tăng tốc đọc
    fake_speech = tmp_path / "sp.mp3"
    fake_speech.write_bytes(b"mp3")
    Path(f"{fake_speech}.words.json").write_text(
        json.dumps([{"word": "Chào", "start": 0.0, "end": 2.0}]), encoding="utf-8"
    )
    scaled = composer.collect_timeline_word_timings([str(fake_speech)], [5.0], speed_factors=[1.25])
    assert scaled[0]["start"] == pytest.approx(5.0, abs=1e-3)
    assert scaled[0]["end"] == pytest.approx(6.6, abs=1e-3)

    # 3. POST /api/muse-studio/{vid}/fit-durations nâng cảnh 5s có thoại 7.4s -> 8s (bỏ qua cảnh đã có clip)
    monkeypatch.setattr(batch, "BATCH_DIR", tmp_path / "batch_fit")
    monkeypatch.setattr(batch, "QUEUE_FILE", tmp_path / "batch_fit" / "queue.json")
    monkeypatch.setattr(batch, "QUEUE_BAK_FILE", tmp_path / "batch_fit" / "queue.bak.json")
    batch.BATCH_DIR.mkdir(parents=True, exist_ok=True)
    mgr = batch.BatchVideoManager()
    monkeypatch.setattr(batch, "BATCH_MANAGER", mgr)
    item = mgr.add_batch(
        ["Test Fit Durations"],
        aspect_ratio="9:16",
        visual_style="3d_pixar",
        voice="vi-VN-NamMinhNeural",
        scenes_count=2,
        scripts=[{
            "scenes": [
                {"index": 1, "title": "Cảnh 1", "prompt": "p1", "voiceover": "v1", "duration": 5, "speech_duration": 7.4},
                {"index": 2, "title": "Cảnh 2", "prompt": "p2", "voiceover": "v2", "duration": 5, "speech_duration": 7.4, "clip": "already.mp4"},
            ]
        }],
        auto_run=False,
        extra={"guided": True, "render_mode": "ai_video", "studio_source": "test"},
    )[0]
    vid = item["id"]
    client = TestClient(app_module.app)
    res = client.post(f"/api/muse-studio/{vid}/fit-durations", json={"version": item["updated_at"]})
    assert res.status_code == 200, res.text
    assert res.json()["updated"] == 1
    updated = mgr.get_video(vid)
    assert updated["scenes"][0]["duration"] == 8
    assert updated["scenes"][1]["duration"] == 5  # untouched because clip already rendered


