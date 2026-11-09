"""qc.py — Kiểm tra chất lượng (QC) kỹ thuật cho clip render xong.

Mục đích: sau mỗi scene render xong, kiểm tra clip trả về có đúng kỹ thuật
không — trước khi ghép phim / báo hoàn thành. CHỈ kiểm tra kỹ thuật, KHÔNG
kiểm tra nội dung (không phát hiện được lỗi nội dung như kính lệch, mặt biến
dạng, sai nhân vật — những lỗi đó vẫn cần mắt người xem lại).

Kiểm tra:
- Tồn tại + đúng định dạng video (ffprobe đọc được stream video).
- Thời lượng thực tế trong dung sai ±1s so với thời lượng kỳ vọng.
- Đúng phân giải (khi có kỳ vọng) và khung hình (fps) hợp lệ.
- Không có khung đen hoàn toàn (đen >= 90% thời lượng) và không có đoạn đen
  kéo dài (>= 2s liên tục).
- Không có khung đứng hình kéo dài (>= 2s liên tục, freezedetect).

Clip không đạt KHÔNG bị xóa — chỉ gắn cờ qc_failed + lý do để Monitor hiện
badge/cảnh báo, người dùng tự quyết định render lại hay giữ.

Dùng ffprobe (hệ thống) nếu có, fallback sang ffmpeg của imageio_ffmpeg để
đọc metadata. Black/freeze detect chạy 1 lượt ffmpeg duy nhất.
"""

import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

# Dung sai / ngưỡng QC (đơn vị giây, pixel, fps).
QC_DURATION_TOLERANCE_S = 1.0        # thời lượng lệch tối đa cho phép
QC_BLACK_FULL_RATIO = 0.9            # đen >= 90% thời lượng = "đen hoàn toàn"
QC_BLACK_SEGMENT_S = 2.0             # đoạn đen liên tục >= 2s = "đen kéo dài"
QC_FREEZE_SEGMENT_S = 2.0            # đứng hình liên tục >= 2s = "đứng hình kéo dài"
QC_MIN_FPS = 5.0                     # fps dưới ngưỡng này coi như clip hỏng
QC_MAX_FPS = 120.0
QC_FPS_TOLERANCE = 2.0               # lệch fps cho phép khi có kỳ vọng

# Phân giải chuẩn 720p mà compose_scene_video scale tới (composer.py):
# 720p mặc định — 9:16 -> 720x1280, 16:9 -> 1280x720.
SIZE_TO_RESOLUTION_720P = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "1:1": (720, 720),
    "4:3": (960, 720),
    "21:9": (1280, 540),
}


def expected_resolution_for_size(size: Optional[str], quality: str = "720p") -> Tuple[int, int]:
    """Phân giải kỳ vọng của clip đã compose theo tỉ lệ khung hình.

    Mặc định 720p (compose_scene_video không truyền quality cũng dùng 720p).
    quality 1080p/preview nhân hệ số tương ứng; tỉ lệ lạ -> suy ra từ tỉ lệ.
    """
    base = SIZE_TO_RESOLUTION_720P.get((size or "16:9").strip(), (1280, 720))
    q = (quality or "720p").lower()
    if q == "1080p":
        return (base[0] * 3 // 2, base[1] * 3 // 2)
    if q == "preview":
        return (base[0] * 2 // 3, base[1] * 2 // 3)
    if q == "4k":
        return (base[0] * 3, base[1] * 3)
    return base


def _ffprobe_exe() -> Optional[str]:
    return shutil.which("ffprobe")


def _ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _parse_fps(raw: Any) -> Optional[float]:
    """'24/1' -> 24.0 ; '30000/1001' -> 29.97 ; 24 -> 24.0."""
    try:
        if raw is None:
            return None
        s = str(raw).strip()
        if "/" in s:
            num, den = s.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return None
            return float(num) / den_f
        return float(s)
    except (TypeError, ValueError):
        return None


def probe_clip(path: str) -> Dict[str, Any]:
    """Đọc metadata clip: duration_s, width, height, fps, codec, has_audio.

    Trả về dict luôn có key "error" (None nếu đọc được). Không raise.
    """
    info: Dict[str, Any] = {
        "duration_s": None, "width": None, "height": None,
        "fps": None, "codec": None, "has_audio": False, "error": None,
    }
    if not path or not os.path.isfile(path):
        info["error"] = "không tìm thấy file clip"
        return info
    if os.path.getsize(path) < 100:
        info["error"] = "file clip quá nhỏ/rỗng"
        return info

    ffprobe = _ffprobe_exe()
    if ffprobe:
        try:
            res = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace", timeout=30,
            )
            data = json.loads(res.stdout or "{}")
            streams = data.get("streams") or []
            video = next((s for s in streams if s.get("codec_type") == "video"), None)
            if video is None:
                info["error"] = "ffprobe không thấy video stream"
                return info
            info["width"] = int(video["width"]) if video.get("width") else None
            info["height"] = int(video["height"]) if video.get("height") else None
            info["fps"] = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
            info["codec"] = video.get("codec_name")
            info["has_audio"] = any(s.get("codec_type") == "audio" for s in streams)
            fmt = data.get("format") or {}
            try:
                info["duration_s"] = float(fmt["duration"]) if fmt.get("duration") else None
            except (TypeError, ValueError):
                info["duration_s"] = None
            if info["duration_s"] is None:
                try:
                    info["duration_s"] = float(video["duration"])
                except (KeyError, TypeError, ValueError):
                    pass
            return info
        except Exception as exc:  # ffprobe lỗi -> fallback ffmpeg -i
            info["error"] = f"ffprobe lỗi: {exc}"

    # Fallback: parse stderr của `ffmpeg -i` (kiểu get_media_duration).
    try:
        ffmpeg = _ffmpeg_exe()
        res = subprocess.run(
            [ffmpeg, "-i", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, errors="replace", timeout=30,
        )
        err = res.stderr or ""
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
        if m:
            info["duration_s"] = float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
        vm = re.search(r"Stream #\d+:\d+.*Video:\s*([a-zA-Z0-9_]+)[^,]*,\s*[^,]*,\s*(\d+)x(\d+)[^,]*,\s*([\d.]+)\s*fps", err)
        if vm:
            info["codec"] = vm.group(1)
            info["width"] = int(vm.group(2))
            info["height"] = int(vm.group(3))
            info["fps"] = float(vm.group(4))
        else:
            vm2 = re.search(r"Stream #\d+:\d+.*Video:.*?(\d+)x(\d+)", err)
            if vm2:
                info["width"] = int(vm2.group(1))
                info["height"] = int(vm2.group(2))
        info["has_audio"] = bool(re.search(r"Stream #\d+:\d+.*Audio:", err))
        if info["width"] is None and info["duration_s"] is None:
            info["error"] = info.get("error") or "ffmpeg không đọc được metadata clip"
        else:
            info["error"] = None
    except Exception as exc:
        info["error"] = f"ffmpeg probe lỗi: {exc}"
    return info


def _detect_black_and_freeze(path: str, timeout: float = 120.0,
                             duration_s: Optional[float] = None) -> Dict[str, Any]:
    """1 lượt ffmpeg: blackdetect + freezedetect trên toàn clip.

    Trả về {"black_segments": [(start, end, dur)], "black_s": float,
             "freeze_segments": [...], "freeze_s": float, "error": None|str}.
    LƯU Ý: freezedetect không flush đoạn đứng hình chạy tới cuối clip (chỉ
    log freeze_start, không có freeze_end/duration) — đoạn dở đó được tính
    là đứng hình tới hết thời lượng clip (cần duration_s từ probe).
    """
    out: Dict[str, Any] = {
        "black_segments": [], "black_s": 0.0,
        "freeze_segments": [], "freeze_s": 0.0, "error": None,
    }
    try:
        ffmpeg = _ffmpeg_exe()
        vf = (
            f"blackdetect=d=0.5:pix_th=0.10:pic_th=0.98,"
            f"freezedetect=n=-60dB:d={QC_FREEZE_SEGMENT_S}"
        )
        res = subprocess.run(
            [ffmpeg, "-nostdin", "-i", path, "-vf", vf,
             "-an", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, errors="replace", timeout=timeout,
        )
        err = res.stderr or ""
        if res.returncode != 0:
            # Lọc bỏ dòng version/config cho ngắn gọn.
            tail = "\n".join(
                ln for ln in err.splitlines()
                if ln and not ln.startswith(("ffmpeg version", "  built with", "  configuration", "  lib"))
            )[-500:]
            out["error"] = f"ffmpeg detect lỗi (exit {res.returncode}): {tail.strip()}"
            return out
        # blackdetect log cả 3 key trên 1 dòng; freezedetect log mỗi key 1
        # dòng (lavfi.freezedetect.freeze_start/end/duration) -> parse riêng.
        for m in re.finditer(
            r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)\s+black_duration:\s*([\d.]+)", err
        ):
            seg = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
            out["black_segments"].append(seg)
            out["black_s"] += seg[2]
        f_starts = re.findall(r"freeze_start:\s*([\d.]+)", err)
        f_ends = re.findall(r"freeze_end:\s*([\d.]+)", err)
        f_durs = re.findall(r"freeze_duration:\s*([\d.]+)", err)
        for s, e, d in zip(f_starts, f_ends, f_durs):
            seg = (float(s), float(e), float(d))
            out["freeze_segments"].append(seg)
            out["freeze_s"] += seg[2]
        # Đoạn đứng hình dở (có freeze_start nhưng không có freeze_end):
        # freezedetect không flush khi freeze chạy tới cuối clip -> tính tới
        # hết thời lượng clip.
        if len(f_starts) > len(f_durs) and duration_s:
            s = float(f_starts[len(f_durs)])
            seg = (s, float(duration_s), float(duration_s) - s)
            if seg[2] > 0:
                out["freeze_segments"].append(seg)
                out["freeze_s"] += seg[2]
    except subprocess.TimeoutExpired:
        out["error"] = "QC black/freeze detect quá thời gian chờ"
    except Exception as exc:
        out["error"] = f"QC black/freeze detect lỗi: {exc}"
    return out


def qc_clip(
    path: str,
    expected_duration: Optional[float] = None,
    expected_width: Optional[int] = None,
    expected_height: Optional[int] = None,
    expected_fps: Optional[float] = None,
) -> Dict[str, Any]:
    """QC kỹ thuật 1 clip. Trả về dict JSON-serializable:

    {"passed": bool, "checks": {duration/resolution/fps/black/freeze: pass|fail|skip},
     "issues": [str tiếng Việt], "duration_s": float|None, "width": ..., "height": ...,
     "fps": ..., "has_audio": bool, "black_s": float, "freeze_s": float, "error": None|str}
    """
    checks: Dict[str, str] = {
        "duration": "skip", "resolution": "skip", "fps": "skip",
        "black": "skip", "freeze": "skip",
    }
    issues: List[str] = []
    result: Dict[str, Any] = {
        "passed": False, "checks": checks, "issues": issues,
        "duration_s": None, "width": None, "height": None,
        "fps": None, "has_audio": False,
        "black_s": 0.0, "freeze_s": 0.0, "error": None,
    }

    info = probe_clip(path)
    result.update({
        "duration_s": info["duration_s"], "width": info["width"],
        "height": info["height"], "fps": info["fps"],
        "has_audio": info["has_audio"], "error": info["error"],
    })
    if info["error"] or info["width"] is None:
        issues.append(f"Không đọc được clip: {info['error'] or 'thiếu video stream'}")
        return result

    # ── Thời lượng (±1s) ──
    if expected_duration is not None:
        try:
            exp = float(expected_duration)
            act = float(info["duration_s"]) if info["duration_s"] is not None else None
            if act is None:
                checks["duration"] = "fail"
                issues.append("Không đo được thời lượng clip")
            elif abs(act - exp) <= QC_DURATION_TOLERANCE_S:
                checks["duration"] = "pass"
            else:
                checks["duration"] = "fail"
                issues.append(
                    f"Sai thời lượng: thực tế {act:.1f}s, kỳ vọng {exp:.1f}s "
                    f"(lệch {abs(act - exp):.1f}s > ±{QC_DURATION_TOLERANCE_S:.0f}s)"
                )
        except (TypeError, ValueError):
            checks["duration"] = "fail"
            issues.append("Thời lượng kỳ vọng không hợp lệ")

    # ── Phân giải ──
    if expected_width is not None and expected_height is not None:
        if info["width"] == expected_width and info["height"] == expected_height:
            checks["resolution"] = "pass"
        else:
            checks["resolution"] = "fail"
            issues.append(
                f"Sai phân giải: thực tế {info['width']}x{info['height']}, "
                f"kỳ vọng {expected_width}x{expected_height}"
            )
    elif info["width"] and info["height"]:
        # Không có kỳ vọng: chỉ ghi nhận (pass nếu kích thước hợp lý).
        checks["resolution"] = "pass" if min(info["width"], info["height"]) >= 240 else "fail"
        if checks["resolution"] == "fail":
            issues.append(f"Phân giải quá thấp: {info['width']}x{info['height']}")

    # ── Khung hình (fps) ──
    if info["fps"] is None:
        checks["fps"] = "fail"
        issues.append("Không đo được fps của clip")
    elif expected_fps is not None:
        if abs(float(info["fps"]) - float(expected_fps)) <= QC_FPS_TOLERANCE:
            checks["fps"] = "pass"
        else:
            checks["fps"] = "fail"
            issues.append(
                f"Sai fps: thực tế {info['fps']:.1f}, kỳ vọng {float(expected_fps):.1f}"
            )
    elif QC_MIN_FPS <= float(info["fps"]) <= QC_MAX_FPS:
        checks["fps"] = "pass"
    else:
        checks["fps"] = "fail"
        issues.append(f"fps bất thường: {info['fps']}")

    # ── Khung đen / đứng hình (1 lượt ffmpeg) ──
    dur = float(info["duration_s"]) if info["duration_s"] else 0.0
    det = _detect_black_and_freeze(path, duration_s=dur if dur > 0 else None)
    result["black_s"] = round(det["black_s"], 3)
    result["freeze_s"] = round(det["freeze_s"], 3)
    if det["error"]:
        # Công cụ detect lỗi: không kết luận được -> skip 2 check này, ghi rõ.
        checks["black"] = "skip"
        checks["freeze"] = "skip"
        issues.append(f"Không kiểm tra được đen/đứng hình: {det['error']}")
    else:
        if dur > 0 and det["black_s"] >= QC_BLACK_FULL_RATIO * dur:
            checks["black"] = "fail"
            issues.append(
                f"Clip đen hoàn toàn ({det['black_s']:.1f}s/{dur:.1f}s là khung đen)"
            )
        elif any(seg[2] >= QC_BLACK_SEGMENT_S for seg in det["black_segments"]):
            longest = max(seg[2] for seg in det["black_segments"])
            checks["black"] = "fail"
            issues.append(f"Có đoạn đen kéo dài {longest:.1f}s trong clip")
        else:
            checks["black"] = "pass"

        if any(seg[2] >= QC_FREEZE_SEGMENT_S for seg in det["freeze_segments"]):
            longest = max(seg[2] for seg in det["freeze_segments"])
            checks["freeze"] = "fail"
            issues.append(f"Clip đứng hình kéo dài {longest:.1f}s")
        else:
            checks["freeze"] = "pass"

    result["passed"] = not issues
    return result
