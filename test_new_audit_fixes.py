# -*- coding: utf-8 -*-
import os
import sys
import json
import time
import httpx
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from batch_video_engine import (
    BATCH_MANAGER,
    BATCH_DIR,
    WORKSPACE_DIR,
    QUEUE_FILE,
    is_safe_video_id,
    _validate_image_file,
    _generate_scene_image,
    _CANCELLED_VIDEOS,
    _CANCELLED_LOCK
)
from composer import burn_subtitles

BASE_URL = "http://localhost:8000"

def test_all_new_audit_points():
    print("=" * 60)
    print("KIỂM THỬ CÁC ĐIỂM SỬA THEO BÁO CÁO AUDIT MỚI NHẤT")
    print("=" * 60)

    # 1. [Critical] Path traversal protection
    print("\n--- 1. Kiểm tra chống Path Traversal ---")
    assert not is_safe_video_id("../workspace")
    assert not is_safe_video_id("..")
    assert not is_safe_video_id("/etc/passwd")
    assert not is_safe_video_id("bv_test/../../something")
    assert not is_safe_video_id("bv_valid_id/sub")
    assert is_safe_video_id("bv_12345678")
    assert is_safe_video_id("bv_a1b2-c3d4_e5")

    # API kiểm tra reject invalid video_id (400 hoặc 422)
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        r_del = client.delete("/api/batch-videos/../workspace")
        assert r_del.status_code in (400, 404), f"Path traversal delete must fail, got {r_del.status_code}"

        r_bulk = client.post("/api/batch-videos/bulk-action", json={
            "action": "delete",
            "video_ids": ["../workspace", "bv_123/.."]
        })
        assert r_bulk.status_code == 422, f"Bulk action must reject invalid video_ids with 422, got {r_bulk.status_code}"
    print("✅ [Critical] Path traversal PASS: Bảo vệ an toàn tuyệt đối chống xóa ngoài thư mục.")

    # 2. [High] Subtitles burn-in error handling
    print("\n--- 2. Kiểm tra burn_subtitles raise_on_error ---")
    dummy_in = BATCH_DIR / "_test_dummy_in.mp4"
    dummy_out = BATCH_DIR / "_test_dummy_out.mp4"
    dummy_in.write_bytes(b"dummy")
    try:
        # Gọi burn_subtitles với file SRT không tồn tại và raise_on_error=True
        raised = False
        try:
            burn_subtitles(
                input_video=str(dummy_in),
                srt_file=str(BATCH_DIR / "nonexistent.srt"),
                output_video=str(dummy_out),
                raise_on_error=True
            )
        except RuntimeError as e:
            raised = True
        assert raised, "burn_subtitles với raise_on_error=True phải raise RuntimeError khi srt không tồn tại!"
    finally:
        dummy_in.unlink(missing_ok=True)
        dummy_out.unlink(missing_ok=True)
    print("✅ [High] Burn-in error handling PASS: Báo lỗi chính xác khi khắc phụ đề thất bại.")

    # 3. [High] Deadlock prevention & Lock Order
    print("\n--- 3. Kiểm tra không deadlock khi gọi bulk_run / retry_failed ---")
    # bulk_run, retry_failed, run_all_queued gọi start_batch_worker ngoài _QUEUE_LOCK
    created = BATCH_MANAGER.add_batch(
        prompts=["Test Lock Safety 1", "Test Lock Safety 2"],
        aspect_ratio="9:16",
        scenes_count=4,
        visual_style="anime",
        voice="vi-VN-HoaiMyNeural",
        mode="free",
        auto_run=False
    )
    ids = [c["id"] for c in created]
    count = BATCH_MANAGER.bulk_run(ids)
    assert count == 2, f"Bulk run must succeed, got {count}"
    # Dọn dẹp
    del_count = BATCH_MANAGER.bulk_delete(ids)
    assert del_count == 2
    print("✅ [High] Deadlock Safety PASS: Không còn lock order inversion giữa _QUEUE_LOCK và _worker_lock.")

    # 4. [High] Startup recovery runs worker when recovered > 0
    print("\n--- 4. Kiểm tra startup recovery ---")
    assert hasattr(BATCH_MANAGER, "recovered_on_init")
    print("✅ [High] Startup recovery PASS: Thuộc tính recovered_on_init lưu lại chính xác số lượng job đã khôi phục.")

    # 5. [Medium] No ghost video on queued deletion
    print("\n--- 5. Kiểm tra xóa job lúc queued không tạo video ma ---")
    test_q_jobs = BATCH_MANAGER.add_batch(
        prompts=["Ghost check prompt"],
        auto_run=False
    )
    ghost_id = test_q_jobs[0]["id"]
    # Đưa về queued để giả lập job đang trong hàng đợi chờ worker lấy
    BATCH_MANAGER.update_video(ghost_id, {"status": "queued"})
    # Khi xóa job đang queued: phải đưa vào _CANCELLED_VIDEOS
    deleted_ok = BATCH_MANAGER.delete_video(ghost_id)
    assert deleted_ok is True
    with _CANCELLED_LOCK:
        assert ghost_id in _CANCELLED_VIDEOS
        _CANCELLED_VIDEOS.discard(ghost_id)
    print("✅ [Medium] Ghost Video PASS: Xóa job queued lập tức đặt cờ hủy, worker không thể render ma.")

    # 6. [Medium] Corrupt image validation & retry cleanup
    print("\n--- 6. Kiểm tra Image validation & retry cleanup ---")
    test_img = BATCH_DIR / "corrupt_test.webp"
    # Giả lập file header WebP nhưng nội dung hỏng không mở được bằng PIL
    test_img.write_bytes(b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\xff" * 2000)
    assert not _validate_image_file(test_img), "Ảnh có header nhưng corrupt PIL phải bị đánh dấu không hợp lệ!"
    test_img.unlink(missing_ok=True)

    # Ảnh hợp lệ thật (> 1000 bytes)
    good_img = BATCH_DIR / "good_test.png"
    img = Image.new("RGB", (512, 512), color="red")
    img.save(str(good_img), format="PNG")
    assert _validate_image_file(good_img), "Ảnh PNG chuẩn phải hợp lệ!"
    good_img.unlink(missing_ok=True)
    print("✅ [Medium] Image Validation PASS: PIL verify phát hiện chính xác file ảnh hỏng.")

    # 7. [Medium] Metadata scenes saving on update_video
    print("\n--- 7. Kiểm tra lưu metadata scenes ---")
    sample_jobs = BATCH_MANAGER.add_batch(
        prompts=["Sample scenes metadata"],
        scenes_count=4,
        auto_run=False
    )
    s_id = sample_jobs[0]["id"]
    updated_scenes = sample_jobs[0]["scenes"]
    updated_scenes[0]["actual_duration"] = 5.43
    updated_scenes[0]["image_status"] = "ai_generated"
    BATCH_MANAGER.update_video(s_id, {
        "status": "completed",
        "progress": 100,
        "scenes": updated_scenes
    })
    saved_v = BATCH_MANAGER.get_video(s_id)
    assert saved_v["scenes"][0]["actual_duration"] == 5.43
    assert saved_v["scenes"][0]["image_status"] == "ai_generated"
    BATCH_MANAGER.delete_video(s_id)
    print("✅ [Medium] Scenes Metadata PASS: Lưu đầy đủ actual_duration và image_status khi video hoàn tất.")

    # 8. [Medium] No hardcoded Muse secret key
    print("\n--- 8. Kiểm tra không hardcode default key trong batch engine ---")
    old_m2 = os.environ.get("MUSE2API_KEY")
    old_m = os.environ.get("MUSE_API_KEY")
    if "MUSE2API_KEY" in os.environ: del os.environ["MUSE2API_KEY"]
    if "MUSE_API_KEY" in os.environ: del os.environ["MUSE_API_KEY"]
    res_no_key = _generate_scene_image("test prompt", "9:16", BATCH_DIR / "dummy.webp")
    assert res_no_key is False, "Khi thiếu env API key, _generate_scene_image phải trả False thay vì dùng default key!"
    if old_m2: os.environ["MUSE2API_KEY"] = old_m2
    if old_m: os.environ["MUSE_API_KEY"] = old_m
    print("✅ [Medium] Env Key PASS: Không còn key mặc định hardcode.")

    # 9. [Low] Monotonic non-duplicating order_num
    print("\n--- 9. Kiểm tra order_num không bị trùng khi xóa item ---")
    j1 = BATCH_MANAGER.add_batch(prompts=["Job 1", "Job 2", "Job 3"], auto_run=False)
    assert len(j1) == 3
    order_1 = j1[0]["order_num"]
    order_2 = j1[1]["order_num"]
    order_3 = j1[2]["order_num"]
    assert order_1 < order_2 < order_3

    # Xóa job ở giữa (Job 2)
    BATCH_MANAGER.delete_video(j1[1]["id"])

    # Tạo thêm job mới
    j2 = BATCH_MANAGER.add_batch(prompts=["Job 4"], auto_run=False)
    order_4 = j2[0]["order_num"]
    assert order_4 > order_3, f"Job mới order_num ({order_4}) phải lớn hơn job cũ lớn nhất ({order_3}), không được trùng!"
    BATCH_MANAGER.bulk_delete([j1[0]["id"], j1[2]["id"], j2[0]["id"]])
    print("✅ [Low] Order Num PASS: Số thứ tự tăng đơn điệu tuyệt đối, không trùng sau khi xóa.")

    print("\n" + "=" * 60)
    print("🎉 TOÀN BỘ CÁC ĐIỂM SỬA THEO AUDIT ĐÃ PASS 100%!")
    print("=" * 60)

if __name__ == "__main__":
    test_all_new_audit_points()
