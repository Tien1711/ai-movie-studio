"""Test cho Truyện thành video (Story-to-Video).

- split_chapters: tiêu đề Chương/Hồi/Phần (số + La Mã), không tiêu đề → cắt
  theo độ dài, chương rỗng bị bỏ.
- Parse DOCX/EPUB: tự tạo file mẫu bằng zipfile trong test, trích đúng text
  + đúng thứ tự chương.
- Import URL: mock httpx, trích đúng văn bản chính, bỏ nav/quảng cáo.
- Search/fetch Wikipedia/Wikisource: mock httpx, parse đúng JSON API.
- Adapt 5 mode: cấu trúc scenes đúng, short có hook, podcast có thông điệp
  kết, scenes_count đúng số yêu cầu.
- Validation: paste quá 500k ký tự → 422, adapt mode lạ → 422, upload PDF
  → lỗi rõ, upload quá 60MB → 422.
- Create-videos: tạo đúng số video, scenes inject sẵn có voiceover từ
  chương (mock BATCH_MANAGER, không render thật).
- Panel + JS story có mặt trong index.html / app.js / ui.js.

Chạy bằng TestClient, không cần Muse key, không gọi API video thật.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import batch_video_engine
import story_engine
from story_engine import (
    ADAPT_MODES,
    adapt_chapter,
    create_project,
    extract_text_from_url,
    fetch_web_text,
    get_library_story,
    list_library,
    parse_text_bytes,
    search_web,
    split_chapters,
)

client = TestClient(app_module.app)
ROOT = Path(__file__).resolve().parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_story_dirs(tmp_path, monkeypatch):
    """Cách ly project truyện + thư mục batch khỏi workspace thật."""
    monkeypatch.setattr(story_engine, "STORY_PROJECTS_DIR", tmp_path / "_story_projects")
    monkeypatch.setattr(batch_video_engine, "BATCH_DIR", tmp_path / "batch_videos")


def _long_text(n_paras=30, words_per_para=40):
    para = " ".join(["Ngày xưa có một chàng trai nghèo sống ở làng quê."] * 8)
    return "\n\n".join([para] * n_paras)


def _chaptered_text():
    return """Chương 1: Khởi đầu

Ngày xưa có một chàng trai nghèo sống ở làng quê. Chàng hiền lành, chăm chỉ, ai cũng quý mến. Một hôm chàng gặp một ông lão ăn xin bên đường.

Chương 2: Cuộc gặp gỡ

Ông lão nói: Cảm ơn cháu đã giúp ta. Ta tặng cháu một hạt giống thần kỳ. Chàng trai mang hạt giống về trồng, cây lớn nhanh như thổi.

Hồi 3: Cái kết

Cây thần nở hoa vàng rực rỡ. Cả làng kéo đến xem. Chàng trai từ đó sống hạnh phúc.
"""


class _FakeResp:
    def __init__(self, text="", status_code=200, json_data=None):
        self.text = text
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


# ── split_chapters ────────────────────────────────────────────────────────

def test_split_chapters_titles_arabic_roman():
    chapters = split_chapters(_chaptered_text())
    assert [c["title"] for c in chapters] == [
        "Chương 1: Khởi đầu", "Chương 2: Cuộc gặp gỡ", "Hồi 3: Cái kết"]
    assert all(c["word_count"] > 5 for c in chapters)
    assert "chàng trai nghèo" in chapters[0]["text"]


def test_split_chapters_no_titles_splits_by_length():
    text = _long_text(n_paras=40)
    chapters = split_chapters(text, chars_per_chapter=2000)
    assert len(chapters) >= 2
    assert all(len(c["text"]) <= 2600 for c in chapters)
    assert chapters[0]["title"] == "Phần 1"


def test_split_chapters_drops_empty_chapters():
    text = ("Chương 1: Có nội dung\n\nChàng trai đi chợ mua rau, mua thêm gạo và thịt về nấu cơm cho cả nhà.\n\n"
            "Chương 2: Trống\n\n"
            "Chương 3: Có tiếp\n\nChàng trai trở về nhà ăn cơm tối cùng gia đình trong niềm vui.")
    chapters = split_chapters(text)
    titles = [c["title"] for c in chapters]
    assert "Chương 2: Trống" not in titles
    assert len(chapters) == 2


def test_split_chapters_long_chapter_split_further():
    text = "Chương 1: Rất dài\n\n" + _long_text(n_paras=30)
    chapters = split_chapters(text, chars_per_chapter=2000)
    assert len(chapters) >= 2
    assert chapters[0]["title"].startswith("Chương 1")


def test_split_chapters_empty_text():
    assert split_chapters("") == []
    assert split_chapters("   \n\n  ") == []


# ── Kho tác phẩm ──────────────────────────────────────────────────────────

def test_library_has_four_stories_two_chapters_each():
    stories = list_library()
    assert len(stories) == 4
    ids = {s["id"] for s in stories}
    assert ids == {"tam-cam", "thach-sanh", "an-khe-tra-vang",
                   "bi-an-con-ma-nha-ho-hua"}
    for s in stories:
        assert s["chapter_count"] == 2
        assert 400 <= s["word_count"] <= 900, f"{s['id']}: {s['word_count']} từ"
        assert s["style"] in batch_video_engine.BATCH_VISUAL_STYLES


def test_library_story_full_text():
    story = get_library_story("tam-cam")
    assert story is not None
    assert story["title"] == "Tấm Cám"
    assert len(story["chapters"]) == 2
    assert "Tấm" in story["chapters"][0]["text"]
    assert get_library_story("khong-co") is None
    assert get_library_story("../tam-cam") is None  # chặn path traversal


# ── Parse file DOCX / EPUB ────────────────────────────────────────────────

def _make_docx(paras):
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="urn:x">'
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_parse_docx_extracts_text_and_chapters():
    paras = ["Chương 1: Khởi đầu", "Chàng trai nghèo đi chợ mua rau và gạo về nấu cơm cho cả nhà.",
             "Chương 2: Cái kết", "Chàng trai trở về nhà ăn cơm tối cùng gia đình trong niềm vui."]
    text = parse_text_bytes(_make_docx(paras), "truyen.docx")
    assert "Chương 1: Khởi đầu" in text
    assert "Chàng trai nghèo đi chợ" in text
    chapters = split_chapters(text)
    assert [c["title"] for c in chapters] == ["Chương 1: Khởi đầu", "Chương 2: Cái kết"]


def _make_epub(files):
    """files: [(href, xhtml_text)] theo đúng thứ tự spine."""
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')
    manifest = "".join(
        f'<item id="f{i}" href="{href}" media-type="application/xhtml+xml"/>'
        for i, (href, _) in enumerate(files))
    spine = "".join(
        f'<itemref idref="f{i}"/>' for i in range(len(files)))
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
           f"<metadata></metadata><manifest>{manifest}</manifest>"
           f"<spine>{spine}</spine></package>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        for href, body in files:
            zf.writestr(f"OEBPS/{href}",
                        f'<?xml version="1.0"?><html><body>{body}</body></html>')
    return buf.getvalue()


def test_parse_epub_keeps_spine_order():
    files = [
        ("c1.xhtml", "<h1>Chương 1: Khởi đầu</h1><p>Chàng trai nghèo đi chợ.</p>"),
        ("c2.xhtml", "<h1>Chương 2: Cái kết</h1><p>Chàng trai trở về nhà.</p>"),
    ]
    text = parse_text_bytes(_make_epub(files), "truyen.epub")
    assert text.index("Chương 1") < text.index("Chương 2")
    assert "Chàng trai nghèo đi chợ." in text
    assert "Chàng trai trở về nhà." in text


def test_parse_pdf_and_unknown_format_errors():
    with pytest.raises(ValueError, match="PDF chưa được hỗ trợ"):
        parse_text_bytes(b"%PDF-1.4 fake", "truyen.pdf")
    with pytest.raises(ValueError, match="chưa được hỗ trợ"):
        parse_text_bytes(b"fake", "truyen.xyz")


def test_parse_txt_and_md():
    assert "Chàng trai" in parse_text_bytes("Chàng trai nghèo đi chợ.".encode(), "a.txt")
    assert "Chàng trai" in parse_text_bytes("# Tựa\n\nChàng trai nghèo.".encode(), "a.md")


# ── Import từ link ────────────────────────────────────────────────────────

def test_extract_text_from_url_prefers_article_strips_ads():
    html_doc = """<html><head><title>Test</title></head><body>
    <nav class="main-menu">Trang chủ | Liên hệ | Quảng cáo giảm giá</nav>
    <div class="ad-banner">Mua ngay giảm 50% chỉ hôm nay</div>
    <article><h1>Chương 1: Khởi đầu</h1>
    <p>Ngày xưa có một chàng trai nghèo sống ở làng quê. Chàng hiền lành chăm chỉ.</p>
    <p>Một hôm chàng gặp một ông lão ăn xin bên đường và giúp đỡ ông.</p>
    <p>Ông lão cảm động, tặng chàng một hạt giống thần kỳ để trồng sau vườn.</p></article>
    <footer>Copyright 2026 - Liên hệ quảng cáo</footer>
    <script>var x = 1;</script></body></html>"""
    text = extract_text_from_url("https://example.com/truyen", http_get=lambda url: _FakeResp(html_doc))
    assert "chàng trai nghèo" in text
    assert "Quảng cáo giảm giá" not in text
    assert "Mua ngay giảm 50%" not in text
    assert "var x = 1" not in text


def test_extract_text_from_url_errors():
    with pytest.raises(ValueError, match="http"):
        extract_text_from_url("ftp://example.com/x")
    with pytest.raises(ValueError, match="HTTP 404"):
        extract_text_from_url("https://example.com/x",
                              http_get=lambda url: _FakeResp("not found", 404))
    with pytest.raises(ValueError, match="Không trích được"):
        extract_text_from_url("https://example.com/x",
                              http_get=lambda url: _FakeResp("<html><body><p>Ngắn.</p></body></html>"))


# ── Tìm trên web (Wikipedia / Wikisource, mock) ───────────────────────────

def _wiki_search_json(titles):
    return {"query": {"search": [
        {"title": t, "snippet": f"<b>{t}</b> là một truyện dân gian."}
        for t in titles]}}


def test_search_web_library_first_then_wiki():
    def fake_get(url, params=None, **kw):
        return _FakeResp(json_data=_wiki_search_json(["Tấm Cám (truyện)", "Lục Vân Tiên"]))
    result = search_web("Tấm Cám", http_get=fake_get)
    assert result["results"][0]["source"] == "library"
    assert result["results"][0]["title"] == "Tấm Cám"
    sources = [r["source"] for r in result["results"]]
    assert "wikipedia" in sources and "wikisource" in sources


def test_search_web_empty_query():
    with pytest.raises(ValueError, match="không được để trống"):
        search_web("   ")


def test_fetch_web_text_wikipedia_mock():
    def fake_get(url, params=None, **kw):
        return _FakeResp(json_data={"query": {"pages": {
            "123": {"extract": "Tấm Cám là truyện cổ tích Việt Nam. " * 20}}}})
    text = fetch_web_text("Tấm Cám", "wikipedia", http_get=fake_get)
    assert "Tấm Cám là truyện cổ tích" in text


def test_fetch_web_text_library_and_errors():
    text = fetch_web_text("tam-cam", "library")
    assert "Tấm" in text
    with pytest.raises(ValueError, match="Không tìm thấy"):
        fetch_web_text("Khong Co Bai Nay", "wikipedia",
                       http_get=lambda url, **kw: _FakeResp(
                           json_data={"query": {"pages": {"-1": {"missing": ""}}}}))
    with pytest.raises(ValueError, match="chỉ nhận"):
        fetch_web_text("Tấm Cám", "google")


# ── Xào nấu AI (5 chế độ) ─────────────────────────────────────────────────

_CHAPTER = (
    "Ngày xưa có một chàng trai nghèo sống ở làng quê. Chàng hiền lành, chăm chỉ, "
    "ai cũng quý mến. Một hôm, chàng gặp một ông lão ăn xin bên đường, liền chia "
    "cho ông nửa nắm cơm của mình. Ông lão cảm động, nói: Cháu tốt bụng lắm, ta "
    "tặng cháu một hạt giống thần kỳ! Chàng trai mang hạt giống về trồng sau vườn. "
    "Đêm đó, bỗng nhiên có tiếng động lạ từ vườn sau. Chàng chạy ra xem, hóa ra "
    "cây thần đã lớn nhanh như thổi, nở hoa vàng rực rỡ. Cả làng kéo đến xem, ai "
    "cũng trầm trồ. Từ đó, chàng trai sống hạnh phúc và giúp đỡ mọi người trong làng."
)


def test_adapt_all_modes_structure():
    for mode in ADAPT_MODES:
        script = adapt_chapter(_CHAPTER, mode, 5, "cinematic")
        assert script["mode"] == mode
        assert 1 <= len(script["scenes"]) <= 5
        for sc in script["scenes"]:
            assert sc["voiceover"].strip(), f"{mode}: voiceover rỗng"
            assert sc["prompt"].strip(), f"{mode}: prompt rỗng"
            assert 5 <= sc["duration"] <= 10
            assert "photorealistic 8k" in sc["prompt"] or "ARRI" in sc["prompt"]


def test_adapt_short_has_hook_and_cliffhanger():
    script = adapt_chapter(_CHAPTER, "short", 5, "cinematic")
    first = script["scenes"][0]["voiceover"]
    assert "?" in first or "60 giây" in first or "Khoan đã" in first
    assert "bắt đầu" in script["scenes"][-1]["voiceover"]


def test_adapt_podcast_ends_with_moral():
    script = adapt_chapter(_CHAPTER, "podcast", 5, "cinematic")
    last = script["scenes"][-1]["voiceover"]
    assert "Bài học" in last or "khép lại" in last


def test_adapt_horror_opens_dark_and_suspense_marks_twist():
    horror = adapt_chapter(_CHAPTER, "horror", 5, "horror_dark")
    assert horror["scenes"][0]["voiceover"].startswith("Đêm ấy,")
    assert "fog" in horror["scenes"][0]["prompt"] or "eerie" in horror["scenes"][0]["prompt"]
    suspense = adapt_chapter(_CHAPTER, "suspense", 5, "cinematic")
    joined = " ".join(s["voiceover"] for s in suspense["scenes"])
    assert "không ai ngờ tới" in joined


def test_adapt_respects_scenes_count_and_extra_req():
    script = adapt_chapter(_CHAPTER, "short", 3, "anime", extra_req="Kể chậm rãi nhé.")
    assert len(script["scenes"]) == 3
    assert script["scenes"][0]["voiceover"].startswith("Kể chậm rãi nhé.")
    assert "anime" in script["scenes"][0]["prompt"] or "Ghibli" in script["scenes"][0]["prompt"]


def test_adapt_invalid_mode_and_empty_chapter():
    with pytest.raises(ValueError, match="không hợp lệ"):
        adapt_chapter(_CHAPTER, "sieu-nhan", 5, "cinematic")
    with pytest.raises(ValueError, match="không có nội dung"):
        adapt_chapter("   ", "short", 5, "cinematic")


# ── Endpoints: thư viện / tìm kiếm ────────────────────────────────────────

def test_api_library_endpoints():
    r = client.get("/api/story/library")
    assert r.status_code == 200
    assert len(r.json()["stories"]) == 4
    r = client.get("/api/story/library/tam-cam")
    assert r.status_code == 200
    assert len(r.json()["chapters"]) == 2
    r = client.get("/api/story/library/khong-co")
    assert r.status_code == 404


def test_api_adapt_modes():
    r = client.get("/api/story/adapt-modes")
    assert r.status_code == 200
    assert {m["id"] for m in r.json()["modes"]} == set(ADAPT_MODES)


def test_api_search_validation(monkeypatch):
    monkeypatch.setattr(story_engine, "search_web",
                        lambda q: {"query": q, "results": [{"title": "Tấm Cám", "source": "library"}]})
    r = client.get("/api/story/search", params={"q": "Tấm Cám"})
    assert r.status_code == 200
    assert r.json()["results"][0]["title"] == "Tấm Cám"
    r = client.get("/api/story/search", params={"q": "   "})
    assert r.status_code == 422


# ── Endpoints: paste / upload / adapt / create-videos ─────────────────────

def _make_project_via_paste():
    r = client.post("/api/story/paste", json={
        "title": "Truyện test", "author": "Test",
        "text": _chaptered_text(), "chars_per_chapter": 6000})
    assert r.status_code == 200, r.text
    return r.json()["project"]


def test_api_paste_creates_project_and_lists():
    project = _make_project_via_paste()
    assert project["chapter_count"] == 3
    assert project["word_count"] > 20
    r = client.get("/api/story/projects")
    assert r.status_code == 200
    assert any(p["id"] == project["id"] for p in r.json()["projects"])
    r = client.get(f"/api/story/projects/{project['id']}")
    assert r.status_code == 200
    assert len(r.json()["chapters"]) == 3
    r = client.get("/api/story/projects/st_khongco")
    assert r.status_code == 404


def test_api_paste_too_long_422():
    r = client.post("/api/story/paste", json={
        "title": "Quá dài", "text": "x" * 500_001})
    assert r.status_code == 422


def test_api_upload_txt_docx_and_pdf_error():
    r = client.post("/api/story/upload",
                    files={"file": ("truyen.txt", _chaptered_text().encode(), "text/plain")},
                    data={"title": "Truyện upload"})
    assert r.status_code == 200, r.text
    assert r.json()["project"]["chapter_count"] == 3

    docx = _make_docx(["Chương 1: Mở đầu", "Chàng trai nghèo đi chợ mua rau và gạo về nấu cơm cho cả nhà.",
                       "Chương 2: Kết", "Chàng trai trở về nhà ăn cơm tối cùng gia đình trong niềm vui."])
    r = client.post("/api/story/upload",
                    files={"file": ("truyen.docx", docx,
                                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                    data={"title": "Truyện docx"})
    assert r.status_code == 200, r.text
    assert r.json()["project"]["chapter_count"] == 2

    r = client.post("/api/story/upload",
                    files={"file": ("truyen.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert r.status_code == 400
    assert "PDF chưa được hỗ trợ" in r.json()["error"]


def test_api_upload_too_big_422(monkeypatch):
    from fastapi import HTTPException

    async def _fake_capped(file, *, max_bytes, label):
        raise HTTPException(status_code=413, detail="File quá lớn")

    monkeypatch.setattr(app_module, "_read_upload_capped", _fake_capped)
    r = client.post("/api/story/upload",
                    files={"file": ("lon.txt", b"x" * 10, "text/plain")})
    assert r.status_code == 422
    assert "60MB" in r.json()["error"]


def test_api_adapt_and_save_script():
    project = _make_project_via_paste()
    pid = project["id"]
    r = client.post(f"/api/story/projects/{pid}/adapt", json={
        "mode": "short", "chapter_index": 0, "scenes_count": 3,
        "visual_style": "cinematic"})
    assert r.status_code == 200, r.text
    adapted = r.json()["adapted"][0]
    assert adapted["scene_count"] == 3
    assert adapted["scenes"][0]["voiceover"]

    # Sửa trực tiếp rồi lưu lại
    scenes = adapted["scenes"]
    scenes[0]["voiceover"] = "Lời thoại đã sửa tay."
    r = client.put(f"/api/story/projects/{pid}/scripts/0", json={
        "scenes": scenes, "mode": "short", "visual_style": "cinematic"})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/story/projects/{pid}")
    assert r.json()["scripts"]["0"]["scenes"][0]["voiceover"] == "Lời thoại đã sửa tay."

    # Mode lạ → 422
    r = client.post(f"/api/story/projects/{pid}/adapt", json={
        "mode": "che-do-la", "chapter_index": 0})
    assert r.status_code == 422


def test_api_create_videos_injects_scenes(monkeypatch):
    project = _make_project_via_paste()
    pid = project["id"]
    captured = {}

    class _FakeManager:
        def add_batch(self, prompts, **kwargs):
            captured["prompts"] = prompts
            captured["scripts"] = kwargs.get("scripts")
            captured["extra"] = kwargs.get("extra")
            captured["auto_run"] = kwargs.get("auto_run")
            return [{"id": f"bv_test{i}", "title": p}
                    for i, p in enumerate(prompts)]

    monkeypatch.setattr(story_engine, "BATCH_MANAGER", _FakeManager())
    r = client.post(f"/api/story/projects/{pid}/create-videos", json={
        "chapter_indexes": [0, 2], "visual_style": "cinematic",
        "voice": "vi-VN-HoaiMyNeural", "aspect_ratio": "9:16",
        "scenes_count": 3, "mode": "short"})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 2
    assert captured["auto_run"] is True
    assert captured["extra"] == {"story_source": "story", "story_project_id": pid}
    assert len(captured["scripts"]) == 2
    for script in captured["scripts"]:
        assert len(script["scenes"]) == 3
        for sc in script["scenes"]:
            assert sc["voiceover"].strip() and sc["prompt"].strip()
    # Voiceover lấy từ chương (chứa từ của chương 1 và chương 3)
    all_vo = " ".join(s["voiceover"] for script in captured["scripts"]
                      for s in script["scenes"])
    assert "chàng trai" in all_vo.lower()


def test_api_create_videos_bad_chapter_422(monkeypatch):
    project = _make_project_via_paste()
    r = client.post(f"/api/story/projects/{project['id']}/create-videos", json={
        "chapter_indexes": [99], "visual_style": "cinematic"})
    assert r.status_code == 422


# ── add_batch: scripts inject sẵn + cờ script_locked ─────────────────────

def test_add_batch_with_preset_scripts_sets_lock():
    scripts = [{
        "title": "Truyện test — Chương 1",
        "viral_title": "Truyện test — Chương 1",
        "caption": "Nghe truyện mỗi ngày",
        "hashtags": ["#truyen"],
        "scenes": [
            {"index": 1, "role": "hook", "prompt": "Cinematic scene, forest",
             "voiceover": "Ngày xưa có một chàng trai nghèo.", "duration": 6},
            {"index": 2, "role": "ending", "prompt": "Cinematic scene, moon",
             "voiceover": "Từ đó chàng sống hạnh phúc.", "duration": 6},
        ],
    }]
    created = batch_video_engine.BATCH_MANAGER.add_batch(
        prompts=["Truyện test — Chương 1"], scenes_count=2,
        visual_style="cinematic", auto_run=False,
        scripts=scripts, extra={"story_source": "story", "story_project_id": "st_x"})
    assert len(created) == 1
    item = created[0]
    assert item["script_locked"] is True
    assert item["story_project_id"] == "st_x"
    assert item["scenes"][0]["voiceover"] == "Ngày xưa có một chàng trai nghèo."
    assert item["title"] == "Truyện test — Chương 1"
    # Không truyền scripts → hành vi cũ, không khóa
    created2 = batch_video_engine.BATCH_MANAGER.add_batch(
        prompts=["Chuyện ma ở ngôi nhà cổ"], scenes_count=3, auto_run=False)
    assert created2[0]["script_locked"] is False


# ── UI: panel + JS story có mặt ───────────────────────────────────────────

def test_ui_story_panel_present():
    assert 'id="panel-story"' in INDEX_HTML
    assert 'id="nav-story"' in INDEX_HTML
    assert 'id="storyStyles"' in INDEX_HTML
    assert 'id="storyAdaptModal"' in INDEX_HTML
    assert 'id="storyReadModal"' in INDEX_HTML
    for tab in ("library", "search", "url", "upload", "paste", "adapt"):
        assert f'id="storyTab{tab.capitalize()}"' in INDEX_HTML, tab
    assert "Truyện thành video" in INDEX_HTML


def test_ui_story_js_present():
    assert "function initStoryPanel" in APP_JS
    assert "function openStoryAdaptModal" in APP_JS
    assert "function createStoryVideos" in APP_JS
    assert "function loadStoryLibrary" in APP_JS
    assert "function searchStoryWeb" in APP_JS
    assert "function uploadStoryFile" in APP_JS
    assert "function pasteStoryText" in APP_JS
    assert "if (name === 'story') initStoryPanel();" in APP_JS
    assert "t: 'story'" in UI_JS
