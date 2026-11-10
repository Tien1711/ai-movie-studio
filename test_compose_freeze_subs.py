"""Hồi quy cho 2 lỗi ghép phim (2026-11-09):
- Lag/đứng hình: clip AI fps trộn (24/25/30) + timebase khác nhau làm xfade
  lỗi "timebase do not match" (rơi về pairwise thiếu khung hình) và concat
  stream-copy lệch timeline + đứng hình ở chỗ nối.
- 2 phụ đề: burn_in vừa khắc phụ đề vào video vừa để file .srt cùng tên
  cạnh mp4 trong final/ -> trình phát tự nạp thêm 1 lớp phụ đề.
"""
import os
import re
import subprocess

import pytest

from composer import (
    FFMPEG,
    TARGET_FPS,
    _clips_share_fps,
    _probe_fps,
    compose_full_movie,
    compose_scene_video,
    merge_clips_advanced,
)

CLIP_DUR = 2.0


def _make_clip(path, fps, dur=CLIP_DUR):
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i",
         f"testsrc=size=720x1280:rate={fps},format=yuv420p",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
         "-t", str(dur), "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         path],
        check=True, capture_output=True, text=True,
    )
    return path


def _mixed_clips(tmp_path):
    return [
        _make_clip(str(tmp_path / "c24.mp4"), 24),
        _make_clip(str(tmp_path / "c30.mp4"), 30),
        _make_clip(str(tmp_path / "c25.mp4"), 25),
    ]


def _count_frames(path):
    res = subprocess.run(
        [FFMPEG, "-i", path, "-map", "0:v", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.findall(r"frame=\s*(\d+)", res.stderr or "")
    return int(m[-1]) if m else 0


def _freeze_seconds(path):
    res = subprocess.run(
        [FFMPEG, "-i", path, "-vf", "freezedetect=n=0.003:d=0.5",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    total = 0.0
    for line in (res.stderr or "").splitlines():
        if "freeze_duration" in line:
            try:
                total += float(line.split("freeze_duration:")[1].split()[0])
            except Exception:
                pass
    return total


def _results(clips):
    return [
        {"index": i + 1, "status": "done", "video_path": c,
         "audio_path": None, "audio_duration": CLIP_DUR,
         "voiceover": f"Cảnh {i + 1} lời thoại kiểm tra",
         "voice_segments": []}
        for i, c in enumerate(clips)
    ]


def test_compose_scene_video_normalizes_fps(tmp_path):
    src = _make_clip(str(tmp_path / "in24.mp4"), 24)
    assert _probe_fps(src) == pytest.approx(24.0)
    out = str(tmp_path / "scene.mp4")
    compose_scene_video(src, None, out, audio_duration=CLIP_DUR,
                        quality="720p", aspect_ratio="9:16")
    assert _probe_fps(out) == pytest.approx(TARGET_FPS)
    assert _count_frames(out) == pytest.approx(CLIP_DUR * TARGET_FPS, abs=3)


def test_clips_share_fps(tmp_path):
    a = _make_clip(str(tmp_path / "a.mp4"), 30)
    b = _make_clip(str(tmp_path / "b.mp4"), 30)
    c = _make_clip(str(tmp_path / "c.mp4"), 24)
    assert _clips_share_fps([a, b]) is True
    assert _clips_share_fps([a, c]) is False
    assert _clips_share_fps([a, str(tmp_path / "missing.mp4")]) is False


def test_merge_mixed_fps_none_no_freeze(tmp_path):
    clips = _mixed_clips(tmp_path)
    out = str(tmp_path / "merged_none.mp4")
    merge_clips_advanced(clips, transitions=["none", "none"],
                         sound_bridge_duration=0.0, output_path=out)
    from composer import get_media_duration
    assert get_media_duration(out) == pytest.approx(3 * CLIP_DUR, abs=0.3)
    assert _probe_fps(out) == pytest.approx(TARGET_FPS)
    assert _count_frames(out) == pytest.approx(3 * CLIP_DUR * TARGET_FPS, abs=6)
    assert _freeze_seconds(out) < 0.2


def test_merge_mixed_fps_dissolve_single_pass(tmp_path):
    clips = _mixed_clips(tmp_path)
    out = str(tmp_path / "merged_dissolve.mp4")
    merge_clips_advanced(clips, transitions=["dissolve", "dissolve"],
                         sound_bridge_duration=0.5, output_path=out)
    from composer import get_media_duration
    # 3 clip 2s - 2 dissolve 0.5s = 5.0s; trước fix: xfade lỗi timebase,
    # rơi về pairwise chỉ còn ~120 frame/14s ở bản 5s (tỉ lệ tương tự).
    assert get_media_duration(out) == pytest.approx(5.0, abs=0.3)
    assert _probe_fps(out) == pytest.approx(TARGET_FPS)
    assert _count_frames(out) == pytest.approx(5.0 * TARGET_FPS, abs=6)
    assert _freeze_seconds(out) < 0.2


def test_compose_full_movie_burn_in_no_sidecar_srt(tmp_path):
    clips = _mixed_clips(tmp_path)
    res = compose_full_movie(_results(clips), str(tmp_path), project_title="repro",
                             subtitle_mode="burn_in", quality="720p",
                             transition="none", aspect_ratio="9:16")
    final_dir = tmp_path / "final"
    assert res["srt_path"] is None
    assert res["srt_filename"] is None
    names = [p.name for p in final_dir.iterdir()]
    assert not any(n.endswith(".srt") for n in names), names
    assert any(n.endswith(".mp4") for n in names)


def test_compose_full_movie_srt_only_keeps_srt(tmp_path):
    clips = _mixed_clips(tmp_path)
    res = compose_full_movie(_results(clips), str(tmp_path), project_title="repro",
                             subtitle_mode="srt_only", quality="720p",
                             transition="none", aspect_ratio="9:16")
    assert res["srt_path"] is not None
    assert os.path.exists(res["srt_path"])
    assert os.path.exists(res["video_path"])


def test_compose_full_movie_none_no_srt(tmp_path):
    clips = _mixed_clips(tmp_path)
    res = compose_full_movie(_results(clips), str(tmp_path), project_title="repro",
                             subtitle_mode="none", quality="720p",
                             transition="none", aspect_ratio="9:16")
    assert res["srt_path"] is None
    final_dir = tmp_path / "final"
    names = [p.name for p in final_dir.iterdir()]
    assert not any(n.endswith(".srt") for n in names), names


# ── Fix 2026-11-10 (PR #16 để sót): pipeline bỏ qua srt_path trả về ──────

def test_sync_job_srt_file_from_compose_result(tmp_path):
    import app as app_module
    job = {"id": "job1"}
    srt = tmp_path / "movie_123.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
    app_module._sync_job_srt_file(job, {"srt_path": str(srt)})
    assert job["srt_file"] == "/api/jobs/job1/subtitles"
    # burn_in / none: compose trả srt_path None -> cờ cũ (nếu có) phải bị xóa
    job["srt_file"] = "/api/jobs/job1/subtitles"
    app_module._sync_job_srt_file(job, {"srt_path": None})
    assert "srt_file" not in job
    app_module._sync_job_srt_file(job, {"srt_path": str(tmp_path / "khong_co.srt")})
    assert "srt_file" not in job


def test_subtitles_endpoint_ignores_stale_srt(tmp_path, monkeypatch):
    """Endpoint chỉ phục vụ SRT cùng basename với video hiện tại; file SRT cũ
    còn sót trong final/ (khác timestamp) phải trả 404, không được glob lấy."""
    import app as app_module
    from pathlib import Path

    job_dir = tmp_path / "job1"
    final_dir = job_dir / "final"
    final_dir.mkdir(parents=True)
    (final_dir / "movie_111.srt").write_text("cu\n", encoding="utf-8")
    (final_dir / "movie_222.mp4").write_bytes(b"fake")
    job = {"id": "job1", "final_video": "/api/jobs/job1/video/movie_222.mp4",
           "req": None}
    monkeypatch.setattr(app_module, "get_or_restore_job", lambda jid: job)
    monkeypatch.setattr(app_module, "get_safe_job_dir", lambda jid: job_dir)
    resp = app_module.api_download_subtitles("job1")
    assert resp.status_code == 404
    # SRT cùng basename với video hiện tại thì phục vụ được
    (final_dir / "movie_222.srt").write_text("moi\n", encoding="utf-8")
    resp = app_module.api_download_subtitles("job1")
    assert resp.status_code == 200
    assert Path(resp.path).name == "movie_222.srt"
