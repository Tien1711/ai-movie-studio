# -*- coding: utf-8 -*-
"""
test_job_lifecycle_audit_2026_11_25.py — Behaviour tests for audit group C
(job lifecycle: #4 cancel-during-compose, #5 anti-overlap gates,
#6 delete/clean-workspace races), verified at main b409486 (2026-11-25).

These tests are RED at b409486 by design: they prove the bug before the
fix and prove the fix after it. They must NOT be weakened to pass by
editing the scenarios — fix app.py instead.

Scope: only app.py job lifecycle. Groups A/B/D/E get their own tests.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import batch_engine
from app import MovieRequest, SceneItem

client = TestClient(app_module.app)


# ── Fixtures / helpers ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    """Mọi test chạy trên WORKSPACE tạm + key giả, không động workspace thật."""
    monkeypatch.setattr(app_module, "WORKSPACE", tmp_path)
    monkeypatch.setattr(app_module, "MUSE_API_KEY", "test-key")
    yield
    for jid in list(app_module.JOBS.keys()):
        if jid.startswith("jobc4"):
            app_module.JOBS.pop(jid, None)
    for jid in list(app_module.RENDER_QUEUE):
        if jid.startswith("jobc4"):
            app_module.RENDER_QUEUE.remove(jid)
    for jid in list(app_module.JOB_TASKS.keys()):
        if jid.startswith("jobc4"):
            t = app_module.JOB_TASKS.pop(jid, None)
            if t is not None and not t.done():
                t.cancel()
    for jid in list(app_module.JOB_CANCEL_EVENTS.keys()):
        if jid.startswith("jobc4"):
            app_module.JOB_CANCEL_EVENTS.pop(jid, None)
    composing = getattr(app_module, "_MANUAL_COMPOSE_JOBS", None)
    if composing is not None:
        for jid in list(composing):
            if jid.startswith("jobc4"):
                composing.discard(jid)


def _make_req(n=1, export_mode="full"):
    return MovieRequest(
        title="Test C",
        scenes=[
            SceneItem(index=i, prompt=f"Prompt canh {i}", voiceover=f"Thoai canh {i}")
            for i in range(1, n + 1)
        ],
        export_mode=export_mode,
    )


def _make_job(job_id, req, status="done", results=None):
    job = {
        "id": job_id,
        "req": req,
        "status": status,
        "progress": 0,
        "total": len(req.scenes),
        "scenes": {},
        "results": list(results or []),
        "failed_scenes": [],
        "logs": [],
        "created_at": 0,
        "cancelled": False,
    }
    for s in req.scenes:
        job["scenes"][str(s.index)] = {
            "status": "pending", "msg": "",
            "prompt": s.prompt, "voiceover": s.voiceover,
        }
    app_module.JOBS[job_id] = job
    return job


def _fake_done_result(tmp_path, idx):
    # audit_movie_scenes chỉ tính "done" khi video_path tồn tại thật (>500B).
    vp = tmp_path / f"scene_{idx}.mp4"
    vp.write_bytes(b"0" * 1200)
    ap = tmp_path / f"scene_{idx}.mp3"
    ap.write_bytes(b"1" * 1200)
    return {
        "index": idx, "status": "done",
        "video_path": str(vp),
        "audio_path": str(ap),
        "audio_duration": 5.0,
    }


def _save_job_dir(job):
    """Ghi job_state.json cho job (như production save_job)."""
    app_module.save_job(job)


# ── #4: Hủy job trong lúc compose ───────────────────────────────────────────

def test_cancel_during_compose_keeps_cancelled(tmp_path, monkeypatch):
    """#4 (full movie): hủy giữa lúc compose_full_movie đang chạy (to_thread)
    thì job phải kết thúc ở cancelled, KHÔNG có final_video."""
    async def _fake_run_batch(**kwargs):
        return [_fake_done_result(tmp_path, s["index"]) for s in kwargs["scenes"]]

    def _slow_compose(**kwargs):
        time.sleep(1.0)
        return {"video_path": str(Path(kwargs["output_dir"]) / "final.mp4")}

    monkeypatch.setattr(app_module, "run_batch", _fake_run_batch)
    monkeypatch.setattr(app_module, "compose_full_movie", _slow_compose)
    req = _make_req(1)
    job = _make_job("jobc4cancel01", req, status="running")

    async def _run():
        task = asyncio.create_task(app_module.film_pipeline(job, req))
        await asyncio.sleep(0.3)  # run_batch fake trả ngay, compose đang ngủ
        # Giả lập api_cancel_job cho job chạy qua hàng đợi (không có trong JOB_TASKS):
        job["cancelled"] = True
        job["status"] = "cancelled"
        ev = app_module.JOB_CANCEL_EVENTS.get(job["id"])
        assert ev is not None
        ev.set()
        await task

    asyncio.run(_run())
    assert job["status"] == "cancelled"
    assert not job.get("final_video")


def test_cancel_during_batch_clips_keeps_cancelled(tmp_path, monkeypatch):
    """#4 (batch_clips): hủy giữa lúc đang ghép clip thứ 2 thì job phải
    cancelled, không được gán done sau khi vòng lặp xong."""
    async def _fake_run_batch(**kwargs):
        return [_fake_done_result(tmp_path, s["index"]) for s in kwargs["scenes"]]

    def _slow_scene_compose(**kwargs):
        time.sleep(0.5)
        return str(kwargs["output"])

    monkeypatch.setattr(app_module, "run_batch", _fake_run_batch)
    monkeypatch.setattr(app_module, "compose_scene_video", _slow_scene_compose)
    req = _make_req(2, export_mode="batch_clips")
    job = _make_job("jobc4cancel02", req, status="running")

    async def _run():
        task = asyncio.create_task(app_module.film_pipeline(job, req))
        await asyncio.sleep(0.8)  # clip 1 xong (0.5s), clip 2 đang ghép
        job["cancelled"] = True
        job["status"] = "cancelled"
        ev = app_module.JOB_CANCEL_EVENTS.get(job["id"])
        assert ev is not None
        ev.set()
        await task

    asyncio.run(_run())
    assert job["status"] == "cancelled"


def test_compose_without_cancel_still_done(tmp_path, monkeypatch):
    """#4 guardrail: job KHÔNG bị hủy thì compose xong vẫn done bình thường."""
    async def _fake_run_batch(**kwargs):
        return [_fake_done_result(tmp_path, s["index"]) for s in kwargs["scenes"]]

    def _fast_compose(**kwargs):
        return {"video_path": str(Path(kwargs["output_dir"]) / "final.mp4")}

    monkeypatch.setattr(app_module, "run_batch", _fake_run_batch)
    monkeypatch.setattr(app_module, "compose_full_movie", _fast_compose)
    req = _make_req(1)
    job = _make_job("jobc4cancel03", req, status="running")
    asyncio.run(app_module.film_pipeline(job, req))
    assert job["status"] == "done"
    assert job.get("final_video")


# ── #5: Gate chống chạy chồng ───────────────────────────────────────────────

def test_retry_single_while_running_returns_409(monkeypatch):
    """#5: retry-single khi job đang running phải trả 409, không spawn chồng."""
    async def _fake_process_scene(**kwargs):
        return {"index": kwargs["scene"]["index"], "status": "error", "error": "mocked"}

    monkeypatch.setattr(batch_engine, "process_scene", _fake_process_scene)
    req = _make_req(1)
    job = _make_job("jobc4run01", req, status="running")
    resp = client.post(f"/api/jobs/{job['id']}/scenes/1/retry", json={})
    assert resp.status_code == 409


def test_retry_single_while_queued_returns_409():
    """#5: retry-single khi job đang chờ trong hàng đợi cũng phải 409
    (film_pipeline sắp chạy sẽ ghi đè kết quả của cảnh đang retry)."""
    req = _make_req(1)
    job = _make_job("jobc4run02", req, status="queued")
    app_module.RENDER_QUEUE.append(job["id"])
    resp = client.post(f"/api/jobs/{job['id']}/scenes/1/retry", json={})
    assert resp.status_code == 409


def test_compose_while_running_returns_409(tmp_path, monkeypatch):
    """#5: compose thủ công khi job đang running phải trả 409, không gọi compose."""
    calls = []
    monkeypatch.setattr(
        app_module, "compose_full_movie",
        lambda **kwargs: calls.append(1) or {"video_path": "/tmp/fake.mp4"},
    )
    req = _make_req(1)
    job = _make_job("jobc4run03", req, status="running",
                    results=[_fake_done_result(tmp_path, 1)])
    resp = client.post(f"/api/jobs/{job['id']}/compose", json={})
    assert resp.status_code == 409
    assert calls == []


def test_compose_while_retrying_returns_409(tmp_path, monkeypatch):
    """#5: compose thủ công khi job đang retry-failed (is_retrying) phải 409."""
    monkeypatch.setattr(
        app_module, "compose_full_movie",
        lambda **kwargs: {"video_path": "/tmp/fake.mp4"},
    )
    req = _make_req(1)
    job = _make_job("jobc4run04", req, status="audit_pending",
                    results=[_fake_done_result(tmp_path, 1)])
    job["is_retrying"] = True
    resp = client.post(f"/api/jobs/{job['id']}/compose", json={})
    assert resp.status_code == 409


def test_compose_while_scene_rerendering_returns_409(tmp_path, monkeypatch):
    """#5: compose thủ công khi có cảnh đang rerendering phải 409."""
    monkeypatch.setattr(
        app_module, "compose_full_movie",
        lambda **kwargs: {"video_path": "/tmp/fake.mp4"},
    )
    req = _make_req(1)
    job = _make_job("jobc4run05", req, status="done",
                    results=[_fake_done_result(tmp_path, 1)])
    job["scenes"]["1"]["rerendering"] = True
    resp = client.post(f"/api/jobs/{job['id']}/compose", json={})
    assert resp.status_code == 409


def test_compose_when_idle_succeeds(tmp_path, monkeypatch):
    """#5 guardrail: compose khi job rảnh vẫn chạy bình thường (200)."""
    monkeypatch.setattr(
        app_module, "compose_full_movie",
        lambda **kwargs: {"video_path": str(tmp_path / "final.mp4")},
    )
    req = _make_req(1)
    job = _make_job("jobc4run06", req, status="done",
                    results=[_fake_done_result(tmp_path, 1)])
    resp = client.post(f"/api/jobs/{job['id']}/compose", json={})
    assert resp.status_code == 200
    assert job.get("final_video")


def test_retry_single_when_idle_succeeds(monkeypatch):
    """#5 guardrail: retry-single khi job rảnh vẫn chạy bình thường (200)."""
    async def _fake_process_scene(**kwargs):
        return {"index": kwargs["scene"]["index"], "status": "error", "error": "mocked"}

    monkeypatch.setattr(batch_engine, "process_scene", _fake_process_scene)
    req = _make_req(1)
    job = _make_job("jobc4run07", req, status="done")
    resp = client.post(f"/api/jobs/{job['id']}/scenes/1/retry", json={})
    assert resp.status_code == 200
    assert resp.json().get("success") is True


def test_spawn_job_task_refuses_second_live_task():
    """#5: _spawn_job_task không được ghi đè handle task cũ khi task còn sống."""
    async def _run():
        async def _slow():
            await asyncio.sleep(5)

        t1 = app_module._spawn_job_task("jobc4spawn01", _slow())
        try:
            t2 = app_module._spawn_job_task("jobc4spawn01", _slow())
            assert t2 is t1
            assert app_module.JOB_TASKS.get("jobc4spawn01") is t1
        finally:
            t1.cancel()
            try:
                await t1
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ── #6: Race xóa job / dọn workspace ────────────────────────────────────────

def test_delete_queued_job_returns_409(tmp_path):
    """#6: xóa job đang chờ trong RENDER_QUEUE phải bị chặn (409),
    job + thư mục còn nguyên."""
    req = _make_req(1)
    job = _make_job("jobc4queue01", req, status="queued")
    _save_job_dir(job)
    app_module.RENDER_QUEUE.append(job["id"])
    job_dir = tmp_path / job["id"]
    assert job_dir.exists()
    resp = client.delete(f"/api/jobs/{job['id']}")
    assert resp.status_code == 409
    assert job["id"] in app_module.JOBS
    assert job_dir.exists()


def test_delete_after_cancel_succeeds(tmp_path):
    """#6 guardrail: hủy trước rồi xóa thì phải xóa được (200)."""
    req = _make_req(1)
    job = _make_job("jobc4queue02", req, status="cancelled")
    job["cancelled"] = True
    _save_job_dir(job)
    job_dir = tmp_path / job["id"]
    assert job_dir.exists()
    resp = client.delete(f"/api/jobs/{job['id']}")
    assert resp.status_code == 200
    assert job["id"] not in app_module.JOBS
    assert not job_dir.exists()


def test_delete_running_job_returns_409():
    """#6 regression: xóa job đang running vẫn bị chặn như cũ (409)."""
    req = _make_req(1)
    job = _make_job("jobc4queue03", req, status="running")
    resp = client.delete(f"/api/jobs/{job['id']}")
    assert resp.status_code == 409
    assert job["id"] in app_module.JOBS


def test_clean_workspace_removes_ghosts(tmp_path):
    """#6: dọn workspace xong thì job đã xóa thư mục không còn trong JOBS
    (không để lại job ma)."""
    req = _make_req(1)
    job1 = _make_job("jobc4clean01", req, status="done")
    job2 = _make_job("jobc4clean02", req, status="done")
    _save_job_dir(job1)
    _save_job_dir(job2)
    assert (tmp_path / job1["id"]).exists()
    assert (tmp_path / job2["id"]).exists()
    resp = client.post("/api/workspace/clean", params={"keep_projects": True})
    assert resp.status_code == 200
    assert resp.json().get("deleted_jobs") == 2
    assert not (tmp_path / job1["id"]).exists()
    assert not (tmp_path / job2["id"]).exists()
    assert job1["id"] not in app_module.JOBS
    assert job2["id"] not in app_module.JOBS


def test_clean_workspace_refuses_when_running(tmp_path):
    """#6 regression: còn job running thì dọn workspace phải 409, không xóa gì."""
    req = _make_req(1)
    job_run = _make_job("jobc4clean03", req, status="running")
    job_done = _make_job("jobc4clean04", req, status="done")
    _save_job_dir(job_done)
    resp = client.post("/api/workspace/clean", params={"keep_projects": True})
    assert resp.status_code == 409
    assert (tmp_path / job_done["id"]).exists()
    assert job_done["id"] in app_module.JOBS


def test_clean_workspace_skips_unknown_dirs(tmp_path):
    """#6: thư mục không có job_state.json khớp job đã biết thì không bị xóa."""
    stray = tmp_path / "straytmpdir"
    stray.mkdir()
    (stray / "notes.txt").write_text("keep me", encoding="utf-8")
    req = _make_req(1)
    job = _make_job("jobc4clean05", req, status="done")
    _save_job_dir(job)
    resp = client.post("/api/workspace/clean", params={"keep_projects": True})
    assert resp.status_code == 200
    assert stray.exists()
    assert not (tmp_path / job["id"]).exists()
    assert job["id"] not in app_module.JOBS
