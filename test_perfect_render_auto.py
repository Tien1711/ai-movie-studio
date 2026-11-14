"""Hồi quy Nhóm 1 — render tự động (2026-11-09).

Phạm vi:
(1) Retry compliance: prompt gốc bị từ chối -> tự thử ĐÚNG 1 lần bằng
    compliance_prompt của shot (nếu có); bị từ chối nữa thì dừng, báo rõ.
(2) QC clip: ffprobe + blackdetect/freezedetect — clip đen hoàn toàn, sai
    thời lượng/phân giải, đứng hình -> qc_failed; clip tốt -> pass.
(3) Hàng đợi render nhiều phim + ước tính thời gian trước khi bấm Sản xuất.

Không gọi API video thật: mọi test dùng mock client / clip giả tạo bằng ffmpeg.
"""

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_mod
import batch_engine
import qc as qc_mod
from app import JOBS, WORKSPACE, app

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ffmpeg_available = shutil.which("ffmpeg") is not None or Path(FFMPEG).exists()

client = TestClient(app)


# ── Fixtures clip giả ───────────────────────────────────────────────────────

def _make_clip(path: str, duration: float = 5.0, size: str = "1280x720",
               source: str = "testsrc", fps: int = 24):
    """Clip giả: testsrc (chuyển động thật) / color đen / color đỏ (đứng hình)."""
    if source == "testsrc":
        video_in = f"testsrc=size={size}:rate={fps}:duration={duration}"
    else:
        video_in = f"color=c={source}:s={size}:r={fps}:d={duration}"
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", video_in,
        "-f", "lavfi", "-i", f"sine=f=440:d={duration}:r=44100",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-shortest", path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ── (1) Retry compliance trong process_scene ───────────────────────────────

class _FakeResp:
    """Response giả: lần 1 raise refusal, lần sau trả task_id."""
    def __init__(self, ok: bool, task_id: str = ""):
        self._ok = ok
        self.status_code = 200 if ok else 400
        self._task_id = task_id

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("policy violation: content refused by policy")

    def json(self):
        return {"id": self._task_id}


class _FakeClient:
    """httpx client giả: đếm số lần post + ghi lại prompt từng lần."""
    def __init__(self, fail_times: int):
        self.posts: list = []
        self.fail_times = fail_times
        self.calls = 0

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        self.posts.append((json or {}).get("prompt", ""))
        return _FakeResp(ok=self.calls > self.fail_times, task_id="task-1")


async def _no_sleep(delay=0, *args, **kwargs):
    return None


def _run_process_scene(monkeypatch, tmp_path, scene, fail_times: int):
    """Chạy process_scene với toàn bộ I/O video/TTS đã mock (chỉ QC chạy thật)."""
    fixture = str(tmp_path / "fixture.mp4")
    _make_clip(fixture, duration=5.0, size="1280x720", source="testsrc")

    fake_client = _FakeClient(fail_times=fail_times)

    async def _fake_poll(*args, **kwargs):
        return "http://example.invalid/video.mp4"

    async def _fake_download(client, url, output_path, expected, max_bytes, timeout=120.0):
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy2(fixture, output_path)
        return output_path

    async def _fake_tts(text, voice, output_path, **kw):
        return (5.0, False, "")

    def _fake_compose(**kw):
        shutil.copy2(kw["scene_video"], kw["output"])
        return kw["output"]

    monkeypatch.setattr(batch_engine, "poll_video_task", _fake_poll)
    monkeypatch.setattr(batch_engine, "_download_validated", _fake_download)
    monkeypatch.setattr(batch_engine, "generate_tts_with_status", _fake_tts)
    monkeypatch.setattr(batch_engine, "extract_last_frame",
                        lambda video_path, out_path: str(tmp_path / "last.jpg"))
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    import composer
    monkeypatch.setattr(composer, "compose_scene_video", _fake_compose)

    sem = asyncio.Semaphore(1)
    result = asyncio.run(batch_engine.process_scene(
        client=fake_client,
        scene=dict(scene),
        sem=sem,
        base_url="http://example.invalid",
        api_key="test-key",
        output_dir=str(tmp_path / "job"),
        voice="vi-VN-HoaiMyNeural",
        progress_cb=lambda idx, msg, extra=None: None,
    ))
    return result, fake_client


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_compliance_retry_success_once(monkeypatch, tmp_path):
    """Prompt gốc bị từ chối -> thử ĐÚNG 1 lần bằng compliance_prompt (nguyên văn) -> done."""
    scene = {
        "index": 1, "prompt": "a knight strikes with a sword in a bloody battle",
        "compliance_prompt": "a knight stands calmly holding a glowing artifact, cinematic",
        "voiceover": "", "duration": 5, "size": "16:9",
    }
    result, fake_client = _run_process_scene(monkeypatch, tmp_path, scene, fail_times=1)

    assert fake_client.calls == 2, f"phải post đúng 2 lần (gốc + compliance), thực tế {fake_client.calls}"
    assert fake_client.posts[1] == scene["compliance_prompt"], \
        "lần 2 phải dùng compliance_prompt NGUYÊN VĂN"
    assert fake_client.posts[0] != scene["compliance_prompt"]
    assert result["status"] == "done", result
    assert result["used_compliance"] is True
    assert result["compliance_failed"] is False


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_compliance_retry_both_refused_stops(monkeypatch, tmp_path):
    """Cả gốc + compliance đều bị từ chối -> dừng, đúng 2 lần post, báo rõ."""
    scene = {
        "index": 2, "prompt": "a knight strikes with a sword in a bloody battle",
        "compliance_prompt": "a knight stands calmly holding a glowing artifact, cinematic",
        "voiceover": "", "duration": 5, "size": "16:9",
    }
    result, fake_client = _run_process_scene(monkeypatch, tmp_path, scene, fail_times=2)

    assert fake_client.calls == 2, f"không được retry lần 3, thực tế {fake_client.calls} lần"
    assert result["status"] == "error"
    assert result["used_compliance"] is True
    assert result["compliance_failed"] is True
    assert "compliance_prompt" in (result.get("error") or ""), \
        f"lỗi phải nói rõ cả compliance cũng bị từ chối: {result.get('error')}"


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_no_compliance_falls_back_to_sanitize_once(monkeypatch, tmp_path):
    """Không có compliance_prompt -> fallback sanitize cũ, vẫn chỉ retry 1 lần."""
    scene = {
        "index": 3, "prompt": "a knight strikes with a sword in a bloody battle",
        "voiceover": "", "duration": 5, "size": "16:9",
    }
    result, fake_client = _run_process_scene(monkeypatch, tmp_path, scene, fail_times=1)

    assert fake_client.calls == 2, f"phải post đúng 2 lần, thực tế {fake_client.calls}"
    assert fake_client.posts[1] != fake_client.posts[0], "lần 2 phải là prompt đã sanitize"
    assert result["used_compliance"] is False
    assert result["status"] == "done", result


# ── (2) QC clip ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_qc_good_clip_passes(tmp_path):
    """Clip tốt (testsrc chuyển động, đúng 5s/1280x720/24fps) -> pass."""
    p = str(tmp_path / "good.mp4")
    _make_clip(p, duration=5.0, size="1280x720", source="testsrc", fps=24)
    res = qc_mod.qc_clip(p, expected_duration=5.0, expected_width=1280,
                         expected_height=720, expected_fps=24)
    assert res["passed"] is True, res
    assert res["issues"] == []
    assert res["duration_s"] == pytest.approx(5.0, abs=0.2)
    assert (res["width"], res["height"]) == (1280, 720)
    assert res["fps"] == pytest.approx(24.0, abs=0.5)


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_qc_fully_black_clip_fails(tmp_path):
    """Clip đen hoàn toàn -> qc_failed, lý do nói rõ đen hoàn toàn."""
    p = str(tmp_path / "black.mp4")
    _make_clip(p, duration=5.0, size="1280x720", source="black", fps=24)
    res = qc_mod.qc_clip(p, expected_duration=5.0, expected_width=1280,
                         expected_height=720, expected_fps=24)
    assert res["passed"] is False
    assert res["checks"]["black"] == "fail"
    assert any("đen hoàn toàn" in i for i in res["issues"]), res["issues"]


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_qc_wrong_duration_fails(tmp_path):
    """Clip 2s trong khi kỳ vọng 5s (lệch > ±1s) -> fail duration."""
    p = str(tmp_path / "short.mp4")
    _make_clip(p, duration=2.0, size="1280x720", source="testsrc", fps=24)
    res = qc_mod.qc_clip(p, expected_duration=5.0, expected_width=1280,
                         expected_height=720, expected_fps=24)
    assert res["passed"] is False
    assert res["checks"]["duration"] == "fail"
    assert any("thời lượng" in i for i in res["issues"]), res["issues"]


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_qc_wrong_resolution_fails(tmp_path):
    """Clip 640x360 trong khi kỳ vọng 1280x720 -> fail resolution."""
    p = str(tmp_path / "small.mp4")
    _make_clip(p, duration=5.0, size="640x360", source="testsrc", fps=24)
    res = qc_mod.qc_clip(p, expected_duration=5.0, expected_width=1280,
                         expected_height=720, expected_fps=24)
    assert res["passed"] is False
    assert res["checks"]["resolution"] == "fail"
    assert any("phân giải" in i for i in res["issues"]), res["issues"]


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_qc_frozen_clip_fails(tmp_path):
    """Clip đứng hình (màu đỏ đặc 5s, không chuyển động) -> fail freeze."""
    p = str(tmp_path / "frozen.mp4")
    _make_clip(p, duration=5.0, size="1280x720", source="red", fps=24)
    res = qc_mod.qc_clip(p, expected_duration=5.0, expected_width=1280,
                         expected_height=720, expected_fps=24)
    assert res["passed"] is False
    assert res["checks"]["freeze"] == "fail"
    assert any("đứng hình" in i for i in res["issues"]), res["issues"]


def test_qc_missing_file_fails_without_crash(tmp_path):
    """File không tồn tại -> passed False + error rõ, không raise."""
    res = qc_mod.qc_clip(str(tmp_path / "khong_co.mp4"), expected_duration=5.0)
    assert res["passed"] is False
    assert res["error"]


def test_qc_duration_within_tolerance_passes(tmp_path):
    """Lệch 0.5s trong dung sai ±1s -> duration pass (dùng probe giả)."""
    # Clip 5s thật, kỳ vọng 5.4s: lệch 0.4s -> pass.
    p = str(tmp_path / "tol.mp4")
    _make_clip(p, duration=5.0, size="1280x720", source="testsrc", fps=24)
    res = qc_mod.qc_clip(p, expected_duration=5.4, expected_width=1280,
                         expected_height=720, expected_fps=24)
    assert res["checks"]["duration"] == "pass", res


def test_expected_resolution_for_size():
    """Map tỉ lệ khung hình -> phân giải 720p mà composer scale tới."""
    assert qc_mod.expected_resolution_for_size("9:16") == (720, 1280)
    assert qc_mod.expected_resolution_for_size("16:9") == (1280, 720)
    assert qc_mod.expected_resolution_for_size("1:1") == (720, 720)


# ── (3a) Ước tính thời gian ─────────────────────────────────────────────────

def _movie_req(n_scenes=3, concurrent=2, chain_mode="none"):
    return app_mod.MovieRequest(
        title="test estimate",
        max_concurrent=concurrent,
        chain_mode=chain_mode,
        scenes=[app_mod.SceneItem(index=i + 1, prompt=f"cảnh {i + 1}",
                                  voiceover="", duration=5) for i in range(n_scenes)],
    )


def test_estimate_fallback_without_history(monkeypatch):
    """Chưa có job nào đo được -> fallback 60s/cảnh + 30s ghép, ghi rõ là ước tính."""
    monkeypatch.setattr(app_mod, "JOBS", {})
    est = app_mod.estimate_render_time(_movie_req(n_scenes=3, concurrent=2))
    # 3 cảnh / concurrent 2 = 2 wave x 60s + 30s ghép = 150s
    assert est["estimated_seconds"] == pytest.approx(150.0)
    assert est["basis"] == "fallback"
    assert "không cam kết" in est["note"]


def test_estimate_uses_measured_timing(monkeypatch):
    """Có job trước đo được poll 120s/2 cảnh + compose 20s -> ước tính theo số đo."""
    monkeypatch.setattr(app_mod, "JOBS", {
        "j1": {"timing": {"poll_s": 120.0, "compose_s": 20.0},
               "results": [{"status": "done"}, {"status": "done"}]},
    })
    est = app_mod.estimate_render_time(_movie_req(n_scenes=4, concurrent=2))
    # avg poll 60s/cảnh; 4 cảnh / 2 luồng = 2 wave x 60s + 20s = 140s
    assert est["avg_poll_s_per_scene"] == pytest.approx(60.0)
    assert est["estimated_seconds"] == pytest.approx(140.0)
    assert est["basis"] == "measured"
    assert est["samples"]["scenes_measured"] == 2


def test_estimate_sequential_uses_concurrent_1(monkeypatch):
    """chain sequential -> concurrent hiệu dụng 1: 3 cảnh x 60s + 30s = 210s."""
    monkeypatch.setattr(app_mod, "JOBS", {})
    est = app_mod.estimate_render_time(_movie_req(n_scenes=3, concurrent=6, chain_mode="sequential"))
    assert est["concurrent"] == 1
    assert est["estimated_seconds"] == pytest.approx(210.0)


def test_estimate_endpoint_no_job_created():
    """POST /api/estimate trả ước tính, không tạo job."""
    before = set(JOBS.keys())
    payload = {
        "title": "test estimate api", "max_concurrent": 2, "chain_mode": "none",
        "scenes": [
            {"index": 1, "prompt": "cảnh 1", "voiceover": "", "duration": 5, "size": "16:9"},
            {"index": 2, "prompt": "cảnh 2", "voiceover": "", "duration": 5, "size": "16:9"},
        ],
    }
    r = client.post("/api/estimate", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["estimated_seconds"] > 0
    assert data["scenes"] == 2
    assert "không cam kết" in data["note"]
    assert set(JOBS.keys()) == before, "estimate không được tạo job"


def test_render_queue_endpoint_shape():
    """GET /api/render-queue trả đúng shape (current/queued/queue_length)."""
    r = client.get("/api/render-queue")
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data.keys()) == {"current", "queued", "queue_length"}
    assert isinstance(data["queued"], list)


# ── (3b) Hàng đợi render ────────────────────────────────────────────────────

def _queue_job(monkeypatch, job_id, title):
    req = app_mod.MovieRequest(
        title=title,
        scenes=[app_mod.SceneItem(index=1, prompt="cảnh 1", voiceover="", duration=5)],
    )
    job = app_mod.new_job(job_id, 1, req=req)
    job["characters"] = []
    return job


def test_render_queue_runs_jobs_sequentially(monkeypatch, tmp_path):
    """2 job xếp hàng: job 2 chỉ bắt đầu sau khi job 1 xong; vị trí đúng."""
    monkeypatch.setattr(app_mod, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(app_mod, "JOBS", {})
    monkeypatch.setattr(app_mod, "RENDER_QUEUE", [])
    monkeypatch.setattr(app_mod, "RENDER_QUEUE_WORKER", None)
    monkeypatch.setattr(app_mod, "RENDER_CURRENT_JOB_ID", None)

    order: list = []

    async def _fake_pipeline(job, req):
        order.append(("start", job["id"]))
        await asyncio.sleep(0.2)
        job["status"] = "done"
        job["progress"] = 100
        order.append(("end", job["id"]))

    monkeypatch.setattr(app_mod, "film_pipeline", _fake_pipeline)

    async def _run():
        job1 = _queue_job(monkeypatch, "qjob1", "Phim một")
        job2 = _queue_job(monkeypatch, "qjob2", "Phim hai")
        p1 = app_mod.enqueue_render_job(job1)
        p2 = app_mod.enqueue_render_job(job2)
        assert p1 == 1 and p2 == 2
        # Lúc job 1 đang chạy, snapshot phải thấy job 2 ở vị trí 1.
        await asyncio.sleep(0.05)
        snap = app_mod._render_queue_snapshot()
        assert snap["current"] and snap["current"]["job_id"] == "qjob1"
        assert [q["job_id"] for q in snap["queued"]] == ["qjob2"]
        assert snap["queued"][0]["queue_position"] == 1
        # Chờ hàng rỗng.
        for _ in range(100):
            worker = app_mod.RENDER_QUEUE_WORKER
            if not app_mod.RENDER_QUEUE and app_mod.RENDER_CURRENT_JOB_ID is None \
                    and (worker is None or worker.done()):
                break
            await asyncio.sleep(0.05)
        return job1, job2

    job1, job2 = asyncio.run(_run())
    assert order == [("start", "qjob1"), ("end", "qjob1"),
                     ("start", "qjob2"), ("end", "qjob2")], \
        f"job phải chạy nối tiếp, không chồng nhau: {order}"
    assert job1["status"] == "done" and job2["status"] == "done"
    assert app_mod._render_queue_snapshot()["queue_length"] == 0


def test_cancel_queued_job_removes_from_queue(monkeypatch, tmp_path):
    """Hủy job đang chờ -> gỡ khỏi hàng đợi, job đó không bao giờ render."""
    monkeypatch.setattr(app_mod, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(app_mod, "JOBS", {})
    monkeypatch.setattr(app_mod, "RENDER_QUEUE", [])
    monkeypatch.setattr(app_mod, "RENDER_QUEUE_WORKER", None)
    monkeypatch.setattr(app_mod, "RENDER_CURRENT_JOB_ID", None)

    started: list = []

    async def _fake_pipeline(job, req):
        started.append(job["id"])
        await asyncio.sleep(0.3)
        job["status"] = "done"

    monkeypatch.setattr(app_mod, "film_pipeline", _fake_pipeline)

    async def _run():
        job1 = _queue_job(monkeypatch, "cjob1", "Phim một")
        job2 = _queue_job(monkeypatch, "cjob2", "Phim hai")
        app_mod.enqueue_render_job(job1)
        app_mod.enqueue_render_job(job2)
        # Hủy job 2 lúc còn chờ (gọi trực tiếp endpoint sync).
        resp = app_mod.api_cancel_job("cjob2")
        assert resp.get("success") is True
        assert "cjob2" not in app_mod.RENDER_QUEUE
        assert job2["status"] == "cancelled"
        for _ in range(100):
            worker = app_mod.RENDER_QUEUE_WORKER
            if not app_mod.RENDER_QUEUE and app_mod.RENDER_CURRENT_JOB_ID is None \
                    and (worker is None or worker.done()):
                break
            await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert started == ["cjob1"], f"job 2 đã hủy không được render: {started}"


def test_create_movie_returns_queue_position():
    """POST /api/movies trả queue_position (job mới xếp hàng, không spawn trực tiếp)."""
    payload = {
        "title": "Phim test queue", "max_concurrent": 6, "chain_mode": "none",
        "scenes": [{"index": 1, "prompt": "cảnh 1", "voiceover": "", "duration": 5, "size": "16:9"}],
    }
    r = client.post("/api/movies", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "job_id" in data
    assert "queue_position" in data, data
    assert data["queue_position"] >= 1
    job_id = data["job_id"]
    try:
        # Chờ worker xử lý xong (không có key -> job error nhanh).
        time.sleep(1.0)
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] in ("queued", "running", "error", "cancelled"), job["status"]
    finally:
        JOBS.pop(job_id, None)
        app_mod.dequeue_render_job(job_id)
        job_dir = Path(WORKSPACE) / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)


# ── (1)+(2) tích hợp film_pipeline: cờ QC/compliance lên job ───────────────

def test_film_pipeline_propagates_qc_and_compliance_flags(monkeypatch, tmp_path):
    """run_batch giả trả scene QC-fail + đã thử compliance -> job ghi đúng cờ."""
    monkeypatch.setattr(app_mod, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(app_mod, "MUSE_API_KEY", "test-key")

    v1 = str(tmp_path / "v1.mp4")
    _make_clip(v1, duration=2.0, size="1280x720", source="testsrc")

    async def _fake_run_batch(*args, **kwargs):
        timing = kwargs.get("timing")
        if timing is not None:
            timing.update({"render_s": 0.2, "poll_s": 0.16, "tts_s": 0.05,
                           "compose_s": 0.0, "total_s": 0.0})
        return [
            {"index": 1, "status": "done", "video_path": v1, "audio_path": "a1.mp3",
             "audio_duration": 5.0, "voiceover": "Xin chào",
             "qc_failed": True, "qc_issues": ["Sai thời lượng: thực tế 2.0s, kỳ vọng 5.0s"],
             "qc": {"passed": False}, "used_compliance": True, "compliance_failed": False,
             "timing": {"render_s": 0.2, "poll_s": 0.16, "tts_s": 0.05}},
            {"index": 2, "status": "error", "error": "Cả prompt gốc và compliance_prompt đều bị từ chối: policy",
             "used_compliance": True, "compliance_failed": True},
        ]

    def _fake_compose(**kwargs):
        return {"video_path": str(tmp_path / "workspace" / "testjobqc" / "final" / "movie.mp4")}

    monkeypatch.setattr(app_mod, "run_batch", _fake_run_batch)
    monkeypatch.setattr(app_mod, "compose_full_movie", _fake_compose)

    job = {"id": "testjobqc", "status": "running", "progress": 0, "logs": [], "scenes": {}}
    req = app_mod.MovieRequest(
        title="test qc flags",
        allow_partial=True,
        scenes=[
            app_mod.SceneItem(index=1, prompt="cảnh một", voiceover="Xin chào", duration=5),
            app_mod.SceneItem(index=2, prompt="cảnh hai", voiceover="Tạm biệt", duration=5),
        ],
    )
    asyncio.run(app_mod.film_pipeline(job, req))

    assert job["qc_failed_scenes"] == [1], job.get("qc_failed_scenes")
    assert job["compliance_retried_scenes"] == [1, 2], job.get("compliance_retried_scenes")
    sc1 = job["scenes"]["1"]
    assert sc1["qc_failed"] is True
    assert sc1["qc_issues"] == ["Sai thời lượng: thực tế 2.0s, kỳ vọng 5.0s"]
    assert sc1["used_compliance"] is True
    sc2 = job["scenes"]["2"]
    assert sc2["compliance_failed"] is True
    assert any("QC không đạt" in log for log in job["logs"]), job["logs"]
