"""Tests cho chồng chéo pipeline (nhóm 4) — prefetch prompt + TTS.

Phạm vi:
  (1) prefetch_scene() chuẩn bị prompt + TTS mà KHÔNG gọi API video,
      KHÔNG đụng reference_image / last frame (chain an toàn).
  (2) sequential 3 cảnh (mock API video): đúng thứ tự + chain đúng
      (last frame cảnh trước nối cảnh sau), TTS cảnh sau chồng lên poll
      cảnh trước, TTS mỗi cảnh chỉ sinh đúng 1 lần.
  (3) parallel + chain_from_previous: worker prefetch trong lúc chờ chain,
      wall time nhanh hơn hẳn chạy nối tiếp từng cảnh.
  (4) cancel giữa lúc chồng chéo: pipeline dừng, không render thêm shot.
  (5) job["timing"] giữ đủ key, tts_s vẫn cộng cả phần TTS prefetched.

Mock: API video giả (POST trả task id + ghi payload để kiểm tra chain),
poll giả (sleep ngắn), download/compose giả (copy clip thật), TTS giả
(sleep 1.0s rồi copy mp3 thật). KHÔNG gọi API video thật, KHÔNG cần key.

Chạy: python -m pytest test_overlap_pipeline.py -v
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

# TTS giả chậm (1.0s) + poll giả nhanh (0.15s): TTS là phần chồng lấn
# đo được — không prefetch thì mỗi cảnh tốn trọn 1.0s TTS nối tiếp.
TTS_SLEEP = 1.0
POLL_SLEEP = 0.15


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


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _scenes():
    return [
        {"index": 1, "prompt": "Cảnh một: chú thỏ trắng đứng trong vườn hoa",
         "voiceover": "Xin chào các bạn, mình là thỏ trắng.", "duration": 2},
        {"index": 2, "prompt": "Cảnh hai: chú thỏ trắng hái một bông hoa đỏ",
         "voiceover": "Hôm nay mình đi hái hoa đỏ.", "duration": 2},
        {"index": 3, "prompt": "Cảnh ba: chú thỏ trắng mang hoa về nhà",
         "voiceover": "Mang hoa về tặng mẹ nào.", "duration": 2},
    ]


@pytest.fixture
def overlap_env(monkeypatch, tmp_path):
    """Mock toàn bộ I/O ngoài của batch_engine cho test chồng chéo."""
    import batch_engine
    import composer
    import tts_manager

    fixture_clip = str(tmp_path / "fixture.mp4")
    _make_clip(fixture_clip, duration=2.0)
    fixture_mp3 = str(tmp_path / "fixture.mp3")
    _make_audio(fixture_mp3, duration=0.5)

    calls = {"tts": [], "posts": [], "polls": []}
    done_times = {}

    async def _fake_synth(text, voice, output_path, **kw):
        t0 = time.monotonic()
        calls["tts"].append({"text": text, "voice": voice, "start": t0})
        await asyncio.sleep(TTS_SLEEP)
        shutil.copy2(fixture_mp3, output_path)
        calls["tts"][-1]["end"] = time.monotonic()
        return 0.5

    monkeypatch.setattr(tts_manager, "synthesize_voice", _fake_synth)
    monkeypatch.setattr(batch_engine, "_TTS_CACHE_DIR", tmp_path / "cache")
    # Last frame do pipeline sinh ra phải resolve được qua
    # _resolve_safe_reference() (chỉ chấp nhận file trong workspace/).
    monkeypatch.setattr(batch_engine, "_WORKSPACE_ROOT", tmp_path)

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, *a, **kw):
            return _FakeResp(200, {})

        async def post(self, url, json=None, *a, **kw):
            calls["posts"].append({"url": url, "payload": json})
            return _FakeResp(200, {"id": f"task-{len(calls['posts'])}"})

    monkeypatch.setattr(batch_engine.httpx, "AsyncClient", _FakeClient)

    async def _fake_poll(client, task_id, base_url, headers, scene_idx,
                         progress_cb=None, cancel_event=None):
        calls["polls"].append(scene_idx)
        await asyncio.sleep(POLL_SLEEP)
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError(f"[Cảnh {scene_idx}] Job đã bị hủy trong lúc chờ video.")
        return "http://fake/video.mp4"

    monkeypatch.setattr(batch_engine, "poll_video_task", _fake_poll)

    async def _fake_download(client, url, dest, **kw):
        shutil.copy2(fixture_clip, dest)

    monkeypatch.setattr(batch_engine, "_download_validated", _fake_download)

    def _fake_compose(scene_video, scene_audio=None, output="", **kw):
        shutil.copy2(scene_video, output)

    monkeypatch.setattr(composer, "compose_scene_video", _fake_compose)

    def _cb(idx, msg, extra=None):
        if isinstance(msg, str) and msg.startswith("✅"):
            done_times[idx] = time.monotonic()

    return {
        "batch_engine": batch_engine, "calls": calls, "done_times": done_times,
        "cb": _cb, "tmp_path": tmp_path, "fixture_clip": fixture_clip,
    }


# ── (1) prefetch_scene đơn vị ───────────────────────────────────────────────

def test_prefetch_prepares_prompt_and_tts_without_video_api(overlap_env):
    """Prefetch: prompt + TTS xong, không gọi API video, không đụng ref."""
    be = overlap_env["batch_engine"]
    out = str(overlap_env["tmp_path"])
    scene = {"index": 2, "prompt": "Cảnh hai: chú thỏ trắng hái hoa",
             "voiceover": "Hôm nay mình đi hái hoa đỏ.", "duration": 2,
             "reference_image": "sentinel-ref-giu-nguyen"}
    res = asyncio.run(be.prefetch_scene(scene, out, "vi-VN-HoaiMyNeural"))

    assert res["prefetched"] is True
    assert res["tts_started"] is True
    assert res["tts_s"] >= TTS_SLEEP * 0.9, f"tts_s phải ≈ {TTS_SLEEP}s, thực tế {res['tts_s']}"
    assert res["tts_failed"] is False
    audio = os.path.join(out, "audio", "scene_002.mp3")
    assert os.path.exists(audio) and os.path.getsize(audio) > 500
    # Chain an toàn: prefetch không được đụng reference_image / last frame.
    assert scene["reference_image"] == "sentinel-ref-giu-nguyen"
    assert "last_frame_path" not in scene
    # Prompt + thoại nguyên văn (không paraphrase).
    built = scene["_prefetched_prompt"]
    assert built["voiceover"] == "Hôm nay mình đi hái hoa đỏ."
    assert "chú thỏ trắng hái hoa" in built["prompt"]
    # TTS chỉ gọi provider đúng 1 lần với đúng thoại.
    assert [c["text"] for c in overlap_env["calls"]["tts"]] == ["Hôm nay mình đi hái hoa đỏ."]
    assert len(overlap_env["calls"]["posts"]) == 0, "prefetch không được POST API video"


def test_prefetch_respects_preset_cancel(overlap_env):
    """Event đã set trước: prefetch không sinh TTS, trả cancelled."""
    be = overlap_env["batch_engine"]
    ev = asyncio.Event()
    ev.set()
    scene = {"index": 1, "prompt": "x", "voiceover": "Có thoại.", "duration": 2}
    res = asyncio.run(be.prefetch_scene(
        scene, str(overlap_env["tmp_path"]), "vi-VN-HoaiMyNeural", cancel_event=ev))
    assert res["cancelled"] is True and res["prefetched"] is False
    assert overlap_env["calls"]["tts"] == []
    assert not os.path.exists(os.path.join(str(overlap_env["tmp_path"]), "audio", "scene_001.mp3"))


def test_build_scene_prompt_matches_process_scene_prompt(monkeypatch, tmp_path):
    """build_scene_prompt cho ra đúng prompt process_scene vẫn dùng
    (không prefetch): kiểm tra mệnh đề lip-sync + thoại nguyên văn."""
    import batch_engine

    scene = {"index": 1, "prompt": "Chú thỏ trắng đứng trong vườn",
             "voiceover": "Xin chào các bạn.", "duration": 2,
             "character": "Thỏ Trắng"}
    built = batch_engine.build_scene_prompt(scene, None, "vi-VN-HoaiMyNeural", "tts")
    assert built["voiceover"] == "Xin chào các bạn."
    assert "Chú thỏ trắng đứng trong vườn" in built["prompt"]
    assert "lip articulation" in built["prompt"]
    assert "Thỏ Trắng" in built["prompt"]
    # Hàm thuần: không sửa scene.
    assert scene["prompt"] == "Chú thỏ trắng đứng trong vườn"
    assert "_prefetched_prompt" not in scene


# ── (2) sequential 3 cảnh: thứ tự + chain + chồng chéo ─────────────────────

def test_sequential_three_scenes_order_chain_and_overlap(overlap_env):
    """Sequential 3 cảnh mock: đúng thứ tự, chain đúng, TTS chồng poll,
    TTS mỗi cảnh 1 lần, wall nhanh hơn chạy nối tiếp từng cảnh."""
    be = overlap_env["batch_engine"]
    out = str(overlap_env["tmp_path"])
    scenes = _scenes()
    timing = {}
    t0 = time.monotonic()
    results = asyncio.run(be.run_batch(
        scenes=scenes, base_url="http://127.0.0.1:9", api_key="x",
        output_dir=out, chain_mode="sequential",
        progress_cb=overlap_env["cb"], timing=timing))
    wall = time.monotonic() - t0

    assert [r["index"] for r in results] == [1, 2, 3]
    assert [r["status"] for r in results] == ["done", "done", "done"], results
    # Chain đúng: cảnh 2 POST kèm reference_image base64 (last frame cảnh 1),
    # cảnh 3 kèm last frame cảnh 2; scene dict cũng nhận đúng đường dẫn.
    posts = overlap_env["calls"]["posts"]
    assert len(posts) == 3
    assert posts[0]["payload"].get("reference_image") in (None, "")
    for i in (1, 2):
        ref = posts[i]["payload"].get("reference_image") or ""
        assert ref.startswith("data:image"), f"cảnh {i + 1} phải chain bằng last frame cảnh {i}"
    assert scenes[1]["reference_image"] == results[0]["last_frame_path"]
    assert scenes[2]["reference_image"] == results[1]["last_frame_path"]
    for r in results:
        assert r["last_frame_path"] and os.path.exists(r["last_frame_path"])
    # Chồng chéo thật: TTS cảnh 2 bắt đầu TRƯỚC khi cảnh 1 báo xong.
    tts2 = [c for c in overlap_env["calls"]["tts"] if "hái hoa" in c["text"]][0]
    assert tts2["start"] < overlap_env["done_times"][1], (
        "TTS cảnh 2 phải bắt đầu trong lúc cảnh 1 còn poll/render")
    # TTS mỗi cảnh đúng 1 lần (prefetch dùng lại, process_scene không sinh lại).
    assert len(overlap_env["calls"]["tts"]) == 3, overlap_env["calls"]["tts"]
    assert [r.get("prefetched") for r in results] == [False, True, True]
    # Wall: không prefetch thì ≥ 3 × (TTS 1.0s + compose/lastframe) ≈ 3.6s;
    # có prefetch thì ≈ 2.2s. Ngưỡng 3.0s tách 2 trường hợp, chừa nhiễu CPU.
    assert wall < 3.0, f"sequential chồng chéo phải < 3.0s, thực tế {wall:.2f}s"
    # Timing giữ đủ key; tts_s cộng cả phần TTS prefetched (>0 mỗi cảnh).
    for key in ("render_s", "poll_s", "tts_s", "compose_s", "total_s"):
        assert key in timing, f"thiếu timing key {key}"
    assert timing["tts_s"] >= 3 * TTS_SLEEP * 0.9, f"tts_s phải cộng cả TTS prefetched: {timing}"
    assert timing["poll_s"] > 0
    for r in results:
        assert r["timing"]["tts_s"] > 0, f"cảnh {r['index']} mất tts_s prefetched"


# ── (3) parallel + chain: prefetch trong lúc chờ chain ─────────────────────

def test_parallel_chained_scenes_overlap_chain_wait(overlap_env):
    """chain_mode none + chain_from_previous: TTS cảnh sau chạy trong lúc
    chờ cảnh trước, wall nhanh hơn hẳn nối tiếp."""
    be = overlap_env["batch_engine"]
    out = str(overlap_env["tmp_path"])
    scenes = _scenes()
    scenes[1]["chain_from_previous"] = True
    scenes[2]["chain_from_previous"] = True
    t0 = time.monotonic()
    results = asyncio.run(be.run_batch(
        scenes=scenes, base_url="http://127.0.0.1:9", api_key="x",
        output_dir=out, chain_mode="none", max_concurrent=3,
        progress_cb=overlap_env["cb"]))
    wall = time.monotonic() - t0

    assert [r["index"] for r in results] == [1, 2, 3]
    assert [r["status"] for r in results] == ["done", "done", "done"], results
    posts = overlap_env["calls"]["posts"]
    assert len(posts) == 3
    assert posts[1]["payload"].get("reference_image", "").startswith("data:image")
    assert posts[2]["payload"].get("reference_image", "").startswith("data:image")
    assert len(overlap_env["calls"]["tts"]) == 3
    # TTS cảnh 2/3 bắt đầu trước khi cảnh 1 xong (chờ chain nhưng TTS chạy).
    tts2 = [c for c in overlap_env["calls"]["tts"] if "hái hoa" in c["text"]][0]
    assert tts2["start"] < overlap_env["done_times"][1]
    assert wall < 2.5, f"parallel+chain phải < 2.5s, thực tế {wall:.2f}s"


# ── (4) cancel giữa lúc chồng chéo ──────────────────────────────────────────

def test_cancel_mid_overlap_stops_no_further_render(monkeypatch, overlap_env):
    """Hủy trong lúc cảnh 1 poll (prefetch cảnh 2 đang chạy TTS):
    CancelledError lan ra, cảnh 2/3 không POST render, không rò task."""
    be = overlap_env["batch_engine"]
    out = str(overlap_env["tmp_path"])
    ev = asyncio.Event()

    async def _cancelling_poll(client, task_id, base_url, headers, scene_idx,
                               progress_cb=None, cancel_event=None):
        await asyncio.sleep(0.05)
        ev.set()  # người dùng bấm hủy giữa lúc poll cảnh 1
        raise asyncio.CancelledError(f"[Cảnh {scene_idx}] Job đã bị hủy trong lúc chờ video.")

    monkeypatch.setattr(be, "poll_video_task", _cancelling_poll)

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(be.run_batch(
                scenes=_scenes(), base_url="http://127.0.0.1:9", api_key="x",
                output_dir=out, chain_mode="sequential", cancel_event=ev))
        loop.run_until_complete(asyncio.sleep(0.1))
        pending = [t for t in asyncio.all_tasks(loop)
                   if not t.done() and t is not asyncio.current_task(loop)]
        assert pending == [], f"rò rỉ task sau cancel: {pending}"
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    posts = overlap_env["calls"]["posts"]
    assert len(posts) == 1, f"chỉ cảnh 1 được POST trước khi hủy, thực tế {len(posts)}"
