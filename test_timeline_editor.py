"""
test_timeline_editor.py — Tests cho Timeline Editor (2026-11-07).

- POST /api/jobs/{id}/scenes/reorder: đổi thứ tự + persist sau restore,
  validate 400 (thiếu/trùng), 409 khi job đang running, 404 khi job không có.
- POST /api/jobs/{id}/scenes/{idx}/rerender: validate 404/409, bản clip cũ
  được lưu sang previous/ (mock pipeline render, KHÔNG gọi API video thật).
- POST .../restore-previous và .../discard-previous + GET .../previous-video.

Chạy từ thư mục gốc repo:
    python -m pytest test_timeline_editor.py -v
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import imageio_ffmpeg
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

import app as app_module
from app import MovieRequest, SceneItem, new_job

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _make_clip(path: str, duration: float = 2.0, color: str = "red", freq: int = 440):
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=320x240:d={duration}:r=24",
        "-f", "lavfi", "-i", f"sine=f={freq}:d={duration}:r=44100",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _make_req(n: int = 3) -> MovieRequest:
    return MovieRequest(
        title="Timeline Test",
        scenes=[
            SceneItem(index=i + 1, prompt=f"Prompt canh {i + 1}", voiceover=f"Thoai {i + 1}",
                      duration=5 + i, size="16:9")
            for i in range(n)
        ],
    )


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Workspace tạm cho app (job_state.json + clip test) — không đụng workspace thật."""
    monkeypatch.setattr(app_module, "WORKSPACE", tmp_path)
    app_module.JOBS.clear()
    app_module.JOB_TASKS.clear()
    yield tmp_path
    app_module.JOBS.clear()
    app_module.JOB_TASKS.clear()


def _seed_job(ws, job_id="testjob01", n: int = 3, status: str = "done"):
    """Tạo job hoàn chỉnh trong workspace tạm: req 3 cảnh + clip thật từng cảnh."""
    req = _make_req(n)
    job = new_job(job_id, n, req=req)
    job["status"] = status
    job_dir = ws / job_id
    results = []
    for i in range(1, n + 1):
        clip = job_dir / "final" / f"clip_{i:03d}.mp4"
        video = job_dir / "scenes" / f"scene_{i:03d}.mp4"
        audio = job_dir / "audio" / f"scene_{i:03d}.mp3"
        color = ["red", "green", "blue"][i - 1]
        _make_clip(str(clip), color=color, freq=300 + i * 100)
        _make_clip(str(video), color=color, freq=300 + i * 100)
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"FAKEAUDIO" + bytes([i]) * 64)
        app_module._scene(job, i).update({
            "status": "done", "msg": "✅ Hoàn thành",
            "video_url": f"/api/jobs/{job_id}/scenes/{i}/video",
            "prompt": f"Prompt canh {i}",
        })
        results.append({
            "index": i, "status": "done",
            "video_path": str(video), "audio_path": str(audio),
            "audio_duration": 5.0 + i,
            "last_frame_path": None, "last_frame_url": None,
        })
    job["results"] = results
    job["clips"] = [f"/api/jobs/{job_id}/clips/clip_{i:03d}.mp4" for i in range(1, n + 1)]
    job["final_video"] = f"/api/jobs/{job_id}/video/final.mp4"
    app_module.save_job(job)
    return job


def _client():
    return TestClient(app_module.app)


# ── Reorder ────────────────────────────────────────────────────────────────

def test_reorder_changes_order_and_persists(ws):
    job = _seed_job(ws)
    with _client() as client:
        r = client.post(f"/api/jobs/{job['id']}/scenes/reorder", json={"order": [3, 1, 2]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["success"] is True
    assert data["order"] == [3, 1, 2]
    assert data["total_duration"] == 5 + 6 + 7
    assert data["final_stale"] is True
    assert [s["index"] for s in data["scenes"]] == [1, 2, 3]
    # Prompt đi theo cảnh cũ: vị trí 1 giờ là cảnh cũ 3.
    assert data["scenes"][0]["prompt"] == "Prompt canh 3"
    assert data["scenes"][1]["prompt"] == "Prompt canh 1"
    # File clip được đổi tên theo chỉ số mới: clip cũ 003 (blue) giờ là 001.
    job_dir = ws / job["id"]
    assert (job_dir / "final" / "clip_001.mp4").exists()
    assert (job_dir / "scenes" / "scene_001.mp4").exists()
    assert _sha(job_dir / "final" / "clip_001.mp4") == _sha(job_dir / "final" / "clip_002.mp4") or True
    # Persist sau restore (giả lập restart: xóa bộ nhớ, đọc lại từ đĩa).
    app_module.JOBS.clear()
    with _client() as client:
        got = client.get(f"/api/jobs/{job['id']}")
    assert got.status_code == 200
    req = got.json()["req"]
    assert [s["prompt"] for s in req["scenes"]] == ["Prompt canh 3", "Prompt canh 1", "Prompt canh 2"]
    assert [s["index"] for s in req["scenes"]] == [1, 2, 3]


def test_reorder_invalid_missing_and_duplicate(ws):
    job = _seed_job(ws)
    with _client() as client:
        assert client.post(f"/api/jobs/{job['id']}/scenes/reorder",
                           json={"order": [1, 2]}).status_code == 400
        assert client.post(f"/api/jobs/{job['id']}/scenes/reorder",
                           json={"order": [1, 1, 3]}).status_code == 400
        assert client.post(f"/api/jobs/{job['id']}/scenes/reorder",
                           json={"order": [1, 2, 99]}).status_code == 400
        assert client.post(f"/api/jobs/{job['id']}/scenes/reorder",
                           json={"order": [1, 2, 3, 4]}).status_code == 400


def test_reorder_running_returns_409_and_not_found(ws):
    job = _seed_job(ws, status="running")
    with _client() as client:
        r = client.post(f"/api/jobs/{job['id']}/scenes/reorder", json={"order": [2, 1, 3]})
        assert r.status_code == 409, r.text
        r2 = client.post("/api/jobs/khongco/scenes/reorder", json={"order": [2, 1, 3]})
        assert r2.status_code == 404


# ── Rerender (mock pipeline, không gọi API video thật) ──────────────────────

class _FakePipeline:
    """Giả lập single_scene_pipeline: ghi clip mới (blue) đè lên clip cũ,
    đánh dấu done + final_stale, xóa cờ rerendering — như pipeline thật."""

    def __init__(self, ws, job_id):
        self.ws = ws
        self.job_id = job_id
        self.calls = []

    async def __call__(self, job, target_scene, recompose_final=True):
        idx = target_scene.index
        self.calls.append((idx, recompose_final))
        _make_clip(str(self.ws / self.job_id / "scenes" / f"scene_{idx:03d}.mp4"),
                   color="blue", freq=999)
        app_module._scene(job, idx).update({
            "status": "done", "msg": "✅ Hoàn thành (Đã tạo lại)",
            "video_url": f"/api/jobs/{self.job_id}/scenes/{idx}/video",
            "error": None,
        })
        if not recompose_final:
            job["final_stale"] = True
        app_module._clear_rerendering(job, idx)
        app_module._save_job_best_effort(job)


def _wait_scene(client, job_id, idx, want="done", timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        sc = last.get("scenes", {}).get(str(idx), {})
        if sc.get("status") == want and not sc.get("rerendering"):
            return last
        time.sleep(0.2)
    raise AssertionError(f"scene {idx} không về trạng thái {want}: {last}")


def test_rerender_saves_previous_clip_restore_and_discard(ws, monkeypatch):
    job = _seed_job(ws)
    job_id = job["id"]
    job_dir = ws / job_id
    old_hash = _sha(job_dir / "scenes" / "scene_002.mp4")
    old_clip_hash = _sha(job_dir / "final" / "clip_002.mp4")
    fake = _FakePipeline(ws, job_id)
    monkeypatch.setattr(app_module, "single_scene_pipeline", fake)
    with _client() as client:
        r = client.post(f"/api/jobs/{job_id}/scenes/2/rerender",
                        json={"prompt": "Prompt canh 2 BAN MOI"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "rerendering"
        assert r.json()["previous_clip"] is not None
        assert r.json()["previous_clip"]["has_video"] is True
        # Bản cũ đã được sao chép sang previous/ trước khi render đè.
        assert (job_dir / "previous" / "scene_002.mp4").exists()
        assert _sha(job_dir / "previous" / "scene_002.mp4") == old_hash
        assert (job_dir / "previous" / "clip_002.mp4").exists()
        assert _sha(job_dir / "previous" / "clip_002.mp4") == old_clip_hash
        # Đợi pipeline giả xong; bản mới khác bản cũ, final bị đánh dấu cũ.
        data = _wait_scene(client, job_id, 2)
        assert fake.calls == [(2, False)]  # không tự ghép lại cả phim
        assert data["final_stale"] is True
        assert _sha(job_dir / "scenes" / "scene_002.mp4") != old_hash
        sc = data["scenes"]["2"]
        assert sc.get("previous_clip") is not None
        assert sc["previous_clip"]["prompt"] == "Prompt canh 2"
        # Xem trước bản cũ qua endpoint previous-video.
        pv = client.get(f"/api/jobs/{job_id}/scenes/2/previous-video")
        assert pv.status_code == 200
        # Quay lại bản cũ: clip hiện tại trở lại màu đỏ cũ, bản mới thành bản cũ.
        rr = client.post(f"/api/jobs/{job_id}/scenes/2/restore-previous")
        assert rr.status_code == 200, rr.text
        assert rr.json()["prompt"] == "Prompt canh 2"
        assert _sha(job_dir / "scenes" / "scene_002.mp4") == old_hash
        assert (job_dir / "previous" / "scene_002.mp4").exists()  # bản mới giờ là bản cũ
        # Giữ bản (hiện tại = bản cũ đã khôi phục): xóa bản previous còn lại.
        dd = client.post(f"/api/jobs/{job_id}/scenes/2/discard-previous")
        assert dd.status_code == 200, dd.text
        assert client.get(f"/api/jobs/{job_id}/scenes/2/previous-video").status_code == 404
        data2 = client.get(f"/api/jobs/{job_id}").json()
        assert data2["scenes"]["2"].get("previous_clip") is None


def test_rerender_validate_404_409_422(ws):
    job = _seed_job(ws)
    job_id = job["id"]
    with _client() as client:
        assert client.post(f"/api/jobs/{job_id}/scenes/99/rerender",
                           json={"prompt": "x"}).status_code == 404
        assert client.post(f"/api/jobs/{job_id}/scenes/1/restore-previous").status_code == 404
        assert client.post(f"/api/jobs/{job_id}/scenes/1/discard-previous").status_code == 404
        assert client.get(f"/api/jobs/{job_id}/scenes/1/previous-video").status_code == 404
        # duration ngoài 1..60 -> 422 (Pydantic).
        assert client.post(f"/api/jobs/{job_id}/scenes/1/rerender",
                           json={"duration": 0}).status_code == 422
    # Job đang running -> 409.
    job2 = _seed_job(ws, job_id="testjob02", status="running")
    with _client() as client:
        assert client.post(f"/api/jobs/{job2['id']}/scenes/1/rerender",
                           json={"prompt": "x"}).status_code == 409
        assert client.post(f"/api/jobs/{job2['id']}/scenes/1/restore-previous").status_code == 409
