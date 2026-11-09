"""Hồi quy nhóm 3 (quản lý phim) — lịch sử version project + preset nền tảng.

- Lưu 2 bản khác nhau -> GET /api/projects/{name}/versions liệt kê đủ 2 bản
  -> POST restore bản #1 -> GET project trả đúng nội dung bản #1 (khôi
  phục tuyệt đối: tiêu đề + prompt cảnh bản #1, không còn dấu bản #2).
- Lưu 22 lần -> chỉ giữ tối đa 20 bản, bản #1/#2 (cũ nhất) tự xóa.
- Endpoints versions trả 404 cho project / bản không tồn tại.
- Preset YouTube set quality 1080p: backend chấp nhận "1080p"
  (enum backend: "preview" | "720p" | "1080p", xem app.py MovieRequest).
- GET /api/projects liệt kê kèm versions_count.

Chạy bằng TestClient, không cần Muse key, không gọi API video thật.
Dọn project test sau mỗi test.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import MAX_PROJECT_VERSIONS, PROJECTS_DIR, app

client = TestClient(app)

PROJ_V = "test_g3_versions"
PROJ_CAP = "test_g3_versions_cap"
PROJ_Q = "test_g3_quality_1080p"


def _payload(name, *, title, prompt, quality="720p"):
    return {
        "name": name,
        "title": title,
        "script_text": f"Kịch bản {title}",
        "aspect_ratio": "16:9",
        "voice": "vi-VN-HoaiMyNeural",
        "voice_mode": "tts",
        "export_mode": "full",
        "chain_mode": "none",
        "chain_fallback_mode": "independent",
        "subtitle_mode": "burn_in",
        "quality": quality,
        "transition": "none",
        "bg_music_path": None,
        "music_volume": 0.25,
        "voice_volume": 1.0,
        "consistency_mode": "standard",
        "scenes": [
            {"index": 1, "prompt": prompt, "voiceover": f"Thuyết minh {title}",
             "duration": 5, "size": "16:9"},
        ],
        "characters": [],
        "style_locks": {"style": "", "lighting": "", "color": "",
                        "camera": "", "character": ""},
    }


def _cleanup(name):
    p = Path(PROJECTS_DIR) / f"{name}.json"
    p.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _clean():
    yield
    for n in (PROJ_V, PROJ_CAP, PROJ_Q):
        _cleanup(n)


def test_save_two_versions_restore_first_returns_first_content():
    r1 = client.post("/api/projects/save", json=_payload(
        PROJ_V, title="Bản 1", prompt="prompt-ban-mot"))
    assert r1.status_code == 200, r1.text
    assert r1.json()["version"] == 1
    assert r1.json()["versions_count"] == 1

    r2 = client.post("/api/projects/save", json=_payload(
        PROJ_V, title="Bản 2", prompt="prompt-ban-hai"))
    assert r2.status_code == 200, r2.text
    assert r2.json()["version"] == 2
    assert r2.json()["versions_count"] == 2

    # File project hiện tại là bản 2.
    cur = client.get(f"/api/projects/{PROJ_V}")
    assert cur.status_code == 200
    assert cur.json()["title"] == "Bản 2"
    assert cur.json()["scenes"][0]["prompt"] == "prompt-ban-hai"

    # Liệt kê versions: mới nhất trước.
    vs = client.get(f"/api/projects/{PROJ_V}/versions")
    assert vs.status_code == 200
    versions = vs.json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["title"] == "Bản 1"
    assert versions[1]["scenes_count"] == 1

    # Khôi phục bản #1 -> nội dung hiện tại đúng bản #1 tuyệt đối.
    rr = client.post(f"/api/projects/{PROJ_V}/versions/1/restore")
    assert rr.status_code == 200, rr.text
    assert rr.json()["restored_version"] == 1

    after = client.get(f"/api/projects/{PROJ_V}")
    assert after.status_code == 200
    assert after.json()["title"] == "Bản 1"
    assert after.json()["scenes"][0]["prompt"] == "prompt-ban-mot"
    assert after.json()["script_text"] == "Kịch bản Bản 1"

    # Lịch kê: bản khôi phục được ghi thêm thành version mới (undo được).
    vs2 = client.get(f"/api/projects/{PROJ_V}/versions")
    assert [v["version"] for v in vs2.json()] == [3, 2, 1]
    assert vs2.json()[0]["title"] == "Bản 1"


def test_versions_capped_at_20_oldest_pruned():
    assert MAX_PROJECT_VERSIONS == 20
    for i in range(1, 23):
        r = client.post("/api/projects/save", json=_payload(
            PROJ_CAP, title=f"Bản {i}", prompt=f"prompt-{i}"))
        assert r.status_code == 200, r.text
    vs = client.get(f"/api/projects/{PROJ_CAP}/versions")
    assert vs.status_code == 200
    versions = vs.json()
    assert len(versions) == 20
    # Bản #1, #2 (cũ nhất) đã tự xóa; bản cũ nhất còn lại là #3.
    assert versions[-1]["version"] == 3
    assert versions[0]["version"] == 22
    # Nội dung hiện tại vẫn là bản mới nhất.
    cur = client.get(f"/api/projects/{PROJ_CAP}")
    assert cur.json()["title"] == "Bản 22"


def test_versions_endpoints_404():
    r = client.get("/api/projects/khong_co_project_nay_xyz/versions")
    assert r.status_code == 404
    r = client.post("/api/projects/khong_co_project_nay_xyz/versions/1/restore")
    assert r.status_code == 404

    client.post("/api/projects/save", json=_payload(
        PROJ_V, title="Bản 1", prompt="prompt-ban-mot"))
    r = client.post(f"/api/projects/{PROJ_V}/versions/99/restore")
    assert r.status_code == 404


def test_save_accepts_1080p_quality_for_youtube_preset():
    """Preset YouTube set quality=1080p — backend phải chấp nhận giá trị này."""
    r = client.post("/api/projects/save", json=_payload(
        PROJ_Q, title="Phim YouTube", prompt="prompt-yt", quality="1080p"))
    assert r.status_code == 200, r.text
    cur = client.get(f"/api/projects/{PROJ_Q}")
    assert cur.json()["quality"] == "1080p"


def test_list_projects_includes_versions_count():
    client.post("/api/projects/save", json=_payload(
        PROJ_V, title="Bản 1", prompt="prompt-ban-mot"))
    client.post("/api/projects/save", json=_payload(
        PROJ_V, title="Bản 2", prompt="prompt-ban-hai"))
    items = client.get("/api/projects").json()
    row = next(i for i in items if i["name"] == PROJ_V)
    assert row["versions_count"] == 2
    assert row["title"] == "Bản 2"
