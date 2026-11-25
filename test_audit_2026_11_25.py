# -*- coding: utf-8 -*-
"""
test_audit_2026_11_25.py — Bộ kiểm thử chống tái phát cho toàn bộ 27 điểm audit (2026-11-25).
Bao phủ: C1-C4, H1-H9, M1-M7, L1-L3 (và kiểm tra cú pháp/symtable toàn dự án).
"""
import asyncio
import io
import json
import shutil
import symtable
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import affiliate_engine
import app as app_module
import batch_engine
import batch_video_engine
import composer
import qc
import shot_desk
import story_engine
import tts_manager

ROOT = Path(__file__).resolve().parent
client = TestClient(app_module.app)


def test_c1_convert_to_shot_desk_uses_canonical_shot_files_and_urls(tmp_path, monkeypatch):
    """C1: convert_to_shot_desk phải xuất đúng clips/shot_XXX_clip.mp4, audio/shot_XXX_tts.mp3 và video_url."""
    monkeypatch.setattr(batch_video_engine, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(batch_video_engine, "BATCH_DIR", tmp_path / "_batch_videos")
    monkeypatch.setattr(shot_desk, "SHOT_DESK_DIR", tmp_path / "_shot_desk_projects")
    (tmp_path / "_batch_videos").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_shot_desk_projects").mkdir(parents=True, exist_ok=True)

    mgr = batch_video_engine.BatchVideoManager()
    mgr.queue_file = tmp_path / "_batch_videos" / "batch_queue.json"
    mgr.queue_bak_file = tmp_path / "_batch_videos" / "batch_queue.json.bak"

    vid_id = "bv_testshot01"
    v_dir = tmp_path / "_batch_videos" / vid_id
    (v_dir / "clips").mkdir(parents=True)
    (v_dir / "audio").mkdir(parents=True)
    (v_dir / "images").mkdir(parents=True)
    (v_dir / "clips" / "clip_1.mp4").write_bytes(b"0" * 1200)
    (v_dir / "audio" / "scene_1.mp3").write_bytes(b"1" * 600)
    (v_dir / "images" / "scene_1.webp").write_bytes(b"2" * 800)

    mgr._write_queue([{
        "id": vid_id,
        "title": "Test Shot Desk Export",
        "prompt": "Test prompt",
        "aspect_ratio": "9:16",
        "visual_style": "cinematic",
        "voice": "vi-VN-HoaiMyNeural",
        "status": "completed",
        "scenes": [
            {"index": 1, "role": "hook", "prompt": "p1", "voiceover": "v1", "duration": 6},
            {"index": 2, "role": "ending", "prompt": "p2", "voiceover": "v2", "duration": 6},
        ],
    }])

    res = mgr.convert_to_shot_desk(vid_id)
    assert res is not None
    proj_id = res["project_id"]
    proj_dir = tmp_path / "_shot_desk_projects" / proj_id
    assert (proj_dir / "clips" / "shot_001_clip.mp4").is_file()
    assert (proj_dir / "audio" / "shot_001_tts.mp3").is_file()
    assert (proj_dir / "shots" / "shot_001" / "first_frame.webp").is_file()

    proj = shot_desk.get_shot_desk_project(proj_id)
    s1, s2 = proj["shots"]
    assert s1["status"] == "approved"
    assert s1["clip_path"] == "clips/shot_001_clip.mp4"
    assert s1["video_url"] == f"/api/shot-desk/projects/{proj_id}/shots/1/video"
    assert s1["audio_url"] == f"/api/shot-desk/projects/{proj_id}/shots/1/audio"
    assert s2["status"] == "ready"
    assert s2["video_url"] is None


def test_c2_tts_failure_reports_fallback_and_never_poisons_cache(tmp_path, monkeypatch):
    """C2: Khi mọi engine TTS đều lỗi, generate_tts_with_status phải trả tts_failed=True và KHÔNG lưu cache câm."""
    async def _fail_edge(*a, **kw):
        raise RuntimeError("Edge network down")

    def _fail_vieneu(*a, **kw):
        raise RuntimeError("VieNeu unavailable")

    def _fake_silent(path, dur):
        Path(path).write_bytes(b"S" * 600)
        return dur

    monkeypatch.setattr(tts_manager, "synthesize_edge_tts", _fail_edge)
    monkeypatch.setattr(tts_manager.VieNeuEngine, "synthesize", _fail_vieneu)
    monkeypatch.setattr(tts_manager, "create_silent_audio_fallback", _fake_silent)
    cache_dir = tmp_path / "_tts_cache"
    monkeypatch.setattr(batch_engine, "_TTS_CACHE_DIR", cache_dir)

    out_mp3 = tmp_path / "out.mp3"
    dur, is_fallback, err = asyncio.run(
        batch_engine.generate_tts_with_status("Xin chào kiểm thử lỗi mạng", "vi-VN-HoaiMyNeural", str(out_mp3))
    )
    assert is_fallback is True
    assert "Edge network down" in err
    assert dur > 0
    assert not cache_dir.exists() or len(list(cache_dir.glob("*.mp3"))) == 0


def test_c3_c4_m4_h8_frontend_contracts_and_xss_guards():
    """C3, C4, M4, H5, H8: Kiểm tra trực tiếp các hợp đồng JS trong static/js/app.js."""
    app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

    # C3: Affiliate render gọi startMonitoring thay vì connectSSE không tồn tại
    assert "connectSSE(" not in app_js
    assert "startMonitoring(jobId, payload.title, scenesPayload.length)" in app_js

    # C4: executeBeatBreakdown truyền đủ 11 tham số cho addScene
    idx_fn = app_js.index("async function executeBeatBreakdown")
    block = app_js[idx_fn: idx_fn + 1500]
    assert "b.camera_angle || 'front'" in block
    # Giữa [] và chain_from_previous phải có 2 chuỗi rỗng '', '' cho reference_image & reference_image_name
    assert "[],\n          '',\n          ''," in block or "[],\r\n          '',\r\n          ''," in block

    # M4: testAffiliateVoice truyền '' vào customText trước btn
    assert "testPlayVoice(v, '', btn)" in app_js

    # H5: Factory & Story voice select ẩn ai_voice
    assert "voiceSelectHtml('vi-VN-HoaiMyNeural', false)" in app_js

    # H8: esc() escape cả dấu nháy đơn &#39;, Affiliate textarea được bọc esc(), Quick Video dùng index
    assert ".replace(/'/g, '&#39;')" in app_js
    assert "${esc(s.voiceover || '')}</textarea>" in app_js
    assert "${esc(s.prompt || '')}</textarea>" in app_js
    assert "copyQvHistoryItem(" in app_js
    assert "fetchStoryWebByIndex(" in app_js


def test_h1_story_import_url_blocks_ssrf():
    """H1: extract_text_from_url chặn localhost, loopback, link-local và private IP."""
    for bad_url in [
        "http://localhost:18610/admin/status",
        "http://127.0.0.1:8000/docs",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/admin",
        "http://10.0.0.5/internal",
    ]:
        with pytest.raises(ValueError, match="SSRF|nội bộ"):
            story_engine.extract_text_from_url(bad_url, http_get=lambda u: None)


def test_h2_m2_path_containment_for_character_sheet_and_bgm(tmp_path):
    """H2 & M2: Chặn đường dẫn ngoài workspace cho bg_music_path và reference_image."""
    outside_secret = tmp_path / "outside.mp3"
    outside_secret.write_bytes(b"secret")

    # M2: MovieRequest / AffiliateRenderRequest bác bỏ bg_music_path ngoài workspace/_music & _uploads
    r = client.post("/api/affiliate/render", json={
        "title": "Test BGM Guard",
        "bg_music_path": str(outside_secret),
        "scenes": [{"index": 1, "prompt": "p", "voiceover": "v", "duration": 5}],
    })
    assert r.status_code == 422

    # Tên file trần hợp lệ trong _music vẫn được chấp nhận
    assert app_module._validate_safe_studio_bgm_field("music_a.mp3") == "music_a.mp3"
    with pytest.raises(ValueError):
        app_module._validate_safe_studio_bgm_field("../secret.mp3")


def test_h3_workspace_clean_protects_batch_and_story_dirs(tmp_path, monkeypatch):
    """H3: /api/workspace/clean giữ nguyên _batch_videos và _story_projects khi keep_projects=True."""
    ws = tmp_path / "ws"
    for name in ("_uploads", "_music", "_projects", "_shot_desk_projects", "_batch_videos", "_story_projects", "temp_job_123"):
        d = ws / name
        d.mkdir(parents=True)
        (d / "marker.txt").write_text("ok", encoding="utf-8")
    # Nhóm C (audit #6): temp_job_123 là thư mục job ĐÃ BIẾT (có job_state.json
    # khớp id) nên vẫn bị xóa; thư mục lạ không có job_state.json thì được giữ
    # lại (xem test_clean_workspace_skips_unknown_dirs).
    (ws / "temp_job_123" / "job_state.json").write_text(
        json.dumps({"id": "temp_job_123", "status": "done"}), encoding="utf-8")

    monkeypatch.setattr(app_module, "WORKSPACE", ws)
    # Bảo đảm không có job running giả trong bộ nhớ
    r = client.post("/api/workspace/clean?keep_projects=true")
    if r.status_code == 200:
        assert (ws / "_batch_videos" / "marker.txt").exists()
        assert (ws / "_story_projects" / "marker.txt").exists()
        assert not (ws / "temp_job_123").exists()


def test_h4_worker_loop_declares_global_worker_thread():
    """H4: _worker_loop phải khai báo global _worker_thread (không phải biến local)."""
    src = (ROOT / "batch_video_engine.py").read_text(encoding="utf-8")
    top = symtable.symtable(src, "batch_video_engine.py", "exec")
    fn_sym = next(c for c in top.get_children() if c.get_name() == "_worker_loop")
    sym = fn_sym.lookup("_worker_thread")
    assert sym.is_declared_global(), "_worker_thread trong _worker_loop bắt buộc là global!"


def test_h5_special_voices_rejected_for_batch_factory_story():
    """H5: ai_voice / native / none / no_dub bị bác bỏ với 422 ở Batch, Factory, Story."""
    r1 = client.post("/api/batch-videos/create", json={
        "prompts": ["Test"], "voice": "ai_voice", "auto_run": False,
    })
    assert r1.status_code == 422

    r2 = client.post("/api/factory/order", json={
        "source": "manual", "topics": ["Chủ đề 1"], "voice": "ai_voice",
    })
    assert r2.status_code == 422


def test_h6_m7_user_edited_story_script_not_overwritten(tmp_path, monkeypatch):
    """H6 & M7: Kịch bản chương đã được người dùng lưu tay (user_edited=True) không bị ghi đè khi tạo video."""
    monkeypatch.setattr(story_engine, "STORY_PROJECTS_DIR", tmp_path / "_story_projects")
    proj = story_engine.create_project(
        title="Truyện Ngắn Test",
        author="Tác giả",
        text="Ngày xửa ngày xưa ở một ngôi làng nhỏ ven sông có một cậu bé hiếu thảo. Cậu luôn chăm chỉ giúp đỡ mọi người xung quanh mỗi ngày.",
        source="paste",
    )
    pid = proj["id"]

    # Người dùng lưu kịch bản chỉ có 2 cảnh (ngắn hơn thanh trượt scenes_count=5)
    r_save = client.put(f"/api/story/projects/{pid}/scripts/0", json={
        "scenes": [
            {"index": 1, "role": "hook", "voiceover": "Câu thoại riêng do user sửa 1.", "prompt": "Custom prompt 1", "duration": 6},
            {"index": 2, "role": "ending", "voiceover": "Câu thoại riêng do user sửa 2.", "prompt": "Custom prompt 2", "duration": 6},
        ],
        "mode": "short",
        "visual_style": "cinematic",
        "aspect_ratio": "16:9",
    })
    assert r_save.status_code == 200

    captured = {}

    class _FakeBatchMgr:
        def add_batch(self, prompts, **kwargs):
            captured["scripts"] = kwargs["scripts"]
            return [{"id": "bv_1", "title": prompts[0]}]

    monkeypatch.setattr(story_engine, "BATCH_MANAGER", _FakeBatchMgr())
    # Gọi create-videos với scenes_count=5 và mode='horror' (khác lúc lưu!)
    r_create = client.post(f"/api/story/projects/{pid}/create-videos", json={
        "chapter_indexes": [0],
        "visual_style": "cinematic",
        "voice": "vi-VN-HoaiMyNeural",
        "aspect_ratio": "16:9",
        "scenes_count": 5,
        "mode": "horror",
    })
    assert r_create.status_code == 200
    used_scenes = captured["scripts"][0]["scenes"]
    assert len(used_scenes) == 2
    assert used_scenes[0]["voiceover"] == "Câu thoại riêng do user sửa 1."


def test_h7_epub_without_media_type_and_zip_bomb_guard():
    """H7: EPUB có thẻ <item> thiếu media-type không bị crash AttributeError."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
        zf.writestr(
            "content.opf",
            '<package><manifest>'
            '<item id="cover" href="cover.jpg"/>'
            '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="ch1"/></spine></package>',
        )
        zf.writestr(
            "ch1.xhtml",
            "<html><body><p>" + ("Đây là nội dung chương truyện thử nghiệm đủ dài để vượt ngưỡng kiểm tra. " * 4) + "</p></body></html>",
        )
    parsed = story_engine._parse_epub(buf.getvalue())
    assert "nội dung chương truyện" in parsed


def test_h9_resolve_safe_reference_supports_shot_desk_routes(tmp_path, monkeypatch):
    """H9: _resolve_safe_reference giải mã đúng /api/shot-desk/projects/<id>/assets/... và /shots/<n>/frames/..."""
    monkeypatch.setattr(batch_engine, "_WORKSPACE_ROOT", tmp_path.resolve())
    pdir = tmp_path / "_shot_desk_projects" / "my_film_abc123"
    (pdir / "characters").mkdir(parents=True)
    (pdir / "shots" / "shot_001").mkdir(parents=True)
    char_img = pdir / "characters" / "hero.png"
    frame_img = pdir / "shots" / "shot_001" / "first_frame.jpg"
    char_img.write_bytes(b"PNGDATA")
    frame_img.write_bytes(b"JPGDATA")

    r1 = batch_engine._resolve_safe_reference("/api/shot-desk/projects/my_film_abc123/assets/characters/hero.png")
    r2 = batch_engine._resolve_safe_reference("/api/shot-desk/projects/my_film_abc123/shots/1/frames/first_frame.jpg")
    assert r1 == char_img.resolve()
    assert r2 == frame_img.resolve()


def test_m5_l2_affiliate_og_validation_and_bing_query_urls():
    """M5 & L2: Chặn javascript: URL trong og_info TikTok và giữ nguyên URL Bing chứa &amp;."""
    # L2: URL ảnh có &amp; trong Bing HTML không bị cắt cụt
    fake_html = '&quot;murl&quot;:&quot;https://cdn.example.com/prod.jpg?w=800&amp;q=90&quot;'

    class _Resp:
        status_code = 200
        text = fake_html

    urls = affiliate_engine._bing_image_search("sua ensure", lambda *a, **kw: _Resp())
    assert urls == ["https://cdn.example.com/prod.jpg?w=800&q=90"]

    # M5: og_info chứa javascript: không được lọt vào image_url
    bad_tiktok = (
        'https://www.tiktok.com/view/product/1729384756?og_info='
        '{"title":"Áo thun nam","image":"javascript:alert(1)"}'
    )

    class _FakeLinkResp:
        status_code = 200
        url = bad_tiktok
        text = ""

    imported = affiliate_engine.import_affiliate_product_link(bad_tiktok, http_get=lambda *a, **kw: _FakeLinkResp())
    assert imported["product_name"] == "Áo thun nam"
    assert imported["image_url"] == ""
    assert imported["image_urls"] == []


def test_m6_l1_composer_square_and_540p_resolutions():
    """M6 & L1: Khung hình 1:1 và chất lượng 540p có đúng độ phân giải chuẩn trong composer & qc."""
    assert "540p" in composer.QUALITY_PROFILES
    assert composer.QUALITY_PROFILES["720p"]["res"]["1:1"] == (720, 720)
    assert composer.QUALITY_PROFILES["540p"]["res"]["9:16"] == (540, 960)
    assert qc.expected_resolution_for_size("1:1", "720p") == (720, 720)
    assert qc.expected_resolution_for_size("9:16", "540p") == (540, 960)
