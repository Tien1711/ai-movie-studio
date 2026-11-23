"""Test cho Xưởng tự động (Auto Factory).

- Parse RSS trending/news với http mock (không gọi mạng thật).
- Validation POST /api/factory/order (mock BatchVideoManager, không render thật).
- build_publish_pack: ZIP đủ 6 file, thumbnail đúng kích thước, metadata đúng.
- Style weaving: prompt ảnh chứa suffix style tương ứng.
- Panel + JS factory có mặt trong index.html / app.js / ui.js.

Chạy bằng TestClient, không cần Muse key, không gọi API video thật.
"""
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as app_module
import auto_factory
from auto_factory import (
    FACTORY_STYLE_IDS,
    build_publish_pack,
    factory_styles,
    fetch_topics,
)
from batch_video_engine import BATCH_VISUAL_STYLES, generate_batch_script_local
from composer import FFMPEG

client = TestClient(app_module.app)
ROOT = Path(__file__).resolve().parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")

TRENDING_URL = "https://trends.google.com/trends/trendingsearches/daily/rss?geo=VN"
VNEXPRESS_URL = "https://vnexpress.net/rss/tin-moi-nhat.rss"
TUOITRE_URL = "https://tuoitre.vn/rss/tin-moi-nhat.rss"


# ── Fixture: cách ly cache RSS + thư mục batch khỏi workspace thật ────────
@pytest.fixture(autouse=True)
def _isolate_factory_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_factory, "FACTORY_CACHE_DIR", tmp_path / "_factory_cache")
    monkeypatch.setattr(auto_factory, "BATCH_DIR", tmp_path / "batch_videos")


def _rss_xml(items):
    body = "".join(
        f"<item><title>{t}</title><link>https://example.com/{i}</link></item>"
        for i, t in enumerate(items)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss version=\"2.0\"><channel><title>Test</title>{body}</channel></rss>"
    )


class _FakeResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


def _make_clip(path, w=720, h=1280, dur=2.0):
    cmd = [
        FFMPEG, "-y", "-f", "lavfi", "-i",
        f"testsrc=size={w}x{h}:rate=30,format=yuv420p",
        "-t", str(dur), "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "23", "-pix_fmt", "yuv420p", "-an", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return path


def _fake_video_dict(status="completed"):
    return {
        "id": "vid-1",
        "title": "Video test xưởng",
        "status": status,
        "progress": 100 if status == "completed" else 0,
        "scenes_count": 5,
        "aspect_ratio": "9:16",
        "visual_style": "cinematic",
        "voice": "vi-VN-HoaiMyNeural",
        "created_at": "2026-11-23T10:00:00",
        "bundle": {
            "title": "Video test xưởng ✨",
            "caption": "Mô tả video test.",
            "hashtags": ["#fyp", "#xuhuong", "#test"],
        },
        "scenes": [{"index": i} for i in range(5)],
    }


class _FakeBatchManager:
    """Giả lập tối thiểu BatchVideoManager cho test publish pack."""

    def __init__(self, video):
        self.video = video

    def get_video(self, video_id):
        if self.video and video_id == self.video["id"]:
            return self.video
        return None


@pytest.fixture()
def _completed_video(tmp_path, monkeypatch):
    """Tạo 1 video 'completed' giả trong BATCH_DIR tạm (file thật để ffmpeg trích thumbnail)."""
    vdir = auto_factory.BATCH_DIR / "vid-1"
    vdir.mkdir(parents=True)
    _make_clip(vdir / "final.mp4")
    (vdir / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nXin chào.\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        auto_factory, "BATCH_MANAGER", _FakeBatchManager(_fake_video_dict("completed"))
    )
    return "vid-1"


# ── 1. Parse RSS trending / news ───────────────────────────────────────────
def test_fetch_trending_parses_10_dedupes_and_cleans():
    items = [f"Chủ đề hot {i}" for i in range(12)]
    items.append("Chủ đề hot 0")  # trùng -> phải khử trùng
    items.append("<b>Tiêu đề có tag</b> &amp; ký tự")  # HTML + entity -> làm sạch
    result = fetch_topics(
        "trending",
        http_get=lambda url: _FakeResp(_rss_xml(items)),
        use_cache=False,
    )
    assert result["source"] == "trending"
    assert result["fallback"] is False
    assert len(result["topics"]) == 10  # tối đa 10
    assert result["topics"][0] == "Chủ đề hot 0"
    assert len(set(result["topics"])) == len(result["topics"])
    assert not any("<" in t or "&amp;" in t for t in result["topics"])


def test_fetch_news_falls_back_to_tuoitre_when_vnexpress_fails():
    def http_get(url):
        if "vnexpress" in url:
            raise RuntimeError("feed chính lỗi")
        assert "tuoitre" in url
        return _FakeResp(_rss_xml(["Tin Tuổi Trẻ 1", "Tin Tuổi Trẻ 2"]))

    result = fetch_topics("news", http_get=http_get, use_cache=False)
    assert result["fallback"] is False
    assert result["topics"] == ["Tin Tuổi Trẻ 1", "Tin Tuổi Trẻ 2"]


def test_fetch_all_feeds_fail_returns_pool_with_fallback_flag():
    def http_get(url):
        raise RuntimeError("mất mạng")

    result = fetch_topics("trending", http_get=http_get, use_cache=False)
    assert result["fallback"] is True
    assert len(result["topics"]) > 0
    assert len(result["topics"]) <= 10


def test_fetch_cache_hit_skips_network():
    calls = {"n": 0}

    def http_get_ok(url):
        calls["n"] += 1
        return _FakeResp(_rss_xml(["Chủ đề A", "Chủ đề B"]))

    first = fetch_topics("trending", http_get=http_get_ok, use_cache=True)
    assert first["cached"] is False
    assert calls["n"] == 1

    def http_get_boom(url):
        raise RuntimeError("không được gọi mạng nữa")

    second = fetch_topics("trending", http_get=http_get_boom, use_cache=True)
    assert second["cached"] is True
    assert second["topics"] == ["Chủ đề A", "Chủ đề B"]
    assert calls["n"] == 1


def test_fetch_bad_source_raises():
    with pytest.raises(ValueError):
        fetch_topics("youtube", http_get=lambda url: _FakeResp(""), use_cache=False)


def test_factory_styles_lists_11():
    styles = factory_styles()
    assert len(styles) == 11
    assert [s["id"] for s in styles] == FACTORY_STYLE_IDS
    assert all(s["name"] and s["prompt_suffix"] for s in styles)
    assert set(FACTORY_STYLE_IDS) <= set(BATCH_VISUAL_STYLES)


# ── 2. Validation POST /api/factory/order ─────────────────────────────────
class _FakeOrderManager:
    """Giả lập BatchVideoManager: add_batch nhận LIST prompt, trả LIST item
    (đúng hợp đồng thật của BatchVideoManager.add_batch)."""

    def __init__(self):
        self.calls = []
        self.updated = []

    def add_batch(self, prompts=None, **kwargs):
        self.calls.append({"prompts": list(prompts or []), **kwargs})
        return [
            {"id": f"vid-{i + 1}", "title": p, "prompt": p, "status": "queued"}
            for i, p in enumerate(prompts or [])
        ]

    def update_video(self, video_id, fields):
        self.updated.append((video_id, fields))
        return True


@pytest.fixture()
def _mock_order_backend(monkeypatch):
    fake = _FakeOrderManager()
    monkeypatch.setattr(app_module, "BATCH_MANAGER", fake)
    monkeypatch.setattr(
        auto_factory,
        "fetch_topics",
        lambda source, **kw: {
            "source": source,
            "topics": [f"Chủ đề {i}" for i in range(1, 8)],
            "fallback": False,
            "cached": False,
        },
    )
    return fake


def _order_payload(**over):
    payload = {
        "source": "trending",
        "count": 3,
        "scenes_count": 5,
        "visual_style": "cinematic",
        "voice": "vi-VN-HoaiMyNeural",
        "aspect_ratio": "9:16",
    }
    payload.update(over)
    return payload


def test_order_count_zero_rejected_422(_mock_order_backend):
    r = client.post("/api/factory/order", json=_order_payload(count=0))
    assert r.status_code == 422


def test_order_count_11_rejected_422(_mock_order_backend):
    r = client.post("/api/factory/order", json=_order_payload(count=11))
    assert r.status_code == 422


def test_order_scenes_count_2_rejected_422(_mock_order_backend):
    r = client.post("/api/factory/order", json=_order_payload(scenes_count=2))
    assert r.status_code == 422


def test_order_bad_source_rejected_422(_mock_order_backend):
    r = client.post("/api/factory/order", json=_order_payload(source="youtube"))
    assert r.status_code == 422


def test_order_manual_missing_topics_rejected_422(_mock_order_backend):
    r = client.post("/api/factory/order", json=_order_payload(source="manual"))
    assert r.status_code == 422


def test_order_manual_too_many_topics_rejected_422(_mock_order_backend):
    r = client.post(
        "/api/factory/order",
        json=_order_payload(source="manual", topics=[f"T{i}" for i in range(11)]),
    )
    assert r.status_code == 422


def test_order_trending_creates_exact_count_with_auto_run(_mock_order_backend):
    fake = _mock_order_backend
    r = client.post(
        "/api/factory/order",
        json=_order_payload(count=3, visual_style="donghua", scenes_count=4),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 3
    assert data["requested"] == 3
    assert len(data["created"]) == 3  # "created" là danh sách video, "total" là số lượng
    assert data["estimated_seconds"] == 3 * 4 * 25
    assert len(fake.calls) == 1  # 1 lần gọi add_batch với cả danh sách prompt
    call = fake.calls[0]
    assert call["auto_run"] is True
    assert call["visual_style"] == "donghua"
    assert call["scenes_count"] == 4
    assert call["prompts"] == ["Chủ đề 1", "Chủ đề 2", "Chủ đề 3"]
    assert [c["id"] for c in data["created"]] == ["vid-1", "vid-2", "vid-3"]
    assert len(fake.updated) == 3
    assert all(f.get("factory_source") == "trending" for _, f in fake.updated)


def test_order_manual_uses_given_topics(_mock_order_backend):
    fake = _mock_order_backend
    r = client.post(
        "/api/factory/order",
        json=_order_payload(
            source="manual", topics=["Chuyện ma Đà Lạt", "Bài học người cha"]
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 2
    assert fake.calls[0]["prompts"] == [
        "Chuyện ma Đà Lạt",
        "Bài học người cha",
    ]


def test_topics_endpoint_bad_source_422():
    r = client.get("/api/factory/topics", params={"source": "youtube"})
    assert r.status_code == 422


def test_styles_endpoint_lists_11():
    r = client.get("/api/factory/styles")
    assert r.status_code == 200
    styles = r.json()["styles"]  # endpoint bọc trong {"styles": [...]}
    assert len(styles) == 11
    assert [s["id"] for s in styles] == FACTORY_STYLE_IDS


# ── 3. build_publish_pack + endpoint ───────────────────────────────────────
def test_build_publish_pack_zip_contents(_completed_video):
    pack = build_publish_pack("vid-1")
    assert pack.exists()
    with zipfile.ZipFile(pack) as zf:
        names = set(zf.namelist())
    assert names == {
        "final.mp4",
        "thumbnail.jpg",
        "title.txt",
        "description.txt",
        "subtitles.srt",
        "metadata.json",
    }
    with zipfile.ZipFile(pack) as zf:
        meta = json.loads(zf.read("metadata.json").decode("utf-8"))
        title = zf.read("title.txt").decode("utf-8")
        desc = zf.read("description.txt").decode("utf-8")
        thumb_bytes = zf.read("thumbnail.jpg")
    assert meta["video_id"] == "vid-1"
    assert meta["title"] == "Video test xưởng"  # title gốc của video
    assert meta["viral_title"] == "Video test xưởng ✨"  # title viral từ bundle
    assert meta["scenes"] == 5
    assert meta["aspect_ratio"] == "9:16"
    assert meta["duration_s"] > 0
    assert "Video test xưởng ✨" in title  # title.txt = tiêu đề viral
    assert "#fyp" in desc and "#xuhuong" in desc

    thumb_path = Path(pack).parent / "_thumb_check.jpg"
    thumb_path.write_bytes(thumb_bytes)
    with Image.open(thumb_path) as img:
        assert img.size == (720, 1280)  # 9:16 -> 720x1280
        assert img.format == "JPEG"


def test_build_publish_pack_reuses_fresh_zip(_completed_video):
    pack1 = build_publish_pack("vid-1")
    mtime1 = pack1.stat().st_mtime
    pack2 = build_publish_pack("vid-1")
    assert pack2 == pack1
    assert pack2.stat().st_mtime == mtime1  # không ghi lại ZIP khi còn mới


def test_build_publish_pack_missing_video_raises(_completed_video):
    with pytest.raises(FileNotFoundError):
        build_publish_pack("khong-co-video")


def test_build_publish_pack_not_completed_raises(tmp_path, monkeypatch):
    vdir = auto_factory.BATCH_DIR / "vid-2"
    vdir.mkdir(parents=True)
    _make_clip(vdir / "final.mp4")
    monkeypatch.setattr(
        auto_factory, "BATCH_MANAGER", _FakeBatchManager(_fake_video_dict("queued"))
    )
    # Video trong fake manager có id vid-1 nhưng thư mục là vid-2 -> đổi id cho khớp
    auto_factory.BATCH_MANAGER.video["id"] = "vid-2"
    with pytest.raises(ValueError, match="chưa hoàn tất"):
        build_publish_pack("vid-2")


def test_publish_pack_endpoint_200_and_409(tmp_path, monkeypatch):
    # Video hoàn tất -> 200 ZIP
    vdir = auto_factory.BATCH_DIR / "vid-1"
    vdir.mkdir(parents=True)
    _make_clip(vdir / "final.mp4")
    (vdir / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nHi.\n")
    monkeypatch.setattr(
        auto_factory, "BATCH_MANAGER", _FakeBatchManager(_fake_video_dict("completed"))
    )
    r = client.get("/api/factory/publish-pack/vid-1")
    assert r.status_code == 200, r.text
    assert "zip" in r.headers["content-type"]

    # Video chưa hoàn tất -> 409
    auto_factory.BATCH_MANAGER.video["status"] = "queued"
    r2 = client.get("/api/factory/publish-pack/vid-1")
    assert r2.status_code == 409

    # Video không tồn tại -> 404
    r3 = client.get("/api/factory/publish-pack/khong-co")
    assert r3.status_code == 404


def test_thumbnail_endpoint(_completed_video):
    r = client.get("/api/factory/thumbnail/vid-1")
    assert r.status_code == 200, r.text
    assert "image/jpeg" in r.headers["content-type"]
    assert len(r.content) > 1000

    r2 = client.get("/api/factory/thumbnail/khong-co")
    assert r2.status_code == 404


# ── 4. Style weaving: prompt ảnh chứa suffix style ─────────────────────────
@pytest.mark.parametrize("style_id", FACTORY_STYLE_IDS)
def test_style_weaving_in_image_prompts(style_id):
    script = generate_batch_script_local(
        "Một câu chuyện về lòng dũng cảm",
        scenes_count=3,
        visual_style=style_id,
        aspect_ratio="9:16",
    )
    suffix = BATCH_VISUAL_STYLES[style_id]["prompt_suffix"]
    assert len(script["scenes"]) == 3
    assert all(suffix in s["prompt"] for s in script["scenes"]), (
        f"style {style_id}: prompt ảnh thiếu suffix"
    )


# ── 5. Panel + JS factory có mặt trong frontend ────────────────────────────
def test_factory_panel_in_index_html():
    assert 'id="panel-factory"' in INDEX_HTML
    assert 'data-panel="factory"' in INDEX_HTML
    assert "Bắt đầu sản xuất" in INDEX_HTML
    assert 'id="factoryVoiceSelect"' in INDEX_HTML
    assert 'id="factoryTopicsList"' in INDEX_HTML
    assert 'id="factoryPublishModal"' in INDEX_HTML
    assert 'id="factoryJobsList"' in INDEX_HTML
    for style_id in FACTORY_STYLE_IDS:
        assert f'value="{style_id}"' in INDEX_HTML


def test_factory_js_functions_present():
    for fn in [
        "initFactoryPanel",
        "setFactorySource",
        "setFactoryRatio",
        "loadFactoryTopics",
        "updateFactoryEstimate",
        "submitFactoryOrder",
        "loadFactoryQueue",
        "renderFactoryItemHtml",
        "openFactoryPublishModal",
        "closeFactoryPublishModal",
        "copyFactoryField",
    ]:
        assert f"function {fn}" in APP_JS, f"thiếu hàm JS {fn}"
    assert "if (name === 'factory') initFactoryPanel();" in APP_JS
    assert "/api/factory/order" in APP_JS
    assert "/api/factory/publish-pack/" in APP_JS


def test_factory_shortcut_alt_f():
    assert "f: 'factory'" in UI_JS
