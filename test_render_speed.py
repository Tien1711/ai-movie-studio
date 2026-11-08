"""Tests cho đợt tối ưu tốc độ render 2026-11-08.

Phạm vi: (1) đo timing từng giai đoạn, (2) xfade single-pass thay pairwise
O(n²), (3) cache TTS theo hash (text + voice + speed).

Chạy từ thư mục gốc repo:
    python -m pytest test_render_speed.py -v
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ffmpeg_available = shutil.which("ffmpeg") is not None or Path(FFMPEG).exists()


def _make_clip(path: str, duration: float = 2.0, color: str = "red", freq: int = 440):
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=320x240:d={duration}:r=24",
        "-f", "lavfi", "-i", f"sine=f={freq}:d={duration}:r=44100",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_audio(path: str, duration: float = 0.5, freq: int = 440):
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", f"sine=f={freq}:d={duration}:r=44100",
        "-c:a", "libmp3lame", "-b:a", "128k",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ── (1) Cache TTS ───────────────────────────────────────────────────────────

def _fake_synthesizer(fixture_mp3: str, calls: list):
    async def _fake(text, voice, output_path, **kw):
        calls.append((text, voice))
        shutil.copy2(fixture_mp3, output_path)
        return 0.5
    return _fake


def test_tts_cache_second_call_does_not_hit_provider(monkeypatch, tmp_path):
    """Gọi 2 lần cùng text+voice: provider chỉ gọi 1 lần, file trả về giống hệt."""
    import batch_engine
    import tts_manager

    fixture = str(tmp_path / "fixture.mp3")
    _make_audio(fixture, duration=0.5)
    calls: list = []
    monkeypatch.setattr(tts_manager, "synthesize_voice", _fake_synthesizer(fixture, calls))
    monkeypatch.setattr(batch_engine, "_TTS_CACHE_DIR", tmp_path / "cache")

    out1 = str(tmp_path / "out1.mp3")
    out2 = str(tmp_path / "out2.mp3")
    d1 = asyncio.run(batch_engine.generate_tts("Xin chào các bạn", "vi-VN-HoaiMyNeural", out1))
    d2 = asyncio.run(batch_engine.generate_tts("Xin chào các bạn", "vi-VN-HoaiMyNeural", out2))

    assert calls == [("Xin chào các bạn", "vi-VN-HoaiMyNeural")], f"provider phải gọi đúng 1 lần, thực tế {len(calls)} lần"
    assert d1 == pytest.approx(d2, abs=0.05)
    assert Path(out1).read_bytes() == Path(out2).read_bytes(), "file cache phải là byte y hệt lần synthesis đầu"
    assert len(list((tmp_path / "cache").glob("*.mp3"))) == 1


def test_tts_cache_miss_on_different_text_or_voice(monkeypatch, tmp_path):
    """Text khác hoặc voice khác -> hash khác -> gọi lại provider."""
    import batch_engine
    import tts_manager

    fixture = str(tmp_path / "fixture.mp3")
    _make_audio(fixture, duration=0.5)
    calls: list = []
    monkeypatch.setattr(tts_manager, "synthesize_voice", _fake_synthesizer(fixture, calls))
    monkeypatch.setattr(batch_engine, "_TTS_CACHE_DIR", tmp_path / "cache")

    asyncio.run(batch_engine.generate_tts("Câu một", "voice-A", str(tmp_path / "a.mp3")))
    asyncio.run(batch_engine.generate_tts("Câu hai", "voice-A", str(tmp_path / "b.mp3")))
    asyncio.run(batch_engine.generate_tts("Câu một", "voice-B", str(tmp_path / "c.mp3")))
    asyncio.run(batch_engine.generate_tts("Câu một", "voice-A", str(tmp_path / "d.mp3")))

    assert len(calls) == 3, f"phải gọi provider 3 lần (2 miss + 1 hit cuối), thực tế {len(calls)}"
    assert len(list((tmp_path / "cache").glob("*.mp3"))) == 3


def test_tts_cache_key_normalizes_whitespace():
    """Text chỉ khác khoảng trắng thừa -> cùng hash (không synthesis lại)."""
    import batch_engine

    assert batch_engine._tts_cache_key("  Xin   chào  ", "v1") == batch_engine._tts_cache_key("Xin chào", "v1")
    assert batch_engine._tts_cache_key("Xin chào", "v1") != batch_engine._tts_cache_key("Xin chào", "v2")
    assert batch_engine._tts_cache_key("Xin chào", "v1") != batch_engine._tts_cache_key("Xin chào!", "v1")


def test_tts_cache_prune_lru(monkeypatch, tmp_path):
    """Vượt giới hạn file -> xóa file cũ nhất (LRU theo mtime)."""
    import batch_engine

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(batch_engine, "_TTS_CACHE_DIR", cache_dir)
    monkeypatch.setattr(batch_engine, "_TTS_CACHE_MAX_FILES", 3)
    base = time.time() - 100
    for i in range(5):
        p = cache_dir / f"f{i}.mp3"
        p.write_bytes(b"x" * 10)
        os.utime(p, (base + i, base + i))
    batch_engine._tts_cache_prune()
    remaining = sorted(p.name for p in cache_dir.glob("*.mp3"))
    assert remaining == ["f2.mp3", "f3.mp3", "f4.mp3"], f"phải giữ 3 file mới nhất, còn lại {remaining}"


def test_tts_cache_skips_empty_and_special_voices(monkeypatch, tmp_path):
    """Text rỗng / voice đặc biệt (ai_voice) thì không ghi cache."""
    import batch_engine
    import tts_manager

    fixture = str(tmp_path / "fixture.mp3")
    _make_audio(fixture, duration=0.5)
    calls: list = []
    monkeypatch.setattr(tts_manager, "synthesize_voice", _fake_synthesizer(fixture, calls))
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(batch_engine, "_TTS_CACHE_DIR", cache_dir)

    asyncio.run(batch_engine.generate_tts("   ", "vi-VN-HoaiMyNeural", str(tmp_path / "e.mp3")))
    asyncio.run(batch_engine.generate_tts("Có thoại", "ai_voice", str(tmp_path / "f.mp3")))
    assert not cache_dir.exists() or len(list(cache_dir.glob("*.mp3"))) == 0


# ── (2) Xfade single-pass ───────────────────────────────────────────────────

@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_single_pass_eight_clips_dissolve_duration(tmp_path):
    """8 clip 2s + 7 dissolve 0.5s -> 16 - 3.5 = 12.5s (single-pass mặc định)."""
    from composer import merge_clips_advanced, get_media_duration

    clips = []
    for i in range(8):
        p = str(tmp_path / f"clip_{i}.mp4")
        _make_clip(p, duration=2.0, color=["red", "blue", "green", "yellow"][i % 4], freq=300 + i * 50)
        clips.append(p)
    out = str(tmp_path / "merged_8.mp4")
    merge_clips_advanced(clips=clips, transitions=["dissolve"] * 7,
                         sound_bridge_duration=0.5, output_path=out)
    dur = get_media_duration(out)
    assert dur == pytest.approx(12.5, abs=0.5), f"single-pass sai độ dài: kỳ vọng ~12.5s, thực tế {dur:.2f}s"


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_single_pass_matches_pairwise_duration(monkeypatch, tmp_path):
    """Single-pass và pairwise (COMPOSE_MODE=pairwise) ra cùng độ dài."""
    from composer import merge_clips_advanced, get_media_duration

    clips = []
    for i in range(8):
        p = str(tmp_path / f"clip_{i}.mp4")
        _make_clip(p, duration=2.0, color=["red", "blue"][i % 2], freq=300 + i * 50)
        clips.append(p)
    out_single = str(tmp_path / "merged_single.mp4")
    merge_clips_advanced(clips=clips, transitions=["dissolve"] * 7,
                         sound_bridge_duration=0.5, output_path=out_single)
    monkeypatch.setenv("COMPOSE_MODE", "pairwise")
    out_pair = str(tmp_path / "merged_pair.mp4")
    merge_clips_advanced(clips=clips, transitions=["dissolve"] * 7,
                         sound_bridge_duration=0.5, output_path=out_pair)
    d1, d2 = get_media_duration(out_single), get_media_duration(out_pair)
    assert d1 == pytest.approx(12.5, abs=0.5)
    assert d2 == pytest.approx(12.5, abs=0.5)
    assert d1 == pytest.approx(d2, abs=0.3), f"single-pass ({d1:.2f}s) lệch pairwise ({d2:.2f}s)"


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_single_pass_mixed_cut_and_dissolve(tmp_path):
    """Trộn cut + dissolve: chỉ dissolve mới trừ overlap (5 clip 2s -> 9.0s)."""
    from composer import merge_clips_advanced, get_media_duration

    clips = []
    for i in range(5):
        p = str(tmp_path / f"clip_{i}.mp4")
        _make_clip(p, duration=2.0, color=["red", "blue"][i % 2], freq=300 + i * 50)
        clips.append(p)
    out = str(tmp_path / "merged_mixed.mp4")
    merge_clips_advanced(clips=clips, transitions=["dissolve", "cut", "dissolve", "cut"],
                         sound_bridge_duration=0.5, output_path=out)
    dur = get_media_duration(out)
    assert dur == pytest.approx(9.0, abs=0.5), f"trộn cut+dissolve sai độ dài: kỳ vọng ~9.0s, thực tế {dur:.2f}s"


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_single_pass_single_clip_and_empty(tmp_path):
    """1 clip -> copy nguyên; 0 clip -> ValueError (fallback nhánh cũ)."""
    from composer import merge_clips_advanced, get_media_duration

    p = str(tmp_path / "one.mp4")
    _make_clip(p, duration=2.0)
    out = str(tmp_path / "one_out.mp4")
    merge_clips_advanced(clips=[p], output_path=out)
    assert get_media_duration(out) == pytest.approx(2.0, abs=0.3)
    with pytest.raises(ValueError):
        merge_clips_advanced(clips=[], output_path=str(tmp_path / "none.mp4"))


# ── (3) Timing ──────────────────────────────────────────────────────────────

def test_summarize_batch_timing():
    """Gộp timing từng scene: poll/tts cộng dồn, render = wall time."""
    import batch_engine

    results = [
        {"index": 1, "status": "done", "timing": {"render_s": 5.0, "poll_s": 4.0, "tts_s": 1.0}},
        {"index": 2, "status": "done", "timing": {"render_s": 6.0, "poll_s": 5.0, "tts_s": 0.5}},
        {"index": 3, "status": "error"},  # thiếu timing -> tính 0, không crash
    ]
    t = batch_engine.summarize_batch_timing(results, wall_s=7.5)
    assert t["render_s"] == pytest.approx(7.5)
    assert t["poll_s"] == pytest.approx(9.0)
    assert t["tts_s"] == pytest.approx(1.5)
    assert t["compose_s"] == 0.0 and t["total_s"] == 0.0


def test_film_pipeline_records_job_timing(monkeypatch, tmp_path):
    """Job chạy xong có timing đầy đủ 5 key; tổng ≈ render + compose."""
    import app as app_mod

    monkeypatch.setattr(app_mod, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(app_mod, "MUSE_API_KEY", "test-key")

    # Audit yêu cầu video_path là file thật (>500 bytes) nên tạo clip thật.
    v1 = str(tmp_path / "v1.mp4")
    v2 = str(tmp_path / "v2.mp4")
    _make_clip(v1, duration=2.0, color="red")
    _make_clip(v2, duration=2.0, color="blue")

    async def _fake_run_batch(*args, **kwargs):
        t0 = time.monotonic()
        await asyncio.sleep(0.2)
        timing = kwargs.get("timing")
        if timing is not None:
            el = time.monotonic() - t0
            timing.update({"render_s": round(el, 3), "poll_s": round(el * 0.8, 3),
                           "tts_s": 0.05, "compose_s": 0.0, "total_s": 0.0})
        return [
            {"index": 1, "status": "done", "video_path": v1, "audio_path": "a1.mp3",
             "audio_duration": 5.0, "voiceover": "Xin chào",
             "timing": {"render_s": 0.2, "poll_s": 0.16, "tts_s": 0.05}},
            {"index": 2, "status": "done", "video_path": v2, "audio_path": "a2.mp3",
             "audio_duration": 5.0, "voiceover": "Tạm biệt",
             "timing": {"render_s": 0.2, "poll_s": 0.16, "tts_s": 0.05}},
        ]

    def _fake_compose(**kwargs):
        time.sleep(0.05)
        return {"video_path": str(tmp_path / "workspace" / "testjob1" / "final" / "movie.mp4")}

    monkeypatch.setattr(app_mod, "run_batch", _fake_run_batch)
    monkeypatch.setattr(app_mod, "compose_full_movie", _fake_compose)

    job = {"id": "testjob1", "status": "running", "progress": 0, "logs": [], "scenes": {}}
    req = app_mod.MovieRequest(
        title="test",
        scenes=[
            app_mod.SceneItem(index=1, prompt="cảnh một", voiceover="Xin chào", duration=5),
            app_mod.SceneItem(index=2, prompt="cảnh hai", voiceover="Tạm biệt", duration=5),
        ],
    )
    asyncio.run(app_mod.film_pipeline(job, req))

    assert job["status"] == "done"
    timing = job.get("timing", {})
    for key in ("render_s", "poll_s", "tts_s", "compose_s", "total_s"):
        assert key in timing, f"job thiếu timing key {key}"
        assert timing[key] >= 0
    assert timing["render_s"] == pytest.approx(0.2, abs=0.15)
    assert timing["compose_s"] > 0
    assert timing["total_s"] == pytest.approx(
        timing["render_s"] + timing["compose_s"], abs=0.5
    ), f"tổng ({timing['total_s']}) phải ≈ render + compose"
