"""
test_perfect_g2_shotdesk_timeline.py — Hồi quy NHÓM 2 / nhiệm vụ 2 (backend):
Timeline kéo-thả + render lại/so sánh A/B cho panel Shot Desk.

- POST /api/shot-desk/projects/{id}/shots/reorder: đổi thứ tự + đổi tên
  file clip/audio/frame theo chỉ số mới + persist, validate 400, 404,
  409 khi đang render.
- POST .../shots/{idx}/render (tái dùng endpoint có sẵn, background=false,
  mock render_single_shot_desk — KHÔNG gọi API video thật): clip cũ được
  lưu sang previous/; GET .../previous-video xem được bản cũ;
  POST .../restore-previous hoán đổi lại; POST .../discard-previous xóa.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

import app as app_module
import shot_desk as shot_desk_module
from shot_desk import get_shot_desk_project, save_shot_desk_project

PID = "test-sd-timeline"


def _seed_project(ws: Path, n: int = 3):
    """Tạo dự án Shot Desk giả trong workspace tạm: project.json + frame
    + clip/audio riêng từng shot (nội dung khác nhau để kiểm tra đổi tên)."""
    proj_dir = ws / PID
    (proj_dir / "shots").mkdir(parents=True, exist_ok=True)
    (proj_dir / "clips").mkdir(parents=True, exist_ok=True)
    (proj_dir / "audio").mkdir(parents=True, exist_ok=True)
    shots = []
    for i in range(1, n + 1):
        shot_dir = proj_dir / "shots" / f"shot_{i:03d}"
        shot_dir.mkdir(parents=True, exist_ok=True)
        (shot_dir / "first_frame.jpg").write_bytes(b"FRAME" + bytes([i]) * 32)
        (proj_dir / "clips" / f"shot_{i:03d}_clip.mp4").write_bytes(
            b"CLIP" + bytes([i]) * 64)
        (proj_dir / "audio" / f"shot_{i:03d}_tts.mp3").write_bytes(
            b"AUDIO" + bytes([i]) * 32)
        shots.append({
            "index": i,
            "title": f"Shot {i}",
            "duration": 10,
            "prompt": f"Prompt shot {i}",
            "voiceover": f"Thoai {i}",
            "voice_mode": "narration",
            "speaker": "",
            "voice": "vi-VN-HoaiMyNeural",
            "lip_sync": False,
            "first_frame": f"shots/shot_{i:03d}/first_frame.jpg",
            "middle_frame": None,
            "last_frame": None,
            "first_frame_url": f"/api/shot-desk/projects/{PID}/shots/{i}/frames/first_frame.jpg",
            "middle_frame_url": None,
            "last_frame_url": None,
            "transition_in": None,
            "transition_out": None,
            "transition_note": "",
            "characters": [],
            "status": "review",
            "error": None,
            "video_url": f"/api/shot-desk/projects/{PID}/shots/{i}/video",
            "audio_url": f"/api/shot-desk/projects/{PID}/shots/{i}/audio",
            "used_prompt": None,
            "used_compliance": False,
            "has_middle_frame": False,
            "audit": {},
        })
    proj = {
        "id": PID,
        "format_version": "1.0",
        "title": "Phim test timeline",
        "genre": "Phim ngắn",
        "logline": "",
        "aspect_ratio": "9:16",
        "default_voice": "vi-VN-HoaiMyNeural",
        "default_voice_mode": "narration",
        "total_duration": n * 10,
        "shot_duration": 10,
        "characters": [],
        "shots": shots,
        "created_at": 0,
        "updated_at": 0,
        "final_video": None,
        "srt_file": None,
        "timeline_settings": {},
    }
    save_shot_desk_project(proj)
    return proj_dir


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Workspace Shot Desk tạm — không đụng workspace thật."""
    monkeypatch.setattr(shot_desk_module, "SHOT_DESK_DIR", tmp_path)
    monkeypatch.setattr(app_module, "SHOT_DESK_DIR", tmp_path)
    app_module.ACTIVE_SHOT_TASKS.clear()
    app_module.ACTIVE_SHOT_DESK_COMPOSE.clear()
    yield tmp_path
    app_module.ACTIVE_SHOT_TASKS.clear()
    app_module.ACTIVE_SHOT_DESK_COMPOSE.clear()


def _client():
    return TestClient(app_module.app)


def _shot(client, idx):
    proj = client.get(f"/api/shot-desk/projects/{PID}").json()
    return next(s for s in proj["shots"] if s["index"] == idx)


# ── Reorder ────────────────────────────────────────────────────────────────

def test_reorder_changes_order_renames_files_and_persists(ws):
    proj_dir = _seed_project(ws)
    old_clip3 = (proj_dir / "clips" / "shot_003_clip.mp4").read_bytes()
    with _client() as client:
        r = client.post(f"/api/shot-desk/projects/{PID}/shots/reorder",
                        json={"order": [3, 1, 2]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["success"] is True
    assert data["order"] == [3, 1, 2]
    assert data["final_stale"] is True
    assert [s["index"] for s in data["shots"]] == [1, 2, 3]
    assert data["shots"][0]["prompt"] == "Prompt shot 3"
    assert data["shots"][1]["prompt"] == "Prompt shot 1"
    # File clip/audio/frame đi theo shot cũ: clip cũ 003 giờ là 001.
    assert (proj_dir / "clips" / "shot_001_clip.mp4").read_bytes() == old_clip3
    assert (proj_dir / "audio" / "shot_001_tts.mp3").read_bytes() == b"AUDIO" + bytes([3]) * 32
    assert (proj_dir / "shots" / "shot_001" / "first_frame.jpg").read_bytes() == b"FRAME" + bytes([3]) * 32
    # URL frame/video của shot mới 1 trỏ đúng chỉ số mới.
    with _client() as client:
        s1 = _shot(client, 1)
    assert s1["first_frame"] == "shots/shot_001/first_frame.jpg"
    assert s1["first_frame_url"] == f"/api/shot-desk/projects/{PID}/shots/1/frames/first_frame.jpg"
    assert s1["video_url"] == f"/api/shot-desk/projects/{PID}/shots/1/video"
    # Persist sau khi đọc lại từ đĩa.
    reloaded = get_shot_desk_project(PID)
    assert [s["prompt"] for s in reloaded["shots"]] == [
        "Prompt shot 3", "Prompt shot 1", "Prompt shot 2"]


def test_reorder_invalid_and_not_found(ws):
    _seed_project(ws)
    with _client() as client:
        assert client.post(f"/api/shot-desk/projects/{PID}/shots/reorder",
                           json={"order": [1, 2]}).status_code == 400
        assert client.post(f"/api/shot-desk/projects/{PID}/shots/reorder",
                           json={"order": [1, 1, 3]}).status_code == 400
        assert client.post(f"/api/shot-desk/projects/{PID}/shots/reorder",
                           json={"order": [1, 2, 99]}).status_code == 400
        assert client.post("/api/shot-desk/projects/khongco/shots/reorder",
                           json={"order": [2, 1, 3]}).status_code == 404


def test_reorder_blocked_while_rendering(ws):
    _seed_project(ws)
    app_module.ACTIVE_SHOT_TASKS[PID] = {2}
    with _client() as client:
        r = client.post(f"/api/shot-desk/projects/{PID}/shots/reorder",
                        json={"order": [2, 1, 3]})
    assert r.status_code == 409, r.text


# ── Render lại + A/B ───────────────────────────────────────────────────────

def test_render_saves_previous_restore_and_discard(ws, monkeypatch):
    proj_dir = _seed_project(ws)
    old_clip = (proj_dir / "clips" / "shot_002_clip.mp4").read_bytes()
    old_audio = (proj_dir / "audio" / "shot_002_tts.mp3").read_bytes()

    async def _fake_render(project_id, shot_index, **kw):
        # Giả lập render xong: ghi clip MỚI đè lên clip cũ + cập nhật trạng thái.
        (proj_dir / "clips" / f"shot_{shot_index:03d}_clip.mp4").write_bytes(
            b"NEWCLIP" + bytes([shot_index]) * 64)
        proj = get_shot_desk_project(project_id)
        shot = next(s for s in proj["shots"] if s["index"] == shot_index)
        shot["status"] = "review"
        shot["video_url"] = f"/api/shot-desk/projects/{project_id}/shots/{shot_index}/video"
        save_shot_desk_project(proj)
        return shot

    monkeypatch.setattr(app_module, "render_single_shot_desk", _fake_render)
    with _client() as client:
        r = client.post(f"/api/shot-desk/projects/{PID}/shots/2/render",
                        json={"background": False})
        assert r.status_code == 200, r.text
        assert r.json()["previous_clip"] is not None
        assert r.json()["previous_clip"]["has_video"] is True
        # Bản cũ đã sao chép sang previous/ trước khi render đè.
        assert (proj_dir / "previous" / "shot_002_clip.mp4").read_bytes() == old_clip
        assert (proj_dir / "previous" / "shot_002_tts.mp3").read_bytes() == old_audio
        assert (proj_dir / "clips" / "shot_002_clip.mp4").read_bytes() != old_clip
        # Xem trước bản cũ qua endpoint previous-video.
        pv = client.get(f"/api/shot-desk/projects/{PID}/shots/2/previous-video")
        assert pv.status_code == 200
        assert pv.content == old_clip
        # Quay lại bản cũ: clip hiện tại trở lại bản cũ, bản mới thành bản cũ.
        rr = client.post(f"/api/shot-desk/projects/{PID}/shots/2/restore-previous")
        assert rr.status_code == 200, rr.text
        assert rr.json()["prompt"] == "Prompt shot 2"
        assert (proj_dir / "clips" / "shot_002_clip.mp4").read_bytes() == old_clip
        assert (proj_dir / "previous" / "shot_002_clip.mp4").exists()
        # Giữ bản (hiện tại = bản cũ đã khôi phục): xóa bản previous còn lại.
        dd = client.post(f"/api/shot-desk/projects/{PID}/shots/2/discard-previous")
        assert dd.status_code == 200, dd.text
        assert client.get(f"/api/shot-desk/projects/{PID}/shots/2/previous-video").status_code == 404
        assert _shot(client, 2).get("previous_clip") is None


def test_previous_endpoints_validate_404(ws):
    _seed_project(ws)
    with _client() as client:
        assert client.post(f"/api/shot-desk/projects/{PID}/shots/1/restore-previous").status_code == 404
        assert client.post(f"/api/shot-desk/projects/{PID}/shots/1/discard-previous").status_code == 404
        assert client.get(f"/api/shot-desk/projects/{PID}/shots/1/previous-video").status_code == 404
        assert client.post("/api/shot-desk/projects/khongco/shots/1/restore-previous").status_code == 404
