import sys
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
"""
composer.py
-----------
Hậu kỳ tự động điện ảnh nâng cao:
  - Kiểm tra tính đầy đủ của các cảnh (Pre-export Scene Audit)
  - Khớp video & audio TTS cho từng scene (hỗ trợ preview scene)
  - Lựa chọn phụ đề linh hoạt: Burn-in, SRT tải về riêng, hoặc Không phụ đề
  - Mix nhạc nền (BGM) đa tầng với volume độc lập (Nhạc nền vs Thoại), Fade in/out
  - Hiệu ứng chuyển cảnh (Transitions: None, Fade, Dissolve)
  - Lựa chọn chất lượng xuất (Preview nhanh, 720p, 1080p, CRF/Bitrate)
  - Hỗ trợ xuất bản nháp (Draft Export) khi thiếu cảnh nếu người dùng xác nhận
"""

import os
import queue
import shutil
import subprocess
import threading
import time
import tempfile
import math
import re
import uuid
import imageio_ffmpeg
from contextvars import ContextVar
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

def _resolve_ffmpeg() -> str:
    """Tu dong phat hien FFmpeg hien dai (>= 4.3 co xfade):
    1. Uu tien bien moi truong IMAGEIO_FFMPEG_EXE neu da dat.
    2. Quet cac vi tri FFmpeg hien dai pho bien tren he thong (Cubase, system PATH, ...).
    3. Fallback sang binary mac dinh di kem imageio-ffmpeg.
    Dong bo nguoc vao os.environ['IMAGEIO_FFMPEG_EXE'] de moi module khac dung chung."""
    env_exe = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if env_exe and os.path.isfile(env_exe):
        return env_exe

    candidates = [
        r"C:\Program Files\Steinberg\Cubase 14\Externals\FFmpeg\5.1.1\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        shutil.which("ffmpeg"),
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            try:
                res = subprocess.run([cand, "-version"], capture_output=True, timeout=5)
                if res.returncode == 0:
                    os.environ["IMAGEIO_FFMPEG_EXE"] = cand
                    return cand
            except Exception:
                pass

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["IMAGEIO_FFMPEG_EXE"] = exe
    return exe


FFMPEG = _resolve_ffmpeg()

_HAS_XFADE_CACHE: Optional[bool] = None


def _has_xfade() -> bool:
    """Kiem tra binary FFmpeg hien hanh co ho tro bo loc xfade khong."""
    global _HAS_XFADE_CACHE
    if _HAS_XFADE_CACHE is not None:
        return _HAS_XFADE_CACHE
    try:
        res = subprocess.run([FFMPEG, "-filters"], capture_output=True, text=True, timeout=10)
        _HAS_XFADE_CACHE = "xfade" in (res.stdout or "")
    except Exception:
        _HAS_XFADE_CACHE = False
    return _HAS_XFADE_CACHE

# ── Chuẩn khung hình (fix lag/đứng hình, 2026-11-09) ────────────────────────
# Clip AI trả về có fps trộn lẫn (24/25/30, có khi VFR) và timebase khác nhau.
# xfade bắt buộc 2 đầu vào cùng timebase (không thì lỗi "First input link
# main timebase ... do not match"), còn concat demuxer stream-copy nối clip
# khác fps sẽ lệch timeline + đứng hình ở chỗ nối. Vì vậy MỌI clip đều được
# chuẩn hóa về CFR 30fps trước khi ghép (TikTok/Reels chuẩn 30fps).
TARGET_FPS = 30


def _probe_fps(clip: str) -> Optional[float]:
    """Đo fps (r_frame_rate) của clip; None nếu đo không được.
    Thử ffprobe trước, fallback sang `ffmpeg -i` (binary đi kèm
    imageio-ffmpeg — luôn có sẵn, còn ffprobe có thể không có trên PATH
    ở máy người dùng)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
             clip],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json
        data = _json.loads(out.stdout or "{}")
        streams = data.get("streams") or []
        if streams:
            num, _, den = (streams[0].get("r_frame_rate") or "0/1").partition("/")
            den_f = float(den or 1)
            if den_f:
                return float(num) / den_f
    except Exception:
        pass
    try:
        res = subprocess.run([FFMPEG, "-i", clip], capture_output=True,
                             text=True, timeout=30)
        m = re.search(r"Video:.*?,\s(\d+(?:\.\d+)?)\s*fps", res.stderr or "")
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def _clips_share_fps(clips: List[str]) -> bool:
    """True khi mọi clip cùng fps (cho phép sai số nhỏ) — đủ an toàn để
    concat stream-copy / concat filter (2 nhánh này yêu cầu cùng fps và
    timebase, không re-encode). Clip đo không được thì coi là KHÔNG cùng
    (đi nhánh encode cho chắc)."""
    fps_list = [_probe_fps(c) for c in clips]
    if any(f is None for f in fps_list):
        return False
    base = fps_list[0]
    return all(abs(f - base) < 0.5 for f in fps_list)


def _probe_video_info(clip: str) -> Dict[str, Any]:
    """Đo đủ thông số 1 clip để quyết định đường ghép: fps, kích thước,
    có audio không, sample rate + số kênh audio. Thử ffprobe trước,
    fallback `ffmpeg -i` (xem _probe_fps). Đo không được -> giá trị None
    (caller coi như không tương thích, đi nhánh encode cho chắc)."""
    info: Dict[str, Any] = {"fps": None, "width": None, "height": None,
                            "has_audio": False, "sample_rate": None,
                            "channels": None}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_entries", "stream=codec_type,width,height,r_frame_rate,sample_rate,channels",
             clip],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json
        data = _json.loads(out.stdout or "{}")
        for st in data.get("streams") or []:
            if st.get("codec_type") == "video" and info["width"] is None:
                info["width"] = st.get("width")
                info["height"] = st.get("height")
                num, _, den = (st.get("r_frame_rate") or "0/1").partition("/")
                den_f = float(den or 1)
                if den_f:
                    info["fps"] = float(num) / den_f
            elif st.get("codec_type") == "audio" and not info["has_audio"]:
                info["has_audio"] = True
                try:
                    info["sample_rate"] = int(st.get("sample_rate") or 0) or None
                except Exception:
                    pass
                info["channels"] = st.get("channels")
        if info["width"] is not None:
            return info
    except Exception:
        pass
    try:
        res = subprocess.run([FFMPEG, "-i", clip], capture_output=True,
                             text=True, timeout=30)
        err = res.stderr or ""
        m = re.search(r"Video:.*?,\s(\d+)x(\d+)", err)
        if m:
            info["width"], info["height"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"Video:.*?,\s(\d+(?:\.\d+)?)\s*fps", err)
        if m:
            info["fps"] = float(m.group(1))
        m = re.search(r"Audio:.*?,\s(\d+)\s*Hz,\s*(mono|stereo|\d+\.\d+|\d+ channels?)", err)
        if m:
            info["has_audio"] = True
            info["sample_rate"] = int(m.group(1))
            info["channels"] = 1 if m.group(2) == "mono" else 2
    except Exception:
        pass
    return info


def _clips_uniform_for_concat(clips: List[str]) -> bool:
    """True chỉ khi mọi clip ĐỒNG NHẤT hoàn toàn: cùng fps, cùng kích thước,
    cùng bố cục audio (có/không audio, sample rate, số kênh). Concat
    demuxer stream-copy KHÔNG re-encode nên mọi khác biệt đều chui thẳng
    vào file đích: khác độ phân giải -> file đổi độ phân giải giữa chừng
    (trình phát đứng hình ở chỗ đổi — tái hiện 2026-11-10: 720p+1080p+720p
    cùng 24fps ra file 144 frame 720p + 72 frame 1080p); khác audio ->
    track audio đứt quãng giữa chừng. Chỉ khi đồng nhất mới được đi
    stream-copy; còn lại phải đi nhánh encode (đã chuẩn hóa)."""
    infos = [_probe_video_info(c) for c in clips]
    if not infos or any(not i["fps"] or not i["width"] or not i["height"] or i["has_audio"] is None
                            or (i["has_audio"] and (not i["sample_rate"] or not i["channels"])) for i in infos):
        return False
    base = infos[0]
    for i in infos[1:]:
        if (abs(i["fps"] - base["fps"]) >= 0.5
                or i["width"] != base["width"] or i["height"] != base["height"]
                or i["has_audio"] != base["has_audio"]
                or i["sample_rate"] != base["sample_rate"]
                or i["channels"] != base["channels"]):
            return False
    return True


def _norm_video_chain(label_in: str, w: int, h: int, label_out: str) -> str:
    """Chuỗi chuẩn hóa video 1 đầu vào: CFR 30fps + scale về kích thước
    chung + cùng timebase — xfade/concat đều bắt buộc các đầu vào đồng
    nhất (khác độ phân giải là xfade lỗi, khác fps/timebase là thiếu
    khung hình)."""
    return (f"{label_in}fps={TARGET_FPS},scale={w}:{h},"
            f"settb=AVTB,format=yuv420p,setpts=PTS-STARTPTS{label_out}")


def _norm_audio_chain(label_in: Optional[str], dur: float, label_out: str) -> str:
    """Chuỗi chuẩn hóa audio 1 đầu vào: 44100Hz stereo. Clip câm
    (label_in None) thì sinh silence bằng anullsrc đủ đúng thời lượng —
    acrossfade/concat đều cần mọi bên đều có audio, thiếu là lỗi cả
    filtergraph (clip AI câm rất hay gặp ở Shot Desk)."""
    if label_in is None:
        return (f"anullsrc=r=44100:cl=stereo,atrim=0:{max(0.1, dur):.3f},"
                f"asetpts=PTS-STARTPTS{label_out}")
    # aresample ép sample rate; aformat ép channel layout stereo (aresample
    # không nhận option 'ocl' trên ffmpeg đi kèm imageio-ffmpeg).
    return (f"{label_in}aresample=44100,aformat=channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS{label_out}")

QUALITY_PROFILES = {
    "draft": {
        "scale_9_16": "scale=480:854",
        "scale_16_9": "scale=854:480",
        "res": {"9:16": (480, 854), "16:9": (854, 480), "1:1": (480, 480), "4:3": (640, 480), "21:9": (854, 360)},
        "crf": "26",
        "preset": "ultrafast",
        "b_a": "128k",
        "b_v": "1500k",
    },
    "540p": {
        "scale_9_16": "scale=540:960",
        "scale_16_9": "scale=960:540",
        "res": {"9:16": (540, 960), "16:9": (960, 540), "1:1": (540, 540), "4:3": (720, 540), "21:9": (960, 406)},
        "crf": "24",
        "preset": "veryfast",
        "b_a": "128k",
        "b_v": "2200k",
    },
    "720p": {
        "scale_9_16": "scale=720:1280",
        "scale_16_9": "scale=1280:720",
        "res": {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (720, 720), "4:3": (960, 720), "21:9": (1280, 540)},
        "crf": "20",
        "preset": "fast",
        "b_a": "192k",
        "b_v": "3500k",
    },
    "1080p": {
        "scale_9_16": "scale=1080:1920",
        "scale_16_9": "scale=1920:1080",
        "res": {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:3": (1440, 1080), "21:9": (1920, 810)},
        "crf": "18",
        "preset": "medium",
        "b_a": "256k",
        "b_v": "8000k",
    },
    "4k": {
        "scale_9_16": "scale=2160:3840",
        "scale_16_9": "scale=3840:2160",
        "res": {"9:16": (2160, 3840), "16:9": (3840, 2160), "1:1": (2160, 2160), "4:3": (2880, 2160), "21:9": (3840, 1620)},
        "crf": "16",
        "preset": "slow",
        "b_a": "320k",
        "b_v": "24000k",
    },
}


def _normalize_aspect_key(aspect_ratio: Optional[str]) -> str:
    ar = (aspect_ratio or "16:9").strip().lower()
    if any(k in ar for k in ("9:16", "9/16", "portrait", "vertical")):
        return "9:16"
    if any(k in ar for k in ("1:1", "1/1", "square")):
        return "1:1"
    if any(k in ar for k in ("4:3", "4/3")):
        return "4:3"
    if any(k in ar for k in ("21:9", "21/9", "ultrawide")):
        return "21:9"
    return "16:9"

SFX_PRESETS = {
    "wind": "anoisesrc=d={dur}:c=pink:r=44100:a=0.3,lowpass=f=800,highpass=f=120,tremolo=f=0.5:d=0.6",
    "rain": "anoisesrc=d={dur}:c=brown:r=44100:a=0.35,bandpass=f=2500:w=1400",
    "footsteps": "anoisesrc=d={dur}:c=brown:a=0.5,lowpass=f=350,tremolo=f=2.0:d=0.9",
    "sword": "sine=f=2400:d={dur},flanger,volume=0.35",
    "whoosh": "anoisesrc=d={dur}:c=pink,bandpass=f=1200:w=800,tremolo=f=1.5:d=0.8",
    "thunder": "anoisesrc=d={dur}:c=brown:a=0.6,lowpass=f=220,tremolo=f=0.8:d=0.8",
    "magic": "sine=f=1200:d={dur},flanger=speed=2:depth=5,volume=0.25",
    "ambience": "anoisesrc=d={dur}:c=pink:a=0.22,lowpass=f=600",
}

# --- Helpers bảo mật / vệ sinh (audit 2026-11-07) ---

_DURATION_CACHE_MAX_SIZE = 512
_DURATION_CACHE: Dict[Tuple[str, float, int], float] = {}
# Audit #11c (2026-11-25): dict tran + check-then-act khong nguyen tu ->
# khoa bao quanh moi get/set/evict (ca o day va o batch_engine.py).
_DURATION_CACHE_LOCK = threading.Lock()


def _unique_suffix() -> str:
    """Hậu tố duy nhất cho file tạm/xuất (ms + uuid ngắn): tránh ghi đè
    khi 2 lần export cùng giây / chạy song song."""
    return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def _concat_list_line(path: str) -> str:
    """Một dòng `file '...'` cho ffmpeg concat demuxer, đã escape dấu `'`
    theo chuẩn ffmpeg (thay ' bằng '\\'')."""
    p = os.path.abspath(path).replace("\\", "/")
    return "file '" + p.replace("'", "'\\''") + "'\n"


def _as_float(value: Any, name: str) -> float:
    """Ép giá trị nội suy vào filter string thành float hữu hạn.
    Chặn filter injection qua volume/noise_db/... (audit §2.10)."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} phải là số, nhận {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} phải là số, nhận {value!r}")
    if not math.isfinite(out):
        raise ValueError(f"{name} phải là số hữu hạn, nhận {value!r}")
    return out


def _safe_sfx_candidate(custom_sfx_dir: Optional[str], sfx_tag: Any) -> Optional[str]:
    """Trả về file SFX thật khớp tag trong custom_sfx_dir, hoặc None.
    Chặn path traversal (audit §2.5): tag chứa '/', '\\' hoặc '..' thì
    raise ValueError; tag không khớp [A-Za-z0-9_-]+ thì bỏ qua lookup
    (rơi về procedural) thay vì nối path bừa."""
    if not custom_sfx_dir or not os.path.isdir(custom_sfx_dir):
        return None
    if not isinstance(sfx_tag, str) or not sfx_tag:
        return None
    if "/" in sfx_tag or "\\" in sfx_tag or ".." in sfx_tag:
        raise ValueError(f"sfx_tag không hợp lệ (chứa path separator): {sfx_tag!r}")
    tag = sfx_tag.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
        return None
    base = Path(custom_sfx_dir).resolve()
    for ext in (".mp3", ".wav", ".aac", ".ogg"):
        cand = (base / f"{tag}{ext}").resolve()
        if cand.is_relative_to(base) and cand.is_file():
            return str(cand)
    return None


def detect_sfx_from_text(text: str) -> Optional[str]:
    """Tự động phân tích từ khóa âm thanh / bối cảnh để chọn preset SFX phù hợp."""
    if not text:
        return None
    t = text.lower()
    if any(k in t for k in ["gió", "wind", "bão", "lốc"]):
        return "wind"
    if any(k in t for k in ["mưa", "rain", "dầm dề"]):
        return "rain"
    if any(k in t for k in ["bước chân", "footsteps", "đi lại", "chạy", "walk"]):
        return "footsteps"
    if any(k in t for k in ["kiếm", "đao", "sword", "vũ khí", "chém", "đâm", "kim loại"]):
        return "sword"
    if any(k in t for k in ["sấm", "sét", "thunder", "nổ", "boom"]):
        return "thunder"
    if any(k in t for k in ["lướt", "bay", "whoosh", "vút", "chớp mắt"]):
        return "whoosh"
    if any(k in t for k in ["phép", "ma thuật", "magic", "hào quang", "phát sáng"]):
        return "magic"
    if any(k in t for k in ["đêm", "công trường", "thành phố", "đô thị", "phòng", "yên tĩnh", "ambient"]):
        return "ambience"
    return None

def generate_sfx_audio(sfx_tag: str, duration: float, output_path: str, custom_sfx_dir: Optional[str] = None) -> str:
    """Tạo hoặc trích xuất file âm thanh SFX thật sự cho cảnh phim."""
    duration = _as_float(duration, "duration")
    # 1. Kiểm tra nếu có file âm thanh thật trong thư mục SFX
    # (tag đã sanitize trong _safe_sfx_candidate: chặn traversal, chỉ
    #  cho phép [A-Za-z0-9_-]; tag khác chỉ dùng procedural bên dưới)
    cand = _safe_sfx_candidate(custom_sfx_dir, sfx_tag)
    if cand:
        cmd = [
            FFMPEG, "-y",
            "-stream_loop", "-1", "-i", cand,
            "-t", f"{max(0.5, duration):.2f}",
            "-c:a", "libmp3lame", "-b:a", "192k",
            output_path
        ]
        _run(cmd, f"Tạo SFX từ file mẫu: {sfx_tag}")
        return output_path

    # 2. Sinh âm thanh SFX thủ tục (procedural audio) điện ảnh chất lượng cao
    norm_tag = sfx_tag.lower().strip()
    preset_expr = SFX_PRESETS.get(norm_tag) or SFX_PRESETS.get("ambience")
    formatted_expr = preset_expr.format(dur=max(0.5, duration))

    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", formatted_expr,
        "-t", f"{max(0.5, duration):.2f}",
        "-c:a", "libmp3lame", "-b:a", "192k",
        output_path
    ]
    _run(cmd, f"Sinh SFX Procedural: {sfx_tag}")
    return output_path


def get_media_duration(file_path: str) -> float:
    """Đo thời lượng thực tế chính xác (giây) của file audio hoặc video bằng FFmpeg.

    Có cache theo (path, mtime, size), tối đa 512 entry (FIFO evict) để
    compose_full_movie / compute_timeline_offsets / create_srt không đo
    lặp cùng một clip nhiều lần (audit §3.5). Chỉ cache kết quả đo thành
    công.

    Audit #11 (2026-11-25): KHÔNG còn trả 5.0 giả khi đo lỗi nữa — file
    thiếu/lỗi trả 0.0 ("không rõ"). Caller phải coi 0.0 là không rõ, KHÔNG
    được dùng làm thời lượng thật (đặc biệt không truyền `-t 0`/`-t 5.0`
    vào ffmpeg — mix_multitrack_timeline / add_background_music bỏ hẳn
    `-t` khi vid_dur <= 0). Những chỗ thực sự cần một con số để tính
    timeline (compute_timeline_offsets, _merge_single_pass, pairwise)
    tự ước lượng 5.0 TẠI CHỖ với comment ghi rõ — ffmpeg sẽ lỗi to nếu
    clip thật sự hỏng, thay vì lặng lẽ cắt cụt phim."""
    if not file_path or not os.path.exists(file_path):
        return 0.0
    cache_key = None
    try:
        st = os.stat(file_path)
        cache_key = (os.path.abspath(file_path), st.st_mtime, st.st_size)
        with _DURATION_CACHE_LOCK:
            hit = _DURATION_CACHE.get(cache_key)
        if hit is not None:
            return hit
    except Exception:
        cache_key = None
    try:
        res = subprocess.run(
            [FFMPEG, "-i", file_path],
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            errors="replace",
            timeout=30,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr)
        if m:
            dur = float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
            if cache_key is not None:
                with _DURATION_CACHE_LOCK:
                    if len(_DURATION_CACHE) >= _DURATION_CACHE_MAX_SIZE:
                        _DURATION_CACHE.pop(next(iter(_DURATION_CACHE)), None)
                    _DURATION_CACHE[cache_key] = dur
            return dur
    except Exception as exc:
        print(f"[get_media_duration error] {exc}")
    return 0.0


def _fmt_srt_time(seconds: float) -> str:
    """Định dạng giây -> HH:MM:SS,mmm, cascade đúng khi ms tròn lên 1000
    (audit §1.13: 119.9996s trước đây ra 00:01:60,000)."""
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def detect_audio_speech_bounds(media_path: str, noise_db: float = -28.0, min_silence: float = 0.12) -> Tuple[float, float, float]:
    """
    Phát hiện chính xác thời điểm bắt đầu (onset) và kết thúc (offset) của âm thanh/tiếng nói trong file media.
    Sử dụng bộ lọc silencedetect của FFmpeg để phụ đề xuất hiện chuẩn xác 100% khi bắt đầu có tiếng,
    loại bỏ hoàn toàn lỗi phụ đề xuất hiện sớm trước khi nhân vật mở lời hoặc khi khung cảnh còn đang thiết lập.
    Trả về: (speech_start, speech_end, total_duration)
    """
    if not media_path or not os.path.exists(media_path):
        return 0.0, 0.0, 0.0

    noise_db = _as_float(noise_db, "noise_db")
    min_silence = _as_float(min_silence, "min_silence")

    try:
        cmd = [
            FFMPEG, "-i", media_path,
            "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f", "null", "-"
        ]
        r = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, errors="replace", timeout=30)

        # 1. Đo tổng thời lượng
        total_dur = 0.0
        for l in r.stderr.split("\n"):
            if "Duration:" in l:
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", l)
                if m:
                    total_dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                break

        # Nếu không có stream âm thanh
        if "Audio:" not in r.stderr or total_dur <= 0.05:
            return 0.0, total_dur, total_dur

        silence_starts = []
        silence_ends = []
        for l in r.stderr.split("\n"):
            if "silence_start:" in l:
                try:
                    parts = l.split("silence_start:")
                    silence_starts.append(float(parts[1].split()[0]))
                except Exception:
                    pass
            elif "silence_end:" in l:
                try:
                    parts = l.split("silence_end:")
                    silence_ends.append(float(parts[1].split()[0]))
                except Exception:
                    pass

        # Kiểm tra nếu clip câm hoàn toàn (silence bao trùm toàn bộ clip)
        if silence_starts and silence_starts[0] <= 0.05:
            if silence_ends and silence_ends[0] >= total_dur - 0.25:
                return 0.0, 0.0, total_dur

        # Thời điểm âm thanh bắt đầu (speech onset)
        speech_start = 0.0
        if silence_starts and silence_starts[0] <= 0.08:
            if silence_ends:
                speech_start = silence_ends[0]

        # Thời điểm âm thanh kết thúc (speech offset)
        speech_end = total_dur
        if silence_starts:
            last_s_start = silence_starts[-1]
            if silence_ends and silence_ends[-1] >= total_dur - 0.3:
                speech_end = last_s_start
            elif total_dur - last_s_start < 2.5 and last_s_start > speech_start:
                speech_end = last_s_start

        # Đệm 0.25s ở cuối để chữ không biến mất đột ngột khi âm thanh còn dư âm
        speech_end = min(total_dur, speech_end + 0.25)

        if speech_end <= speech_start:
            speech_start = 0.0
            speech_end = total_dur

        return round(speech_start, 3), round(speech_end, 3), round(total_dur, 3)
    except Exception as e:
        print(f"[detect_audio_speech_bounds Warning] {e}")
        dur = get_media_duration(media_path)
        return 0.0, dur, dur


def compute_timeline_offsets(clips: List[str], transitions: Optional[List[str]] = None, sound_bridge_duration: float = 0.0) -> List[float]:
    """
    Tính toán chính xác mốc thời gian bắt đầu (offset) của từng clip trên timeline phim hoàn chỉnh,
    có tính đến thời gian chồng lấn của chuyển cảnh (transitions) và sound bridge (acrossfade).
    """
    sound_bridge_duration = _as_float(sound_bridge_duration, "sound_bridge_duration")
    n = len(clips)
    if n == 0:
        return []
    if n == 1:
        return [0.0]

    # Audit #11: clip do khong duoc (get_media_duration -> 0.0) thi uoc
    # luong 5.0 TAI CHO cho phep tinh timeline — ghi ro chu khong giau
    # trong ham do; ffmpeg se loi to neu clip that su hong.
    durations = [get_media_duration(c) or 5.0 for c in clips]
    t_durs = []
    for i in range(1, n):
        trans = (transitions[i - 1] or "dissolve").lower() if (transitions and (i - 1) < len(transitions)) else "dissolve"
        if trans in ("none", "cut", "match cut") or sound_bridge_duration <= 0.05:
            t_durs.append(0.0)
        else:
            td = max(0.1, min(sound_bridge_duration, min(durations[i - 1], durations[i]) * 0.4))
            t_durs.append(td)

    offsets = [0.0] * n
    curr_offset = 0.0
    for i in range(1, n):
        curr_offset += max(0.0, durations[i - 1] - t_durs[i - 1])
        offsets[i] = round(curr_offset, 3)

    return offsets


# ── Phụ đề ngắn cho Reels/TikTok/YouTube Shorts (fix 2026-11-23) ─────────────
# Trước đây create_srt ghi 1 cue/cảnh = toàn bộ voiceover (tới 420 ký tự) →
# burn-in hiện khối 7–9 dòng, che 40–50% khung hình (video Ăn Khế Trả Vàng ch.1).
# Giờ mỗi cue tối đa 2 dòng, mỗi dòng ≤ 42 ký tự; thời gian chia tỉ lệ theo số
# ký tự trong cửa sổ speech. Voiceover ngắn vẫn ra 1 cue như cũ.
_SRT_MAX_CHARS_PER_LINE = 42
_SRT_MAX_LINES_PER_CUE = 2
_SRT_SENT_END_RE = re.compile(r"(?<=[.!?…])\s+")
_SRT_CLAUSE_END_RE = re.compile(r"(?<=,)\s+")


def _wrap_subtitle_lines(segment: str, max_chars: int = _SRT_MAX_CHARS_PER_LINE) -> List[str]:
    """Ngắt 1 đoạn văn thành các dòng ≤ max_chars, ưu tiên ngắt sau dấu kết câu,
    rồi sau dấu phẩy, cuối cùng mới ngắt từ. Chỉ chèn ngắt dòng, không đổi chữ:
    nối các dòng bằng 1 khoảng trắng sẽ khôi phục đúng đoạn gốc."""
    segment = re.sub(r"\s+", " ", segment or "").strip()
    if not segment:
        return []
    if len(segment) <= max_chars:
        return [segment]
    # Tách thành mệnh đề ở ranh giới câu / dấu phẩy (giữ dấu câu ở cuối mệnh đề).
    chunks: List[str] = []
    for sent in _SRT_SENT_END_RE.split(segment):
        chunks.extend(c for c in _SRT_CLAUSE_END_RE.split(sent) if c.strip())
    lines: List[str] = []
    cur = ""
    for ch in chunks:
        ch = ch.strip()
        if not ch:
            continue
        if not cur:
            cur = ch
        elif len(cur) + 1 + len(ch) <= max_chars:
            cur = f"{cur} {ch}"
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    # Mệnh đề nào vẫn dài hơn giới hạn: ngắt từ; từ đơn quá dài thì cắt cứng.
    wrapped: List[str] = []
    for line in lines:
        if len(line) <= max_chars:
            wrapped.append(line)
            continue
        cur = ""
        for w in line.split(" "):
            while len(w) > max_chars:
                if cur:
                    wrapped.append(cur)
                    cur = ""
                wrapped.append(w[:max_chars])
                w = w[max_chars:]
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= max_chars:
                cur = f"{cur} {w}"
            else:
                wrapped.append(cur)
                cur = w
        if cur:
            wrapped.append(cur)
    return wrapped


def _split_subtitle_cues(text: str) -> List[str]:
    """Chia voiceover 1 cảnh thành các cue phụ đề (mỗi cue ≤ 2 dòng, ≤ 42 ký tự/dòng).
    Ngắt cứng " | " (đã thành "\\n" trước đó) luôn mở cue mới."""
    cues: List[str] = []
    for hard in (text or "").split("\n"):
        lines = _wrap_subtitle_lines(hard)
        for j in range(0, len(lines), _SRT_MAX_LINES_PER_CUE):
            cues.append("\n".join(lines[j:j + _SRT_MAX_LINES_PER_CUE]))
    return cues


def create_srt(
    results: List[Dict],
    output_srt: str,
    clip_durations: Optional[List[float]] = None,
    media_paths: Optional[List[str]] = None,
    timeline_offsets: Optional[List[float]] = None,
) -> str:
    """
    Tạo file .srt đồng bộ chính xác 100% với âm thanh và timeline ghép video.
    - Tự động nhận diện chính xác thời điểm âm thanh/tiếng nói bắt đầu (Audio Speech Onset).
    - Phụ đề chỉ xuất hiện ĐÚNG KHI ÂM THANH BẮT ĐẦU CẤT LÊN, không xuất hiện sớm trước khung cảnh.
    - Tự động bù trừ độ lệch thời gian khi có hiệu ứng chuyển cảnh (transitions / sound bridge).
    - Mỗi cảnh chia thành nhiều cue ngắn (tối đa 2 dòng/cue, ≤ 42 ký tự/dòng), thời gian
      các cue chia tỉ lệ theo số ký tự trong cửa sổ speech của cảnh.
    """
    n = len(results)
    if not timeline_offsets or len(timeline_offsets) < n:
        timeline_offsets = [0.0] * n
        accum = 0.0
        for idx in range(n):
            timeline_offsets[idx] = round(accum, 3)
            dur = clip_durations[idx] if (clip_durations and idx < len(clip_durations)) else float(results[idx].get("duration") or results[idx].get("audio_duration") or 5.0)
            accum += dur

    sub_index = 1
    with open(output_srt, "w", encoding="utf-8") as f:
        for i, r in enumerate(results):
            raw_text = r.get("voiceover", "") or ""
            text = raw_text.replace(" | ", "\n").strip()
            if not text:
                continue

            dur = clip_durations[i] if (clip_durations and i < len(clip_durations)) else float(r.get("duration") or r.get("audio_duration") or 5.0)
            shot_start = timeline_offsets[i] if i < len(timeline_offsets) else 0.0

            # Lấy file clip thực tế để phân tích onset âm thanh
            media_p = None
            if media_paths and i < len(media_paths):
                media_p = media_paths[i]
            elif r.get("clip_path"):
                media_p = r.get("clip_path")
            elif r.get("video_path"):
                media_p = r.get("video_path")

            onset = 0.0
            offset = dur
            if media_p and os.path.exists(media_p):
                sp_start, sp_end, total_d = detect_audio_speech_bounds(media_p)
                if sp_end > sp_start:
                    onset = sp_start
                    offset = sp_end

            # Điểm bắt đầu phụ đề: Khớp chính xác thời điểm âm thanh bắt đầu xuất hiện
            sub_start = round(shot_start + onset, 3)
            # Điểm kết thúc phụ đề: Khớp thời điểm âm thanh kết thúc (đệm tối thiểu 1.5s để người xem kịp đọc)
            sub_end = round(max(sub_start + 1.5, shot_start + offset), 3)
            # Không vượt quá giới hạn của cảnh trên timeline
            boundary = min(shot_start + dur, timeline_offsets[i + 1]) if i + 1 < n else shot_start + dur
            sub_end = min(round(boundary, 3), sub_end)
            if sub_end <= sub_start:
                continue

            # Chia voiceover thành các cue ngắn (tối đa 2 dòng/cue, ≤ 42 ký tự/dòng)
            # để burn-in không che nửa khung hình; thời gian chia tỉ lệ theo số ký tự
            # trong cửa sổ speech [sub_start, sub_end] của cảnh.
            cues = _split_subtitle_cues(text)
            if not cues:
                continue
            cue_chars = [len(c.replace("\n", "")) for c in cues]
            total_chars = sum(cue_chars) or 1
            bounds = [sub_start]
            acc = 0
            for n_chars in cue_chars[:-1]:
                acc += n_chars
                bounds.append(sub_start + (sub_end - sub_start) * acc / total_chars)
            bounds.append(sub_end)
            # Làm tròn mili-giây, ép đơn điệu tăng (≥1ms) để không có cue rỗng;
            # cue cuối chốt đúng sub_end.
            bounds = [round(b, 3) for b in bounds]
            for k in range(1, len(bounds) - 1):
                if bounds[k] <= bounds[k - 1]:
                    bounds[k] = round(bounds[k - 1] + 0.001, 3)
            bounds[-1] = round(sub_end, 3)

            for ci, cue in enumerate(cues):
                f.write(f"{sub_index}\n{_fmt_srt_time(bounds[ci])} --> {_fmt_srt_time(bounds[ci + 1])}\n{cue}\n\n")
                sub_index += 1

    return output_srt


_COMPOSE_CANCEL_CHECK = ContextVar("compose_cancel_check", default=None)


def _check_compose_cancelled() -> None:
    should_cancel = _COMPOSE_CANCEL_CHECK.get()
    if should_cancel is not None and should_cancel():
        raise RuntimeError("Đã hủy xuất phim.")


def run_cancellable_compose(compose, should_cancel, **kwargs):
    token = _COMPOSE_CANCEL_CHECK.set(should_cancel)
    try:
        _check_compose_cancelled()
        return compose(**kwargs)
    finally:
        _COMPOSE_CANCEL_CHECK.reset(token)


def _run_compose_ffmpeg(cmd, *, capture_output=True, text=False, timeout, cwd=None, check=False):
    if _COMPOSE_CANCEL_CHECK.get() is None:
        return subprocess.run(
            cmd, capture_output=capture_output, text=text, timeout=timeout, cwd=cwd, check=check,
        )

    _check_compose_cancelled()
    deadline = time.monotonic() + timeout
    pipe = subprocess.PIPE if capture_output else None
    with subprocess.Popen(cmd, stdout=pipe, stderr=pipe, text=text, cwd=cwd,
                          **({"encoding": "utf-8", "errors": "replace"} if text else {})) as proc:
        try:
            while True:
                _check_compose_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    stdout, stderr = proc.communicate(timeout=min(remaining, 0.2))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            proc.kill()
            proc.communicate()
            raise
        result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def _run(cmd: List[str], label: str = "", timeout: int = 120):
    print(f"[FFmpeg] {label}")
    try:
        result = _run_compose_ffmpeg(cmd, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"FFmpeg timeout ({label}) sau {timeout}s")
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg lỗi ({label}):\n{result.stderr[-2000:]}"
        )


def check_video_has_audio(video_path: str) -> bool:
    """Kiểm tra file video có chứa stream âm thanh hay không."""
    try:
        cmd = [FFMPEG, "-i", video_path]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return "Audio:" in res.stderr
    except Exception:
        return False


def compose_scene_video(
    scene_video: str,
    scene_audio: Optional[str] = None,
    output: str = "",
    audio_duration: Optional[float] = None,
    target_duration: Optional[float] = None,
    quality: str = "720p",
    aspect_ratio: str = "9:16",
    ambience_volume: float = 0.0,
):
    """
    Ghép 1 cảnh: Kéo dài hoặc loop video để khớp đúng với thời lượng audio TTS hoặc target_duration.
    Đảm bảo không cắt ngắn video khi audio ngắn hơn video (giữ trọn vẹn thước phim điện ảnh).
    Tự động chuẩn hóa codec sang H.264 / AAC để đảm bảo tương thích mọi trình duyệt và concat trơn tru.
    Hỗ trợ chuẩn xác cả khung hình dọc 9:16 (Shorts/TikTok/Reels) và khung hình ngang 16:9 (Cinema/YouTube/TV).
    Nếu không có scene_audio (chế độ không lồng tiếng), giữ nguyên 100% âm thanh & giọng gốc do AI sinh ra!
    """
    vf_filters = []
    ar_key = _normalize_aspect_key(aspect_ratio)
    q = (quality or "720p").lower()

    if q in ("preview", "draft"):
        w, h = QUALITY_PROFILES["draft"]["res"][ar_key]
        crf = "28"
        preset = "ultrafast"
    elif q == "540p":
        w, h = QUALITY_PROFILES["540p"]["res"][ar_key]
        crf = "24"
        preset = "veryfast"
    elif q == "1080p":
        w, h = QUALITY_PROFILES["1080p"]["res"][ar_key]
        crf = "18"
        preset = "medium"
    elif q == "4k":
        w, h = QUALITY_PROFILES["4k"]["res"][ar_key]
        crf = "16"
        preset = "slow"
    else:
        # Default 720p
        w, h = QUALITY_PROFILES["720p"]["res"][ar_key]
        crf = "22"
        preset = "fast"

    # Chuẩn hóa về CFR 30fps TRƯỚC scale/pad (fix lag/đứng hình): clip AI có
    # fps trộn (24/25/30/VFR) — không chuẩn hóa thì xfade lỗi timebase và
    # concat stream-copy lệch timeline ở chỗ nối.
    vf_filters.append(f"fps={TARGET_FPS}")
    vf_filters.append(f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    vf_str = ",".join(vf_filters) if vf_filters else "null"

    # Tính toán thời lượng chuẩn: giữ ít nhất thời lượng đầy đủ của video gốc (không lãng phí AI render)
    vid_dur = get_media_duration(scene_video)
    aud_dur = 0.0
    has_audio = False
    if scene_audio and os.path.exists(scene_audio) and os.path.getsize(scene_audio) > 500:
        has_audio = True
        aud_dur = get_media_duration(scene_audio)
    elif audio_duration and float(audio_duration) > 0:
        aud_dur = float(audio_duration)

    # Tính toán thời lượng chuẩn xác theo timeline và kịch bản:
    # Nếu có target_duration, tuân thủ đúng target_duration; chỉ kéo dài nếu audio lồng tiếng (TTS) dài hơn để tránh cụt thoại.
    if target_duration and float(target_duration) > 0:
        t_dur = float(target_duration)
        dur = max(t_dur, aud_dur) if aud_dur > 0 else t_dur
    else:
        dur = max(vid_dur, aud_dur) if (vid_dur > 0 or aud_dur > 0) else 5.0

    if dur <= 0.1:
        dur = 5.0

    # Bảo vệ chống trùng lặp cảnh: nếu video tải về từ Muse bị nối tiếp dài hơn thời lượng kịch bản,
    # tự động cắt đúng phần đuôi (nội dung mới sinh) thay vì lấy nhầm phần đầu (cảnh cũ bị Muse nối vào).
    # LƯU Ý (audit §1.17, kiểm chứng thực tế 2026-11-07): "-ss <offset>" đặt
    # trước "-stream_loop -1 -i" KHÔNG giữ offset khi loop — ffmpeg loop lại
    # từ ĐẦU clip (đoạn đầu cũ) thay vì từ offset. Vì vậy ở đây cắt đuôi
    # thành file tạm re-encode rồi mới loop file tạm đó (loop đúng đoạn đuôi).
    input_video = scene_video
    trimmed_video = None
    seek_args = []
    if vid_dur > dur * 1.15 and (vid_dur - dur) > 0.4:
        offset = max(0.0, vid_dur - dur)
        tail_dur = max(0.1, vid_dur - offset)
        print(f"[compose_scene_video] Canh bao: Video goc ({vid_dur:.2f}s) dai hon thoi luong ({dur:.2f}s). Lay {tail_dur:.2f}s doan duoi moi sinh (offset={offset:.2f}s)...")
        _fd, trimmed_video = tempfile.mkstemp(prefix="composer_tail_", suffix=".mp4")
        os.close(_fd)
        try:
            _run([
                FFMPEG, "-y",
                "-ss", f"{offset:.3f}", "-i", scene_video,
                "-t", f"{tail_dur:.3f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                trimmed_video,
            ], "Cắt đuôi video tích lũy")
        except Exception:
            try: os.remove(trimmed_video)
            except Exception: pass
            trimmed_video = None
            raise
        input_video = trimmed_video
        seek_args = []

    if has_audio:
        speech_filter = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo,apad"
        audio_mapping = ["-map", "1:a:0", "-af", speech_filter]
        if ambience_volume > 0 and check_video_has_audio(input_video):
            mix = (
                f"[1:a]{speech_filter}[speech];"
                "[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={ambience_volume}[ambience];"
                "[speech][ambience]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=false[audio]"
            )
            audio_mapping = ["-filter_complex", mix, "-map", "[audio]"]
        # Có file lồng tiếng riêng (TTS voiceover / dialogue): ghép đè scene_audio vào video
        cmd = [
            FFMPEG, "-y",
            *seek_args,
            "-stream_loop", "-1", "-i", input_video,   # Loop video nếu audio dài hơn
            "-i", scene_audio,
            "-map", "0:v:0",
            *audio_mapping,
            "-vf", vf_str,
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p",
            "-threads", "4",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-t", f"{dur:.3f}",
            output,
        ]
    else:
        # Chế độ không lồng tiếng (Dùng giọng & âm thanh gốc của AI):
        # Kiểm tra xem scene_video có chứa audio stream gốc do AI sinh ra không
        if check_video_has_audio(scene_video):
            # GIỮ NGUYÊN âm thanh / giọng gốc của AI và chuẩn hóa sang 44100Hz stereo AAC
            cmd = [
                FFMPEG, "-y",
                *seek_args,
                "-stream_loop", "-1", "-i", input_video,
                "-map", "0:v:0",
                "-map", "0:a:0",
                "-af", "aresample=44100:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo,apad",
                "-vf", vf_str,
                "-c:v", "libx264", "-preset", preset, "-crf", crf,
                "-pix_fmt", "yuv420p",
                "-threads", "4",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-t", f"{dur:.3f}",
                output,
            ]
        else:
            # Video câm: tạo audio track tĩnh chuẩn 44100Hz stereo để concat demuxer trơn tru
            cmd = [
                FFMPEG, "-y",
                *seek_args,
                "-stream_loop", "-1", "-i", input_video,
                "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-vf", vf_str,
                "-c:v", "libx264", "-preset", preset, "-crf", crf,
                "-pix_fmt", "yuv420p",
                "-threads", "4",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-t", f"{dur:.3f}",
                output,
            ]
    try:
        _run(cmd, f"Ghép scene: {os.path.basename(output)}", timeout=300)
    finally:
        if trimmed_video and os.path.exists(trimmed_video):
            try: os.remove(trimmed_video)
            except Exception: pass


def _merge_single_pass(
    clips: List[str],
    transitions: List[str],
    sound_bridge_duration: float,
    output_path: str,
    progress_cb: Optional[Any] = None,
    quality: str = "720p",
    aspect_ratio: str = "9:16",
) -> str:
    """Ghép toàn bộ clip bằng MỘT lần gọi ffmpeg duy nhất (single-pass):
    - Video: chuỗi xfade nối tiếp cho dissolve/fade; concat cho bước cut.
    - Audio: chuỗi acrossfade nối tiếp cho dissolve/fade; concat cho bước cut.
    Tổng thời lượng = sum(durations) - sum(overlaps), khớp 100% với
    compute_timeline_offsets (3 clip 2s + 2 dissolve 0.5s -> đúng 5.0s).
    Lỗi ở bất kỳ bước nào (khác fps/kích thước/sample-rate...) thì raise để
    caller fallback về nhánh pairwise cũ — không bao giờ dừng đột ngột.
    - quality/aspect_ratio (audit #6, 2026-11-25): kich thuoc dich lay tu
      QUALITY_PROFILES (vd draft 9:16 -> 480x854); profile khong nhan ra
      thi giu kich thuoc clip dau nhu cu.
    """
    n = len(clips)
    # Audit #11: uoc luong 5.0 tai cho cho clip do khong duoc (xem
    # compute_timeline_offsets) — ffmpeg se loi to neu clip that su hong.
    durations = [get_media_duration(c) or 5.0 for c in clips]
    infos = [_probe_video_info(c) for c in clips]
    # Kích thước đích: uu tien profile quality/aspect_ratio duoc chon
    # (audit #6, 2026-11-25); khong co profile thi giu kich thuoc clip
    # dau nhu cu; do khong duoc thi bo scale, xfade loi se roi ve pairwise.
    _w0 = infos[0].get("width") or 0
    _h0 = infos[0].get("height") or 0
    _q = (quality or "720p").lower()
    if _q in ("preview", "draft"):
        _q = "draft"
    _prof = QUALITY_PROFILES.get(_q)
    if _prof is not None:
        _ar = _normalize_aspect_key(aspect_ratio)
        _pw, _ph = _prof["res"].get(_ar, (0, 0))
        if _pw and _ph:
            _w0, _h0 = _pw, _ph
    t_durs: List[float] = []
    for i in range(1, n):
        trans = (transitions[i - 1] or "dissolve").lower()
        if trans in ("none", "cut", "match cut") or sound_bridge_duration <= 0.05:
            t_durs.append(0.0)
        else:
            t_durs.append(max(0.1, min(sound_bridge_duration, min(durations[i - 1], durations[i]) * 0.4)))

    if progress_cb:
        progress_cb(40, f"Đang ghép single-pass xfade ({n} cảnh, 1 lần encode)...")

    parts: List[str] = []
    for i in range(n):
        # Chuẩn hóa MỌI đầu vào về cùng CFR 30fps + cùng kích thước + cùng
        # timebase + cùng audio 44100 stereo (xfade/acrossfade/concat đều
        # bắt buộc đồng nhất). Clip khác độ phân giải mà không scale thì
        # xfade lỗi "Error reinitializing filters" rồi rơi về pairwise
        # (vốn cũng lỗi khung hình — xem nhánh 3b); clip câm mà không
        # anullsrc thì cả filtergraph lỗi vì thiếu stream audio.
        v_in = f"[{i}:v]"
        if _w0 and _h0:
            parts.append(_norm_video_chain(v_in, _w0, _h0, f"[v{i}in]"))
        else:
            parts.append(f"{v_in}fps={TARGET_FPS},settb=AVTB,format=yuv420p,setpts=PTS-STARTPTS[v{i}in]")
        a_in = f"[{i}:a]" if infos[i].get("has_audio") else None
        parts.append(_norm_audio_chain(a_in, durations[i], f"[a{i}in]"))

    # Chuỗi video: xfade cho dissolve/fade (nếu hỗ trợ), concat cho cut (giữ đúng độ dài).
    cur_v = "v0in"
    acc_dur = durations[0]
    has_xf = _has_xfade()
    for i in range(1, n):
        t = t_durs[i - 1]
        if t > 0.05 and has_xf:
            offset = max(0.0, acc_dur - t)
            parts.append(
                f"[{cur_v}][v{i}in]xfade=transition=fade:duration={t:.3f}:offset={offset:.3f}[x{i}]"
            )
        else:
            parts.append(f"[{cur_v}][v{i}in]concat=n=2:v=1:a=0[x{i}]")
        cur_v = f"x{i}"
        acc_dur = acc_dur + durations[i] - (t if (t > 0.05 and has_xf) else 0.0)

    # Chuỗi audio: acrossfade cho dissolve/fade, concat cho cut.
    cur_a = "a0in"
    for i in range(1, n):
        t = t_durs[i - 1]
        if t > 0.05 and has_xf:
            parts.append(f"[{cur_a}][a{i}in]acrossfade=d={t:.3f}[a{i}]")
        else:
            parts.append(f"[{cur_a}][a{i}in]concat=n=2:v=0:a=1[a{i}]")
        cur_a = f"a{i}"

    fc = ";".join(parts)
    inputs: List[str] = []
    for c in clips:
        inputs.extend(["-i", c])
    cmd = [
        FFMPEG, "-y",
        *inputs,
        "-filter_complex", fc,
        "-map", f"[{cur_v}]",
        "-map", f"[{cur_a}]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]
    _run(cmd, f"Ghép single-pass xfade ({n} cảnh)", timeout=600)
    return output_path


def merge_clips_advanced(
    clips: List[str],
    transitions: Optional[List[str]] = None,
    sound_bridge_duration: float = 0.75,
    output_path: str = "merged.mp4",
    quality: str = "720p",
    aspect_ratio: str = "9:16",
    progress_cb: Optional[Any] = None,
) -> str:
    """
    Ghép toàn bộ chuỗi clip timeline với hiệu năng tối đa (High Performance Single-Pass Engine):
    - Concat Demuxer siêu tốc không suy hao (Lossless Stream Copy) khi các cảnh là Cut / Match Cut
    - Chuyển cảnh & Sound Bridge (Acrossfade + Dissolve) xử lý Single-Pass trong 1 lượt encode duy nhất
    - Loại bỏ hoàn toàn suy hao nén H.264 nhiều vòng (Generational Loss) và loại bỏ file tạm O(N^2)
    - Tự động fallback an toàn đảm bảo không bao giờ dừng đột ngột
    - Dissolve/fade hoặc sound_bridge >= 0.05s dùng nhánh Single-Pass xfade
      (1 lần encode, đúng độ dài timeline); chỉ cut thuần mới dùng concat.
      COMPOSE_MODE=pairwise ép dùng nhánh pairwise cũ để đối chiếu.
    """
    if not clips:
        raise ValueError("Danh sách clip trống")
    if len(clips) == 1:
        shutil.copy2(clips[0], output_path)
        return output_path

    n = len(clips)
    transitions = transitions or ["dissolve"] * (n - 1)
    while len(transitions) < n - 1:
        transitions.append("dissolve")

    sound_bridge_duration = _as_float(sound_bridge_duration, "sound_bridge_duration")
    # Chỉ dùng concat thuần khi TẤT CẢ transitions là cut/none VÀ không có
    # sound bridge VÀ mọi clip ĐỒNG NHẤT hoàn toàn (cùng fps, cùng kích
    # thước, cùng bố cục audio): concat (demuxer lẫn filter) nối trực
    # tiếp theo timestamp, KHÔNG re-encode — mọi khác biệt đều chui thẳng
    # vào file đích. Tái hiện 2026-11-09: clip khác fps/timebase nối bằng
    # stream-copy ra 15.71s thay vì 15.0s + 1.29s đứng hình. Tái hiện
    # 2026-11-10: clip cùng 24fps nhưng khác độ phân giải (720p+1080p+720p)
    # ra file đổi độ phân giải giữa chừng (144 frame 720p + 72 frame
    # 1080p) — trình phát đứng hình ở chỗ đổi. Clip không đồng nhất thì
    # bỏ qua 2 nhánh concat, đi thẳng single-pass xfade (đã chuẩn hóa).
    same_all = _clips_uniform_for_concat(clips)
    all_cuts = (
        all((t or "none").lower() in ("none", "cut", "match cut") for t in transitions)
        and sound_bridge_duration < 0.05
        and same_all
    )

    # ── Chiến lược 1: Concat Demuxer siêu tốc (Zero Re-encoding khi toàn bộ là Cut) ──
    if all_cuts:
        if progress_cb:
            progress_cb(40, "Đang ghép nối timeline siêu tốc (Lossless Concat)...")
        clist = output_path + f".concat_{_unique_suffix()}.txt"
        with open(clist, "w", encoding="utf-8") as f:
            for c in clips:
                f.write(_concat_list_line(c))
        try:
            try:
                _run_compose_ffmpeg([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist, "-c", "copy", output_path], check=True, capture_output=True, timeout=120)
                return output_path
            except Exception:
                pass
            # Fallback sang encode 1 lượt nếu stream copy không tương thích
            _run_compose_ffmpeg([
                FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", output_path
            ], check=True, capture_output=True, timeout=120)
            return output_path
        except Exception as _demux_err:
            print(f"[merge_clips_advanced] Concat Demuxer thất bại ({_demux_err}), thử Concat Filter...")
        finally:
            if os.path.exists(clist):
                try: os.remove(clist)
                except Exception: pass

    # ── Chiến lược 2: Concat Filter — CHỈ cho cut/none (fallback khi Demuxer lỗi).
    # Dissolve/fade hoặc sound_bridge >= 0.05s bỏ qua nhánh này, đi thẳng Pairwise xfade.
    if all_cuts:
        try:
            if progress_cb:
                progress_cb(35, f"Đang ghép nối timeline mượt mà ({n} cảnh)...")

            inputs = []
            filter_in = ""
            for i, c in enumerate(clips):
                inputs.extend(["-i", c])
                filter_in += f"[{i}:v:0][{i}:a:0]"
            fc = f"{filter_in}concat=n={n}:v=1:a=1[v][a]"
            cmd = [
                FFMPEG, "-y",
                *inputs,
                "-filter_complex", fc,
                "-map", "[v]",
                "-map", "[a]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                output_path
            ]
            _run(cmd, f"Ghép Concat Filter ({n} cảnh)", timeout=300)
            return output_path

        except Exception as sp_err:
            print(f"[merge_clips_advanced] Concat Filter gặp exception ({sp_err}), chuyển sang Pairwise Fallback...")

    # ── Chiến lược 3: Single-Pass xfade (mặc định) — 1 lần gọi ffmpeg duy
    # nhất cho toàn bộ timeline, thay cho pairwise O(n²) encode lại toàn bộ
    # phần đã ghép ở mỗi bước. COMPOSE_MODE=pairwise ép dùng nhánh cũ
    # (để benchmark đối chiếu); single-pass lỗi thì fallback pairwise.
    if os.environ.get("COMPOSE_MODE", "single").strip().lower() != "pairwise":
        try:
            return _merge_single_pass(
                clips, transitions, sound_bridge_duration, output_path, progress_cb,
                quality=quality, aspect_ratio=aspect_ratio,
            )
        except Exception as sp_err:
            print(f"[merge_clips_advanced] Single-pass xfade thất bại ({sp_err}), fallback Pairwise...")

    # ── Chiến lược 3b: Pairwise Fallback Dự Phòng An Toàn Tuyệt Đối ──
    out_dir = os.path.dirname(os.path.abspath(output_path))
    current_merged = clips[0]
    total_steps = n - 1

    _pairwise_steps: List[str] = []
    try:
        for i in range(1, n):
            next_clip = clips[i]
            trans = (transitions[i - 1] or "dissolve").lower()
            step_out = os.path.join(out_dir, f"_step_{i}_{_unique_suffix()}.mp4")
            _pairwise_steps.append(step_out)

            # Audit #11: uoc luong 5.0 tai cho cho clip do khong duoc.
            d1 = get_media_duration(current_merged) or 5.0
            d2 = get_media_duration(next_clip) or 5.0
            encode_timeout = max(120, int((d1 + d2) * 2))
            t_dur = min(sound_bridge_duration, min(d1, d2) * 0.4)

            if progress_cb:
                pct = 25 + int((i / total_steps) * 45)
                progress_cb(pct, f"Dựng chuyển cảnh ({i}/{total_steps}): {trans}...")

            # Chuẩn hóa cặp clip trước mọi phép ghép: kích thước đích =
            # kích thước clip hiện tại (clip đầu của cả timeline); audio
            # câm thì sinh silence (xem _norm_audio_chain).
            _info0 = _probe_video_info(current_merged)
            _info1 = _probe_video_info(next_clip)
            _pw, _ph = _info0.get("width"), _info0.get("height")
            if not _pw or not _ph:
                _pw, _ph = _info1.get("width"), _info1.get("height")

            def _pw_vchain(idx: int) -> str:
                if _pw and _ph:
                    return _norm_video_chain(f"[{idx}:v]", _pw, _ph, f"[v{idx}]")
                return (f"[{idx}:v]fps={TARGET_FPS},settb=AVTB,format=yuv420p,"
                        f"setpts=PTS-STARTPTS[v{idx}]")

            def _pw_achain(idx: int, info: Dict, dur: float) -> str:
                a_in = f"[{idx}:a]" if info.get("has_audio") else None
                return _norm_audio_chain(a_in, dur, f"[a{idx}]")

            if t_dur < 0.1 or trans in ("none", "cut", "match cut"):
                if _clips_uniform_for_concat([current_merged, next_clip]):
                    # Cặp đồng nhất: stream-copy nhanh như cũ.
                    clist = step_out + ".txt"
                    with open(clist, "w", encoding="utf-8") as f:
                        f.write(_concat_list_line(current_merged))
                        f.write(_concat_list_line(next_clip))
                    try:
                        try:
                            _run_compose_ffmpeg([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist, "-c", "copy", step_out], check=True, capture_output=True, timeout=120)
                        except Exception:
                            _run_compose_ffmpeg([
                                FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist,
                                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", step_out
                            ], check=True, capture_output=True, timeout=120)
                    finally:
                        if os.path.exists(clist):
                            try: os.remove(clist)
                            except Exception: pass
                else:
                    # Cặp KHÔNG đồng nhất: cấm stream-copy — file sẽ đổi
                    # độ phân giải/audio giữa chừng -> trình phát đứng hình
                    # (tái hiện 2026-11-10). Ghép bằng concat filter sau
                    # khi chuẩn hóa cả 2 đầu vào.
                    fc = (f"{_pw_vchain(0)};{_pw_vchain(1)};"
                          f"{_pw_achain(0, _info0, d1)};{_pw_achain(1, _info1, d2)};"
                          f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
                    cmd = [
                        FFMPEG, "-y",
                        "-i", current_merged,
                        "-i", next_clip,
                        "-filter_complex", fc,
                        "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k",
                        step_out
                    ]
                    _run(cmd, f"Ghép nối clip {i} (chuẩn hóa)", timeout=encode_timeout)
            else:
                offset = max(0.1, d1 - t_dur)
                if _has_xfade():
                    fc = (f"{_pw_vchain(0)};{_pw_vchain(1)};"
                          f"[v0][v1]xfade=transition=fade:duration={t_dur:.2f}:offset={offset:.2f}[v];"
                          f"{_pw_achain(0, _info0, d1)};{_pw_achain(1, _info1, d2)};"
                          f"[a0][a1]acrossfade=d={t_dur:.2f}[a]")
                else:
                    fc = (f"{_pw_vchain(0)};{_pw_vchain(1)};"
                          f"{_pw_achain(0, _info0, d1)};{_pw_achain(1, _info1, d2)};"
                          f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
                cmd = [
                    FFMPEG, "-y",
                    "-i", current_merged,
                    "-i", next_clip,
                    "-filter_complex", fc,
                    "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k",
                    step_out
                ]
                _run(cmd, f"Ghép chuyển cảnh clip {i}: {trans}", timeout=encode_timeout)

            if current_merged != clips[0] and os.path.exists(current_merged):
                try: os.remove(current_merged)
                except Exception: pass
            current_merged = step_out

        shutil.move(current_merged, output_path)
    except Exception:
        for _p in _pairwise_steps:
            if _p != output_path and os.path.exists(_p):
                try: os.remove(_p)
                except Exception: pass
        raise
    return output_path


def merge_all_scenes(scene_clips: List[str], output_merged: str, transition: str = "none"):
    """
    Ghép tất cả các clip scene theo thứ tự qua merge_clips_advanced.
    """
    if not scene_clips:
        raise ValueError("Danh sách clip trống!")
    if len(scene_clips) == 1:
        shutil.copy2(scene_clips[0], output_merged)
        return

    transitions = [transition] * (len(scene_clips) - 1)
    bridge_dur = 0.75 if transition in ("fade", "dissolve") else 0.0
    merge_clips_advanced(scene_clips, transitions=transitions, sound_bridge_duration=bridge_dur, output_path=output_merged)


def mix_multitrack_timeline(
    video_path: str,
    music_path: Optional[str] = None,
    sfx_path: Optional[str] = None,
    output_path: str = "final.mp4",
    voice_volume: float = 1.0,
    music_volume: float = 0.25,
    sfx_volume: float = 0.4,
    fade_duration: float = 1.5,
    quality: str = "720p",
    aspect_ratio: str = "9:16",
) -> str:
    """
    Hòa âm đa tầng chuyên nghiệp (Thoại + SFX + BGM) kết hợp áp dụng profile Quality:
    - Scale chuẩn theo tỉ lệ (9:16 hoặc 16:9) và độ phân giải chọn (draft, 720p, 1080p, 4k)
    - Hòa âm 2 hoặc 3 track âm thanh độc lập với bộ lọc làm mờ audio (fade in/out)
    """
    voice_volume = _as_float(voice_volume, "voice_volume")
    music_volume = _as_float(music_volume, "music_volume")
    sfx_volume = _as_float(sfx_volume, "sfx_volume")
    fade_duration = _as_float(fade_duration, "fade_duration")
    # Audit #11: vid_dur = 0.0 nghia la KHONG DO DUOC (khong phai video 0s)
    # -> khong truyen -t (de ffmpeg chay het video) thay vi -t 5.0 gia.
    vid_dur = get_media_duration(video_path)
    fade_out_start = max(0.0, vid_dur - fade_duration) if vid_dur > 0 else None

    # Audit #5b (2026-11-25): "preview" map ve draft 480p nhu
    # compose_scene_video (truoc day map ve 720p).
    q_key = (quality or "720p").lower()
    if q_key in ("preview", "draft"):
        q_key = "draft"
    q_prof = QUALITY_PROFILES.get(q_key, QUALITY_PROFILES["720p"])
    ar_key = _normalize_aspect_key(aspect_ratio)
    tw, th = q_prof["res"][ar_key]
    scale_pad_filter = (
        f"scale=w={tw}:h={th}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )

    inputs = ["-i", video_path]
    idx_music = -1
    idx_sfx = -1
    cur_idx = 1

    if sfx_path and os.path.exists(sfx_path):
        inputs.extend(["-stream_loop", "-1", "-i", sfx_path])
        idx_sfx = cur_idx
        cur_idx += 1

    if music_path and os.path.exists(music_path):
        inputs.extend(["-stream_loop", "-1", "-i", music_path])
        idx_music = cur_idx
        cur_idx += 1

    filter_complex_parts = []
    filter_complex_parts.append(f"[0:v]{scale_pad_filter}[vout]")

    audio_inputs = [f"[0:a]volume={voice_volume}[voice]"]
    mix_labels = ["[voice]"]

    if idx_sfx != -1:
        audio_inputs.append(f"[{idx_sfx}:a]volume={sfx_volume}[sfx]")
        mix_labels.append("[sfx]")

    if idx_music != -1:
        # Khong biet thoi luong video thi chi fade-in, khong fade-out
        # (khong co moc cuoi de tinh st).
        if fade_out_start is not None:
            audio_inputs.append(f"[{idx_music}:a]volume={music_volume},afade=t=in:ss=0:d={fade_duration},afade=t=out:st={fade_out_start:.2f}:d={fade_duration}[music]")
        else:
            audio_inputs.append(f"[{idx_music}:a]volume={music_volume},afade=t=in:ss=0:d={fade_duration}[music]")
        mix_labels.append("[music]")

    if len(mix_labels) > 1:
        mix_str = "".join(mix_labels) + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=2[aout]"
        filter_complex_parts.extend(audio_inputs)
        filter_complex_parts.append(mix_str)
        out_map_a = "[aout]"
    else:
        out_map_a = "0:a"

    fc_str = ";".join(filter_complex_parts)

    b_v_num = int(q_prof["b_v"].replace("k", ""))
    bufsize_str = f"{b_v_num * 2}k"

    cmd = [
        FFMPEG, "-y",
        *inputs,
        "-filter_complex", fc_str,
        "-map", "[vout]",
        "-map", out_map_a,
        "-c:v", "libx264",
        "-preset", q_prof["preset"],
        "-crf", q_prof["crf"],
        "-b:v", q_prof["b_v"],
        "-maxrate", q_prof["b_v"],
        "-bufsize", bufsize_str,
        "-c:a", "aac",
        "-b:a", q_prof["b_a"],
    ]
    # Audit #11: chi gioi han -t khi biet chac thoi luong video; khong
    # biet thi de ffmpeg chay het (truyen -t 5.0 gia se cat cut phim).
    if vid_dur > 0:
        cmd += ["-t", f"{vid_dur:.3f}"]
    cmd += [output_path]

    _run(cmd, f"Xuất phim hoàn chỉnh ({quality} - Đa tầng âm thanh)", timeout=600)
    return output_path


def merge_audio_with_transitions(
    audio_clips: List[str],
    transitions: Optional[List[str]] = None,
    sound_bridge_duration: float = 0.75,
    output_path: str = "master_sfx.mp3",
) -> str:
    """
    Ghép các track âm thanh (SFX/Ambience) có áp dụng Acrossfade đồng bộ 100% với video timeline.
    Loại bỏ hoàn toàn hiện tượng lệch pha âm thanh khi các clip video chồng mờ (crossfade) qua nhau.
    """
    sound_bridge_duration = _as_float(sound_bridge_duration, "sound_bridge_duration")
    n = len(audio_clips)
    if n == 0:
        return output_path
    if n == 1:
        shutil.copy2(audio_clips[0], output_path)
        return output_path

    # Audit #11: uoc luong 5.0 tai cho cho clip do khong duoc.
    durations = [get_media_duration(c) or 5.0 for c in audio_clips]
    t_durs = []
    for i in range(1, n):
        trans = (transitions[i - 1] or "dissolve").lower() if (transitions and (i - 1) < len(transitions)) else "dissolve"
        if trans in ("none", "cut", "match cut") or sound_bridge_duration <= 0.05:
            t_durs.append(0.0)
        else:
            td = max(0.1, min(sound_bridge_duration, min(durations[i - 1], durations[i]) * 0.4))
            t_durs.append(td)

    # Nếu tất cả là cut: ghép concat thông thường
    if all(td <= 0.05 for td in t_durs):
        clist = output_path + f".sfx_concat_{_unique_suffix()}.txt"
        with open(clist, "w", encoding="utf-8") as fsfx:
            for sc in audio_clips:
                fsfx.write(_concat_list_line(sc))
        try:
            subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist, "-c:a", "libmp3lame", "-b:a", "192k", output_path], check=True, capture_output=True, timeout=120)
        finally:
            if os.path.exists(clist):
                try: os.remove(clist)
                except Exception: pass
        return output_path

    # Nếu có chuyển cảnh chồng chéo: ghép Single-Pass acrossfade
    try:
        a_parts = []
        prev_a = "0:a"
        for i in range(1, n):
            td = t_durs[i - 1]
            if td > 0.05:
                a_parts.append(f"[{prev_a}][{i}:a]acrossfade=d={td:.2f}[a_prev_{i}]")
                prev_a = f"a_prev_{i}"
            else:
                a_parts.append(f"[{prev_a}][{i}:a]acrossfade=d=0.01[a_prev_{i}]")
                prev_a = f"a_prev_{i}"

        fc = ";".join(a_parts)
        inputs = []
        for ac in audio_clips:
            inputs.extend(["-i", ac])

        cmd = [
            FFMPEG, "-y",
            *inputs,
            "-filter_complex", fc,
            "-map", f"[{prev_a}]",
            "-c:a", "libmp3lame", "-b:a", "192k",
            output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return output_path
    except Exception as e:
        print(f"[merge_audio_with_transitions warning] Acrossfade error ({e}), fallback to concat...")
        clist = output_path + f".sfx_concat_{_unique_suffix()}.txt"
        with open(clist, "w", encoding="utf-8") as fsfx:
            for sc in audio_clips:
                fsfx.write(_concat_list_line(sc))
        try:
            subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist, "-c:a", "libmp3lame", "-b:a", "192k", output_path], check=True, capture_output=True, timeout=120)
        finally:
            if os.path.exists(clist):
                try: os.remove(clist)
                except Exception: pass
        return output_path


def _write_karaoke_ass(srt_path: str, ass_path: str, font_size: int, alignment: int, style: str = "karaoke"):
    """Distribute each timed SRT cue over short phrases, highlighting the current word."""
    def centiseconds(stamp):
        h, m, s, ms = map(int, re.split("[:,]", stamp))
        return (h * 3600 + m * 60 + s) * 100 + ms // 10

    def stamp(t):
        return f"{t // 360000}:{t // 6000 % 60:02}:{t // 100 % 60:02}.{t % 100:02}"

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResY: 288\nWrapStyle: 0\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Main,Arial,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,1.4,0,{alignment},12,12,22,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    if style == "word_box":
        header = header.replace("&H00FFFFFF,&H00FFFFFF,&H00000000", "&H00000000,&H00000000,&H0000DAFF").replace(",1,1.4,0,", ",3,2,0,")
    events = []
    for block in re.split(r"\n\s*\n", Path(srt_path).read_text(encoding="utf-8-sig").strip()):
        lines = block.splitlines()
        start, end = map(centiseconds, lines[1].split(" --> "))
        words = " ".join(lines[2:]).translate(str.maketrans({"\\": "＼", "{": "｛", "}": "｝"})).split()
        total = sum(map(len, words))
        elapsed = 0
        for i, word in enumerate(words):
            a = start + (end - start) * elapsed // total
            elapsed += len(word)
            b = start + (end - start) * elapsed // total
            phrase = words[i // 4 * 4:i // 4 * 4 + 4]
            phrase[i % 4] = r"{\c&H00CFFF&}" + word + r"{\rMain}"
            if style == "word_box":
                phrase = [word]
            if b > a:
                events.append(f"Dialogue: 0,{stamp(a)},{stamp(b)},Main,,0,0,0,,{' '.join(phrase)}")
    Path(ass_path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def burn_subtitles(
    input_video: str,
    srt_file: str,
    output_video: str,
    font_size: int = 10,
    progress_cb: Optional[Any] = None,
    total_duration: Optional[float] = None,
    raise_on_error: bool = False,
    timeout: float = 300,
    style: str = "cinema",
    position: str = "bottom",
):
    """Khắc phụ đề (burn-in) phong cách điện ảnh: Chữ trắng viền đen sắc nét, căn giữa đáy, không hộp đen, không che nhân vật.

    timeout: tran tong cho TOAN BO qua trinh burn-in, ke ca vong doc
    stdout (audit #5d, 2026-11-25: truoc day timeout 300s chi ap sau
    vong `for line in proc.stdout` — ffmpeg treo giua chung thi ket
    vinh vien). Doc stdout trong thread rieng + deadline; het han thi
    kill ffmpeg va fallback video goc (hoac raise neu raise_on_error).
    """
    if not os.path.exists(srt_file) or os.path.getsize(srt_file) < 10:
        if raise_on_error:
            raise RuntimeError(f"File phụ đề rỗng hoặc không tồn tại ({srt_file})")
        print(f"[FFmpeg] File phụ đề rỗng hoặc không tồn tại ({srt_file}), bỏ qua burn-in.")
        shutil.copy2(input_video, output_video)
        return output_video

    work_dir = os.path.dirname(os.path.abspath(srt_file))
    
    # Tạo bản sao tên ASCII thuần túy tuyệt đối an toàn cho FFmpeg subtitles filter
    clean_srt_name = f"_temp_sub_{int(time.time())}_{uuid.uuid4().hex[:6]}.srt"
    clean_srt_path = os.path.join(work_dir, clean_srt_name)
    shutil.copy2(srt_file, clean_srt_path)

    in_base = os.path.relpath(os.path.abspath(input_video), work_dir).replace("\\", "/")
    out_base = os.path.relpath(os.path.abspath(output_video), work_dir).replace("\\", "/")

    subtitle_style = (
        f"FontName=Arial,"
        f"FontSize={font_size},"
        "PrimaryColour=&H00FFFFFF,"   # Chữ trắng tinh
        "OutlineColour=&H00000000,"   # Viền đen sắc nét
        "BorderStyle=1,"              # Viền đổ bóng tự nhiên (KHÔNG dùng box hộp đen che hình)
        "Outline=1.2,"
        "Shadow=0.5,"
        "Alignment=2,"                # Căn giữa đáy màn hình
        "MarginV=25"                  # Cách đáy an toàn cho cả màn hình ngang và 9:16
    )
    alignment = {"top": 8, "middle": 5, "bottom": 2}.get(position, 2)
    subtitle_style = subtitle_style.replace("Alignment=2", f"Alignment={alignment}")
    if style == "headline":
        subtitle_style = subtitle_style.replace("&H00FFFFFF", "&H0000DAFF") + ",Bold=-1"
    elif style == "neon":
        subtitle_style = subtitle_style.replace("OutlineColour=&H00000000", "OutlineColour=&H0025C5FF").replace("Outline=1.2", "Outline=2").replace("Shadow=0.5", "Shadow=1.5")
    elif style == "boxed":
        subtitle_style = subtitle_style.replace("BorderStyle=1", "BorderStyle=3")
    vf = f"subtitles={clean_srt_name}:force_style='{subtitle_style}'"
    if style in {"karaoke", "word_box"}:
        Path(clean_srt_path).unlink()
        clean_srt_name = Path(clean_srt_name).with_suffix(".ass").name
        clean_srt_path = os.path.join(work_dir, clean_srt_name)
        _write_karaoke_ass(srt_file, clean_srt_path, font_size, alignment, style)
        vf = f"ass={clean_srt_name}"

    cmd = [
        FFMPEG, "-y",
        "-i", in_base,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-threads", "4",
        "-c:a", "copy",
        "-progress", "pipe:1",
        out_base,
    ]
    print(f"[FFmpeg] Burn-in phụ đề tại {work_dir}")

    try:
        if progress_cb and total_duration and total_duration > 0:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err_file:
                proc = subprocess.Popen(cmd, cwd=work_dir, stdout=subprocess.PIPE, stderr=err_file, text=True, bufsize=1)
                # Audit #5d: doc stdout trong thread rieng, vong chinh chi
                # doi queue voi deadline — ffmpeg treo giua chung (khong
                # EOF, khong thoat) cung bi kill khi het timeout. (Khong
                # dung select: khong hoat dong tren pipe cua Windows.)
                _lines: "queue.Queue[Optional[str]]" = queue.Queue()
                _eof = object()

                def _read_stdout():
                    try:
                        for _line in proc.stdout:
                            _lines.put(_line)
                    except Exception:
                        pass
                    finally:
                        _lines.put(_eof)

                _reader = threading.Thread(target=_read_stdout, daemon=True)
                _reader.start()
                _deadline = time.monotonic() + float(timeout)
                _timed_out = False
                while True:
                    _remaining = _deadline - time.monotonic()
                    if _remaining <= 0:
                        _timed_out = True
                        break
                    try:
                        line = _lines.get(timeout=min(_remaining, 1.0))
                    except queue.Empty:
                        if proc.poll() is not None:
                            break
                        continue
                    if line is _eof:
                        break
                    if line.startswith("out_time="):
                        t_str = line.split("=")[1].strip()
                        try:
                            parts = t_str.split(":")
                            if len(parts) == 3:
                                sec = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                                sub_pct = min(100, int((sec / total_duration) * 100))
                                overall_pct = min(90, 40 + int((sec / total_duration) * 50))
                                progress_cb(overall_pct, f"Đang khắc phụ đề điện ảnh: {sub_pct}% ({int(sec)}s / {int(total_duration)}s)...")
                        except Exception:
                            pass
                if _timed_out:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        pass
                    if raise_on_error:
                        raise RuntimeError(f"Quá thời gian (timeout {timeout}s) khi khắc phụ đề video bằng FFmpeg.")
                    print(f"[FFmpeg Cảnh báo] Burn-in timeout sau {timeout}s, fallback video gốc.")
                    shutil.copy2(input_video, output_video)
                    return output_video
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    if raise_on_error:
                        raise RuntimeError(f"Quá thời gian (timeout {timeout}s) khi khắc phụ đề video bằng FFmpeg.")
                    print(f"[FFmpeg Cảnh báo] Burn-in timeout sau {timeout}s, fallback video gốc.")
                    shutil.copy2(input_video, output_video)
                    return output_video
                if proc.returncode != 0:
                    err_file.seek(0)
                    err = err_file.read()[-1000:]
                    if raise_on_error:
                        raise RuntimeError(f"Lỗi khắc phụ đề bằng FFmpeg: {err.strip()}")
                    print(f"[FFmpeg Cảnh báo] Lỗi burn phụ đề, fallback video gốc: {err}")
                    shutil.copy2(input_video, output_video)
                    return output_video
        else:
            result = _run_compose_ffmpeg(cmd, cwd=work_dir, text=True, timeout=timeout)
            if result.returncode != 0:
                if raise_on_error:
                    raise RuntimeError(f"Lỗi khắc phụ đề bằng FFmpeg: {result.stderr[-1000:].strip()}")
                print(f"[FFmpeg Cảnh báo] Lỗi burn phụ đề, fallback video gốc: {result.stderr[-1000:]}")
                shutil.copy2(input_video, output_video)
                return output_video
    finally:
        if os.path.exists(clean_srt_path):
            try:
                os.remove(clean_srt_path)
            except Exception:
                pass
    return output_video


def add_background_music(
    input_video: str,
    music_file: str,
    output_video: str,
    music_volume: float = 0.15,
    voice_volume: float = 1.0,
    fade_duration: float = 1.5,
):
    """
    Hòa âm nhạc nền (BGM) đa tầng với giọng thoại:
    - Chỉnh volume độc lập cho nhạc nền và giọng đọc.
    - Tự động loop nhạc nếu video dài hơn nhạc.
    - Tự động áp dụng Audio Fade-in đầu video và Fade-out cuối video để âm thanh êm dịu.
    """
    music_volume = _as_float(music_volume, "music_volume")
    voice_volume = _as_float(voice_volume, "voice_volume")
    fade_duration = _as_float(fade_duration, "fade_duration")
    # Audit #11: vid_dur = 0.0 nghia la KHONG DO DUOC -> khong truyen -t
    # (de ffmpeg chay het video, -shortest se dung o cuoi video vi nhac
    # loop vo han); chi fade-out khi biet moc cuoi.
    vid_dur = get_media_duration(input_video)
    if vid_dur > 0:
        fade_out_start = max(0.0, vid_dur - fade_duration)
        music_chain = (f"[1:a]volume={music_volume},afade=t=in:ss=0:d={fade_duration},"
                       f"afade=t=out:st={fade_out_start:.2f}:d={fade_duration}[music]")
    else:
        music_chain = f"[1:a]volume={music_volume},afade=t=in:ss=0:d={fade_duration}[music]"

    filter_complex = (
        f"[0:a]volume={voice_volume}[voice];"
        f"{music_chain};"
        f"[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )

    cmd = [
        FFMPEG, "-y",
        "-i", input_video,
        "-stream_loop", "-1", "-i", music_file,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
    ]
    # Audit #11: chi gioi han -t khi biet chac thoi luong video.
    if vid_dur > 0:
        cmd += ["-t", f"{vid_dur:.3f}"]
    cmd += [output_video]
    _run(cmd, "Mix nhạc nền với Audio Fade", timeout=600)


def audit_movie_scenes(arg1: Any, arg2: Any = None) -> Dict[str, Any]:
    """
    Kiểm tra tổng quan toàn bộ cảnh phim trước khi xuất (Scene Audit):
    - Thống kê cảnh thành công, thất bại, đang chờ.
    - Lưu lại chi tiết lỗi và prompt đã gửi cho từng cảnh lỗi.
    - Xác định phim có đầy đủ 100% cảnh hay không.
    Hỗ trợ linh hoạt và an toàn tuyệt đối mọi kiểu tham số:
      audit_movie_scenes(results: List[Dict], total_scenes: int)
      audit_movie_scenes(scenes_dicts: List[Dict], results: List[Dict])
    """
    if isinstance(arg2, int):
        results = arg1 if isinstance(arg1, list) else []
        total_scenes = arg2
        all_indices = set(range(1, total_scenes + 1))
    elif isinstance(arg1, int):
        results = arg2 if isinstance(arg2, list) else []
        total_scenes = arg1
        all_indices = set(range(1, total_scenes + 1))
    elif isinstance(arg1, list) and isinstance(arg2, list):
        if any(isinstance(x, dict) and ("video_path" in x or "status" in x) for x in arg2):
            scenes_list = arg1
            results = arg2
        elif any(isinstance(x, dict) and ("video_path" in x or "status" in x) for x in arg1):
            results = arg1
            scenes_list = arg2
        else:
            scenes_list = arg1
            results = arg2

        all_indices = set()
        for s in scenes_list:
            if isinstance(s, dict) and "index" in s:
                all_indices.add(s["index"])
            elif isinstance(s, int):
                all_indices.add(s)
        if not all_indices:
            all_indices = set(range(1, len(scenes_list) + 1))
        total_scenes = len(all_indices)
    else:
        results = arg1 if isinstance(arg1, list) else []
        total_scenes = len(results)
        all_indices = set(range(1, total_scenes + 1))

    def _is_valid_video_path(p: Any) -> bool:
        if not p:
            return False
        sp = str(p)
        if os.path.exists(sp) and os.path.getsize(sp) > 500:
            return True
        studio_dir = Path(__file__).parent.resolve()
        cand = studio_dir / sp
        return cand.exists() and cand.stat().st_size > 500

    done_scenes = [
        r for r in results
        if isinstance(r, dict)
        and r.get("status") == "done"
        and _is_valid_video_path(r.get("video_path"))
    ]
    failed_scenes = [r for r in results if isinstance(r, dict) and r.get("status") in ("error", "failed")]
    done_indices = set(s["index"] for s in done_scenes if isinstance(s, dict) and "index" in s)
    failed_indices = set(s["index"] for s in failed_scenes if isinstance(s, dict) and "index" in s)
    pending_indices = list(all_indices - done_indices - failed_indices)
    pending_indices.sort()

    is_complete = (len(done_scenes) == total_scenes) and (total_scenes > 0)

    failed_details = []
    for s in failed_scenes:
        if isinstance(s, dict):
            failed_details.append({
                "index": s.get("index"),
                "error": s.get("error") or "Không xác định",
                "prompt": s.get("prompt") or "",
                "revised_prompt": s.get("revised_prompt") or "",
            })

    return {
        "total": total_scenes,
        "done_count": len(done_scenes),
        "failed_count": len(failed_scenes),
        "pending_count": len(pending_indices),
        "is_complete": is_complete,
        "done_indices": sorted(list(done_indices)),
        "failed_indices": sorted(list(failed_indices)),
        "pending_indices": pending_indices,
        "failed_details": failed_details,
    }


def compose_full_movie(
    results: List[Dict],
    output_dir: str,
    project_title: str = "movie",
    bg_music_path: Optional[str] = None,
    music_volume: float = 0.15,
    voice_volume: float = 1.0,
    subtitle_mode: str = "burn_in",   # "burn_in" | "srt_only" | "none"
    font_size: int = 22,
    quality: str = "720p",            # "preview" | "720p" | "1080p"
    transition: str = "none",          # "none" | "fade" | "dissolve"
    aspect_ratio: str = "16:9",
    allow_partial: bool = False,       # True: Cho phép xuất bản nháp; False: Bắt buộc đủ 100% cảnh
    total_scenes: Optional[int] = None,
    subtitle_style: str = "cinema",
    subtitle_position: str = "bottom",
) -> Dict[str, Any]:
    """
    Pipeline hậu kỳ toàn diện từ danh sách results của batch_engine:
    1. Kiểm tra tính đầy đủ của cảnh (Scene Audit) - Ngăn chặn xuất thiếu ngoài ý muốn
    2. Ghép video + audio cho từng cảnh -> clip chuẩn hóa
    3. Tạo file SRT phụ đề chuẩn mili-giây
    4. Ghép tất cả clip (Hỗ trợ transitions: fade, dissolve)
    5. Xử lý phụ đề theo lựa chọn (Burn-in, SRT tải về riêng, Không phụ đề)
    6. Mix nhạc nền (BGM) đa tầng với volume độc lập và audio fade
    Trả về Dictionary kết quả xuất phim chi tiết.
    """
    ts = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    final_dir = os.path.join(output_dir, "final")
    os.makedirs(final_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="_compose_", dir=output_dir)
    published_paths = []
    completed = False

    try:
        _check_compose_cancelled()
        expected_total = total_scenes or len(results)
        audit = audit_movie_scenes(results, expected_total)

        if not audit["is_complete"] and not allow_partial:
            missing_count = expected_total - audit["done_count"]
            raise ValueError(
                f"Không thể xuất phim vì thiếu {missing_count}/{expected_total} cảnh! "
                f"Cảnh lỗi: {audit['failed_indices']}, Cảnh chờ: {audit['pending_indices']}. "
                f"Vui lòng chọn 'Render lại cảnh lỗi' hoặc bật 'Xuất bản nháp có cảnh thiếu'."
            )

        def _resolve_media_path(p: Any) -> str:
            if not p:
                return ""
            sp = str(p)
            if os.path.isabs(sp) and os.path.exists(sp):
                return sp
            if os.path.exists(sp):
                return os.path.abspath(sp)
            studio_dir = os.path.dirname(os.path.abspath(__file__))
            cand = os.path.join(studio_dir, sp)
            if os.path.exists(cand):
                return cand
            return sp

        # Lọc các cảnh thành công, sắp xếp đúng thứ tự
        ok_results = []
        for r in results:
            if isinstance(r, dict) and r.get("status") == "done":
                vp = _resolve_media_path(r.get("video_path"))
                if vp and os.path.exists(vp) and os.path.getsize(vp) > 500:
                    r_copy = dict(r)
                    r_copy["video_path"] = vp
                    if r_copy.get("audio_path"):
                        r_copy["audio_path"] = _resolve_media_path(r_copy["audio_path"])
                    ok_results.append(r_copy)
        ok_results.sort(key=lambda x: x["index"])

        if not ok_results:
            raise ValueError("Không có cảnh nào thành công để ghép phim!")

        # Bước 1: Ghép video + audio từng cảnh
        scene_clips = []
        clip_durations = []
        for r in ok_results:
            _check_compose_cancelled()
            idx = r["index"]
            clip_out = os.path.join(tmp_dir, f"clip_{idx:03d}.mp4")
            act_audio = get_media_duration(r["audio_path"]) if (r.get("audio_path") and os.path.exists(r["audio_path"])) else float(r.get("audio_duration", 5))
            compose_scene_video(
                scene_video=r["video_path"],
                scene_audio=r["audio_path"],
                output=clip_out,
                audio_duration=act_audio,
                quality=quality,
                aspect_ratio=aspect_ratio,
            )
            scene_clips.append(clip_out)
            real_clip_dur = get_media_duration(clip_out)
            clip_durations.append(real_clip_dur)
            print(f"[Hậu kỳ] Clip cảnh {idx} OK ({real_clip_dur:.2f}s) → {clip_out}")

        # Bước 2: Tạo SRT đồng bộ chính xác với audio onset và timeline chuyển cảnh
        transitions = [r.get("transition_out") or transition for r in ok_results[:-1]]
        bridge_dur = 0.75 if any(t in ("fade","dissolve") for t in transitions) else 0.0
        timeline_offsets = compute_timeline_offsets(scene_clips, transitions, bridge_dur)

        # Chuẩn hóa tên file an toàn cho hệ thống tập tin và FFmpeg (bỏ emoji, dấu ngoặc, dấu phẩy)
        clean_title = re.sub(r'[^a-zA-Z0-9_\-]', '_', project_title)
        clean_title = re.sub(r'_+', '_', clean_title).strip('_')
        safe_title = clean_title[:45] if clean_title else "movie"

        # ── 2 PHỤ ĐỀ (fix 2026-11-09) ─────────────────────────────────────
        # Trước đây: burn_in vừa khắc phụ đề VÀO video vừa để file .srt cùng
        # tên ({safe_title}_{ts}.srt) ngay cạnh file mp4 ({safe_title}_{ts}.mp4)
        # trong final/ — trình phát nào tự nạp phụ đề cùng tên (VLC, trình
        # phát trên điện thoại, CapCut...) sẽ hiện thêm 1 lớp phụ đề nữa
        # chồng lên chữ đã khắc → 2 phụ đề. Sửa:
        #   - "burn_in": SRT chỉ là file trung gian để khắc → ghi vào _tmp,
        #     KHÔNG để cạnh mp4, KHÔNG trả về srt_path (UI cũng ẩn nút tải).
        #   - "srt_only": SRT là sản phẩm chính → giữ trong final/ như cũ.
        #   - "none": không tạo SRT.
        srt_filename = f"{safe_title}_{ts}.srt"
        srt_path = None
        if subtitle_mode in ("srt_only", "burn_in"):
            srt_path = os.path.join(tmp_dir, srt_filename)
            create_srt(
                ok_results,
                srt_path,
                clip_durations=clip_durations,
                media_paths=scene_clips,
                timeline_offsets=timeline_offsets,
            )

        _check_compose_cancelled()
        merged = os.path.join(tmp_dir, f"merged_{ts}.mp4")
        merge_clips_advanced(scene_clips, transitions, bridge_dur, merged, quality, aspect_ratio)
        print(f"[Hậu kỳ] Đã ghép video thành công: {merged}")

        video_to_mix = merged
        if subtitle_mode == "burn_in" and srt_path and os.path.getsize(srt_path) > 0:
            with_sub = os.path.join(tmp_dir, f"with_sub_{ts}.mp4")
            burn_subtitles(merged, srt_path, with_sub, font_size=font_size, raise_on_error=True,
                           style=subtitle_style, position=subtitle_position)
            if not os.path.isfile(with_sub) or os.path.getsize(with_sub) <= 1000:
                raise RuntimeError("FFmpeg không tạo được video đã khắc phụ đề.")
            video_to_mix = with_sub

        _check_compose_cancelled()
        final_video_name = f"{safe_title}_{ts}.mp4"
        final_path = os.path.join(final_dir, final_video_name)
        if bg_music_path and os.path.exists(bg_music_path):
            mixed = os.path.join(tmp_dir, "mixed.mp4")
            add_background_music(
                input_video=video_to_mix,
                music_file=bg_music_path,
                output_video=mixed,
                music_volume=music_volume,
                voice_volume=voice_volume,
            )
            video_to_mix = mixed
        total_movie_duration = get_media_duration(video_to_mix)

        _check_compose_cancelled()
        if subtitle_mode == "srt_only":
            final_srt = os.path.join(final_dir, srt_filename)
            os.replace(srt_path, final_srt)
            published_paths.append(final_srt)
        else:
            final_srt = None
        os.replace(video_to_mix, final_path)
        published_paths.append(final_path)
        _check_compose_cancelled()
        completed = True
        return {
            "video_path": final_path,
            "video_filename": final_video_name,
            # Chỉ "srt_only" mới trả file SRT (burn_in đã khắc vào video, file
            # SRT chỉ nằm trong _tmp để khắc; "none" thì không có SRT) — tránh
            # trình phát tự nạp phụ đề cùng tên chồng lên chữ đã khắc.
            "srt_path": final_srt,
            "srt_filename": srt_filename if subtitle_mode == "srt_only" else None,
            "duration": round(total_movie_duration, 2),
            "audit": audit,
            "scenes_used": [r["index"] for r in ok_results],
            "is_draft": not audit["is_complete"],
            "subtitle_mode": subtitle_mode,
            "quality": quality,
            "transition": transition,
        }

    finally:
        if not completed:
            for path in published_paths:
                Path(path).unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
