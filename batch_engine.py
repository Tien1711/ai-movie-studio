import sys
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
"""
batch_engine.py
---------------
Batch Async Engine: Xử lý hàng loạt cảnh video song song đa luồng.
Mỗi Worker độc lập: Sinh TTS + Gửi Muse2API + Poll + Tải video.
"""

import asyncio
import base64
import hashlib
import mimetypes
import os
import re
import shutil
import time
import subprocess
import uuid
import httpx
import edge_tts
import aiofiles
from pathlib import Path
from typing import List, Dict, Any, Callable, Optional, Tuple

# ── Giới hạn / timeout (audit §1.11, §1.12, §3.6-3.8) ─────────────────────────
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024          # video tải về tối đa 500 MB
MAX_KEYFRAME_BYTES = 50 * 1024 * 1024           # ảnh keyframe tối đa 50 MB
MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024   # ảnh tham chiếu đọc vào RAM tối đa 20 MB
FFMPEG_TIMEOUT = 120                            # timeout mặc định cho mỗi lệnh ffmpeg
CHAIN_WAIT_TIMEOUT = 600.0                      # chờ cảnh trước trong chaining tối đa 600s
TRANSIENT_HTTP_STATUSES = {408, 429, 502, 503, 504}

# Neo lại nhân vật trong chuỗi sequential: lỗi nhỏ tích lũy theo chuỗi nên
# cứ mỗi N scene thì dùng ảnh nhân vật/keyframe thay cho prev_last_frame
# để reset drift (consistency.py — chế độ Đồng nhất tối đa).
from consistency import CONSISTENCY_ANCHOR_EVERY, consistency_anchor_reference  # noqa: E402

# Cache duration theo (path, mtime, size) — tránh spawn ffmpeg lặp lại (§3.5).
_DURATION_CACHE: Dict[Tuple[str, float, int], float] = {}
_DURATION_CACHE_MAX = 512

# Thư mục workspace (resolve 1 lần). Mọi đường dẫn file cục bộ do user cung cấp
# chỉ được phép nằm trong cây này — xem _resolve_safe_reference().
_WORKSPACE_ROOT = Path("workspace").resolve()
_PROJ_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _sniff_media_kind(data: bytes) -> Optional[str]:
    """Nhận diện loại media qua magic bytes (không tin Content-Type/header)."""
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "mp4"  # mp4 / mov đều dùng box ftyp
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _is_valid_media_file(path: str, kinds: Tuple[str, ...]) -> bool:
    """File đã tải về có tồn tại, đủ lớn và đúng magic bytes hay không."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 100:
            return False
        with open(path, "rb") as f:
            return _sniff_media_kind(f.read(16)) in kinds
    except OSError:
        return False


def _escape_concat_path(path: str) -> str:
    """Escape đường dẫn cho ffmpeg concat demuxer: ' -> '\\'' (chuẩn ffmpeg)."""
    return "'" + path.replace("'", "'\\''") + "'"


async def _download_validated(
    client: httpx.AsyncClient,
    url: str,
    output_path: str,
    expected: str,  # "video" | "image"
    max_bytes: int,
    timeout: float = 120.0,
) -> str:
    """Tải file theo chunk (không nạp hết RAM), kiểm tra Content-Type + magic
    bytes + giới hạn kích thước. Trả về output_path, raise nếu không hợp lệ."""
    expected_ct = "video/" if expected == "video" else "image/"
    expected_kinds = ("mp4", "webm") if expected == "video" else ("png", "jpg", "gif", "webp")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    total = 0
    first_chunk = b""
    async with client.stream("GET", url, timeout=timeout) as resp:
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and not ctype.startswith(expected_ct):
            raise RuntimeError(
                f"File tải về có Content-Type không hợp lệ: {ctype!r} (cần {expected_ct}*) — URL: {url[:120]}"
            )
        length = resp.headers.get("content-length")
        if length is not None and int(length) > max_bytes:
            raise RuntimeError(f"File tải về quá lớn ({length} bytes > {max_bytes}) — URL: {url[:120]}")
        async with aiofiles.open(output_path, "wb") as f:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"File tải về vượt giới hạn {max_bytes} bytes — URL: {url[:120]}")
                if not first_chunk:
                    first_chunk = chunk[:16]
                await f.write(chunk)
    if total < 100:
        raise RuntimeError(f"File tải về quá nhỏ hoặc rỗng ({total} bytes) — URL: {url[:120]}")
    if _sniff_media_kind(first_chunk) not in expected_kinds:
        raise RuntimeError(
            f"File tải về không đúng định dạng {expected} (magic bytes lạ, có thể là trang lỗi HTML) — URL: {url[:120]}"
        )
    return output_path


def _resolve_safe_reference(ref_str: str) -> Optional[Path]:
    """Resolve reference_image thành đường dẫn file cục bộ AN TOÀN, hoặc None.

    Chỉ cho phép:
    (a) /api/uploads/<basename>            -> workspace/_uploads/<basename>
    (b) /api/shot-desk/projects/<proj>/<rel đã sanitize> -> workspace/<proj>/<rel>
    (c) đường dẫn tuyệt đối/tương đối NHƯNG resolve xong vẫn nằm trong
        workspace/ (phục vụ chaining nội bộ: last-frame do chính pipeline sinh
        ra) — KHÔNG cho đọc file tùy ý ngoài workspace (nhánh os.path.exists
        tuyệt đối cũ đã bị xóa, audit §2.3).
    URL http/https/data: KHÔNG qua hàm này (passthrough ở caller).
    Trả về None nếu không hợp lệ / không tồn tại / không phải file.
    (Không tái dùng guard của app.py: app.py import batch_engine nên import
    ngược sẽ gây circular import; logic ở đây tương đương resolve_safe_*.)
    """
    s = (ref_str or "").strip()
    if not s or s.startswith(("http://", "https://", "data:")):
        return None
    s_norm = s.replace("\\", "/")
    try:
        if s_norm.startswith("/api/uploads/"):
            name = os.path.basename(s_norm)
            if not name or name in (".", ".."):
                return None
            target = (_WORKSPACE_ROOT / "_uploads" / name).resolve()
            base = (_WORKSPACE_ROOT / "_uploads").resolve()
        elif s_norm.startswith("/api/shot-desk/projects/"):
            rest = s_norm[len("/api/shot-desk/projects/"):]
            parts = [p for p in rest.split("/") if p not in ("", ".")]
            if len(parts) < 2 or not _PROJ_ID_RE.match(parts[0]):
                return None
            proj_id, rel_parts = parts[0], parts[1:]
            if any(p == ".." or ":" in p for p in rel_parts):
                return None
            base = (_WORKSPACE_ROOT / proj_id).resolve()
            target = (base.joinpath(*rel_parts)).resolve()
        else:
            # Đường dẫn filesystem: chỉ chấp nhận khi nằm trong workspace/.
            target = (Path(s_norm).resolve())
            base = _WORKSPACE_ROOT
        if not target.is_relative_to(base):
            return None
        if not target.is_file():
            return None
        return target
    except (ValueError, OSError, RuntimeError):
        return None


def _run_ffmpeg(cmd: List[str], timeout: float = FFMPEG_TIMEOUT, **kwargs) -> subprocess.CompletedProcess:
    """Chạy 1 lệnh ffmpeg (sync) luôn có timeout — tránh treo vô thời hạn."""
    return subprocess.run(cmd, timeout=timeout, **kwargs)


async def _is_complete_audio_file(path: Optional[str]) -> bool:
    """File audio có hoàn chỉnh không: tồn tại, size > 500 và không đang được
    ghi dở (size + mtime ổn định qua 2 lần stat cách nhau 0.2s)."""
    if not path:
        return False
    try:
        st1 = os.stat(path)
        if st1.st_size <= 500:
            return False
        await asyncio.sleep(0.2)
        st2 = os.stat(path)
        return st2.st_size == st1.st_size and st2.st_mtime == st1.st_mtime
    except OSError:
        return False


async def get_media_duration_async(file_path: str) -> float:
    """Bản async của get_media_duration (không block event loop)."""
    return await asyncio.to_thread(get_media_duration, file_path)


def get_media_duration(file_path: str) -> float:
    """Đo thời lượng thực tế chính xác (giây) của file audio hoặc video bằng FFmpeg."""
    try:
        st = os.stat(file_path)
        key = (os.path.abspath(file_path), st.st_mtime, st.st_size)
        cached = _DURATION_CACHE.get(key)
        if cached is not None:
            return cached
    except OSError:
        return 5.0
    try:
        import subprocess
        import re
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        res = subprocess.run(
            [ffmpeg, "-i", file_path],
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            errors="replace",
            timeout=30,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr)
        if m:
            dur = float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
            if len(_DURATION_CACHE) >= _DURATION_CACHE_MAX:
                _DURATION_CACHE.clear()
            _DURATION_CACHE[key] = dur
            return dur
    except Exception as exc:
        print(f"[get_media_duration error] {exc}")
    return 5.0


# ── TTS cache: cache file audio theo hash (text chuẩn hóa + voice + speed) ──
# Lần 2 trùng hash thì trả file cache luôn, không gọi lại provider.
# File cache là byte y hệt lần synthesis đầu (không đổi giọng/âm thanh).
# Giới hạn: tối đa 500 file HOẶC 500MB (cái nào chạm trước), dọn LRU theo mtime.
_TTS_CACHE_DIR = Path(__file__).resolve().parent / "workspace" / "_tts_cache"
_TTS_CACHE_MAX_FILES = 500
_TTS_CACHE_MAX_BYTES = 500 * 1024 * 1024
_TTS_CACHE_VERSION = "tts-cache-v1"


def _tts_cache_key(text: str, voice: str, speed: float = 1.0) -> str:
    """Hash SHA256 của (text chuẩn hóa + voice + speed)."""
    norm_text = " ".join((text or "").split())
    raw = f"{_TTS_CACHE_VERSION}\n{voice}\n{speed}\n{norm_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() + ".mp3"


def _tts_cache_prune() -> None:
    """Dọn cache khi vượt giới hạn: xóa file cũ nhất (LRU theo mtime)."""
    try:
        if not _TTS_CACHE_DIR.is_dir():
            return
        entries = [(p, p.stat()) for p in _TTS_CACHE_DIR.glob("*.mp3")]
        total_bytes = sum(s.st_size for _, s in entries)
        if len(entries) <= _TTS_CACHE_MAX_FILES and total_bytes <= _TTS_CACHE_MAX_BYTES:
            return
        entries.sort(key=lambda ps: ps[1].st_mtime)  # cũ nhất trước
        # Duyệt trên bản snapshot: xóa trong lúc lặp list gốc sẽ bỏ sót phần tử.
        for p, s in list(entries):
            if len(entries) <= _TTS_CACHE_MAX_FILES and total_bytes <= _TTS_CACHE_MAX_BYTES:
                break
            try:
                p.unlink()
                total_bytes -= s.st_size
                entries.remove((p, s))
            except OSError:
                pass
    except Exception as exc:
        print(f"[TTS Cache] Prune thất bại (bỏ qua): {exc}")


def _tts_cache_store(cache_path: Path, src_path: str) -> None:
    """Lưu file audio vào cache (ghi temp + os.replace để không race)."""
    try:
        _TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _TTS_CACHE_DIR / f".tmp_{uuid.uuid4().hex[:8]}.mp3"
        shutil.copy2(src_path, tmp)
        os.replace(tmp, cache_path)
        _tts_cache_prune()
    except Exception as exc:
        print(f"[TTS Cache] Lưu cache thất bại (bỏ qua): {exc}")


async def generate_tts_with_status(text: str, voice: str, output_path: str):
    """
    Như generate_tts nhưng trả thêm trạng thái lỗi:
    (duration, tts_failed, tts_error).
    - tts_failed=True khi synthesis lỗi và phải dùng silent fallback
      (audit #9): audio trả về là im lặng, cảnh sẽ KHÔNG có tiếng —
      caller phải đánh dấu scene tts_failed + cảnh báo rõ trong
      Monitor/log thay vì để job chạy tiếp trong im lặng.
    - Vẫn GIỮ fallback im lặng để không gián đoạn việc sinh video.
    Cache: text+voice trùng hash thì trả file cache, không gọi lại provider
    (không cache text rỗng, voice đặc biệt, hay audio fallback im lặng).
    """
    try:
        from tts_manager import synthesize_voice
        clean = (text or "").strip()
        use_cache = bool(clean) and voice not in ("ai_voice", "native", "none", "no_dub")
        cache_path = None
        if use_cache:
            cache_path = _TTS_CACHE_DIR / _tts_cache_key(clean, voice)
            if cache_path.exists() and cache_path.stat().st_size > 0:
                shutil.copy2(cache_path, output_path)
                dur = await asyncio.to_thread(get_media_duration, output_path)
                if dur and dur > 0.3:
                    return dur, False, ""
                return max(2.0, len(clean) / 15.0), False, ""
        dur = await synthesize_voice(text=text, voice=voice, output_path=output_path)
        if use_cache and cache_path is not None and dur and dur > 0:
            try:
                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    _tts_cache_store(cache_path, output_path)
            except OSError:
                pass
        return dur, False, ""
    except Exception as e:
        err = str(e)
        print(f"[TTS Fallback] Lỗi tts_manager ({err}), đang tạo audio dự phòng...")
        estimated_dur = max(2.0, len((text or "").strip()) / 15.0)
        try:
            from tts_manager import create_silent_audio_fallback
            # create_silent_audio_fallback trả None khi ffmpeg fail (audit §1.12):
            # không được coi duration giả là "đã có audio" — chỉ dùng ước lượng
            # để video vẫn render được, file audio thật thì không tồn tại.
            fb_dur = await asyncio.to_thread(
                create_silent_audio_fallback, output_path, estimated_dur
            )
            if fb_dur is None:
                print("[TTS Fallback] Silent fallback thất bại (không có file audio thật).")
            return (fb_dur if fb_dur is not None else estimated_dur), True, err
        except Exception:
            return estimated_dur, True, err


async def generate_tts(text: str, voice: str, output_path: str) -> float:
    """
    Sinh file audio TTS qua hệ thống đa engine tts_manager (VieNeu-TTS, OmniVoice, Edge-TTS).
    Hỗ trợ voice riêng từng nhân vật (per-scene voice).
    Có cơ chế dự phòng an toàn (silent fallback) để không bao giờ làm gián đoạn việc sinh video.
    Trả về thời lượng thực tế chính xác của file âm thanh.
    Giữ nguyên signature cũ (chỉ trả duration) để test/code khác không vỡ;
    muốn biết TTS có lỗi hay không thì dùng generate_tts_with_status.
    """
    dur, _failed, _err = await generate_tts_with_status(text, voice, output_path)
    return dur


async def generate_multi_voice_tts_with_status(
    segments: list,
    output_path: str,
    tmp_dir: str,
):
    """
    Như generate_multi_voice_tts nhưng trả (total_duration, tts_failed, tts_error).
    tts_failed=True khi ÍT NHẤT 1 đoạn lỗi synthesis và phải dùng silent
    fallback (audit #9) — đoạn đó im lặng trong file ghép cuối.
    """
    import shutil
    import subprocess
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

    os.makedirs(tmp_dir, exist_ok=True)

    # TTS đa giọng chạy song song (gather giữ nguyên thứ tự kết quả),
    # giới hạn concurrency bằng semaphore để không spawn quá nhiều engine.
    sem_tts = asyncio.Semaphore(4)

    async def _one_seg(i: int, seg: dict):
        seg_path = os.path.join(tmp_dir, f"seg_{i:03d}.mp3")
        async with sem_tts:
            return await generate_tts_with_status(seg["text"], seg["voice"], seg_path)

    seg_results = await asyncio.gather(
        *[_one_seg(i, seg) for i, seg in enumerate(segments)]
    )
    durations = [r[0] for r in seg_results]
    failed_parts = []
    for i, r in enumerate(seg_results):
        if r[1]:
            who = (segments[i].get("character") or segments[i].get("speaker") or f"đoạn {i + 1}").strip()
            failed_parts.append(f"{who}: {r[2]}")
    tts_failed = bool(failed_parts)
    tts_error = "; ".join(failed_parts)
    seg_files = [os.path.join(tmp_dir, f"seg_{i:03d}.mp3") for i in range(len(segments))]
    total_duration = float(sum(durations))

    if len(seg_files) == 1:
        shutil.copy2(seg_files[0], output_path)
        actual_dur = await get_media_duration_async(output_path)
        return (actual_dur if actual_dur > 0.3 else total_duration), tts_failed, tts_error

    # Ghép nhiều đoạn bằng ffmpeg concat (chống lỗi đường dẫn Windows)
    concat_list = os.path.join(tmp_dir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for fp in seg_files:
            # Escape dấu ' theo chuẩn ffmpeg concat demuxer (audit §1.14).
            f.write(f"file {_escape_concat_path(os.path.basename(fp))}\n")
    abs_out = os.path.abspath(output_path)
    try:
        await asyncio.to_thread(
            _run_ffmpeg,
            [FFMPEG, "-y", "-f", "concat", "-safe", "0",
             "-i", "concat.txt", "-c", "copy", abs_out],
            FFMPEG_TIMEOUT,
            cwd=tmp_dir,
            check=True, capture_output=True,
        )
    finally:
        try: os.remove(concat_list)
        except: pass
    for fp in seg_files:
        try: os.remove(fp)
        except: pass

    actual_dur = await get_media_duration_async(output_path)
    return (actual_dur if actual_dur > 0.3 else total_duration), tts_failed, tts_error


async def generate_multi_voice_tts(
    segments: list,
    output_path: str,
    tmp_dir: str,
) -> float:
    """
    Sinh audio nhiều nhân vật, mỗi đoạn 1 giọng riêng, ghép lại thành 1 file.
    segments: [{"text": "...", "voice": "vi-VN-HoaiMyNeural"}, ...]
    Giữ nguyên signature cũ (chỉ trả duration); muốn biết TTS có lỗi hay
    không thì dùng generate_multi_voice_tts_with_status.
    """
    total, _failed, _err = await generate_multi_voice_tts_with_status(
        segments, output_path, tmp_dir
    )
    return total



import re

GENERIC_POLICY_REPLACEMENTS = [
    # 1. Smashing / Crushing / Shattering / Pulverizing
    (r"\b(?:raising\s+(?:a\s+)?massive\s+\w+\s+and\s+)?smashing\s+(?:it\s+)?down\s+onto\b", "unleashing a sweeping radiant shockwave across"),
    (r"\bsmashing\s+(?:down\s+(?:onto|on)|into|through|apart)\b", "unleashing brilliant energy shockwaves across"),
    (r"\bviolently\s+crushing\b", "firmly gripping"),
    (r"\bcrushing\s+(?:white\s+marble\s+debris|marble|stone|armor|rocks?|metal)\s+into\s+dust\b", "holding glowing crystalline fragments as magical sparks drift"),
    (r"\bcrushing\s+(?:a\s+piece\s+of\s+)?(?:shattered|broken)\b", "examining an ancient glowing"),
    (r"\bcrush(?:ing|es|ed)?\b", "channeling energy into"),
    (r"\bshatter(?:ing|s|ed)?\b", "illuminating with brilliant fractures of light"),
    (r"\bexplodes?\s+.*?\s+into\s+flying\s+shards\b", "emanates bursts of brilliant arcane light"),

    # 2. Hurling bodies / Slamming into abyss / Violent physical knockouts
    (r"\bhurling\s+(?:[a-zA-Z\s]+?)\s+through\s+the\s+air\b", "creating a dramatic gust of glowing wind and drifting embers"),
    (r"\bhurling\b", "sweeping past"),
    (r"\bslamming\s+(?:[a-zA-Z\s]+?)\s+(?:clean\s+)?off\s+the\s+(?:edge\s+of\s+the\s+)?(?:\w+)\s*(?:into\s+the\s+(?:dark\s+)?abyss\s*(?:below)?)?\b", "executing a breathtaking high-speed cinematic pass alongside the opponent"),
    (r"\bslamming\s+(?:into|onto|against)\b", "charging with blazing speed past"),
    (r"\binto\s+the\s+(?:dark\s+)?abyss\s*(?:below)?\b", "across the expansive cinematic realm"),

    # 3. Penetrating attacks / Stabbing / Impaling
    (r"\bplung(?:ing|es|ed)?\s+(?:his\s+|her\s+)?(?:glowing\s+)?(?:greatsword|sword|spear|weapon|blade|dagger|knife)\s+into\b", "raising radiant weapon toward"),
    (r"\bsplitting\s+the\s+(?:entire\s+)?(?:\w+)\s+in\s+half\b", "unleashing an ethereal dome of golden light and drifting sparks across the realm"),
    (r"\bstabb?(?:ing|s|ed)?\s*(?:into|through)?\b", "clashing dynamically against"),
    (r"\bimpales?\b", "outmaneuvers"),

    # 4. Blood / Gory / Death / Mutilation
    (r"\b(?:blood|bloody|bleeding|blood-stained)\b", "dramatic crimson rim light"),
    (r"\bdeath|dying|suicidal|corpse|dead\s+body\b", "unyielding defiant"),
    (r"\bgore|gory|severed|decapitated|slaughter|massacre|bloodbath\b", "epic cinematic duel"),
    (r"\bseverely\s+injured\b", "battle-tested warrior"),
    (r"\bleaking\s+thick\s+(?:black\s+)?oil\b", "dark metallic armor reflecting bright embers"),

    # 5. Firearms / Modern weaponry in fantasy
    (r"\bbarrel\s+of\s+(?:her\s+|his\s+)?(?:silver\s+)?gun\b", "tip of mystical scepter"),
    (r"\bholding\s+a\s+(?:silver\s+)?gun\b", "holding a mystical silver scepter"),
    (r"\bsilver\s+gun\b", "silver glowing relic"),
    (r"\bexplosions?\s+blast\b", "magical shockwaves illuminate"),
    (r"\bcamera\s+violently\s+shakes\b", "camera dynamically tracks"),

    # 6. Combat actions, archery, daggers, and direct strikes
    (r"\b(?:releasing|shoots?|firing)\s+(?:a\s+)?(?:glowing\s+)?(?:flying\s+)?arrows?\s+(?:toward|at)\b", "releasing a streak of radiant starlight soaring toward"),
    (r"\b(?:throwing|unleashes?)\s+(?:a\s+)?(?:volley\s+of\s+)?(?:silver\s+)?daggers?\b", "conjuring shimmering silver light darts"),
    (r"\bvolleys?\s+of\s+(?:silver\s+)?daggers?\b", "shimmering silver light darts"),
    (r"\bdeflect(?:ing|s|ed)?\s+(?:the\s+)?(?:flying\s+)?arrows?\b", "deflecting incoming trails of light"),
    (r"\bstrikes?\s+(?:the\s+)?(?:archer|enemy|opponent|target|assassin)\b", "sweeps past the target"),
    (r"\bcuts?\s+diagonally\b", "traces a diagonal arc of silver light"),
    (r"\bdisarming\s+([a-zA-Z\s]+?)\s+in\s+one\s+fluid\s+movement\b", r"outmaneuvering \1 with breathtaking fluid elegance"),
    (r"\bcollapses?\s+onto\s+the\s+(?:muddy\s+)?ground\s+in\s+defeat\b", "drops into a low stance on the ground"),
    (r"\bpointing\s+(?:his|her)?\s*(?:wet\s+)?(?:blade|sword)\s+at\b", "holding a glowing artifact toward"),
    (r"\bgiant\s+sabre\b", "ancient ceremonial blade"),
    (r"\bsabres?\b", "ancient ceremonial blade"),

    # 7. Restraint / Grabbing / Capturing / Choking / Hands of darkness (Chống lỗi 束缚动作)
    (r"\b(?:hands?\s+of\s+)?(?:dark\s+)?(?:mist|fog|smoke|shadows?)\s+(?:hands?\s+)?grabb?(?:ing|s|ed)?\s+([a-zA-Z\s]+?)\b", r"swirling dark mist gently encircling \1"),
    (r"\b(?:dark\s+)?(?:mist|fog|shadows?)\s+(?:reaches?\s+out\s+and\s+)?grabb?(?:ing|s|ed)?\b", "ethereal dark mist swirling gracefully around"),
    (r"\bgrabb?(?:ing|s|ed)?\s+(?:her|him|them|the\s+\w+)(?:\s+by\s+(?:the\s+)?(?:neck|throat|arm|wrist|hair))?\b", "reaching forward toward the silhouette"),
    (r"\bgrabb?(?:ing|s|ed)?\b", "reaching toward"),
    (r"\brestrain(?:ing|s|ed)?\b", "encircling in dramatic shadows"),
    (r"\bchok(?:ing|es|ed)?|strangl(?:ing|es|ed)?\b", "gasping in high cinematic tension"),
    (r"\bseiz(?:ing|es|ed)?|snatch(?:ing|es|ed)?\b", "surging dynamically toward"),
    (r"\bbound\s+by\s+(?:chains?|ropes?|dark\s+mist|shadows?)\b", "shrouded in dramatic swirling mist"),
    (r"\bclutch(?:ing|es|ed)?\s+(?:her|his|the)?\s*(?:throat|neck)\b", "looking upward in atmospheric shock"),
    (r"\btrapped\s+inside\s+(?:the\s+)?(?:mirror|glass|darkness)\b", "reflected enigmatically within the mirror"),
    (r"\bdrag(?:ging|ged|s)?\s+(?:her|him|them)\s+(?:into|down|away)\b", "gliding with camera tracking backward"),
]


def clean_script_prompt(raw_prompt: str) -> tuple[str, Optional[str]]:
    """
    Tự động chuẩn hoá prompt từ kịch bản thô:
    - Loại bỏ mốc thời gian: '0-10s:', '10-20s:'
    - Giữ lại mô tả thị giác từ '[Màu sắc: ...]'
    - Tách lời thoại trong ngoặc kép hoặc '[Lồng tiếng: ...]' để làm voiceover
    - Trả về (clean_prompt, extracted_dialogue)
    """
    p = raw_prompt.strip()
    extracted_voice = None

    # Trích xuất Màu sắc nếu có
    color_note = ""
    col_m = re.search(r"\[(?:Màu\s*sắc|Color|Tone):\s*([^\]]+)\]", p, re.IGNORECASE)
    if col_m:
        color_note = col_m.group(1).strip()

    # Bóc tách câu thoại từ [Lồng tiếng: ...] nếu có (hỗ trợ cả trong và ngoài ngoặc vuông)
    voice_match = re.search(r"\[(?:Lồng\s*tiếng|Voiceover|Thoại)[^\]]*\]\s*([^\"“\n\r]*?)\s*[:：]\s*[\"“]([^\"”\r\n]+)[\"”]", p, re.IGNORECASE)
    if voice_match:
        extracted_voice = voice_match.group(2).strip()
    else:
        voice_match_inside = re.search(r"\[(?:Lồng\s*tiếng|Voiceover|Thoại)[^\]]*?[:：]\s*[\"“]?([^\"”\]\r\n]+)[\"”]?[^\]]*\]", p, re.IGNORECASE)
        if voice_match_inside:
            extracted_voice = voice_match_inside.group(1).strip()

    # Xóa mốc thời gian và các thẻ cấu trúc [...]
    p = re.sub(r"^\s*\d+\s*[-–—]\s*\d+\s*s?\s*:\s*", "", p, flags=re.IGNORECASE)
    without_tags = re.sub(r"\[[^\]]+\]", " ", p)
    without_tags = re.sub(r"\s+", " ", without_tags).strip()

    # Bóc tách thoại trong ngoặc kép nếu chưa có extracted_voice
    quote_matches = list(re.finditer(r'["“]([^"”\n\r]+)["”]', without_tags))
    speech_cuts = []
    if quote_matches:
        for qm in quote_matches:
            speech = qm.group(1).strip()
            if not extracted_voice:
                extracted_voice = speech
            q_idx = qm.start()
            before = without_tags[:q_idx].strip()
            spk_match = re.search(r'(?:^|[,\.!\?;\n])\s*([^,.\n:"]*?)\s*(?:nói|bảo|hỏi|đáp|thì thầm|gào thét|gầm lên|ngắt quãng|lẩm bẩm|hô|quát)?[^,.\n:"]*:\s*$', before, re.IGNORECASE)
            lead_in_start = q_idx
            if spk_match:
                full_m = spk_match.group(0)
                m_idx = before.rfind(full_m)
                sep_m = re.match(r'^[,\.!\?;\n]\s*', full_m)
                sep_len = len(sep_m.group(0)) if sep_m else 0
                lead_in_start = m_idx + sep_len
            speech_cuts.append((lead_in_start, qm.end()))

    # Dựng prompt thị giác không chứa thoại
    clean_prompt = ""
    last_idx = 0
    for s_cut, e_cut in speech_cuts:
        clean_prompt += without_tags[last_idx:s_cut] + " "
        last_idx = e_cut
    clean_prompt += without_tags[last_idx:]
    clean_prompt = re.sub(r"\s+", " ", clean_prompt).strip(" ,.")

    if color_note and color_note.lower() not in clean_prompt.lower():
        clean_prompt = f"{clean_prompt}, {color_note}" if clean_prompt else color_note

    return clean_prompt, extracted_voice


def sanitize_prompt_heuristics(prompt: str, error_msg: str = "", aggressive: bool = False) -> str:
    """
    Bộ làm sạch Prompt HOÀN TOÀN TỰ ĐỘNG & ĐỘC LẬP:
    - Loại bỏ triệt để mọi từ khóa bạo lực thể xác, máu me, vũ khí sát thương, bắt giữ/trói buộc (束缚动作).
    - Tự động thay thế từ vựng tiếng Trung / tiếng Việt vi phạm thành ngôn ngữ điện ảnh an toàn.
    - Chuyển thành chuyển động máy quay (Camera Move) và ánh sáng điện ảnh an toàn 100%.
    """
    p = prompt
    for pattern, repl in GENERIC_POLICY_REPLACEMENTS:
        p = re.sub(pattern, repl, p, flags=re.IGNORECASE)

    # Từ vựng tiếng Trung / tiếng Việt thường gặp khi prompt dịch hoặc đa ngôn ngữ
    zh_vn_replaces = [
        (r"黑雾之手抓住", "黑雾轻柔环绕"),
        (r"抓住", "伸向"),
        (r"束缚", "环绕"),
        (r"掐住|勒住", "注视"),
        (r"bắt lấy|tóm lấy|chộp lấy", "vươn tay về phía"),
        (r"trói buộc|ghì chặt", "bao quanh bởi làn sương"),
        (r"đánh đập|bóp cổ", "kịch tính điện ảnh"),
    ]
    for pattern, repl in zh_vn_replaces:
        p = re.sub(pattern, repl, p)

    is_refusal_trigger = aggressive or any(k in error_msg.lower() for k in [
        "未生成video", "踩线", "束缚", "violation", "refused", "policy", "từ chối", "safety"
    ])
    if is_refusal_trigger:
        # Làm sạch quyết liệt: thay thế toàn bộ động từ tương tác thể xác bằng chuyển động máy quay
        p = re.sub(r"\b(?:kill|attack|destroy|strike|smash|hit|slam|grab|seize|restrain|drag|choke|pull)\b", "move with", p, flags=re.IGNORECASE)
        p = re.sub(r"\b(?:weapon|blade|sword|lance|spear|knife|chains?)\b", "glowing artifact", p, flags=re.IGNORECASE)
        p = re.sub(r"\b(?:violent|aggressive|deadly|brutal|bloody|dark\s+mirror-self)\b", "mysterious ethereal double", p, flags=re.IGNORECASE)
        if not re.search(r"\b(?:slow|camera|push-in|zoom|pan|tracking)\b", p, re.IGNORECASE):
            p = f"Slow cinematic push-in, {p}"

    # Đảm bảo có phong cách điện ảnh chuẩn
    if "cinematic" not in p.lower():
        p += ", cinematic lighting, 4k resolution, masterpiece"

    return re.sub(r"\s+", " ", p).strip()


async def heal_prompt_with_ai(
    prompt: str,
    error_msg: str,
    base_url: str = "http://localhost:18610",
    api_key: Optional[str] = None,
    timeout: float = 6.0
) -> str:
    """
    Tối ưu prompt tự động bằng bộ làm sạch an toàn cục bộ.

    LƯU Ý: hàm này KHÔNG gọi AI / KHÔNG gọi API nào — tên hàm giữ nguyên chỉ
    để không vỡ caller (app.py gọi heal_prompt_with_ai(...)). Mọi tham số
    base_url/api_key/timeout được giữ lại vì tương thích signature cũ nhưng
    không được dùng để gọi mạng. api_key mặc định là None (KHÔNG còn giá trị
    hardcode — audit §2.2); nếu caller truyền key hoặc env MUSE2API_KEY có
    giá trị thì cũng chỉ được đọc, không gửi đi đâu trong hàm này.
    """
    _ = base_url
    _ = timeout
    _ = api_key or os.getenv("MUSE2API_KEY")  # chỉ resolve, không dùng để gọi AI
    return sanitize_prompt_heuristics(prompt, error_msg, aggressive=True)


async def poll_video_task(
    client: httpx.AsyncClient,
    task_id: str,
    base_url: str,
    headers: dict,
    scene_idx: int,
    progress_cb: Optional[Callable] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> str:
    """
    Poll trạng thái task video thích ứng & không spam API:
    - Không giới hạn thời gian chờ quá sớm (hỗ trợ queue nhiều tài khoản).
    - cancel_event: được set khi người dùng hủy job (audit #8) — vòng poll
      kiểm tra mỗi lượt và raise CancelledError để dừng ngay thay vì poll
      đủ 600s.
    """
    start_time = time.time()
    max_wait_seconds = 600  # 10 phút tối đa (đảm bảo đủ thời gian render video AI & xoay vòng tài khoản)
    attempt = 0
    consecutive_net_errors = 0

    while time.time() - start_time < max_wait_seconds:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError(
                f"[Cảnh {scene_idx}] Job đã bị hủy trong lúc chờ video.")
        attempt += 1
        elapsed = int(time.time() - start_time)

        if elapsed < 20:
            sleep_interval = 6
        elif elapsed < 60:
            sleep_interval = 5
        else:
            sleep_interval = 4

        await asyncio.sleep(sleep_interval)

        try:
            res = await client.get(
                f"{base_url}/v1/videos/{task_id}",
                headers=headers,
                timeout=25.0,
            )
            # Audit §1.11: kiểm tra HTTP status trước khi đọc JSON — 5xx/HTML
            # không được coi như "đang render" rồi poll lặng lẽ đủ 600s.
            if res.status_code in TRANSIENT_HTTP_STATUSES:
                raise httpx.HTTPStatusError(
                    f"HTTP {res.status_code} (transient)", request=res.request, response=res
                )
            res.raise_for_status()
            consecutive_net_errors = 0
            try:
                data = res.json()
            except ValueError as json_err:
                raise RuntimeError(
                    f"[Cảnh {scene_idx}] Muse2API trả về nội dung không phải JSON "
                    f"(HTTP {res.status_code}): {json_err}"
                ) from json_err
            status = data.get("status", "unknown")

            if status == "completed":
                url = data.get("video_url") or data.get("url") or ""
                return f"{base_url}{url}" if url.startswith("/") else url

            if status == "failed":
                err_msg = data.get("error", "N/A")
                raise RuntimeError(
                    f"[Cảnh {scene_idx}] Muse2API báo thất bại: {err_msg}"
                )

            prog = data.get("progress")
            acc = data.get("account")
            parts = [f"Đang render... ({elapsed}s)"]
            if prog:
                parts.append(f"{prog}%")
            if acc:
                parts.append(f"acc:{acc[:6]}")
            if progress_cb:
                progress_cb(scene_idx, " • ".join(parts))

        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                httpx.HTTPStatusError) as net_err:
            # Chỉ retry lỗi transient (timeout / 502 / 503 / 5xx...); lỗi HTTP
            # khác đã raise_for_status() ở trên và propagate ra ngoài.
            if isinstance(net_err, httpx.HTTPStatusError) and (
                net_err.response is None
                or net_err.response.status_code not in TRANSIENT_HTTP_STATUSES
            ):
                raise
            consecutive_net_errors += 1
            if consecutive_net_errors >= 15:
                raise RuntimeError(f"[Cảnh {scene_idx}] Mất kết nối liên tục với máy chủ Muse2API ({net_err})")
            await asyncio.sleep(4)

    raise TimeoutError(f"[Cảnh {scene_idx}] Đã hết thời gian chờ tối đa ({int(max_wait_seconds/60)} phút)")


def extract_last_frame(video_path: str, output_image_path: str) -> Optional[str]:
    """
    Trích xuất khung hình cuối cùng (last frame) của video MP4 bằng FFmpeg.
    Dùng làm first_frame cho cảnh tiếp theo theo kỹ thuật First/Last Frame Chaining.
    """
    if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        return None

    import imageio_ffmpeg
    import subprocess
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(os.path.dirname(os.path.abspath(output_image_path)), exist_ok=True)

    # Chiến lược 1: Seek tới 0.1s trước khi kết thúc video (-sseof -0.1)
    try:
        cmd1 = [
            FFMPEG, "-y",
            "-sseof", "-0.1",
            "-i", video_path,
            "-update", "1",
            "-frames:v", "1",
            "-q:v", "2",
            output_image_path
        ]
        _run_ffmpeg(cmd1, timeout=60, capture_output=True)
        if os.path.exists(output_image_path) and os.path.getsize(output_image_path) > 300:
            return output_image_path
    except Exception:
        pass

    # Chiến lược 2 (chỉ khi clip quá ngắn khiến -sseof thất bại): đảo ngược
    # video và lấy frame đầu tiên. KHÔNG decode ngược toàn bộ clip vô điều
    # kiện với clip dài (audit §3.6); clip đủ dài thì seek tuyệt đối thay thế.
    clip_dur = get_media_duration(video_path)
    if clip_dur and clip_dur > 1.0:
        try:
            cmd_seek = [
                FFMPEG, "-y",
                "-ss", f"{max(0.0, clip_dur - 0.1):.3f}",
                "-i", video_path,
                "-update", "1",
                "-frames:v", "1",
                "-q:v", "2",
                output_image_path
            ]
            _run_ffmpeg(cmd_seek, timeout=60, capture_output=True)
            if os.path.exists(output_image_path) and os.path.getsize(output_image_path) > 300:
                return output_image_path
        except Exception:
            pass
        return None
    try:
        cmd2 = [
            FFMPEG, "-y",
            "-i", video_path,
            "-vf", "reverse",
            "-frames:v", "1",
            "-q:v", "2",
            output_image_path
        ]
        _run_ffmpeg(cmd2, timeout=60, capture_output=True)
        if os.path.exists(output_image_path) and os.path.getsize(output_image_path) > 300:
            return output_image_path
    except Exception:
        pass

def apply_style_locks(prompt: str, style_locks: Optional[Dict[str, str]] = None) -> str:
    """Ghép các khóa phong cách điện ảnh (Style, Lighting, Color grade, Camera lens) vào prompt."""
    if not style_locks:
        return prompt
    parts = [prompt.strip()]
    style = style_locks.get("style", "").strip()
    lighting = style_locks.get("lighting", "").strip()
    color = style_locks.get("color", "").strip()
    camera = style_locks.get("camera", "").strip()
    character = style_locks.get("character", "").strip()

    if style and style.lower() not in prompt.lower():
        parts.append(f"Style: {style}")
    if lighting and lighting.lower() not in prompt.lower():
        parts.append(f"Lighting: {lighting}")
    if color and color.lower() not in prompt.lower():
        parts.append(f"Color grading: {color}")
    if camera and camera.lower() not in prompt.lower():
        parts.append(f"Camera: {camera}")
    if character and character.lower() not in prompt.lower():
        parts.append(f"Character: {character}")

    return ", ".join(parts)


async def generate_keyframe_image(
    prompt: str,
    base_url: str,
    api_key: str,
    output_path: str,
    aspect_ratio: str = "9:16",
    reference_image: Optional[str] = None,
) -> Dict[str, Any]:
    """Tạo ảnh tĩnh Keyframe trước khi render video qua muse2api với polling an toàn."""
    payload: Dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "size": aspect_ratio,
        "model": "muse-image",
        "response_format": "url",
        "async": True,
    }
    if reference_image:
        payload["reference_image"] = reference_image

    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Gửi request tạo task async
        try:
            r = await client.post(f"{base_url}/v1/images/tasks", json=payload, headers=headers)
        except Exception:
            r = await client.post(f"{base_url}/v1/images/generations", json=payload, headers=headers)

        if r.status_code in (200, 202):
            try:
                data = r.json()
            except ValueError as json_err:
                raise RuntimeError(f"Muse2API trả về nội dung không phải JSON: {json_err}") from json_err
            task_id = data.get("id") or data.get("task_id")
            if task_id:
                # Poll cho tới khi hoàn tất (tối đa 180s)
                start_t = time.time()
                while time.time() - start_t < 180:
                    await asyncio.sleep(4)
                    poll_res = await client.get(f"{base_url}/v1/images/tasks/{task_id}", headers=headers)
                    # Audit §1.11: không poll lặng lẽ khi server trả lỗi.
                    if poll_res.status_code in TRANSIENT_HTTP_STATUSES:
                        continue
                    poll_res.raise_for_status()
                    try:
                        pdata = poll_res.json()
                    except ValueError as json_err:
                        raise RuntimeError(f"Muse2API poll ảnh trả về nội dung không phải JSON: {json_err}") from json_err
                    st = pdata.get("status")
                    if st == "completed":
                        img_url = pdata.get("url") or (pdata.get("data", [{}])[0].get("url") if pdata.get("data") else None)
                        if img_url:
                            download_url = f"{base_url}{img_url}" if img_url.startswith("/") else img_url
                            await _download_validated(
                                client, download_url, output_path,
                                expected="image", max_bytes=MAX_KEYFRAME_BYTES, timeout=60.0,
                            )
                            return {"url": download_url, "local_path": output_path}
                    elif st == "failed":
                        err_msg = pdata.get("error", "Muse AI báo lỗi khi tạo ảnh")
                        raise RuntimeError(f"Lỗi sinh ảnh keyframe: {err_msg}")
                raise TimeoutError("Quá thời gian chờ tạo ảnh keyframe (180s)")
            elif "data" in data and data["data"]:
                img_url = data["data"][0].get("url")
                if img_url:
                    download_url = f"{base_url}{img_url}" if img_url.startswith("/") else img_url
                    await _download_validated(
                        client, download_url, output_path,
                        expected="image", max_bytes=MAX_KEYFRAME_BYTES, timeout=60.0,
                    )
                    return {"url": download_url, "local_path": output_path}

        raise RuntimeError(f"Muse2API trả về HTTP {r.status_code}: {r.text[:300]}")


async def process_scene(
    client: httpx.AsyncClient,
    scene: Dict[str, Any],
    sem: asyncio.Semaphore,
    base_url: str,
    api_key: str,
    output_dir: str,
    voice: str,
    progress_cb: Optional[Callable] = None,
    style_locks: Optional[Dict[str, str]] = None,
    voice_mode: str = "tts",
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """
    Pipeline xử lý 1 cảnh độc lập với cơ chế kiểm soát retry thông minh (Non-Spamming):
    - Tự động tiền kiểm & làm sạch an toàn trước khi gửi API (Pre-flight Sanitization).
    - Hỗ trợ tham chiếu Production Bible & First/Last Frame Chaining.
    - Hỗ trợ chế độ AI Voice: Đưa lời thoại vào prompt để AI tự tạo tiếng nhân vật & âm thanh gốc.
    - Nếu gặp lỗi kiểm duyệt: Chỉ thử lại TỐI ĐA 1 LẦN với prompt đã làm sạch triệt để.
    - Tuyệt đối không retry mù quáng hay lặp vô tận.
    """
    async with sem:
        # ── Setup scene: BỌC TOÀN BỘ trong try (§1.9) ──
        try:
            idx = scene["index"]
            raw_prompt = scene.get("prompt", "")
            voiceover = scene.get("voiceover", "")
            duration = int(scene.get("duration", 5))
            size = scene.get("size", "16:9")
            scene_voice = scene.get("voice") or voice
            scene_voice_mode = scene.get("voice_mode") or voice_mode or "tts"
            is_ai_voice = bool(
                scene_voice_mode in ("ai_voice", "native", "none", "no_dub")
                or scene_voice in ("ai_voice", "native", "none", "no_dub")
            )
            voice_segments = scene.get("voice_segments") or []

            result: Dict[str, Any] = {
                "index": idx,
                "status": "processing",
                "video_path": None,
                "audio_path": None,
                "voiceover": voiceover,
                "audio_duration": duration,
                "last_frame_path": None,
                "last_frame_url": None,
                "error": None,
                "tts_failed": False,
                "tts_error": None,
            }

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            audio_path = os.path.join(output_dir, "audio", f"scene_{idx:03d}.mp3") if not is_ai_voice else None
            video_path = os.path.join(output_dir, "scenes", f"scene_{idx:03d}.mp4")
            tmp_seg_dir = os.path.join(output_dir, "audio", f"segs_{idx:03d}")

            original_prompt = scene.get("original_prompt") or raw_prompt
            # Tự động bóc tách kịch bản thô (loại bỏ [Màu sắc], [Âm thanh], [Lồng tiếng])
            cleaned_prompt, ext_voice = clean_script_prompt(raw_prompt)
            if not voiceover and ext_voice:
                voiceover = ext_voice
                result["voiceover"] = voiceover

            # Áp dụng khóa phong cách (Style, Lighting, Color, Camera)
            styled_prompt = apply_style_locks(cleaned_prompt, style_locks)
            enhanced_prompt = styled_prompt

            # Tự động tiền kiểm tra & làm sạch an toàn
            prompt = sanitize_prompt_heuristics(styled_prompt)

            spk_names = []
            if voice_segments:
                for seg in voice_segments:
                    cname = (seg.get("character") or seg.get("speaker") or "").strip()
                    if cname and cname not in spk_names:
                        spk_names.append(cname)
            if not spk_names and scene.get("character"):
                spk_names.append(scene.get("character").strip())

            p_lower = prompt.lower()
            has_dialogue = bool(voiceover or voice_segments)

            if is_ai_voice:
                # 🤖 CHẾ ĐỘ ĐƯA THOẠI VÀO PROMPT ĐỂ AI TỰ SINH TIẾNG NHÂN VẬT & ÂM THANH
                if voice_segments and len(voice_segments) > 0:
                    dialogue_parts = []
                    for seg in voice_segments:
                        cname = (seg.get("character") or seg.get("speaker") or "").strip()
                        txt = (seg.get("text") or "").strip()
                        if cname and txt:
                            dialogue_parts.append(f"{cname} says '{txt}'")
                        elif txt:
                            dialogue_parts.append(f"character says '{txt}'")
                    dialogue_clause = ", ".join(dialogue_parts)
                    prompt = f"{prompt}, audible sound design and native speech: characters speaking naturally with synchronized realistic vocal audio and clear ambient atmosphere: {dialogue_clause}"
                elif voiceover:
                    spk_clause = f"the character {spk_names[0]}" if spk_names else "the character"
                    prompt = f"{prompt}, audible sound design and native speech: {spk_clause} speaking with natural voice '{voiceover}', synchronized realistic vocal audio and clear ambient atmosphere"
                else:
                    prompt = f"{prompt}, realistic synchronized sound design, clear natural ambient audio and Foley sound effects"
            else:
                # 🎙️ CHẾ ĐỘ LỒNG TIẾNG TTS: Bổ sung chỉ thị khẩu hình (Lip-sync Articulation)
                if has_dialogue and not any(k in p_lower for k in ["lip movement", "mouth movement", "speaking dialogue", "lip sync", "khớp môi", "articulation", "talking with"]):
                    spk_clause = f"the character {', '.join(spk_names)}" if spk_names else "the character"
                    prompt = f"{prompt}, medium shot of {spk_clause} talking with expressive natural synchronized mouth movement and realistic lip articulation speaking Vietnamese dialogue"

            if prompt != raw_prompt:
                scene["prompt"] = prompt

            healed_prompt = scene.get("healed_prompt")
            result["original_prompt"] = original_prompt
            result["enhanced_prompt"] = enhanced_prompt
            result["healed_prompt"] = healed_prompt
            result["used_prompt"] = prompt

        except Exception as setup_err:
            idx_val = scene.get("index") if isinstance(scene, dict) else None
            err_msg = f"Lỗi setup scene {idx_val}: {setup_err}"
            print(f"[batch_engine] {err_msg}")
            if progress_cb:
                try:
                    progress_cb(idx_val, f"❌ {err_msg[:80]}")
                except TypeError:
                    pass
            return {
                "index": idx_val,
                "status": "error",
                "video_path": None,
                "audio_path": None,
                "voiceover": scene.get("voiceover", "") if isinstance(scene, dict) else "",
                "audio_duration": 5,
                "last_frame_path": None,
                "last_frame_url": None,
                "error": err_msg,
            }

        max_attempts = 2
        tts_task = None  # khởi tạo trước vòng retry để except handler luôn cancel được (§1.10)
        for attempt in range(1, max_attempts + 1):
            try:
                # Audit #8: kiểm tra cờ hủy trước mỗi attempt — không bắt đầu
                # render cảnh mới khi job đã bị hủy.
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError(
                        f"[Cảnh {idx}] Job đã bị hủy trước khi render cảnh.")
                # Timing từng giai đoạn của scene (đo tốc độ render):
                # render_s = wall time của attempt (submit + poll + tải + đồng bộ),
                # poll_s = thời gian chờ API video, tts_s = thời gian sinh TTS
                # (TTS chạy song song với poll nên poll_s/tts_s chồng lấn nhau,
                # không cộng trực tiếp vào render_s).
                attempt_t0 = time.monotonic()
                tts_started = None
                tts_s = 0.0
                poll_s = 0.0
                if attempt > 1:
                    if progress_cb:
                        progress_cb(idx, f"🔄 Đang thử lại lần {attempt}/{max_attempts}...")
                    await asyncio.sleep(6 * attempt)
                else:
                    if progress_cb:
                        msg_init = "Đang gửi tới muse2api & đưa thoại vào prompt (AI Voice)..." if is_ai_voice else "Đang gửi tới muse2api & sinh TTS..."
                        progress_cb(idx, msg_init)

                # 1. Sinh TTS (Bỏ qua khi ở chế độ AI Voice)
                # Audit #9: dùng bản _with_status để biết TTS có lỗi không —
                # lỗi thì vẫn fallback im lặng nhưng phải đánh dấu scene.
                tts_failed = False
                tts_error = ""
                if is_ai_voice:
                    tts_task = None
                else:
                    if not await _is_complete_audio_file(audio_path):
                        if voice_segments and len(voice_segments) > 0:
                            tts_task = asyncio.create_task(
                                generate_multi_voice_tts_with_status(voice_segments, audio_path, tmp_seg_dir)
                            )
                        else:
                            tts_task = asyncio.create_task(
                                generate_tts_with_status(voiceover, scene_voice, audio_path)
                            )
                    else:
                        tts_task = None
                if tts_task is not None:
                    tts_started = time.monotonic()

                payload = {"prompt": prompt, "duration": duration, "size": size, "timeout": 600}
                ref_img = scene.get("reference_image") or scene.get("image")
                if ref_img:
                    ref_str = str(ref_img).strip()
                    if ref_str.startswith(("http://", "https://", "data:")):
                        # URL/data: passthrough, không fetch ở đây (§2.3).
                        payload["reference_image"] = ref_str
                    else:
                        # Chỉ đọc file sau khi _resolve_safe_reference() xác nhận
                        # nằm trong workspace/ (nhánh đọc đường dẫn tuyệt đối
                        # tùy ý đã bị xóa — audit §2.3).
                        safe_ref = _resolve_safe_reference(ref_str)
                        if safe_ref is not None:
                            if safe_ref.stat().st_size > MAX_REFERENCE_IMAGE_BYTES:
                                print(f"[batch_engine] Bỏ qua reference_image quá lớn "
                                      f"({safe_ref.stat().st_size} bytes): {ref_str[:80]}")
                            else:
                                m = mimetypes.guess_type(str(safe_ref))[0] or "image/jpeg"
                                with open(safe_ref, "rb") as f:
                                    payload["reference_image"] = (
                                        f"data:{m};base64,{base64.b64encode(f.read()).decode('ascii')}"
                                    )
                        else:
                            print(f"[batch_engine] Bỏ qua đường dẫn reference_image không tồn tại hoặc không hợp lệ: {ref_str[:80]}")

                res = await client.post(
                    f"{base_url}/v1/videos",
                    json=payload,
                    headers=headers,
                    timeout=60.0,
                )
                res.raise_for_status()
                try:
                    task_id = res.json().get("id", "")
                except ValueError as json_err:
                    raise RuntimeError(
                        f"Muse2API trả về nội dung không phải JSON (HTTP {res.status_code}): {json_err}"
                    ) from json_err
                if not task_id:
                    raise ValueError("Muse2API không trả về task_id hợp lệ")

                if progress_cb:
                    progress_cb(idx, f"⏳ Task ID: {task_id[:12]}... đang render")

                # 2. Poll kết quả
                _poll_t0 = time.monotonic()
                video_url = await poll_video_task(
                    client, task_id, base_url, headers, idx, progress_cb,
                    cancel_event=cancel_event,
                )
                poll_s += time.monotonic() - _poll_t0

                if tts_task:
                    audio_duration, tts_failed, tts_error = await tts_task
                    if tts_started is not None:
                        tts_s = time.monotonic() - tts_started
                    if tts_failed:
                        # Audit #9: TTS lỗi -> audio im lặng, cảnh mất tiếng.
                        # Báo ngay qua progress (Monitor/log thấy liền) và
                        # đánh dấu vào result để film_pipeline cảnh báo tiếp.
                        print(f"[batch_engine Warning] TTS scene {idx} lỗi "
                              f"({tts_error[:120]}): dùng audio im lặng — cảnh sẽ không có tiếng.")
                        if progress_cb:
                            warn_msg = (f"⚠️ TTS lỗi: {tts_error[:60]} — cảnh này "
                                        f"sẽ không có tiếng (audio im lặng).")
                            try:
                                progress_cb(idx, warn_msg, {"tts_failed": True, "tts_error": tts_error})
                            except TypeError:
                                progress_cb(idx, warn_msg)
                    if audio_path and not await _is_complete_audio_file(audio_path):
                        print(f"[batch_engine Warning] TTS scene {idx} xong nhưng file audio "
                              f"không hoàn chỉnh — dùng duration ước lượng.")
                        audio_duration = max(2.0, len(voiceover) / 3.0)
                        if not tts_failed and (voiceover or voice_segments):
                            tts_failed = True
                            tts_error = "file audio không hoàn chỉnh sau khi sinh TTS"
                else:
                    if is_ai_voice:
                        text_len = len(voiceover) if voiceover else sum(len(s.get("text", "")) for s in voice_segments)
                        audio_duration = max(float(duration), min(10.0, max(2.5, text_len / 3.0)))
                    else:
                        audio_duration = max(2.0, len(voiceover) / 3.0)

                if progress_cb:
                    progress_cb(idx, "📥 Đang tải video về...")

                raw_video_path = os.path.join(output_dir, "scenes", f"scene_{idx:03d}_raw.mp4")
                await _download_validated(
                    client, video_url, raw_video_path,
                    expected="video", max_bytes=MAX_DOWNLOAD_BYTES, timeout=300.0,
                )

                # Bảo vệ chống video tích lũy/kéo dài: tự động cắt phần đuôi mới sinh nếu video bị dài hơn thời lượng
                expected_dur = max(float(duration), float(audio_duration or 0.0))
                try:
                    raw_dur = await get_media_duration_async(raw_video_path)
                    if raw_dur > (expected_dur + 0.3):
                        offset = max(0.0, raw_dur - expected_dur)
                        temp_raw = raw_video_path + ".trim.mp4"
                        import imageio_ffmpeg
                        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                        await asyncio.to_thread(
                            _run_ffmpeg,
                            [ffmpeg_exe, "-y",
                             "-ss", f"{offset:.3f}",
                             "-i", raw_video_path,
                             "-t", f"{expected_dur:.3f}",
                             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                             "-pix_fmt", "yuv420p",
                             "-c:a", "aac", "-b:a", "192k",
                             temp_raw],
                            180,
                            check=True, capture_output=True)
                        if os.path.exists(temp_raw) and os.path.getsize(temp_raw) > 500:
                            os.replace(temp_raw, raw_video_path)
                except Exception as trim_err:
                    print(f"[batch_engine Warning] Trimming raw video scene {idx} failed: {trim_err}")

                # Ghép ngay audio thoại vào video cảnh để bản preview có tiếng ngay lập tức
                final_dir = os.path.join(output_dir, "final")
                os.makedirs(final_dir, exist_ok=True)
                clip_path = os.path.join(final_dir, f"clip_{idx:03d}.mp4")

                try:
                    from composer import compose_scene_video
                    if progress_cb:
                        msg_sync = "🎬 Đang chuẩn hóa âm thanh & giọng gốc AI..." if is_ai_voice else "🎬 Đang đồng bộ lời thoại & âm thanh..."
                        progress_cb(idx, msg_sync)

                    has_real_audio = bool(audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 500)
                    await asyncio.to_thread(
                        compose_scene_video,
                        scene_video=raw_video_path,
                        scene_audio=audio_path if has_real_audio else None,
                        output=video_path,
                        audio_duration=audio_duration,
                        target_duration=expected_dur,
                        aspect_ratio=size,
                    )
                    import shutil
                    shutil.copy2(video_path, clip_path)
                except Exception as comp_err:
                    print(f"[batch_engine Warning] Ghép âm thanh ngay cho scene {idx} thất bại: {comp_err}")
                    import shutil
                    # Fallback chỉ được copy file ĐÃ validate hợp lệ (§1.12) —
                    # không bao giờ copy file rác làm clip_path.
                    if not _is_valid_media_file(raw_video_path, ("mp4", "webm")):
                        raise RuntimeError(
                            f"Video gốc scene {idx} không hợp lệ (magic bytes/size) "
                            f"nên không thể fallback sau khi compose lỗi: {comp_err}"
                        ) from comp_err
                    if not os.path.exists(video_path):
                        shutil.copy2(raw_video_path, video_path)
                    if not os.path.exists(clip_path):
                        shutil.copy2(video_path, clip_path)

                # 3. Trích xuất khung hình cuối cùng (Last Frame Extraction)
                last_frame_path = os.path.join(output_dir, "scenes", f"scene_{idx:03d}_last_frame.jpg")
                extracted_frame = await asyncio.to_thread(
                    extract_last_frame, video_path, last_frame_path
                )
                job_id = os.path.basename(output_dir)
                last_frame_url = f"/api/jobs/{job_id}/last-frame/{idx}" if extracted_frame else None

                full_voiceover = voiceover
                if voice_segments:
                    full_voiceover = " | ".join(
                        f"{seg.get('character', '')}: {seg.get('text', '')}" if seg.get('character') else seg.get('text', '')
                        for seg in voice_segments
                    )

                result.update(
                    {
                        "status": "done",
                        "video_path": video_path,
                        "raw_video_path": raw_video_path,
                        "audio_path": audio_path,
                        "audio_duration": audio_duration,
                        "voiceover": full_voiceover,
                        "last_frame_path": extracted_frame,
                        "last_frame_url": last_frame_url,
                        "error": None,
                        "tts_failed": tts_failed,
                        "tts_error": tts_error or None,
                        "timing": {
                            "render_s": round(time.monotonic() - attempt_t0, 3),
                            "poll_s": round(poll_s, 3),
                            "tts_s": round(tts_s, 3),
                        },
                    }
                )

                if progress_cb:
                    extra_data = {
                        "status": "done",
                        "video_url": f"/api/jobs/{job_id}/scenes/{idx}/video",
                        "last_frame_url": last_frame_url,
                        "prompt": scene.get("prompt", ""),
                        "voiceover": full_voiceover,
                        "error": None,
                        "tts_failed": tts_failed,
                        "tts_error": tts_error or None,
                    }
                    if tts_failed:
                        msg_text = "✅ Hoàn thành ⚠️ TTS lỗi — cảnh mất tiếng (audio im lặng)"
                    else:
                        msg_text = "✅ Hoàn thành (Đã trích xuất Last Frame)" if extracted_frame else "✅ Hoàn thành!"
                    try:
                        progress_cb(idx, msg_text, extra_data)
                    except TypeError:
                        progress_cb(idx, msg_text)
                break

            except asyncio.CancelledError:
                if tts_task is not None and not tts_task.done():
                    tts_task.cancel()
                    try:
                        await tts_task
                    except (asyncio.CancelledError, Exception):
                        pass
                raise
            except Exception as e:
                # Task TTS nền không được để rò rỉ: cancel + await trong mọi
                # nhánh lỗi để nó không ghi đè file khi retry (§1.10).
                if tts_task is not None and not tts_task.done():
                    tts_task.cancel()
                    try:
                        await tts_task
                    except (asyncio.CancelledError, Exception):
                        pass
                clean_err = str(e).replace('\n', ' ')
                if "Server disconnected" in clean_err or "Connection reset" in clean_err:
                    clean_err = "Mất kết nối tạm thời với server Muse"

                # Lỗi xác thực nghiêm trọng: dừng ngay
                if "401" in clean_err or "Unauthorized" in clean_err or "Forbidden" in clean_err:
                    result["status"] = "error"
                    result["error"] = "Lỗi xác thực API Key với Muse2API"
                    if progress_cb:
                        progress_cb(idx, "❌ Lỗi xác thực API Key")
                    break

                # Nhận diện nếu Muse AI từ chối nội dung
                is_prompt_refusal = any(k in clean_err for k in [
                    "模型未生成媒体", "仅返回文本", "没法直接生成", "做不了", 
                    "抱歉", "换个方向", "不带血腥", "去掉", "换个思路", "无法直接生成", "policy"
                ])

                # Nhận diện lỗi tải trang hoặc mạng bận tạm thời của muse.ai
                is_transient_error = any(k in clean_err for k in [
                    "页面加载超时", "timeout", "timed out", "502", "503", "504", 
                    "Mất kết nối", "Connection reset", "aborted", "10053"
                ])

                # Nếu bị lỗi ở lần 1, thử lại ĐÚNG 1 LẦN sau khoảng nghỉ
                if attempt == 1:
                    if is_prompt_refusal:
                        if progress_cb:
                            progress_cb(idx, "🪄 Muse từ chối nội dung. Đang tự động làm sạch prompt...")
                        healed_prompt = sanitize_prompt_heuristics(prompt, clean_err, aggressive=True)
                        if healed_prompt != prompt:
                            prompt = healed_prompt
                            scene["prompt"] = healed_prompt
                            result["revised_prompt"] = healed_prompt
                            if progress_cb:
                                progress_cb(idx, "✨ Thử lại lần 2 với prompt an toàn...")
                            continue
                    elif is_transient_error:
                        if progress_cb:
                            progress_cb(idx, "⏳ Muse2API đang bận tải trang, tự động thử lại lần 2...")
                        await asyncio.sleep(8)
                        continue

                # Đã thử hết số lần hoặc lỗi không thể khắc phục -> DỪNG NGAY
                result["status"] = "error"
                result["error"] = clean_err
                result["prompt"] = prompt
                if is_prompt_refusal:
                    result["suggested_prompt"] = sanitize_prompt_heuristics(prompt, clean_err, aggressive=True)
                if progress_cb:
                    err_extra = {
                        "status": "error",
                        "error": clean_err,
                        "prompt": prompt,
                        "suggested_prompt": result.get("suggested_prompt", ""),
                        "is_refusal": is_prompt_refusal,
                    }
                    try:
                        progress_cb(idx, f"❌ Thất bại: {clean_err[:60]}", err_extra)
                    except TypeError:
                        progress_cb(idx, f"❌ Thất bại: {clean_err[:60]}")
                break

        return result


def summarize_batch_timing(results: List[Dict[str, Any]], wall_s: float) -> Dict[str, float]:
    """Gộp timing từng scene thành timing của cả batch.
    - render_s: wall time toàn bộ run_batch (submit + poll + tải + đồng bộ).
    - poll_s / tts_s: TỔNG thời gian chờ API / sinh TTS của các scene.
      Hai số này chồng lấn nhau (TTS chạy song song với poll) và ở chế độ
      song song còn chồng lấn giữa các scene — nên poll_s/tts_s có thể lớn
      hơn render_s; chúng dùng để soi bottleneck, KHÔNG cộng vào total.
    - compose_s / total_s: do film_pipeline điền sau (ghép + tổng pipeline).
    Scene nào thiếu timing (lỗi setup, job cũ) thì tính là 0.
    """
    def _t(r: Dict[str, Any], key: str) -> float:
        try:
            return float((r.get("timing") or {}).get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "render_s": round(wall_s, 3),
        "poll_s": round(sum(_t(r, "poll_s") for r in results), 3),
        "tts_s": round(sum(_t(r, "tts_s") for r in results), 3),
        "compose_s": 0.0,
        "total_s": 0.0,
    }


def _cancelled_result(scene: Any) -> Dict[str, Any]:
    """Kết quả giả cho 1 cảnh chưa kịp render vì job bị hủy (audit #8)."""
    idx = scene.get("index") if isinstance(scene, dict) else None
    return {
        "index": idx,
        "status": "cancelled",
        "error": "Job đã bị hủy bởi người dùng.",
        "video_path": None,
        "audio_path": None,
        "voiceover": scene.get("voiceover", "") if isinstance(scene, dict) else "",
        "audio_duration": int(scene.get("duration", 5)) if isinstance(scene, dict) else 5,
    }


async def run_batch(
    scenes: List[Dict[str, Any]],
    base_url: str,
    api_key: str,
    output_dir: str,
    voice: str = "vi-VN-HoaiMyNeural",
    max_concurrent: int = 6,
    progress_cb: Optional[Callable] = None,
    chain_mode: str = "none",
    chain_fallback_mode: str = "independent",
    style_locks: Optional[Dict[str, str]] = None,
    start_scene_idx: int = 1,
    voice_mode: str = "tts",
    timing: Optional[Dict[str, float]] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> List[Dict[str, Any]]:
    """
    Chạy hàng loạt tất cả các cảnh với hỗ trợ kỹ thuật First/Last Frame Chaining & Fallback:
    - chain_mode == 'sequential': Chạy theo chuỗi nối tiếp.
    - chain_mode == 'none': Chạy song song (nếu cảnh nào có chain_from_previous=True thì đợi cảnh trước).
    - chain_fallback_mode: 'independent' (render độc lập), 'fallback_image' (dùng keyframe), 'stop' (tạm dừng).
    - start_scene_idx: Cho phép chạy lại từ cảnh N mà không làm mất cảnh 1..N-1 đã hoàn thành.
    - timing: dict tùy chọn — run_batch điền timing từng giai đoạn vào đó
      (render_s/poll_s/tts_s; compose_s/total_s do film_pipeline điền sau).
    - cancel_event: asyncio.Event được set khi người dùng hủy job (audit #8).
      run_batch kiểm tra trước mỗi cảnh + trong vòng poll từng cảnh
      (qua process_scene/poll_video_task); cảnh chưa render trả về
      status "cancelled" thay vì chạy tiếp. GIỚI HẠN: ffmpeg đồng bộ chạy
      trong asyncio.to_thread (ghép clip/last frame) không bị giết khi
      cancel — cảnh đang chạy sẽ xong bước đó rồi mới dừng hẳn.
    """
    batch_t0 = time.monotonic()

    def _finish(res: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if timing is not None:
            timing.update(summarize_batch_timing(res, time.monotonic() - batch_t0))
        return res

    os.makedirs(os.path.join(output_dir, "audio"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "scenes"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "final"), exist_ok=True)

    # Audit #8: job đã bị hủy trước khi batch bắt đầu -> không gọi API nữa.
    if cancel_event is not None and cancel_event.is_set():
        return _finish([_cancelled_result(s) for s in scenes])

    sem = asyncio.Semaphore(max_concurrent)
    sorted_scenes = sorted(
        scenes, key=lambda x: x.get("index", 0) if isinstance(x, dict) else 0
    )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Pre-check health của base_url trước khi bắn batch (§1.11):
        # healthz trả non-200 thì DỪNG batch với lỗi rõ ràng thay vì chạy tiếp.
        def _health_error_results(detail: str) -> List[Dict[str, Any]]:
            return [
                {
                    "index": s["index"],
                    "status": "error",
                    "error": detail,
                    "video_path": None,
                    "audio_path": None,
                    "voiceover": s.get("voiceover", ""),
                    "audio_duration": int(s.get("duration", 5)),
                }
                for s in scenes
            ]

        health_ok = False
        health_err = f"Không thể kết nối tới Muse2API tại {base_url}. Vui lòng kiểm tra server."
        for health_path in ("/healthz", "/admin/status"):
            try:
                r = await client.get(f"{base_url}{health_path}", timeout=5.0)
            except Exception as exc:
                health_err = (
                    f"Không thể kết nối tới Muse2API tại {base_url} ({exc}). "
                    "Vui lòng kiểm tra server."
                )
                continue
            if r.status_code == 200:
                health_ok = True
                break
            health_err = (
                f"Muse2API health-check trả về HTTP {r.status_code} ({health_path}) "
                "— dừng batch, không render tiếp."
            )
        if not health_ok:
            return _finish(_health_error_results(health_err))

        # ── Kỹ thuật Chaining: Tuần tự toàn bộ phim (Seamless Sequential Chaining) ──
        if chain_mode == "sequential":
            results = []
            prev_last_frame = None
            for si, s in enumerate(sorted_scenes):
                idx = s["index"]
                # Audit #8: dừng trước mỗi cảnh khi job đã bị hủy; các cảnh
                # còn lại trả về status "cancelled" thay vì render tiếp.
                if cancel_event is not None and cancel_event.is_set():
                    results.extend(_cancelled_result(x) for x in sorted_scenes[si:])
                    break
                if idx < start_scene_idx and s.get("status") == "done" and s.get("video_path"):
                    results.append(s)
                    if s.get("last_frame_path"):
                        prev_last_frame = s["last_frame_path"]
                    continue

                # Neo lại nhân vật định kỳ (Đồng nhất tối đa): cứ mỗi
                # CONSISTENCY_ANCHOR_EVERY scene thì ưu tiên ảnh nhân vật /
                # keyframe của chính scene đó thay cho prev_last_frame để
                # reset drift tích lũy. Không có ảnh neo thì giữ nguyên
                # Last Frame (không crash, không đổi hành vi).
                anchor_ref = None
                if idx > 1 and idx % CONSISTENCY_ANCHOR_EVERY == 0:
                    anchor_ref = consistency_anchor_reference(s)
                if anchor_ref:
                    s["reference_image"] = anchor_ref
                    if progress_cb:
                        progress_cb(idx, f"🎯 Neo lại nhân vật Cảnh {idx} "
                                         f"(anchor mỗi {CONSISTENCY_ANCHOR_EVERY} cảnh) "
                                         f"➔ Dùng ảnh nhân vật/keyframe thay Last Frame...")
                elif prev_last_frame:
                    s["reference_image"] = prev_last_frame
                    if progress_cb:
                        progress_cb(idx, f"🔗 Khóa Last Frame Cảnh {idx-1} ➔ Đang đặt làm First Frame...")
                elif idx > 1:
                    if chain_fallback_mode == "fallback_image" and (s.get("keyframe_path") or s.get("character_reference")):
                        s["reference_image"] = s.get("keyframe_path") or s.get("character_reference")
                        if progress_cb:
                            progress_cb(idx, f"🖼️ Cảnh {idx-1} không có frame ➔ Sử dụng ảnh Keyframe dự phòng")
                    else:
                        s["reference_image"] = None

                try:
                    res = await process_scene(
                        client, s, sem, base_url, api_key,
                        output_dir, voice, progress_cb, style_locks=style_locks,
                        voice_mode=voice_mode, cancel_event=cancel_event,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as seq_err:
                    # Sequential mode cũng không sập batch vì 1 scene lỗi (§1.9).
                    res = {
                        "index": s.get("index"),
                        "status": "error",
                        "error": f"Lỗi scene {s.get('index')}: {seq_err}",
                        "video_path": None,
                        "audio_path": None,
                        "voiceover": s.get("voiceover", ""),
                    }
                results.append(res)
                if res.get("status") == "done" and res.get("last_frame_path"):
                    prev_last_frame = res["last_frame_path"]
                else:
                    prev_last_frame = None
            results.sort(key=lambda x: x.get("index") if isinstance(x.get("index"), int) else -1)
            return _finish(results)

        # ── Kỹ thuật Chaining theo từng phân cảnh (Selective Chaining with Async Events) ──
        done_events: Dict[int, asyncio.Event] = {s["index"]: asyncio.Event() for s in sorted_scenes}
        results_map: Dict[int, Dict[str, Any]] = {}

        # Khởi tạo trạng thái cho các cảnh trước start_scene_idx đã xong
        for s in sorted_scenes:
            idx = s["index"]
            if idx < start_scene_idx and s.get("status") == "done" and s.get("video_path"):
                results_map[idx] = s
                done_events[idx].set()

        async def worker_with_chain(s: Dict[str, Any], order_idx: int):
            idx = s["index"]
            if idx < start_scene_idx and s.get("status") == "done" and s.get("video_path"):
                return s

            # Giãn cách nhẹ (staggered launch) 1.5s giữa các cảnh để tránh sốc tải đồng thời lên Chrome CDP / muse.ai
            if not s.get("chain_from_previous") and order_idx > 0:
                stagger = min(order_idx * 1.5, 6.0)
                await asyncio.sleep(stagger)

            # Nếu cảnh này yêu cầu nối tiếp cảnh trước đó
            if s.get("chain_from_previous") and (idx - 1) in done_events:
                if progress_cb:
                    progress_cb(idx, f"⏳ Đang đợi Cảnh {idx-1} hoàn thành để lấy khung hình cuối...")
                try:
                    # §1.9: chờ có timeout — event không được set thì lỗi scene
                    # thay vì treo chain vĩnh viễn.
                    await asyncio.wait_for(done_events[idx - 1].wait(), timeout=CHAIN_WAIT_TIMEOUT)
                except asyncio.TimeoutError:
                    err_msg = (f"Cảnh {idx-1} không hoàn thành sau {int(CHAIN_WAIT_TIMEOUT / 60)} phút "
                               f"chờ chaining — Cảnh {idx} lỗi timeout.")
                    if progress_cb:
                        progress_cb(idx, f"⛔ {err_msg}")
                    res = {
                        "index": idx,
                        "status": "error",
                        "error": err_msg,
                        "prompt": s.get("prompt"),
                        "video_path": None,
                        "audio_path": None,
                        "voiceover": s.get("voiceover", ""),
                    }
                    results_map[idx] = res
                    done_events[idx].set()
                    return res
                prev_res = results_map.get(idx - 1)
                if prev_res and prev_res.get("last_frame_path"):
                    s["reference_image"] = prev_res["last_frame_path"]
                    if progress_cb:
                        progress_cb(idx, f"🔗 Đã nhận Last Frame Cảnh {idx-1} ➔ Bắt đầu render First Frame")
                else:
                    # Xử lý khi cảnh trước bị lỗi
                    if chain_fallback_mode == "stop":
                        err_msg = f"Cảnh {idx-1} bị lỗi, tạm dừng Cảnh {idx} theo cấu hình chờ."
                        if progress_cb:
                            progress_cb(idx, f"⛔ {err_msg}")
                        res = {
                            "index": idx,
                            "status": "error",
                            "error": err_msg,
                            "prompt": s.get("prompt"),
                            "video_path": None,
                            "audio_path": None,
                            "voiceover": s.get("voiceover", ""),
                        }
                        results_map[idx] = res
                        done_events[idx].set()
                        return res
                    elif chain_fallback_mode == "fallback_image" and (s.get("keyframe_path") or s.get("character_reference")):
                        fallback_ref = s.get("keyframe_path") or s.get("character_reference")
                        s["reference_image"] = fallback_ref
                        if progress_cb:
                            progress_cb(idx, f"🖼️ Cảnh {idx-1} lỗi ➔ Dùng ảnh Keyframe/Character dự phòng cho Cảnh {idx}")
                    else:
                        s["reference_image"] = None
                        if progress_cb:
                            progress_cb(idx, f"⚡ Cảnh {idx-1} lỗi ➔ Tự động chuyển Cảnh {idx} sang render Text-to-Video độc lập")

            try:
                # Audit #8: không bắt đầu render cảnh mới khi job đã bị hủy
                # (kiểm tra sau cả lúc chờ chain + stagger).
                if cancel_event is not None and cancel_event.is_set():
                    res = _cancelled_result(s)
                    results_map[idx] = res
                    done_events[idx].set()
                    return res
                res = await process_scene(
                    client, s, sem, base_url, api_key,
                    output_dir, voice, progress_cb, style_locks=style_locks,
                    voice_mode=voice_mode, cancel_event=cancel_event,
                )
            except asyncio.CancelledError:
                raise
            except Exception as worker_err:
                # 1 worker lỗi không được sập cả batch (§1.9): ghi lỗi scene.
                res = {
                    "index": idx,
                    "status": "error",
                    "error": f"Lỗi worker cảnh {idx}: {worker_err}",
                    "prompt": s.get("prompt"),
                    "video_path": None,
                    "audio_path": None,
                    "voiceover": s.get("voiceover", ""),
                }
            finally:
                # Luôn set event để cảnh chain sau không treo (§1.9).
                done_events[idx].set()
            results_map[idx] = res
            return res

        tasks = [worker_with_chain(s, oi) for oi, s in enumerate(sorted_scenes)]
        # §1.9: return_exceptions=True — 1 scene lỗi không sập cả batch.
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        results = []
        for scene_obj, item in zip(sorted_scenes, gathered):
            if isinstance(item, dict):
                results.append(item)
            elif isinstance(item, asyncio.CancelledError):
                # Audit #8: worker bị hủy giữa chừng -> cảnh "cancelled"
                # (không phải "error") để film_pipeline nhận diện và dừng.
                results.append(_cancelled_result(scene_obj))
            else:  # BaseException lọt qua (vd CancelledError được gather bọc)
                results.append({
                    "index": scene_obj["index"],
                    "status": "error",
                    "error": f"Lỗi worker cảnh {scene_obj['index']}: {item!r}",
                    "video_path": None,
                    "audio_path": None,
                    "voiceover": scene_obj.get("voiceover", ""),
                })

    # Sắp xếp đúng thứ tự
    results.sort(key=lambda x: x.get("index") if isinstance(x.get("index"), int) else -1)
    return _finish(results)
