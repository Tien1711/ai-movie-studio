import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as studio
import batch_video_engine as batch
import composer
import tts_manager as tts


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "WORKSPACE", tmp_path)
    monkeypatch.setattr(studio, "JOBS", {})
    monkeypatch.setattr(studio, "RENDER_QUEUE", [])
    monkeypatch.setattr(studio, "RENDER_QUEUE_WORKER", None)
    monkeypatch.setattr(studio, "_JOBS_LOADED_ONCE", False)
    monkeypatch.delenv("STUDIO_API_KEY", raising=False)
    return tmp_path


def _job(status):
    job = studio.new_job("reviewfixture", 0, studio.MovieRequest(title="Fixture", scenes=[]))
    job["status"] = status
    studio.JOBS[job["id"]] = job
    return job


def test_bind_defaults_to_loopback_and_requires_key_for_public_bind(monkeypatch):
    monkeypatch.delenv("STUDIO_HOST", raising=False)
    monkeypatch.delenv("STUDIO_API_KEY", raising=False)
    assert studio._studio_bind_host() == "127.0.0.1"
    monkeypatch.setenv("STUDIO_HOST", "0.0.0.0")
    with pytest.raises(SystemExit, match="STUDIO_API_KEY"):
        studio._studio_bind_host()
    monkeypatch.setenv("STUDIO_API_KEY", "fixture-key")
    assert studio._studio_bind_host() == "0.0.0.0"


def test_browser_session_authorizes_video_ranges_downloads_and_sse(jobs, monkeypatch):
    monkeypatch.setenv("STUDIO_API_KEY", "fixture-key")
    job = _job("done")
    final = jobs / job["id"] / "final"
    final.mkdir(parents=True)
    (final / "movie.mp4").write_bytes(b"fixture video")
    (final / "movie.srt").write_text("fixture subtitles", encoding="utf-8")
    job_url = f"/api/jobs/{job['id']}"
    job["final_video"] = f"{job_url}/video/movie.mp4"

    with TestClient(studio.app) as client:
        assert client.get(job["final_video"]).status_code == 401
        assert client.post("/api/auth/session", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get(job["final_video"], headers={"X-API-Key": "fixture-key"}).status_code == 200

        login = client.post("/api/auth/session", headers={"X-API-Key": "fixture-key"})
        assert login.status_code == 200
        assert "httponly" in login.headers["set-cookie"].lower()
        assert "samesite=strict" in login.headers["set-cookie"].lower()
        assert "fixture-key" not in login.headers["set-cookie"]
        assert client.get("/api/auth/status").json()["authenticated"] is True
        video = client.get(job["final_video"], headers={"Range": "bytes=0-6"})
        assert video.status_code == 206
        assert video.content == b"fixture"
        assert client.get(f"{job_url}/subtitles").text == "fixture subtitles"
        assert 'text/event-stream' in client.get(f"{job_url}/sse").headers["content-type"]
        assert client.delete("/api/auth/session").status_code == 200
        assert client.get(job["final_video"]).status_code == 401


def test_resume_accepts_job_loaded_at_startup(jobs, monkeypatch):
    job = _job("running")
    studio.save_job(job)
    studio.JOBS.clear()
    queued = []
    monkeypatch.setattr(studio, "enqueue_render_job", lambda job: queued.append(job) or 1)

    with TestClient(studio.app) as client:
        response = client.post(f"/api/jobs/{job['id']}/resume", json={"from_scene_idx": 2})
    assert response.status_code == 200
    assert queued[0]["req"].start_scene_idx == 2


def test_manual_compose_blocks_resume_and_cancels_active_process(jobs, monkeypatch):
    job = _job("audit_pending")
    job["results"] = [{"status": "done"}]
    processes = []
    started = threading.Event()
    popen = composer.subprocess.Popen

    def capture_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    def slow_compose(**kwargs):
        composer._run_compose_ffmpeg([sys.executable, "-c", "import time; time.sleep(20)"], timeout=30)

    async def run():
        compose = asyncio.create_task(studio.api_compose_job(job["id"]))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            resume = await studio.api_resume_job(job["id"], studio.ResumeJobRequest(from_scene_idx=2))
            assert resume.status_code == 409
            assert job["req"].start_scene_idx != 2
        finally:
            await studio.api_cancel_job(job["id"])
            result = await asyncio.wait_for(compose, timeout=5)
        assert result.status_code == 409

    monkeypatch.setattr(composer.subprocess, "Popen", capture_process)
    monkeypatch.setattr(studio, "compose_full_movie", slow_compose)
    asyncio.run(run())
    assert processes[0].poll() is not None
    assert job["status"] == "cancelled"
    assert not job.get("final_video")


def test_batch_startup_starts_existing_queued_jobs(monkeypatch):
    started = []
    monkeypatch.setattr(batch, "_UNDER_PYTEST", False)
    monkeypatch.setattr(batch.BATCH_MANAGER, "_recover_interrupted_jobs", lambda: started.append("recover") or 1)
    monkeypatch.setattr(batch.BATCH_MANAGER, "recovered_on_init", 0)
    monkeypatch.setattr(batch.BATCH_MANAGER, "_read_queue", lambda: [{"status": "queued"}])
    monkeypatch.setattr(batch, "start_batch_worker", lambda: started.append("worker"))
    batch.start_batch_runtime()
    assert started == ["recover", "worker"]
    assert batch.BATCH_MANAGER.recovered_on_init == 1


def test_importing_batch_engine_does_not_recover_a_running_comic(tmp_path):
    source = Path(batch.__file__)
    shutil.copyfile(source, tmp_path / source.name)
    queue = tmp_path / "workspace" / "_batch_videos" / "batch_queue.json"
    queue.parent.mkdir(parents=True)
    active = {
        "videos": [{"id": "bv_runningcomic", "status": "running", "guided": True,
                    "operation": {"action": "comic", "active": ["comic"]}}],
    }
    queue.write_text(json.dumps(active), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(source.parent)}
    env.pop("PYTEST_CURRENT_TEST", None)
    subprocess.run(
        [sys.executable, "-c", "import batch_video_engine"],
        cwd=tmp_path, env=env, check=True, capture_output=True, timeout=15,
    )
    assert json.loads(queue.read_text(encoding="utf-8")) == active


def test_burn_in_failure_does_not_publish_a_video(tmp_path, monkeypatch):
    source = tmp_path / "scene.mp4"
    source.write_bytes(b"x" * 1200)
    scene = {
        "index": 1, "status": "done", "video_path": str(source),
        "audio_path": "", "audio_duration": 2, "voiceover": "Lời thoại.",
    }

    def fail_burn(*args, **kwargs):
        assert kwargs["raise_on_error"] is True
        raise RuntimeError("FFmpeg subtitle failure")

    monkeypatch.setattr(composer, "compose_scene_video",
                        lambda scene_video, output, **kwargs: shutil.copyfile(scene_video, output))
    monkeypatch.setattr(composer, "get_media_duration", lambda path: 2.0)
    monkeypatch.setattr(composer, "burn_subtitles", fail_burn)
    with pytest.raises(RuntimeError, match="FFmpeg subtitle failure"):
        composer.compose_full_movie([scene], str(tmp_path), subtitle_mode="burn_in")
    assert not list((tmp_path / "final").iterdir())
    assert not list(tmp_path.glob("_compose_*"))


def test_timed_out_tts_cannot_overwrite_fallback(tmp_path, monkeypatch):
    output = tmp_path / "voice.mp3"
    started, release = threading.Event(), threading.Event()

    def previous_engine(**kwargs):
        started.set()
        assert release.wait(5)
        Path(kwargs["output_path"]).write_bytes(b"late result")
        return 4.0

    async def fallback(text, voice, output_path, **kwargs):
        Path(output_path).write_bytes(b"valid fallback")
        return 2.0

    async def run():
        try:
            duration = await tts.synthesize_voice("Xin chào", "vieneu:TrucLy", str(output), timeout=0.2)
            assert started.is_set()
            assert duration == 2.0
        finally:
            release.set()

    monkeypatch.setattr(tts.VieNeuEngine, "synthesize", previous_engine)
    monkeypatch.setattr(tts, "synthesize_edge_tts", fallback)
    asyncio.run(run())
    assert output.read_bytes() == b"valid fallback"
    assert list(tmp_path.iterdir()) == [output]
