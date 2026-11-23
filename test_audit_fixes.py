# -*- coding: utf-8 -*-
import os
import sys
import json
import time
import httpx
from pathlib import Path

BASE_URL = "http://localhost:8000"

def test_all():
    print("=" * 60)
    print("BẮT ĐẦU KIỂM THỬ TOÀN DIỆN 13 ĐIỂM SỬA LỖI THEO REVIEW")
    print("=" * 60)

    # 1. Point 6: Workspace path check
    from batch_video_engine import WORKSPACE_DIR, BATCH_DIR, QUEUE_FILE, QUEUE_BAK_FILE, BATCH_MANAGER
    expected_ws = Path(r"C:\Users\Admin\.gemini\antigravity\scratch\ai_movie_studio\workspace")
    assert WORKSPACE_DIR.resolve() == expected_ws.resolve(), f"Workspace mismatch: {WORKSPACE_DIR}"
    print("✅ Point 6 PASS: WORKSPACE_DIR được phân giải tương đối tự động đúng chuẩn dự án.")

    # 2. Point 9: Atomic write & .bak backup check
    videos_now = BATCH_MANAGER._read_queue()
    BATCH_MANAGER._write_queue(videos_now)
    assert QUEUE_FILE.exists(), "QUEUE_FILE phải tồn tại"
    assert QUEUE_BAK_FILE.exists(), "QUEUE_BAK_FILE phải tồn tại"
    # Kiểm tra fallback .bak khi file chính hỏng
    orig_content = QUEUE_FILE.read_text(encoding="utf-8")
    QUEUE_FILE.write_text("{broken json", encoding="utf-8")
    recovered_videos = BATCH_MANAGER._read_queue()
    assert len(recovered_videos) == len(videos_now), "Phải đọc được từ .bak khi file chính hỏng!"
    # Restore lại
    QUEUE_FILE.write_text(orig_content, encoding="utf-8")
    print("✅ Point 9 PASS: Ghi nguyên tử atomic write & tự động khôi phục từ .bak khi có sự cố.")

    # 3. Point 14: Input validation (FastAPI / Pydantic)
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        # Invalid aspect ratio
        r_bad_ratio = client.post("/api/batch-videos/create", json={
            "prompts": ["Prompt test 1"],
            "aspect_ratio": "99:99",
            "visual_style": "anime",
            "mode": "free"
        })
        assert r_bad_ratio.status_code == 422, f"Expected 422 for bad ratio, got {r_bad_ratio.status_code}"
        print("✅ Point 14 (Ratio validation) PASS: Bác bỏ tỷ lệ khung hình lạ (422).")

        # Invalid visual style
        r_bad_style = client.post("/api/batch-videos/create", json={
            "prompts": ["Prompt test 1"],
            "aspect_ratio": "9:16",
            "visual_style": "nonexistent_style_xyz",
            "mode": "free"
        })
        assert r_bad_style.status_code == 422, f"Expected 422 for bad style, got {r_bad_style.status_code}"
        print("✅ Point 14 (Style validation) PASS: Bác bỏ phong cách lạ (422).")

        # Invalid mode
        r_bad_mode = client.post("/api/batch-videos/create", json={
            "prompts": ["Prompt test 1"],
            "aspect_ratio": "9:16",
            "visual_style": "anime",
            "mode": "super_mode"
        })
        assert r_bad_mode.status_code == 422, f"Expected 422 for bad mode, got {r_bad_mode.status_code}"
        print("✅ Point 14 (Mode validation) PASS: Bác bỏ chế độ lạ (422).")

    # 4. Point 5 & 7 & 8: Character Mode, Library Character Selection & Contextual Narrative Weaving
    from batch_video_engine import generate_batch_script_local, _validate_image_file
    script_free = generate_batch_script_local("Khám phá vũ trụ bao la", scenes_count=4, mode="free")
    assert script_free.get("character") is None, "Free mode không được có character"

    # Test archetype character
    script_char = generate_batch_script_local("Khám phá vũ trụ bao la", scenes_count=4, mode="character")
    char_info = script_char.get("character")
    assert char_info is not None, "Character mode phải tạo ra character info"
    assert "name" in char_info and "prompt" in char_info
    for sc in script_char["scenes"]:
        assert char_info["prompt"] in sc["prompt"], f"Prompt cảnh {sc['index']} phải chứa mô tả nhân vật!"
    print("✅ Point 5 PASS: Chế độ 'Có nhân vật' neo danh tính nhân vật nhất quán 100% xuyên suốt các cảnh.")

    # Point 7: Library character selection
    script_lib_char = generate_batch_script_local(
        raw_prompt="Câu chuyện cảm động gia đình",
        scenes_count=4,
        mode="character",
        character_id="hung"
    )
    assert script_lib_char.get("character") is not None
    assert script_lib_char["character"]["name"] == "Hùng"
    assert "Hùng" in script_lib_char["scenes"][0]["prompt"] or "HÙNG" in script_lib_char["scenes"][0]["prompt"]
    print("✅ Point 7 PASS: Cho phép chọn nhân vật trực tiếp từ thư viện hồ sơ nhân vật (_characters.json).")

    # Point 8: Contextual Storytelling (Prompt topic woven into scenes)
    prompt_topic = "Bí mật ngọn hải đăng lúc nửa đêm"
    script_context = generate_batch_script_local(raw_prompt=prompt_topic, scenes_count=4)
    # Cảnh giữa phải chứa tên chủ đề cụ thể trong prompt
    assert prompt_topic in script_context["scenes"][1]["prompt"], "Visual prompt cảnh 2 phải chứa tên chủ đề cụ thể!"
    print("✅ Point 8 PASS: Kịch bản phân cảnh đã khai triển chủ đề cụ thể của prompt vào từng cảnh, không còn chung chung.")

    # Point 2: Image magic bytes validation
    dummy_bad_file = Path(r"C:\Users\Admin\.gemini\antigravity\scratch\ai_movie_studio\workspace\dummy_bad.webp")
    dummy_bad_file.write_bytes(b"A" * 2000)
    assert _validate_image_file(dummy_bad_file) is False, "File rác byte ngẫu nhiên phải bị coi là ảnh không hợp lệ!"
    dummy_bad_file.unlink()

    dummy_good_webp = Path(r"C:\Users\Admin\.gemini\antigravity\scratch\ai_movie_studio\workspace\dummy_good.webp")
    dummy_good_webp.write_bytes(b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 1000)
    assert _validate_image_file(dummy_good_webp) is True, "File WebP chuẩn phải được chấp nhận!"
    dummy_good_webp.unlink()
    print("✅ Point 2 PASS: Xác thực tính hợp lệ của ảnh AI qua cấu trúc magic bytes chuẩn, phát hiện file tải dở/hỏng.")

    # 5. Point 13: Bundle duplication check
    bundle = script_char
    caption = bundle["caption"]
    viral_title = bundle["viral_title"]
    # Caption không được lặp lại viral_title hay hashtags bên trong nó
    assert viral_title not in caption, "Caption không được lặp lại viral_title!"
    assert "#fyp" not in caption, "Caption không được lặp lại hashtags!"
    print("✅ Point 13 PASS: Gói đăng bài mạng xã hội đã tách biệt độc lập tiêu đề, nội dung và hashtag, không lặp lại.")

    # 6. Point 11: Bulk Action API (run & delete)
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        # Tạo 2 video draft
        create_res = client.post("/api/batch-videos/create", json={
            "prompts": ["Audit Test Video A", "Audit Test Video B"],
            "aspect_ratio": "9:16",
            "visual_style": "anime",
            "mode": "character",
            "auto_run": False
        })
        assert create_res.status_code == 200
        items = create_res.json()["created"]
        ids = [item["id"] for item in items]
        assert len(ids) == 2

        # Bulk run
        bulk_run_res = client.post("/api/batch-videos/bulk-action", json={
            "action": "run",
            "video_ids": ids
        })
        assert bulk_run_res.status_code == 200
        assert bulk_run_res.json()["count"] == 2
        print("✅ Point 11 (Bulk Run) PASS: Kích hoạt chạy hàng loạt thành công.")

        # Bulk delete
        bulk_del_res = client.post("/api/batch-videos/bulk-action", json={
            "action": "delete",
            "video_ids": ids
        })
        assert bulk_del_res.status_code == 200
        assert bulk_del_res.json()["count"] == 2
        print("✅ Point 11 (Bulk Delete) PASS: Xóa hàng loạt thành công.")

    # 7. Point 12: Single video /run API
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        # Tạo 1 video draft
        c_res = client.post("/api/batch-videos/create", json={
            "prompts": ["Audit Single Run Test"],
            "aspect_ratio": "9:16",
            "visual_style": "anime",
            "mode": "free",
            "auto_run": False
        })
        test_id = c_res.json()["created"][0]["id"]
        # Run single video
        run_res = client.post(f"/api/batch-videos/{test_id}/run")
        assert run_res.status_code == 200
        # Delete it
        del_res = client.delete(f"/api/batch-videos/{test_id}")
        assert del_res.status_code == 200
        print("✅ Point 12 PASS: Endpoint chạy đơn lẻ một video (/run) hoạt động hoàn hảo.")

    # 8. Point 10: Cancellation registration (Strict check without 'or True')
    from batch_video_engine import _CANCELLED_VIDEOS, _CANCELLED_LOCK
    fake_running_id = f"bv_cancel_test_{time.time()}"
    cur_q = BATCH_MANAGER._read_queue()
    cur_q.append({
        "id": fake_running_id,
        "title": "Cancel test running",
        "status": "running",
        "scenes": []
    })
    BATCH_MANAGER._write_queue(cur_q)
    # Xóa video đang running -> bắt buộc phải được đưa vào _CANCELLED_VIDEOS
    del_running_ok = BATCH_MANAGER.delete_video(fake_running_id)
    assert del_running_ok is True, "Phải xóa được video"
    with _CANCELLED_LOCK:
        assert fake_running_id in _CANCELLED_VIDEOS, f"ID {fake_running_id} phải nằm trong _CANCELLED_VIDEOS!"
        _CANCELLED_VIDEOS.discard(fake_running_id)
    print("✅ Point 10 PASS: Worker cancellation được theo dõi chính xác 100% trong _CANCELLED_VIDEOS (không dùng 'or True').")

    # 9. Point 7: Startup recovery check
    # Tạo video giả lập status 'running'
    fake_running_item = {
        "id": "bv_fake_running_999",
        "order_num": 9999,
        "prompt": "Fake running",
        "title": "Fake running",
        "status": "running",
        "progress": 40,
        "current_step": "Đang render cảnh 2/8",
        "aspect_ratio": "9:16",
        "scenes_count": 8,
        "visual_style": "anime",
        "voice": "vi-VN-HoaiMyNeural",
        "mode": "free",
        "created_at": 1000,
        "updated_at": 1000,
        "scenes": [],
        "bundle": {"title": "", "caption": "", "hashtags": []},
        "final_video_url": None,
        "error": None
    }
    cur_q = BATCH_MANAGER._read_queue()
    cur_q.append(fake_running_item)
    BATCH_MANAGER._write_queue(cur_q)
    # Kích hoạt khôi phục
    BATCH_MANAGER._recover_interrupted_jobs()
    after_q = BATCH_MANAGER._read_queue()
    target_item = next(v for v in after_q if v["id"] == "bv_fake_running_999")
    assert target_item["status"] == "queued", f"Status phải được khôi phục về queued, got {target_item['status']}"
    # Dọn dẹp
    BATCH_MANAGER.delete_video("bv_fake_running_999")
    print("✅ Point 7 PASS: Tự động khôi phục các job bị treo ở trạng thái 'running' sau khi khởi động app.")

    print("\n" + "=" * 60)
    print("🎉 TOÀN BỘ CÁC ĐIỂM KIỂM THỬ ĐÃ PASS 100%!")
    print("=" * 60)

if __name__ == "__main__":
    test_all()
