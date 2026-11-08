"""Benchmark tốc độ ghép (compose) + cache TTS — đợt tối ưu render 2026-11-08.

Tự sinh clip test bằng ffmpeg, KHÔNG cần API ngoài, KHÔNG cần Muse key.

Chạy từ thư mục gốc repo:
    /tmp/studio-venv2/bin/python scripts/bench_compose.py

Đo:
  - merge_clips_advanced với 8 và 16 clip (2s/clip, dissolve 0.5s) ở cả
    2 chế độ: pairwise cũ (COMPOSE_MODE=pairwise) và single-pass mới.
  - generate_tts 3 lần cùng text+voice (fake provider sleep 0.3s):
    lần 1 miss, lần 2-3 hit cache.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def make_clip(path: str, duration: float = 2.0, color: str = "red", freq: int = 440):
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=720x1280:d={duration}:r=24",
        "-f", "lavfi", "-i", f"sine=f={freq}:d={duration}:r=44100",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def bench_compose():
    from composer import merge_clips_advanced, get_media_duration

    print("=== Benchmark ghép video (720x1280, clip 2s, dissolve 0.5s) ===")
    print(f"{'Clips':>6} | {'Chế độ':>10} | {'Thời gian':>10} | {'Độ dài ra':>10} | {'Kỳ vọng':>8}")
    print("-" * 62)
    results = {}
    with tempfile.TemporaryDirectory(prefix="bench_compose_") as td:
        for n in (8, 16):
            clips = []
            for i in range(n):
                p = os.path.join(td, f"clip_{n}_{i}.mp4")
                make_clip(p, duration=2.0, color=["red", "blue", "green", "yellow"][i % 4],
                          freq=300 + (i % 8) * 50)
                clips.append(p)
            expected = n * 2.0 - (n - 1) * 0.5
            for mode in ("pairwise", "single"):
                os.environ["COMPOSE_MODE"] = mode
                out = os.path.join(td, f"merged_{n}_{mode}.mp4")
                t0 = time.perf_counter()
                merge_clips_advanced(clips=clips, transitions=["dissolve"] * (n - 1),
                                     sound_bridge_duration=0.5, output_path=out)
                el = time.perf_counter() - t0
                dur = get_media_duration(out)
                ok = "✓" if abs(dur - expected) <= 0.5 else "✗ SAI ĐỘ DÀI"
                print(f"{n:>6} | {mode:>10} | {el:>9.2f}s | {dur:>9.2f}s | {expected:>7.1f}s {ok}")
                results[(n, mode)] = el
    print()
    for n in (8, 16):
        pw, sp = results[(n, "pairwise")], results[(n, "single")]
        print(f"  {n} clip: single-pass nhanh hơn {pw / sp:.1f}x "
              f"(pairwise {pw:.2f}s -> single-pass {sp:.2f}s, tiết kiệm {pw - sp:.2f}s)")
    return results


def bench_tts_cache():
    import batch_engine
    import tts_manager

    print()
    print("=== Benchmark cache TTS (fake provider sleep 0.3s/lần) ===")
    with tempfile.TemporaryDirectory(prefix="bench_tts_") as td:
        fixture = os.path.join(td, "fixture.mp3")
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=f=440:d=0.5:r=44100",
                        "-c:a", "libmp3lame", "-b:a", "128k", fixture],
                       check=True, capture_output=True)
        calls = {"n": 0}

        async def _fake_synth(text, voice, output_path, **kw):
            calls["n"] += 1
            await asyncio.sleep(0.3)  # giả lập provider chậm
            shutil.copy2(fixture, output_path)
            return 0.5

        orig = tts_manager.synthesize_voice
        tts_manager.synthesize_voice = _fake_synth
        orig_dir = batch_engine._TTS_CACHE_DIR
        batch_engine._TTS_CACHE_DIR = Path(td) / "cache"
        try:
            t0 = time.perf_counter()
            for i in range(3):
                asyncio.run(batch_engine.generate_tts(
                    "Xin chào các bạn, đây là câu thoại test.",
                    "vi-VN-HoaiMyNeural", os.path.join(td, f"out{i}.mp3")))
            el = time.perf_counter() - t0
        finally:
            tts_manager.synthesize_voice = orig
            batch_engine._TTS_CACHE_DIR = orig_dir
        print(f"  3 lần gọi cùng text+voice: provider gọi {calls['n']} lần (1 miss + 2 hit)")
        print(f"  Tổng thời gian: {el:.2f}s (không cache sẽ mất ~0.90s) "
              f"-> tiết kiệm ~{0.90 - el:.2f}s")
        return calls["n"], el


if __name__ == "__main__":
    bench_compose()
    bench_tts_cache()
