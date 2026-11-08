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
import shutil
import subprocess
import time
import tempfile
import math
import re
import uuid
import imageio_ffmpeg
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

QUALITY_PROFILES = {
    "draft": {
        "scale_9_16": "scale=480:854",
        "scale_16_9": "scale=854:480",
        "crf": "26",
        "preset": "ultrafast",
        "b_a": "128k",
        "b_v": "1500k",
    },
    "720p": {
        "scale_9_16": "scale=720:1280",
        "scale_16_9": "scale=1280:720",
        "crf": "20",
        "preset": "fast",
        "b_a": "192k",
        "b_v": "3500k",
    },
    "1080p": {
        "scale_9_16": "scale=1080:1920",
        "scale_16_9": "scale=1920:1080",
        "crf": "18",
        "preset": "medium",
        "b_a": "256k",
        "b_v": "8000k",
    },
    "4k": {
        "scale_9_16": "scale=2160:3840",
        "scale_16_9": "scale=3840:2160",
        "crf": "16",
        "preset": "slow",
        "b_a": "320k",
        "b_v": "24000k",
    },
}

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
    công; file thiếu/lỗi vẫn trả 5.0 như trước."""
    if not file_path or not os.path.exists(file_path):
        return 5.0
    cache_key = None
    try:
        st = os.stat(file_path)
        cache_key = (os.path.abspath(file_path), st.st_mtime, st.st_size)
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
                if len(_DURATION_CACHE) >= _DURATION_CACHE_MAX_SIZE:
                    _DURATION_CACHE.pop(next(iter(_DURATION_CACHE)), None)
                _DURATION_CACHE[cache_key] = dur
            return dur
    except Exception as exc:
        print(f"[get_media_duration error] {exc}")
    return 5.0


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

    durations = [get_media_duration(c) for c in clips]
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
            sub_end = min(round(shot_start + dur, 3), sub_end)
            if sub_end <= sub_start:
                sub_end = round(shot_start + dur, 3)

            f.write(f"{sub_index}\n{_fmt_srt_time(sub_start)} --> {_fmt_srt_time(sub_end)}\n{text}\n\n")
            sub_index += 1

    return output_srt


def _run(cmd: List[str], label: str = "", timeout: int = 120):
    print(f"[FFmpeg] {label}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
):
    """
    Ghép 1 cảnh: Kéo dài hoặc loop video để khớp đúng với thời lượng audio TTS hoặc target_duration.
    Đảm bảo không cắt ngắn video khi audio ngắn hơn video (giữ trọn vẹn thước phim điện ảnh).
    Tự động chuẩn hóa codec sang H.264 / AAC để đảm bảo tương thích mọi trình duyệt và concat trơn tru.
    Hỗ trợ chuẩn xác cả khung hình dọc 9:16 (Shorts/TikTok/Reels) và khung hình ngang 16:9 (Cinema/YouTube/TV).
    Nếu không có scene_audio (chế độ không lồng tiếng), giữ nguyên 100% âm thanh & giọng gốc do AI sinh ra!
    """
    vf_filters = []
    is_vertical = any(k in aspect_ratio.lower() for k in ["9:16", "9/16", "portrait", "vertical"])
    q = (quality or "720p").lower()

    if q == "preview":
        w, h = (480, 854) if is_vertical else (854, 480)
        crf = "28"
        preset = "ultrafast"
    elif q == "1080p":
        w, h = (1080, 1920) if is_vertical else (1920, 1080)
        crf = "18"
        preset = "medium"
    elif q == "4k":
        w, h = (2160, 3840) if is_vertical else (3840, 2160)
        crf = "16"
        preset = "slow"
    else:
        # Default 720p
        w, h = (720, 1280) if is_vertical else (1280, 720)
        crf = "22"
        preset = "fast"

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
        # Có file lồng tiếng riêng (TTS voiceover / dialogue): ghép đè scene_audio vào video
        cmd = [
            FFMPEG, "-y",
            *seek_args,
            "-stream_loop", "-1", "-i", input_video,   # Loop video nếu audio dài hơn
            "-i", scene_audio,
            "-map", "0:v:0",
            "-map", "1:a:0",
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
) -> str:
    """Ghép toàn bộ clip bằng MỘT lần gọi ffmpeg duy nhất (single-pass):
    - Video: chuỗi xfade nối tiếp cho dissolve/fade; concat cho bước cut.
    - Audio: chuỗi acrossfade nối tiếp cho dissolve/fade; concat cho bước cut.
    Tổng thời lượng = sum(durations) - sum(overlaps), khớp 100% với
    compute_timeline_offsets (3 clip 2s + 2 dissolve 0.5s -> đúng 5.0s).
    Lỗi ở bất kỳ bước nào (khác fps/kích thước/sample-rate...) thì raise để
    caller fallback về nhánh pairwise cũ — không bao giờ dừng đột ngột.
    """
    n = len(clips)
    durations = [get_media_duration(c) for c in clips]
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
        parts.append(f"[{i}:v]format=yuv420p,setpts=PTS-STARTPTS[v{i}in]")
        parts.append(f"[{i}:a]aresample=44100,asetpts=PTS-STARTPTS[a{i}in]")

    # Chuỗi video: xfade cho dissolve/fade, concat cho cut (giữ đúng độ dài).
    cur_v = "v0in"
    acc_dur = durations[0]
    for i in range(1, n):
        t = t_durs[i - 1]
        if t > 0.05:
            offset = max(0.0, acc_dur - t)
            parts.append(
                f"[{cur_v}][v{i}in]xfade=transition=fade:duration={t:.3f}:offset={offset:.3f}[x{i}]"
            )
        else:
            parts.append(f"[{cur_v}][v{i}in]concat=n=2:v=1:a=0[x{i}]")
        cur_v = f"x{i}"
        acc_dur = acc_dur + durations[i] - t

    # Chuỗi audio: acrossfade cho dissolve/fade, concat cho cut.
    cur_a = "a0in"
    for i in range(1, n):
        t = t_durs[i - 1]
        if t > 0.05:
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
    # sound bridge: dissolve/fade hoặc bridge >= 0.05s phải đi nhánh xfade.
    all_cuts = (
        all((t or "none").lower() in ("none", "cut", "match cut") for t in transitions)
        and sound_bridge_duration < 0.05
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
                subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist, "-c", "copy", output_path], check=True, capture_output=True, timeout=120)
                return output_path
            except Exception:
                pass
            # Fallback sang encode 1 lượt nếu stream copy không tương thích
            subprocess.run([
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
                clips, transitions, sound_bridge_duration, output_path, progress_cb
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

            d1 = get_media_duration(current_merged)
            d2 = get_media_duration(next_clip)
            t_dur = min(sound_bridge_duration, min(d1, d2) * 0.4)

            if progress_cb:
                pct = 25 + int((i / total_steps) * 45)
                progress_cb(pct, f"Dựng chuyển cảnh ({i}/{total_steps}): {trans}...")

            if t_dur < 0.1 or trans in ("none", "cut", "match cut"):
                clist = step_out + ".txt"
                with open(clist, "w", encoding="utf-8") as f:
                    f.write(_concat_list_line(current_merged))
                    f.write(_concat_list_line(next_clip))
                try:
                    try:
                        subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist, "-c", "copy", step_out], check=True, capture_output=True, timeout=120)
                    except Exception:
                        subprocess.run([
                            FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", clist,
                            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", step_out
                        ], check=True, capture_output=True, timeout=120)
                finally:
                    if os.path.exists(clist):
                        try: os.remove(clist)
                        except Exception: pass
            else:
                offset = max(0.1, d1 - t_dur)
                fc = f"[1:v]format=yuva420p,fade=t=in:st=0:d={t_dur:.2f}:alpha=1,setpts=PTS-STARTPTS+{offset:.2f}/TB[v1];[0:v]format=yuva420p[v0];[v0][v1]overlay=eof_action=pass[v];[0:a][1:a]acrossfade=d={t_dur:.2f}[a]"
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
                _run(cmd, f"Ghép chuyển cảnh clip {i}: {trans}")

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
    bridge_dur = 0.5 if transition in ("fade", "dissolve") else 0.0
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
    vid_dur = get_media_duration(video_path)
    fade_out_start = max(0.0, vid_dur - fade_duration)

    q_prof = QUALITY_PROFILES.get(quality, QUALITY_PROFILES["720p"])
    is_vertical = any(k in aspect_ratio.lower() for k in ["9:16", "9/16", "portrait", "vertical"])
    scale_filter = q_prof["scale_9_16"] if is_vertical else q_prof["scale_16_9"]

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
    filter_complex_parts.append(f"[0:v]{scale_filter}:flags=lanczos,format=yuv420p[vout]")

    audio_inputs = [f"[0:a]volume={voice_volume}[voice]"]
    mix_labels = ["[voice]"]

    if idx_sfx != -1:
        audio_inputs.append(f"[{idx_sfx}:a]volume={sfx_volume}[sfx]")
        mix_labels.append("[sfx]")

    if idx_music != -1:
        audio_inputs.append(f"[{idx_music}:a]volume={music_volume},afade=t=in:ss=0:d={fade_duration},afade=t=out:st={fade_out_start:.2f}:d={fade_duration}[music]")
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
        "-t", f"{vid_dur:.3f}",
        output_path
    ]

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

    durations = [get_media_duration(c) for c in audio_clips]
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


def burn_subtitles(
    input_video: str,
    srt_file: str,
    output_video: str,
    font_size: int = 10,
    progress_cb: Optional[Any] = None,
    total_duration: Optional[float] = None,
):
    """Khắc phụ đề (burn-in) phong cách điện ảnh: Chữ trắng viền đen sắc nét, căn giữa đáy, không hộp đen, không che nhân vật."""
    if not os.path.exists(srt_file) or os.path.getsize(srt_file) < 10:
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
    vf = f"subtitles={clean_srt_name}:force_style='{subtitle_style}'"

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
                for line in proc.stdout:
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
                try:
                    proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    print("[FFmpeg Cảnh báo] Burn-in timeout sau 300s, fallback video gốc.")
                    shutil.copy2(input_video, output_video)
                    return output_video
                if proc.returncode != 0:
                    err_file.seek(0)
                    err = err_file.read()[-1000:]
                    print(f"[FFmpeg Cảnh báo] Lỗi burn phụ đề, fallback video gốc: {err}")
                    shutil.copy2(input_video, output_video)
                    return output_video
        else:
            result = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
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
    vid_dur = get_media_duration(input_video)
    fade_out_start = max(0.0, vid_dur - fade_duration)

    filter_complex = (
        f"[0:a]volume={voice_volume}[voice];"
        f"[1:a]volume={music_volume},afade=t=in:ss=0:d={fade_duration},afade=t=out:st={fade_out_start:.2f}:d={fade_duration}[music];"
        f"[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )

    _run([
        FFMPEG, "-y",
        "-i", input_video,
        "-stream_loop", "-1", "-i", music_file,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{vid_dur:.3f}",
        "-shortest",
        output_video,
    ], "Mix nhạc nền với Audio Fade", timeout=600)


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
    tmp_dir = os.path.join(output_dir, "_tmp")
    os.makedirs(final_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    try:
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
        bridge_dur = 0.5 if transition in ("fade", "dissolve") else 0.0
        timeline_offsets = compute_timeline_offsets(scene_clips, [transition] * max(0, len(scene_clips) - 1), bridge_dur)

        # Chuẩn hóa tên file an toàn cho hệ thống tập tin và FFmpeg (bỏ emoji, dấu ngoặc, dấu phẩy)
        clean_title = re.sub(r'[^a-zA-Z0-9_\-]', '_', project_title)
        clean_title = re.sub(r'_+', '_', clean_title).strip('_')
        safe_title = clean_title[:45] if clean_title else "movie"

        srt_filename = f"{safe_title}_{ts}.srt"
        srt_path = os.path.join(final_dir, srt_filename)
        create_srt(
            ok_results,
            srt_path,
            clip_durations=clip_durations,
            media_paths=scene_clips,
            timeline_offsets=timeline_offsets,
        )
        print(f"[Hậu kỳ] Đã tạo file SRT phụ đề chuẩn âm thanh: {srt_path}")

        # Bước 3: Ghép tất cả clip với hiệu ứng chuyển cảnh
        merged = os.path.join(tmp_dir, f"merged_{ts}.mp4")
        merge_all_scenes(scene_clips, merged, transition=transition)
        print(f"[Hậu kỳ] Đã ghép video thành công: {merged}")

        # Bước 4: Xử lý phụ đề theo cấu hình người dùng
        if subtitle_mode == "burn_in":
            with_sub = os.path.join(tmp_dir, f"with_sub_{ts}.mp4")
            try:
                burn_subtitles(merged, srt_path, with_sub, font_size=font_size)
                if os.path.exists(with_sub) and os.path.getsize(with_sub) > 1000:
                    video_to_mix = with_sub
                    print(f"[Hậu kỳ] Đã khắc phụ đề Burn-in: {with_sub}")
                else:
                    video_to_mix = merged
            except Exception as burn_err:
                print(f"[Hậu kỳ Cảnh báo] Không thể khắc phụ đề ({burn_err}), giữ nguyên video gốc.")
                video_to_mix = merged
        else:
            # "srt_only" hoặc "none" -> Giữ video gốc không khắc chữ
            video_to_mix = merged
            print(f"[Hậu kỳ] Bỏ qua Burn-in (Chế độ phụ đề: {subtitle_mode})")

        # Bước 5: Mix nhạc nền nếu có
        final_video_name = f"{safe_title}_{ts}.mp4"
        final_path = os.path.join(final_dir, final_video_name)

        if bg_music_path and os.path.exists(bg_music_path):
            add_background_music(
                input_video=video_to_mix,
                music_file=bg_music_path,
                output_video=final_path,
                music_volume=music_volume,
                voice_volume=voice_volume,
            )
            print(f"[Hậu kỳ] Đã mix nhạc nền hoàn tất: {final_path}")
        else:
            shutil.copy2(video_to_mix, final_path)
            print(f"[Hậu kỳ] Không có nhạc nền, xuất thẳng video: {final_path}")

        total_movie_duration = get_media_duration(final_path)

        return {
            "video_path": final_path,
            "video_filename": final_video_name,
            "srt_path": srt_path if subtitle_mode in ("burn_in", "srt_only") else None,
            "srt_filename": srt_filename if subtitle_mode in ("burn_in", "srt_only") else None,
            "duration": round(total_movie_duration, 2),
            "audit": audit,
            "scenes_used": [r["index"] for r in ok_results],
            "is_draft": not audit["is_complete"],
            "subtitle_mode": subtitle_mode,
            "quality": quality,
            "transition": transition,
        }

    finally:
        # Dọn thư mục tạm (_tmp: clip cảnh / merged / with_sub) kể cả khi lỗi.
        shutil.rmtree(tmp_dir, ignore_errors=True)
