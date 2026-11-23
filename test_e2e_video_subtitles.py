# -*- coding: utf-8 -*-
import os
import sys
import time
import httpx
from pathlib import Path

BASE_URL = "http://localhost:8000"

def test_render_with_subs():
    print("Testing end-to-end render with subtitles & character mode...")
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        # Kiểm tra xem có video đã hoàn tất sẵn trong hàng đợi để kiểm tra nhanh (fast mode) hay tạo mới
        queue_res = client.get("/api/batch-videos/queue").json()
        completed_vids = [v for v in queue_res.get("videos", []) if v.get("status") == "completed"]
        
        if completed_vids:
            vid_id = completed_vids[0]["id"]
            print(f"Sử dụng video thành phẩm sẵn có '{vid_id}' để xác minh subtitles & stream video...")
        else:
            # Tạo 1 video 3 cảnh và chờ với timeout thực tế 360s (tránh timeout khi Muse2API tải chậm)
            res = client.post("/api/batch-videos/create", json={
                "prompts": ["Bí ẩn căn nhà gỗ trong rừng sâu"],
                "aspect_ratio": "9:16",
                "scenes_count": 3,
                "visual_style": "anime",
                "mode": "character",
                "auto_run": True
            })
            assert res.status_code == 200, res.text
            vid_id = res.json()["created"][0]["id"]
            print(f"Created video: {vid_id}, waiting up to 360s for rendering...")

            # Poll status with 360s timeout (Point 9: chống timeout giả)
            for loop in range(120):
                time.sleep(3)
                status_res = client.get(f"/api/batch-videos/{vid_id}")
                if status_res.status_code == 200:
                    v = status_res.json()
                    if loop % 5 == 0:
                        print(f"[{v.get('status')}] {v.get('progress')}% - {v.get('current_step')}")
                    if v.get("status") in ("completed", "failed"):
                        break

        v_final = client.get(f"/api/batch-videos/{vid_id}").json()
        assert v_final.get("status") == "completed", f"Video render failed: {v_final.get('error')}"
        print(f"Video '{vid_id}' completed successfully!")

        # Verify SRT file
        srt_res = client.get(f"/api/batch-videos/{vid_id}/subtitles")
        assert srt_res.status_code == 200, f"SRT endpoint failed: {srt_res.status_code}"
        assert len(srt_res.text) > 20, "SRT content is too short"
        assert "-->" in srt_res.text, "SRT timecodes missing"
        print(f"✅ Subtitles SRT verified:\n{srt_res.text[:200]}...")

        # Verify final MP4
        mp4_res = client.get(f"/api/batch-videos/{vid_id}/video")
        assert mp4_res.status_code == 200
        assert len(mp4_res.content) > 10000
        print(f"✅ Final MP4 video with subtitles verified! Size: {len(mp4_res.content)} bytes")

if __name__ == "__main__":
    test_render_with_subs()
