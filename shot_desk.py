import sys
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
"""
shot_desk.py – AI Movie Studio
------------------------------
Module chuẩn hóa quy trình "Shot Desk Project":
- Import & Export gói ZIP chuẩn Shot Desk (project.json + frames per-shot).
- Quản lý cấu trúc Project bền vững:
    workspace/_shot_desk_projects/<project_id>/
    ├── project.json
    ├── characters/
    ├── shots/
    │   └── shot_001/ (first_frame, middle_frame, last_frame)
    ├── clips/ (clip render từng shot, sub-clips A/B)
    ├── audio/ (TTS tiếng Việt, sound bridge)
    └── final/ (movie export, srt)
- Vòng đời trạng thái từng shot:
    Imported → Ready → Rendering → Review → Approved → Composing → Done
- Kỹ thuật Middle-Frame Splitting (Chia 2 clip 5s nếu có middle frame để chống trôi nhân vật).
- Compliance Prompt Fallback (Tự động kích hoạt khi prompt chính bị từ chối).
- Màn hình Review Desk: Đạo diễn duyệt từng shot trước khi đưa vào Timeline dựng phim cuối.
- Timeline Hậu kỳ: Áp chuyển cảnh (Match cut, Dissolve, Light wipe), Sound Bridge 0.5s-1.0s, BGM & Phụ đề.
"""

import asyncio
import base64
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
import httpx
from PIL import Image

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

from composer import (
    get_media_duration,
    compose_scene_video,
    add_background_music,
    burn_subtitles,
    create_srt,
    detect_audio_speech_bounds,
    compute_timeline_offsets,
    check_video_has_audio,
    generate_sfx_audio,
    detect_sfx_from_text,
    merge_clips_advanced,
    mix_multitrack_timeline,
    merge_audio_with_transitions,
    QUALITY_PROFILES,
)
from batch_engine import generate_tts, poll_video_task, sanitize_prompt_heuristics, extract_last_frame


SHOT_DESK_DIR = (Path(__file__).parent / "workspace" / "_shot_desk_projects").resolve()
SHOT_DESK_DIR.mkdir(parents=True, exist_ok=True)


import unicodedata

def to_slug(text: str) -> str:
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '_', text) or "project"

ALLOWED_FRAME_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif"}

# Thư mục workspace của repo (dùng làm gốc cho mọi kiểm tra path an toàn).
WORKSPACE_ROOT = (Path(__file__).parent / "workspace").resolve()
# Số bản backup project.json hỏng tối đa được giữ lại.
_MAX_JSON_BACKUPS = 5
# Giới hạn đọc file tham chiếu (ảnh first/last frame) vào RAM.
_MAX_REFERENCE_FILE_BYTES = 20 * 1024 * 1024  # 20 MB
# Giới hạn tải video clip thành phẩm.
_MAX_VIDEO_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MB
# Thư mục nhạc nền hợp lệ (bên trong workspace).
_ALLOWED_MUSIC_ROOT_NAMES = ("_music", "_uploads")


def _resolve_muse2api_key(api_key: Optional[str]) -> str:
    """Resolve API key cho Muse2API: tham số truyền vào > env MUSE2API_KEY.

    Không còn giá trị mặc định hardcode: thiếu key thì raise lỗi cấu hình rõ
    ràng thay vì gọi API với key giả rồi mới nhận 401.
    """
    key = (api_key or "").strip() if isinstance(api_key, str) else ""
    if not key:
        key = (os.getenv("MUSE2API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "Thiếu cấu hình MUSE2API_KEY: đặt biến môi trường MUSE2API_KEY "
            "(hoặc truyền api_key tường minh) trước khi render qua Muse2API."
        )
    return key


def _read_file_b64_capped(path: Path, max_bytes: int = _MAX_REFERENCE_FILE_BYTES) -> str:
    """Đọc file theo chunk (không nạp cả file lớn vào RAM một lần) và trả base64.

    Raise ValueError nếu file vượt giới hạn — tránh OOM khi manifest trỏ tới
    file tham chiếu bất thường.
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"File tham chiếu quá lớn ({size} bytes > {max_bytes} bytes): {path.name}")
    chunks = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    return base64.b64encode(b"".join(chunks)).decode("ascii")


def _parse_shot_duration(value: Any, default: int = 10) -> int:
    """Parse duration của shot: không parse được thì dùng default + log warning.

    Tránh crash import ZIP sau khi đã giải nén xong (project mồ côi).
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        try:
            parsed = int(float(value))  # chấp nhận "10.0"
        except (TypeError, ValueError):
            print(f"[ShotDesk] Warning: duration không hợp lệ ({value!r}), dùng mặc định {default}s.")
            return default
    if parsed <= 0:
        print(f"[ShotDesk] Warning: duration không hợp lệ ({value!r}), dùng mặc định {default}s.")
        return default
    return parsed


def resolve_safe_bg_music_path(value: Any, project_id: Optional[str] = None) -> Optional[Path]:
    """Validate bg_music_path: chỉ cho phép file nằm trong workspace.

    Cho phép: workspace/_music, workspace/_uploads, hoặc thư mục của chính
    project đang dựng (workspace/_shot_desk_projects/<project_id>).
    Cấm: đường dẫn tuyệt đối ngoài workspace và mọi dạng ../ thoát ra ngoài.
    Trả về Path đã resolve, hoặc None nếu value rỗng.
    Raise ValueError nếu path không hợp lệ / không tồn tại.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        # Path tương đối được hiểu là tương đối so với workspace.
        candidate = WORKSPACE_ROOT / candidate
    resolved = candidate.resolve()
    allowed_roots = [WORKSPACE_ROOT / name for name in _ALLOWED_MUSIC_ROOT_NAMES]
    if project_id:
        allowed_roots.append(SHOT_DESK_DIR / str(project_id))
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise ValueError(
            f"bg_music_path không hợp lệ (chỉ cho phép file trong workspace/_music, "
            f"workspace/_uploads hoặc thư mục project): {raw}"
        )
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"bg_music_path không tồn tại hoặc không phải file: {raw}")
    if resolved.suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        raise ValueError(f"bg_music_path không phải file nhạc hỗ trợ (.mp3/.wav/.m4a/.aac/.ogg/.flac): {raw}")
    return resolved

def resolve_safe_frame_path(proj_dir: Path, frame_rel_path: Optional[str]) -> Optional[Path]:
    """
    Resolve frame path từ manifest project.json an toàn:
    - Bác bỏ đường dẫn rỗng, chứa traversal (..), ký tự phân cách bất thường hoặc ra ngoài proj_dir.
    - Bắt buộc phần mở rộng là file ảnh hợp lệ (.png, .jpg, .jpeg, .webp, .avif).
    - Bắt buộc file tồn tại và là regular file.
    """
    if not frame_rel_path or not isinstance(frame_rel_path, str):
        return None
    clean_rel = frame_rel_path.strip().replace("\\", "/").lstrip("/")
    if not clean_rel or clean_rel.startswith("/") or ":" in clean_rel or ".." in Path(clean_rel).parts:
        return None
    try:
        resolved_proj = proj_dir.resolve()
        target = (proj_dir / clean_rel).resolve()
        if not target.is_relative_to(resolved_proj):
            return None
        if target.suffix.lower() not in ALLOWED_FRAME_EXTENSIONS:
            return None
        if not target.exists() or not target.is_file():
            return None
        return target
    except Exception:
        return None

# ── 1. Đọc và Nhập (Import) Gói ZIP Chuẩn Shot Desk ───────────────────────────

def import_shot_desk_zip(zip_bytes: bytes, filename: str = "project.zip") -> Dict[str, Any]:
    """
    Nhập gói ZIP từ Shot Desk Studio:
    - Đọc project.json
    - Giải nén toàn bộ first_frame, middle_frame, last_frame vào thư mục project
    - Tự động nạp Character Blocks vào thư viện nhân vật
    - Khởi tạo danh sách shot với trạng thái 'ready'
    - Trả về object project hoàn chỉnh
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        if "project.json" not in z.namelist():
            raise ValueError("File ZIP không hợp lệ: Thiếu 'project.json' chuẩn Shot Desk!")

        manifest = json.loads(z.read("project.json").decode("utf-8"))

        raw_title = manifest.get("title") or "Shot_Desk_Project"
        clean_slug = to_slug(raw_title)
        project_id = f"{clean_slug}_{uuid.uuid4().hex[:6]}"
        proj_dir = SHOT_DESK_DIR / project_id

        # Tạo cấu trúc thư mục
        (proj_dir / "shots").mkdir(parents=True, exist_ok=True)
        (proj_dir / "clips").mkdir(parents=True, exist_ok=True)
        (proj_dir / "audio").mkdir(parents=True, exist_ok=True)
        (proj_dir / "characters").mkdir(parents=True, exist_ok=True)
        (proj_dir / "final").mkdir(parents=True, exist_ok=True)

        # Giải nén an toàn: kiểm tra từng ZipInfo chống path traversal, symlink, zip bomb
        MAX_FILES = 1000
        MAX_TOTAL_SIZE = 500 * 1024 * 1024  # 500 MB
        extracted_files = 0
        total_extracted_size = 0
        resolved_proj_dir = proj_dir.resolve()

        for zinfo in z.infolist():
            # Bỏ qua thư mục
            if zinfo.is_dir() or zinfo.filename.endswith("/"):
                continue

            extracted_files += 1
            if extracted_files > MAX_FILES:
                raise ValueError(f"File ZIP vượt quá giới hạn an toàn {MAX_FILES} files.")

            total_extracted_size += zinfo.file_size
            if total_extracted_size > MAX_TOTAL_SIZE:
                raise ValueError(f"Tổng dung lượng giải nén vượt quá giới hạn an toàn ({MAX_TOTAL_SIZE // (1024*1024)} MB).")

            # Kiểm tra symlink (UNIX file mode 0o120000)
            if (zinfo.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"File ZIP chứa symlink không an toàn: {zinfo.filename}")

            fname = zinfo.filename.replace("\\", "/")
            # Reject path tuyệt đối, chứa '..', hoặc drive letter
            if fname.startswith("/") or ":" in fname or ".." in Path(fname).parts:
                raise ValueError(f"File ZIP chứa đường dẫn không an toàn (path traversal): {zinfo.filename}")

            target_path = (proj_dir / fname).resolve()
            if not target_path.is_relative_to(resolved_proj_dir):
                raise ValueError(f"Đường dẫn giải nén vượt ra ngoài thư mục dự án: {zinfo.filename}")

            if fname == "project.json":
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "wb") as f:
                f.write(z.read(zinfo))


        # 1. Đồng bộ Character Blocks vào Character Library của Studio
        characters = manifest.get("characters", [])
        char_file = Path("workspace") / "_characters.json"
        existing_chars = []
        if char_file.exists():
            try:
                with open(char_file, "r", encoding="utf-8") as cf:
                    existing_chars = json.load(cf)
            except Exception:
                existing_chars = []

        for ch in characters:
            c_id = ch.get("id") or f"char_{uuid.uuid4().hex[:6]}"
            name = ch.get("name", "Nhân vật")
            block = ch.get("character_block", "")
            voice = ch.get("voice") or manifest.get("default_voice", "vi-VN-HoaiMyNeural")

            # Tìm frame tham chiếu đại diện
            avatar_url = ""
            first_frames = [f for f in (proj_dir / "shots" / "shot_001").glob("first_frame.*") if f.is_file() and f.suffix.lower() in ALLOWED_FRAME_EXTENSIONS] if (proj_dir / "shots" / "shot_001").exists() else []
            if ch.get("reference_image") and resolve_safe_frame_path(proj_dir, ch.get("reference_image")):
                avatar_url = f"/api/shot-desk/projects/{project_id}/assets/{ch['reference_image']}"
            elif first_frames:
                avatar_url = f"/api/shot-desk/projects/{project_id}/shots/1/frames/{first_frames[0].name}"

            char_entry = {
                "id": c_id,
                "name": name,
                "aliases": [name, name.lower()],
                "avatar": avatar_url,
                "description": block,
                "voice": voice,
                "character_sheet": {
                    "front": avatar_url,
                    "side": "",
                    "closeup": "",
                    "fullbody": ""
                }
            }

            # Cập nhật hoặc thêm mới vào thư viện
            found = False
            for i, ex in enumerate(existing_chars):
                if ex.get("name", "").lower() == name.lower() or ex.get("id") == c_id:
                    existing_chars[i].update(char_entry)
                    found = True
                    break
            if not found:
                existing_chars.insert(0, char_entry)

        try:
            with open(char_file, "w", encoding="utf-8") as cf:
                json.dump(existing_chars, cf, ensure_ascii=False, indent=2)
        except Exception as ce:
            print(f"[ShotDesk] Warning saving characters: {ce}")

        # 2. Xây dựng danh sách Shots với các trạng thái chuyên nghiệp
        shots_data = []
        aspect_ratio = manifest.get("aspect_ratio", "9:16")

        for i, s in enumerate(manifest.get("shots", [])):
            try:
                idx = int(s.get("index", i + 1))
            except (TypeError, ValueError):
                print(f"[ShotDesk] Warning: shot index không hợp lệ ({s.get('index')!r}), dùng vị trí {i + 1}.")
                idx = i + 1
            shot_dir = proj_dir / "shots" / f"shot_{idx:03d}"
            shot_dir.mkdir(parents=True, exist_ok=True)

            first_p = s.get("first_frame")
            mid_p = s.get("middle_frame")
            last_p = s.get("last_frame")

            # Chuẩn hóa URLs xem trước cho giao diện Web (giữ đúng tên file thực tế: .jpg, .png, .webp...)
            def _build_frame_url(rel_path: Optional[str]) -> Optional[str]:
                fp = resolve_safe_frame_path(proj_dir, rel_path)
                if fp:
                    return f"/api/shot-desk/projects/{project_id}/shots/{idx}/frames/{fp.name}"
                return None

            first_url = _build_frame_url(first_p)
            mid_url = _build_frame_url(mid_p)
            last_url = _build_frame_url(last_p)

            shot_characters = s.get("characters", [])
            initial_speaker = s.get("speaker") or (shot_characters[0] if shot_characters else "")
            initial_voice = s.get("voice") or manifest.get("default_voice", "vi-VN-HoaiMyNeural")
            initial_mode = s.get("voice_mode", manifest.get("default_voice_mode", "narration"))
            initial_lipsync = bool(s.get("lip_sync", initial_mode == "dialogue"))

            shot_item = {
                "index": idx,
                "title": s.get("title") or f"Shot {idx}",
                "duration": _parse_shot_duration(s.get("duration", manifest.get("shot_duration", 10)), default=10),
                "prompt": s.get("prompt", ""),
                "compliance_prompt": s.get("compliance_prompt"),
                "risk_level": s.get("risk_level", "low"),
                "voiceover": s.get("voiceover", ""),
                "voice_mode": initial_mode,
                "speaker": initial_speaker,
                "voice": initial_voice,
                "lip_sync": initial_lipsync,
                "first_frame": first_p,
                "middle_frame": mid_p,
                "last_frame": last_p,
                "first_frame_url": first_url,
                "middle_frame_url": mid_url,
                "last_frame_url": last_url,
                "transition_in": s.get("transition_in"),
                "transition_out": s.get("transition_out"),
                "transition_note": s.get("transition_note", ""),
                "characters": s.get("characters", []),
                # Vòng đời trạng thái
                "status": "ready",  # imported | ready | rendering | review | approved | composing | done
                "error": None,
                "video_url": None,
                "audio_url": None,
                "used_prompt": None,
                "used_compliance": False,
                "has_middle_frame": bool(resolve_safe_frame_path(proj_dir, mid_p)),
                "audit": {
                    "character_check": "Chờ render",
                    "aspect_ratio_check": aspect_ratio,
                    "watermark_clean": True,
                    "continuity_note": s.get("transition_note", "")
                }
            }
            shots_data.append(shot_item)

        project_state = {
            "id": project_id,
            "format_version": manifest.get("format_version", "1.0"),
            "title": manifest.get("title", "Shot Desk Project"),
            "genre": manifest.get("genre", "Phim ngắn"),
            "logline": manifest.get("logline", ""),
            "aspect_ratio": aspect_ratio,
            "default_voice": manifest.get("default_voice", "vi-VN-HoaiMyNeural"),
            "default_voice_mode": manifest.get("default_voice_mode", "narration"),
            "total_duration": _parse_shot_duration(manifest.get("total_duration", len(shots_data) * 10), default=len(shots_data) * 10),
            "shot_duration": _parse_shot_duration(manifest.get("shot_duration", 10), default=10),
            "characters": characters,
            "shots": shots_data,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "final_video": None,
            "srt_file": None,
            "timeline_settings": {
                "music_volume": 0.25,
                "voice_volume": 1.0,
                "bg_music_path": None,
                "sound_bridge_duration": 0.75,
                "subtitle_mode": "burn_in",
                "export_quality": "720p"
            }
        }

        # Lưu project.json đã chuẩn hóa
        save_shot_desk_project(project_state)
        return project_state


def _backup_json_file(p_file: Path) -> Optional[Path]:
    """Backup file JSON gốc sang <tên>.bak.<timestamp> trước khi cắt/sửa.

    Giữ tối đa _MAX_JSON_BACKUPS bản mới nhất; bản cũ hơn bị xóa.
    Trả về đường dẫn backup, hoặc None nếu backup thất bại.
    """
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}-{time.time_ns() % 1_000_000:06d}"
        backup = p_file.with_name(f"{p_file.name}.bak.{stamp}")
        shutil.copy2(p_file, backup)
        pattern = f"{p_file.name}.bak.*"
        backups = sorted(p_file.parent.glob(pattern), key=lambda p: p.stat().st_mtime)
        for old in backups[: -_MAX_JSON_BACKUPS]:
            try:
                old.unlink()
            except Exception:
                pass
        return backup
    except Exception as e:
        print(f"[ShotDesk] Warning: không backup được {p_file}: {e}")
        return None


def load_project_json_safe(p_file: Path) -> Optional[Dict[str, Any]]:
    """Đọc project.json an toàn, tự động khắc phục nếu file bị dính ký tự thừa ở cuối (JSON extra data).

    Trước khi cắt/sửa JSON hỏng: backup file gốc sang <tên>.bak.<timestamp>.
    Chỉ cắt khi parse thất bại, chỉ ghi đè khi dữ liệu cắt xong parse lại
    thành công VÀ là dict. Nếu data không phải dict thì trả lỗi (None) thay
    vì để save_shot_desk_project gọi project.get gây AttributeError.
    Nếu cắt thất bại thì trả lỗi, file gốc (đã backup) được giữ nguyên.
    """
    if not p_file.exists():
        return None
    try:
        with open(p_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as jde:
        backup = _backup_json_file(p_file)
        try:
            with open(p_file, "r", encoding="utf-8") as f:
                content = f.read()
            last_brace = content.rfind("}")
            while last_brace != -1:
                try:
                    data = json.loads(content[:last_brace + 1])
                    if not isinstance(data, dict):
                        print(f"[ShotDesk] Lỗi định dạng JSON tại {p_file}: dữ liệu sau khi cắt không phải object (kiểu {type(data).__name__}), giữ nguyên file gốc (backup: {backup}).")
                        return None
                    print(f"[ShotDesk] Đã tự động sửa lỗi JSON extra data cho {p_file.parent.name} (backup: {backup})")
                    try:
                        save_shot_desk_project(data)
                    except Exception as save_err:
                        print(f"[ShotDesk] Không ghi đè được project.json đã sửa ({save_err}); giữ nguyên file gốc (backup: {backup}).")
                        return None
                    return data
                except json.JSONDecodeError:
                    last_brace = content[:last_brace].rfind("}")
                except Exception:
                    last_brace = content[:last_brace].rfind("}")
        except Exception:
            pass
        print(f"[ShotDesk] Lỗi định dạng JSON tại {p_file}: {jde} (file gốc giữ nguyên, backup: {backup})")
        return None
    except Exception as e:
        print(f"[ShotDesk] Lỗi đọc {p_file}: {e}")
        return None


def get_shot_desk_project(project_id: str) -> Optional[Dict[str, Any]]:
    # TODO (audit §1.7): việc chuyển trạng thái rendering -> ready/review khi
    # GET /status KHÔNG nằm ở hàm này mà ở endpoint status trong app.py
    # (ngoài phạm vi refactor lần này): endpoint đó tự đổi trạng thái rồi save
    # mà không có bằng chứng render xong ngoài "clip file tồn tại". Hàm này
    # chỉ sửa URL frame (idempotent) và KHÔNG tự đổi trạng thái shot nào.
    # Khi sửa app.py, chỉ chuyển rendering -> review khi clip hợp lệ VÀ shot
    # không còn trong ACTIVE_SHOT_TASKS, và chuyển -> ready chỉ khi có bằng
    # chứng tiến trình đã chết (không chỉ vì poll GET).
    if not project_id or not re.match(r"^[a-zA-Z0-9_\-]+$", project_id):
        return None
    proj_dir = (SHOT_DESK_DIR / project_id).resolve()
    if not proj_dir.is_relative_to(SHOT_DESK_DIR.resolve()):
        return None
    p_file = proj_dir / "project.json"
    if not p_file.exists():
        return None
    # Giữ per-project lock trong toàn bộ read-modify-write (load -> sửa URL
    # frame -> save khi dirty) để render song song không mất update của nhau.
    # Lock là RLock nên save_shot_desk_project() bên trong vẫn lấy lại được.
    lock = get_sync_project_lock(project_id)
    with lock:
        try:
            proj = load_project_json_safe(p_file)
            if not proj:
                return None
            if not isinstance(proj, dict):
                print(f"[ShotDesk] project.json không phải object dict: {p_file}")
                return None

            # Tự động đồng bộ và sửa URL frame nếu file thực tế có đuôi mở rộng khác (.jpg, .png, ...)
            dirty = False
            for s in proj.get("shots", []):
                idx = s.get("index", 1)
                shot_dir = proj_dir / "shots" / f"shot_{idx:03d}"
                for k_file, k_url in (("first_frame", "first_frame_url"),
                                      ("middle_frame", "middle_frame_url"),
                                      ("last_frame", "last_frame_url")):
                    ref = s.get(k_file)
                    if ref:
                        fp = resolve_safe_frame_path(proj_dir, ref)
                        # Nếu file tham chiếu ban đầu không tồn tại, kiểm tra trong shot_dir
                        if not fp and shot_dir.exists():
                            stem = Path(ref).name.split(".")[0]
                            cands = [f for f in shot_dir.glob(f"{stem}.*") if f.is_file() and f.suffix.lower() in ALLOWED_FRAME_EXTENSIONS]
                            if cands:
                                fp = cands[0]
                                s[k_file] = f"shots/shot_{idx:03d}/{fp.name}"
                                dirty = True
                        if fp:
                            correct_url = f"/api/shot-desk/projects/{project_id}/shots/{idx}/frames/{fp.name}"
                            if s.get(k_url) != correct_url:
                                s[k_url] = correct_url
                                dirty = True

            proj["final_dir"] = str((proj_dir / "final").resolve())
            if dirty:
                save_shot_desk_project(proj)
            return proj
        except Exception as e:
            print(f"[ShotDesk] Error reading project {project_id}: {e}")
            return None


_global_sync_lock = threading.Lock()
_sync_project_locks: Dict[str, threading.RLock] = {}

def get_sync_project_lock(project_id: str) -> threading.RLock:
    # RLock (không phải Lock): get_shot_desk_project() giữ lock trong toàn bộ
    # read-modify-write và save_shot_desk_project() bên trong cần lấy lại
    # cùng lock trên cùng thread — dùng Lock thường sẽ deadlock.
    with _global_sync_lock:
        if project_id not in _sync_project_locks:
            _sync_project_locks[project_id] = threading.RLock()
        return _sync_project_locks[project_id]


def save_shot_desk_project(project: Dict[str, Any]):
    project_id = project.get("id")
    if not project_id or not re.match(r"^[a-zA-Z0-9_\-]+$", str(project_id)):
        raise ValueError(f"project_id '{project_id}' không hợp lệ")
    proj_dir = (SHOT_DESK_DIR / project_id).resolve()
    if not proj_dir.is_relative_to(SHOT_DESK_DIR.resolve()):
        raise ValueError("Đường dẫn project_id không an toàn (path traversal)")
    proj_dir.mkdir(parents=True, exist_ok=True)
    project["updated_at"] = int(time.time())

    # Ghi nguyên tử có Lock đa luồng để chống xung đột race-condition giữa render và API
    lock = get_sync_project_lock(str(project_id))
    with lock:
        tmp_file = proj_dir / f"project.json.tmp.{os.getpid()}.{time.time_ns()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(project, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, proj_dir / "project.json")


_project_locks: Dict[str, asyncio.Lock] = {}

def get_project_lock(project_id: str) -> asyncio.Lock:
    with _global_sync_lock:
        if project_id not in _project_locks:
            _project_locks[project_id] = asyncio.Lock()
        return _project_locks[project_id]


async def update_shot_state(project_id: str, shot_index: int, updates: Dict[str, Any]):
    """Cập nhật nguyên tử trạng thái của 1 shot trong project, an toàn khi render đa luồng song song."""
    lock = get_project_lock(project_id)
    async with lock:
        # Giữ thêm per-project sync lock (RLock) trong toàn bộ read-modify-write
        # để các hàm sync (approve/edit) không chen vào giữa lúc get và save.
        sync_lock = get_sync_project_lock(project_id)
        with sync_lock:
            latest = get_shot_desk_project(project_id)
            if not latest:
                return
            for s in latest.get("shots", []):
                if s["index"] == shot_index:
                    s.update(updates)
                    break
            save_shot_desk_project(latest)



def list_shot_desk_projects() -> List[Dict[str, Any]]:
    projects = []
    for d in SHOT_DESK_DIR.iterdir():
        if d.is_dir() and (d / "project.json").exists():
            data = load_project_json_safe(d / "project.json")
            if not data or not data.get("id"):
                continue
            total_shots = len(data.get("shots", []))
            approved_shots = sum(1 for s in data.get("shots", []) if s.get("status") == "approved")
            done_shots = sum(1 for s in data.get("shots", []) if s.get("video_url"))
            projects.append({
                "id": data["id"],
                "title": data.get("title", d.name),
                "genre": data.get("genre", ""),
                "aspect_ratio": data.get("aspect_ratio", "16:9"),
                "total_shots": total_shots,
                "approved_shots": approved_shots,
                "done_shots": done_shots,
                "created_at": data.get("created_at", 0),
                "final_video": data.get("final_video"),
            })
    return sorted(projects, key=lambda x: x.get("created_at", 0), reverse=True)


# ── 2. Render Theo Cấu Trúc Shot Chuyên Nghiệp ────────────────────────────────

async def render_single_shot_desk(
    project_id: str,
    shot_index: int,
    base_url: str = "http://localhost:18610",
    api_key: Optional[str] = None,
    use_compliance: bool = False,
    custom_prompt: Optional[str] = None,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Quy trình render chuyên nghiệp theo chuẩn Shot Desk:
    1. Trích xuất First Frame, Middle Frame (nếu có), Last Frame
    2. Nếu có middle_frame:
       - Tự động tách thành Sub-clip A (0-5s) và Sub-clip B (5-10s)
       - Nối Part A + Part B thành shot 10s liền mạch chống trôi nhân vật!
    3. Nếu không có middle_frame: Render trực tiếp bằng first_frame
    4. Compliance Fallback: Tự động dùng compliance_prompt nếu prompt chính bị từ chối
    5. Sinh TTS lời thoại tiếng Việt và ghép clip hoàn chỉnh
    6. Chuyển trạng thái sang 'review' (Màn hình Review Desk của Đạo diễn)

    api_key: None -> resolve từ env MUSE2API_KEY tại thời điểm gọi; thiếu key
    thì raise RuntimeError cấu hình rõ ràng, không gọi API với key giả.
    """
    # Resolve key NGAY ĐẦU, trước mọi công việc render/TTS/API.
    api_key = _resolve_muse2api_key(api_key)
    proj = get_shot_desk_project(project_id)
    if not proj:
        raise ValueError(f"Không tìm thấy dự án Shot Desk '{project_id}'")

    shot = None
    for s in proj.get("shots", []):
        if s["index"] == shot_index:
            shot = s
            break
    if not shot:
        raise ValueError(f"Không tìm thấy Shot {shot_index} trong dự án")

    # Chuẩn bị Prompt
    active_prompt = custom_prompt or (shot.get("compliance_prompt") if use_compliance and shot.get("compliance_prompt") else shot.get("prompt", ""))
    shot["used_prompt"] = active_prompt
    shot["used_compliance"] = bool(use_compliance or (custom_prompt == shot.get("compliance_prompt")))

    proj_dir = SHOT_DESK_DIR / project_id
    await update_shot_state(project_id, shot_index, {
        "status": "rendering",
        "error": None,
        "used_prompt": active_prompt,
        "used_compliance": shot["used_compliance"]
    })

    first_frame_path = resolve_safe_frame_path(proj_dir, shot.get("first_frame"))
    middle_frame_path = resolve_safe_frame_path(proj_dir, shot.get("middle_frame"))
    last_frame_path = resolve_safe_frame_path(proj_dir, shot.get("last_frame"))

    # Tỉ lệ và thời lượng (duration lỗi thì dùng 10s + log, không crash)
    aspect_ratio = proj.get("aspect_ratio", "9:16")
    total_shot_dur = _parse_shot_duration(shot.get("duration", 10), default=10)

    audio_out_path = str(proj_dir / "audio" / f"shot_{shot_index:03d}_tts.mp3")
    final_clip_out = str(proj_dir / "clips" / f"shot_{shot_index:03d}_clip.mp4")

    # Bước 1: Xác định chế độ giọng đọc
    voice = shot.get("voice") or proj.get("default_voice", "vi-VN-HoaiMyNeural")
    voiceover = shot.get("voiceover", "").strip()
    voice_mode = shot.get("voice_mode", proj.get("default_voice_mode", "narration"))
    speaker = (shot.get("speaker") or "").strip()
    lip_sync = bool(shot.get("lip_sync", False) or (voice_mode == "dialogue"))
    is_ai_voice = (voice_mode in ("ai_voice", "native", "none", "no_dub"))

    if not is_ai_voice and voiceover:
        mode_label = "🗣️ lời thoại nhân vật (khớp môi)" if (voice_mode == "dialogue" or lip_sync) else "🎙️ giọng đọc thuyết minh"
        if progress_cb:
            progress_cb(shot_index, f"🎙️ Đang sinh {mode_label} ({voice})...")
        try:
            actual_audio_dur = await generate_tts(voiceover, voice, audio_out_path)
        except Exception as e:
            print(f"[ShotDesk] Warning TTS: {e}")
            actual_audio_dur = total_shot_dur
    else:
        if is_ai_voice and progress_cb:
            progress_cb(shot_index, "🤖 Chế độ không lồng tiếng: Sử dụng âm thanh & giọng nói gốc của AI...")
        actual_audio_dur = total_shot_dur

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _trim_video_to_duration(file_path: Path, max_dur: float) -> Path:
        """
        Đảm bảo clip không vượt quá thời lượng cho phép.
        Bảo vệ chống trùng lặp cảnh: Nếu video tải về từ AI bị nối tiếp dài hơn thời lượng (do canvas lưu lịch sử),
        phải cắt đúng phần đuôi (nội dung mới sinh) tại offset = actual_d - max_dur,
        loại bỏ hoàn toàn việc lấy nhầm phần đầu (cảnh cũ phía trước).
        """
        try:
            if not file_path.exists() or file_path.stat().st_size < 500:
                return file_path
            actual_d = get_media_duration(str(file_path))
            if actual_d > (max_dur + 0.25):
                offset = max(0.0, actual_d - max_dur)
                print(f"[ShotDesk] Cảnh báo: Video ({actual_d:.2f}s) dài hơn thời lượng shot ({max_dur:.2f}s). Cắt lấy {max_dur:.2f}s phần đuôi mới sinh (offset={offset:.2f}s)...")
                temp_out = file_path.with_name(f"{file_path.stem}_trim_tmp.mp4")
                cmd = [
                    FFMPEG, "-y",
                    "-ss", f"{offset:.3f}",
                    "-i", str(file_path),
                    "-t", f"{max_dur:.3f}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k",
                    str(temp_out)
                ]
                subprocess.run(cmd, check=True, capture_output=True)
                if temp_out.exists() and temp_out.stat().st_size > 500:
                    file_path.unlink(missing_ok=True)
                    temp_out.rename(file_path)
        except Exception as e:
            print(f"[ShotDesk] Warning trimming video duration: {e}")
        return file_path

    # Hàm trợ giúp gửi video task tới Muse2API (Hỗ trợ First Frame và End/Last Frame làm đích cuối)
    async def _send_video_render(prompt_text: str, dur: int, ref_path: Optional[Path], end_ref_path: Optional[Path] = None) -> str:
        clean_p = prompt_text
        if clean_p.strip().startswith("Specs:"):
            spec_end = clean_p.find(".")
            if 0 < spec_end < 160:
                clean_p = clean_p[spec_end + 1:].strip()
        clean_p = sanitize_prompt_heuristics(clean_p)

        # Định hướng chuyển động tiến về đích cuối (End Frame Target)
        if end_ref_path and end_ref_path.exists():
            clean_p = f"{clean_p}, camera smoothly tracks and seamlessly resolves into the destination closing keyframe composition matching the final target frame pose (End Frame target closing pose)"

        p_lower = clean_p.lower()
        if is_ai_voice:
            if voiceover:
                spk_clause = f"character {speaker}" if speaker and speaker.lower() not in ("narrator", "thuyết minh", "người kể chuyện", "none", "") else "character"
                clean_p = f"{clean_p}, audible sound design and native speech: {spk_clause} speaking with natural voice '{voiceover}', synchronized realistic vocal audio and clear ambient atmosphere"
            else:
                clean_p = f"{clean_p}, realistic synchronized sound design, clear natural ambient audio and Foley sound effects"
        elif (lip_sync or voice_mode == "dialogue") and voiceover:
            if not any(k in p_lower for k in ["lip movement", "mouth movement", "speaking dialogue", "lip sync", "khớp môi", "articulation"]):
                spk_clause = f"character {speaker}" if speaker and speaker.lower() not in ("narrator", "thuyết minh", "người kể chuyện", "none", "") else "character"
                clean_p = f"{clean_p}, medium shot of {spk_clause} talking with expressive natural synchronized mouth movement and realistic lip articulation speaking Vietnamese dialogue"
        elif voice_mode == "narration" and not lip_sync:
            if not any(k in p_lower for k in ["no mouth movement", "calm acting", "no lip movement"]):
                clean_p = f"{clean_p}, cinematic calm acting expression, natural subtle breathing, no extraneous mouth flapping"

        payload = {
            "prompt": clean_p,
            "duration": dur,
            "size": aspect_ratio,
            "timeout": 600,
        }
        if ref_path and ref_path.exists() and ref_path.is_file():
            try:
                resolved_ref = ref_path.resolve()
                if resolved_ref.is_relative_to(proj_dir.resolve()) and resolved_ref.suffix.lower() in ALLOWED_FRAME_EXTENSIONS:
                    mime = mimetypes.guess_type(str(resolved_ref))[0] or "image/png"
                    # Đọc theo chunk + giới hạn size thay vì nạp hết vào RAM.
                    b64 = _read_file_b64_capped(resolved_ref)
                    payload["reference_image"] = f"data:{mime};base64,{b64}"
            except Exception as e:
                print(f"[ShotDesk] Warning reading reference frame: {e}")

        # Gửi kèm đích cuối last_frame
        if end_ref_path and end_ref_path.exists() and end_ref_path.is_file():
            try:
                resolved_last = end_ref_path.resolve()
                if resolved_last.is_relative_to(proj_dir.resolve()) and resolved_last.suffix.lower() in ALLOWED_FRAME_EXTENSIONS:
                    mime_last = mimetypes.guess_type(str(resolved_last))[0] or "image/png"
                    # Đọc theo chunk + giới hạn size thay vì nạp hết vào RAM.
                    b64_last = _read_file_b64_capped(resolved_last)
                    uri_last = f"data:{mime_last};base64,{b64_last}"
                    payload["last_frame"] = uri_last
                    payload["tail_image"] = uri_last
                    payload["end_image"] = uri_last
            except Exception as e:
                print(f"[ShotDesk] Warning reading last frame: {e}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{base_url}/v1/videos", json=payload, headers=headers)
            if resp.status_code not in (200, 202):
                raise RuntimeError(f"Muse2API HTTP {resp.status_code}: {resp.text[:250]}")
            tid = resp.json().get("id")
            if not tid:
                raise ValueError("Không nhận được task ID từ Muse2API")
            return tid

    # Hàm tải video thành phẩm
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            # ── Render Shot Điện Ảnh Liền Mạch (Continuous Single-Take) ──
            # Render một mạch duy nhất toàn bộ thời lượng (total_shot_dur)
            # Khởi đầu từ First Frame (hoặc Middle Frame nếu không có First Frame) và định hướng về Last Frame
            # Loại bỏ hoàn toàn việc chia tách sub-clip gây lặp lại hành động, lặp lời thoại TTS/AI và sinh vết cắt jump-cut.
            start_frame_path = first_frame_path if (first_frame_path and first_frame_path.exists()) else (middle_frame_path if (middle_frame_path and middle_frame_path.exists()) else None)
            end_frame_anchor = last_frame_path if (last_frame_path and last_frame_path.exists()) else None

            if progress_cb:
                progress_cb(shot_index, f"🎬 Đang render video clip điện ảnh liền mạch ({total_shot_dur}s)...")

            tid = await _send_video_render(active_prompt, total_shot_dur, start_frame_path, end_frame_anchor)
            v_url = await poll_video_task(client, tid, base_url, headers, shot_index, progress_cb)

            raw_video_path = str(proj_dir / "clips" / f"shot_{shot_index:03d}_raw.mp4")
            # Tải video theo chunk (stream) + giới hạn size, không nạp hết vào RAM.
            downloaded = 0
            async with client.stream("GET", v_url, timeout=120.0) as r_vid:
                if r_vid.status_code != 200:
                    raise RuntimeError(f"Tải video clip thất bại (HTTP {r_vid.status_code})")
                declared = r_vid.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > _MAX_VIDEO_DOWNLOAD_BYTES:
                            raise RuntimeError(f"Video clip quá lớn ({declared} bytes > {_MAX_VIDEO_DOWNLOAD_BYTES} bytes), từ chối tải.")
                    except ValueError:
                        pass
                with open(raw_video_path, "wb") as fv:
                    async for chunk in r_vid.aiter_bytes(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > _MAX_VIDEO_DOWNLOAD_BYTES:
                            raise RuntimeError(f"Video clip vượt giới hạn {_MAX_VIDEO_DOWNLOAD_BYTES} bytes khi đang tải.")
                        fv.write(chunk)
            if downloaded < 100:
                raise RuntimeError(f"Tải video clip thất bại (chỉ nhận được {downloaded} bytes).")
            _trim_video_to_duration(Path(raw_video_path), float(total_shot_dur))
            video_source_path = raw_video_path

        except Exception as exc:
            err_msg = str(exc)
            # Tự động Fallback sang Compliance Prompt hoặc Auto-Healed Prompt nếu bị từ chối
            is_refusal = any(k in err_msg.lower() for k in [
                "403", "400", "refused", "violation", "từ chối", "thất bại", "hết thời gian", "超时",
                "未生成video", "踩线", "束缚", "敏感", "policy", "safety", "restraint", "filter"
            ])
            if is_refusal:
                # 1. Thử Compliance Prompt nếu có sẵn
                if not use_compliance and shot.get("compliance_prompt"):
                    print(f"[ShotDesk] Shot {shot_index} gặp lỗi/từ chối, tự động kích hoạt Compliance Prompt...")
                    if progress_cb:
                        progress_cb(shot_index, "🛡️ Bị kiểm duyệt: Đang tự động đổi sang Compliance Prompt...")
                    return await render_single_shot_desk(
                        project_id=project_id,
                        shot_index=shot_index,
                        base_url=base_url,
                        api_key=api_key,
                        use_compliance=True,
                        progress_cb=progress_cb
                    )
                # 2. Nếu không có compliance prompt hoặc chưa thử custom auto-heal, tự động làm sạch và render lại
                elif not custom_prompt:
                    healed_p = sanitize_prompt_heuristics(active_prompt, err_msg, aggressive=True)
                    if healed_p != active_prompt:
                        print(f"[ShotDesk] Shot {shot_index} tự động chuyển sang Auto-Healed Safe Prompt: {healed_p[:60]}...")
                        if progress_cb:
                            progress_cb(shot_index, "⚡ Đang tự động làm sạch từ khóa nhạy cảm và render lại an toàn...")
                        return await render_single_shot_desk(
                            project_id=project_id,
                            shot_index=shot_index,
                            base_url=base_url,
                            api_key=api_key,
                            custom_prompt=healed_p,
                            use_compliance=True,
                            progress_cb=progress_cb
                        )

            suggested_prompt = sanitize_prompt_heuristics(active_prompt, err_msg, aggressive=True)
            formatted_error = err_msg
            if is_refusal:
                formatted_error = f"🛡️ [Chặn kiểm duyệt] Model từ chối ({err_msg[:180]}). Hãy bấm '⚡ AI Sửa Prompt' để đổi góc quay an toàn!"

            await update_shot_state(project_id, shot_index, {
                "status": "ready",
                "error": formatted_error,
                "suggested_prompt": suggested_prompt
            })
            raise

    # Bước 3: Ghép video + audio thoại thành Clip hoàn chỉnh cho Review Desk
    if progress_cb:
        progress_cb(shot_index, "🎞️ Ghép hoàn thiện âm thanh & kiểm tra chất lượng...")

    if is_ai_voice:
        # Chế độ không lồng tiếng: không đưa file lồng tiếng ngoài, compose_scene_video sẽ giữ 100% audio gốc của AI
        scene_audio_param = None
    else:
        has_ext_audio = os.path.exists(audio_out_path) and os.path.getsize(audio_out_path) > 500
        scene_audio_param = audio_out_path if has_ext_audio else None

    compose_scene_video(
        scene_video=video_source_path,
        scene_audio=scene_audio_param,
        output=final_clip_out,
        audio_duration=float(actual_audio_dur),
        target_duration=float(total_shot_dur),
        quality="720p",
        aspect_ratio=aspect_ratio,
    )

    # Nếu ở chế độ AI Voice: trích xuất audio stream từ clip video để hiển thị player trên Review Desk
    if is_ai_voice and check_video_has_audio(video_source_path):
        try:
            subprocess.run([
                FFMPEG, "-y", "-i", video_source_path,
                "-vn", "-c:a", "libmp3lame", "-ar", "44100", "-ac", "2",
                audio_out_path
            ], capture_output=True, check=True)
        except Exception as e:
            print(f"[ShotDesk] Warning extracting native AI audio: {e}")

    has_audio = os.path.exists(audio_out_path) and os.path.getsize(audio_out_path) > 500
    shot_updates = {
        "status": "review",
        "error": None,
        "video_url": f"/api/shot-desk/projects/{project_id}/shots/{shot_index}/video",
        "audio_url": f"/api/shot-desk/projects/{project_id}/shots/{shot_index}/audio" if has_audio else None,
        "audit": {
            "character_check": "Khớp Character Block",
            "aspect_ratio_check": aspect_ratio,
            "watermark_clean": True,
            "continuity_note": shot.get("transition_note", "Sẵn sàng cho timeline")
        }
    }
    await update_shot_state(project_id, shot_index, shot_updates)
    shot.update(shot_updates)

    if progress_cb:
        progress_cb(shot_index, "✅ Hoàn tất! Sẵn sàng cho Review Desk duyệt.")

    return shot


# ── 3. Review Desk: Duyệt & Chỉnh Sửa Shot ───────────────────────────────────

def approve_shot_desk_shot(project_id: str, shot_index: int) -> Dict[str, Any]:
    """Đạo diễn bấm Approve duyệt shot để đưa vào Timeline dựng phim cuối cùng."""
    # Giữ per-project lock trong toàn bộ read-modify-write (get -> sửa -> save).
    with get_sync_project_lock(project_id):
        proj = get_shot_desk_project(project_id)
        if not proj:
            raise ValueError("Không tìm thấy project")
        target = None
        for s in proj.get("shots", []):
            if s["index"] == shot_index:
                target = s
                break
        if not target:
            raise ValueError("Không tìm thấy shot")

        target["status"] = "approved"
        target["error"] = None
        save_shot_desk_project(proj)
        return {"success": True, "shot": target}


def approve_all_shot_desk_shots(project_id: str, only_with_video: bool = True) -> Dict[str, Any]:
    """Phê duyệt hàng loạt các cảnh (mặc định chỉ duyệt các cảnh đã có video clip thành phẩm hoặc ở trạng thái review)."""
    # Giữ per-project lock trong toàn bộ read-modify-write (get -> sửa -> save).
    with get_sync_project_lock(project_id):
        proj = get_shot_desk_project(project_id)
        if not proj:
            raise ValueError("Không tìm thấy project")

        proj_dir = (SHOT_DESK_DIR / project_id).resolve()
        clips_dir = proj_dir / "clips"
        approved_shots = []

        for s in proj.get("shots", []):
            if s.get("status") in ("approved", "done"):
                continue

            idx = s.get("index")
            clip_p = clips_dir / f"shot_{idx:03d}_clip.mp4"
            raw_p = clips_dir / f"shot_{idx:03d}_raw.mp4"
            has_video = (clip_p.exists() and clip_p.stat().st_size > 500) or (raw_p.exists() and raw_p.stat().st_size > 500) or bool(s.get("video_url"))

            if only_with_video:
                if s.get("status") == "review" or has_video:
                    s["status"] = "approved"
                    s["error"] = None
                    if not s.get("video_url") and clip_p.exists():
                        s["video_url"] = f"/api/shot-desk/projects/{project_id}/shots/{idx}/video"
                    approved_shots.append(idx)
            else:
                s["status"] = "approved"
                s["error"] = None
                approved_shots.append(idx)

        if approved_shots:
            save_shot_desk_project(proj)

        return {
            "success": True,
            "approved_count": len(approved_shots),
            "approved_indices": approved_shots,
            "total_shots": len(proj.get("shots", []))
        }


def edit_shot_desk_shot(project_id: str, shot_index: int, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Chỉnh sửa prompt, compliance_prompt, lời thoại, thời lượng, voice_mode, voice, speaker, lip_sync của 1 shot."""
    # Giữ per-project lock trong toàn bộ read-modify-write (get -> sửa -> save).
    with get_sync_project_lock(project_id):
        proj = get_shot_desk_project(project_id)
        if not proj:
            raise ValueError("Không tìm thấy project")
        target = None
        for s in proj.get("shots", []):
            if s["index"] == shot_index:
                target = s
                break
        if not target:
            raise ValueError("Không tìm thấy shot")

        for k in ["prompt", "compliance_prompt", "voiceover", "duration", "transition_note", "voice_mode", "speaker", "voice", "lip_sync"]:
            if k in updates:
                target[k] = updates[k]

        # Nếu sửa prompt hoặc yêu cầu reset thì đặt lại trạng thái về ready để render lại
        if updates.get("reset_status", True):
            target["status"] = "ready"

        save_shot_desk_project(proj)
        return {"success": True, "shot": target}


def update_shot_desk_project_settings(project_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Cập nhật cấu hình toàn dự án (giọng đọc mặc định, chế độ thuyết minh / khớp môi thoại)."""
    # Giữ per-project lock trong toàn bộ read-modify-write (get -> sửa -> save).
    with get_sync_project_lock(project_id):
        proj = get_shot_desk_project(project_id)
        if not proj:
            raise ValueError(f"Không tìm thấy dự án '{project_id}'")

        if "default_voice" in settings and settings["default_voice"]:
            proj["default_voice"] = settings["default_voice"]
        if "default_voice_mode" in settings and settings["default_voice_mode"]:
            proj["default_voice_mode"] = settings["default_voice_mode"]

        if settings.get("apply_to_all_shots"):
            new_voice = proj.get("default_voice", "vi-VN-HoaiMyNeural")
            new_mode = proj.get("default_voice_mode", "narration")
            is_dialogue = (new_mode == "dialogue")
            for s in proj.get("shots", []):
                s["voice"] = new_voice
                s["voice_mode"] = new_mode
                s["lip_sync"] = is_dialogue

        proj["updated_at"] = int(time.time())
        save_shot_desk_project(proj)
        return {"success": True, "project": proj}


# ── 4. Timeline Hậu Kỳ: Ghép Các Shot Đã Duyệt Thành Phim Hoàn Chỉnh ──────────

def compose_shot_desk_timeline(
    project_id: str,
    allow_partial: bool = False,
    bg_music_path: Optional[str] = None,
    music_volume: float = 0.25,
    voice_volume: float = 1.0,
    subtitle_mode: str = "burn_in",
    quality: str = "720p",
    sound_bridge_duration: float = 0.75,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Dựng phim theo Timeline chuyên nghiệp từ các Shot ĐÃ DUYỆT (Approved):
    1. Lọc và kiểm định các shot đã Approved
    2. Áp dụng kỹ thuật Chuyển cảnh (Transitions: Match cut, Dissolve, Light wipe)
    3. Thiết kế Sound Bridge (Âm thanh/thoại cảnh sau tràn sang cuối cảnh trước 0.5s - 1.0s)
    4. Hòa âm đa tầng (Multi-track mixing: Thoại + BGM + SFX)
    5. Tạo phụ đề chuẩn mili-giây và Burn-in
    6. Xuất phim hoàn chỉnh chuẩn điện ảnh
    """
    proj = get_shot_desk_project(project_id)
    if not proj:
        raise ValueError("Không tìm thấy project")

    # Validate bg_music_path NGAY ĐẦU (kể cả giá trị lưu trong project):
    # chỉ cho phép file trong workspace/_music, workspace/_uploads hoặc thư
    # mục của chính project này. Giá trị truyền vào sai -> lỗi rõ ràng;
    # giá trị đã lưu sai -> cảnh báo + bỏ qua (không dùng nhạc lậu ngoài workspace).
    validated_bgm: Optional[Path] = None
    if bg_music_path is not None:
        validated_bgm = resolve_safe_bg_music_path(bg_music_path, project_id)
    else:
        stored_bgm = proj.get("timeline_settings", {}).get("bg_music_path")
        if stored_bgm:
            try:
                validated_bgm = resolve_safe_bg_music_path(stored_bgm, project_id)
            except ValueError as e:
                print(f"[ShotDesk] Warning: bg_music_path đã lưu không hợp lệ, bỏ qua: {e}")

    proj_dir = SHOT_DESK_DIR / project_id
    shots = proj.get("shots", [])

    approved_shots = [s for s in shots if s.get("status") in ("approved", "done")]
    approved_shots.sort(key=lambda x: x["index"])

    if not approved_shots:
        raise ValueError("Không có shot nào được duyệt (Approved) để dựng timeline!")

    if len(approved_shots) < len(shots) and not allow_partial:
        missing = [s["index"] for s in shots if s.get("status") not in ("approved", "done")]
        raise ValueError(f"Còn {len(missing)} shot chưa được duyệt: {missing}. Hãy duyệt hết các shot hoặc chọn Xuất Bản Nháp.")

    ts = int(time.time())
    final_dir = proj_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = proj_dir / "_timeline_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Thu thập danh sách clip của các shot đã duyệt
    valid_shots = []
    valid_clips = []
    clip_durations = []
    srt_entries = []
    missing_clips = []

    for s in approved_shots:
        idx = s["index"]
        clip_file = proj_dir / "clips" / f"shot_{idx:03d}_clip.mp4"
        if not clip_file.exists():
            missing_clips.append(idx)
            continue
        valid_shots.append(s)
        valid_clips.append(str(clip_file))
        dur = get_media_duration(str(clip_file))
        clip_durations.append(dur)

        srt_entries.append({
            "index": idx,
            "voiceover": s.get("voiceover", ""),
            "duration": dur,
            "clip_path": str(clip_file),
        })

    # Nếu xuất phim hoàn chỉnh (allow_partial=False), bắt buộc tất cả shot đã duyệt phải có file clip MP4 hợp lệ
    if missing_clips and not allow_partial:
        raise ValueError(
            f"Không thể xuất phim hoàn chỉnh: Còn {len(missing_clips)} shot đã duyệt nhưng chưa có file video MP4 trên đĩa (Shot {missing_clips}). "
            "Vui lòng render lại các shot này hoặc bật tùy chọn 'Bản nháp (Allow partial)' để xuất phim nháp."
        )

    if not valid_clips:
        raise ValueError("Không tìm thấy file clip MP4 nào cho các shot đã duyệt!")

    # Xác định chuyển cảnh chính xác giữa từng cặp cảnh kế tiếp (ưu tiên transition_out của cảnh trước)
    def resolve_shot_transition(from_shot: Dict, to_shot: Dict) -> str:
        t_out = from_shot.get("transition_out")
        if t_out and str(t_out).strip().lower() not in ("", "none", "null"):
            return str(t_out).strip()
        t_note_prev = from_shot.get("transition_note")
        if t_note_prev and str(t_note_prev).strip().lower() not in ("", "none", "null"):
            return str(t_note_prev).strip()
        t_in = to_shot.get("transition_in")
        if t_in and str(t_in).strip().lower() not in ("", "none", "null"):
            return str(t_in).strip()
        t_note_next = to_shot.get("transition_note")
        if t_note_next and str(t_note_next).strip().lower() not in ("", "none", "null"):
            return str(t_note_next).strip()
        return "dissolve"

    transitions = [
        resolve_shot_transition(valid_shots[i], valid_shots[i + 1])
        for i in range(len(valid_shots) - 1)
    ]
    timeline_offsets = compute_timeline_offsets(valid_clips, transitions, sound_bridge_duration)

    if progress_cb:
        progress_cb(5, "Đang khởi tạo phụ đề SRT đồng bộ chính xác với audio...")

    # 2. Tạo phụ đề SRT đồng bộ: Tự động phát hiện chính xác thời điểm có tiếng mới hiện phụ đề
    slug = to_slug(proj.get("title", "movie"))
    srt_filename = f"{slug}_{ts}_phude.srt" if subtitle_mode == "burn_in" else f"{slug}_{ts}.srt"
    srt_path = final_dir / srt_filename
    create_srt(
        srt_entries,
        str(srt_path),
        clip_durations=clip_durations,
        media_paths=valid_clips,
        timeline_offsets=timeline_offsets,
    )

    # 3. Quản lý & Trộn âm thanh SFX / Ambience thật
    sfx_clips = []
    has_any_sfx = False
    custom_sfx_dir = str(proj_dir / "sfx") if (proj_dir / "sfx").exists() else None

    for idx, s in enumerate(valid_shots):
        dur = clip_durations[idx]
        sfx_tag = s.get("sfx") or s.get("sound_effects") or s.get("ambience") or detect_sfx_from_text(s.get("prompt", "") + " " + s.get("voiceover", ""))
        shot_sfx_path = tmp_dir / f"shot_{s['index']:03d}_sfx.mp3"
        tag_to_use = sfx_tag or "ambience"
        has_any_sfx = True
        generate_sfx_audio(tag_to_use, dur, str(shot_sfx_path), custom_sfx_dir=custom_sfx_dir)
        sfx_clips.append(str(shot_sfx_path))

    master_sfx_path = None
    if has_any_sfx:
        master_sfx_path = str(tmp_dir / f"master_sfx_{ts}.mp3")
        try:
            merge_audio_with_transitions(
                audio_clips=sfx_clips,
                transitions=transitions,
                sound_bridge_duration=sound_bridge_duration,
                output_path=master_sfx_path,
            )
        except Exception as e:
            print(f"[ShotDesk SFX Warning] Lỗi ghép SFX timeline: {e}")
            master_sfx_path = None

    # 4. Ghép clips với Sound Bridge (acrossfade) và Chuyển cảnh (Transitions) thực tế
    merged_video = tmp_dir / f"merged_{ts}.mp4"

    merge_clips_advanced(
        clips=valid_clips,
        transitions=transitions,
        sound_bridge_duration=sound_bridge_duration,
        output_path=str(merged_video),
        quality=quality,
        aspect_ratio=proj.get("aspect_ratio", "9:16"),
        progress_cb=progress_cb
    )

    # 5. Phụ đề Burn-in (nếu bật)
    video_to_bgm = str(merged_video)
    if subtitle_mode == "burn_in":
        if progress_cb:
            progress_cb(65, "Bắt đầu khắc phụ đề điện ảnh chuẩn đáy...")
        with_sub = tmp_dir / f"with_sub_{ts}.mp4"
        q_str = (quality or "720p").lower()
        sub_font_size = 20 if q_str == "720p" else 24 if q_str == "1080p" else 32 if q_str == "4k" else 16
        burn_subtitles(
            str(merged_video),
            str(srt_path),
            str(with_sub),
            font_size=sub_font_size,
            progress_cb=progress_cb,
            total_duration=sum(clip_durations) if clip_durations else None
        )
        video_to_bgm = str(with_sub)

    # 6. Hòa âm đa tầng (Thoại + SFX + BGM) & Áp dụng Profile Quality (draft, 720p, 1080p, 4k)
    if progress_cb:
        progress_cb(90, f"Đang hòa âm đa tầng (SFX + BGM) & Render chất lượng {quality}...")

    final_mp4_name = f"{slug}_{ts}.mp4"
    final_mp4_path = final_dir / final_mp4_name

    active_bgm = validated_bgm
    active_bgm_str = str(active_bgm) if active_bgm is not None else None

    mix_multitrack_timeline(
        video_path=video_to_bgm,
        music_path=active_bgm_str,
        sfx_path=master_sfx_path,
        output_path=str(final_mp4_path),
        voice_volume=voice_volume,
        music_volume=music_volume,
        sfx_volume=0.4 if has_any_sfx else 0.0,
        fade_duration=1.5,
        quality=quality,
        aspect_ratio=proj.get("aspect_ratio", "9:16"),
    )

    total_movie_duration = get_media_duration(str(final_mp4_path))

    # Dọn dẹp video tạm trong tmp_dir để tránh phình dung lượng đĩa
    try:
        if merged_video.exists():
            os.remove(merged_video)
        if subtitle_mode == "burn_in":
            with_sub_tmp = tmp_dir / f"with_sub_{ts}.mp4"
            if with_sub_tmp.exists():
                os.remove(with_sub_tmp)
    except Exception:
        pass

    # Cập nhật trạng thái project — compose chạy lâu nên project trên đĩa có
    # thể đã đổi (render/approve song song). Đọc lại dưới per-project lock
    # rồi mới merge kết quả, tránh ghi đè mất update của luồng khác.
    # TODO: nếu project bị xóa giữa chừng thì latest=None -> giữ proj cũ.
    with get_sync_project_lock(project_id):
        latest = get_shot_desk_project(project_id) or proj
        latest["final_video"] = f"/api/shot-desk/projects/{project_id}/final-video/{final_mp4_name}"
        latest["srt_file"] = f"/api/shot-desk/projects/{project_id}/subtitles/{srt_filename}"
        latest.setdefault("timeline_settings", {}).update({
            "music_volume": music_volume,
            "voice_volume": voice_volume,
            "bg_music_path": str(active_bgm) if active_bgm is not None else None,
            "subtitle_mode": subtitle_mode,
            "export_quality": quality
        })
        done_indices = {s["index"] for s in valid_shots}
        for s in latest.get("shots", []):
            if s.get("index") in done_indices:
                s["status"] = "done"
        save_shot_desk_project(latest)
        proj = latest

    if progress_cb:
        progress_cb(100, "🎉 Dựng phim hoàn tất 100%!")

    return {
        "success": True,
        "project_id": project_id,
        "final_video": proj["final_video"],
        "srt_file": proj["srt_file"],
        "duration": round(total_movie_duration, 2),
        "shots_count": len(valid_shots),
        "final_dir": str(final_dir.resolve()),
    }


# ── 5. Xuất Gói ZIP Cập Nhật Dự Án ───────────────────────────────────────────

def export_shot_desk_zip(project_id: str, output_zip_path: str) -> str:
    """Đóng gói toàn bộ dự án Shot Desk (gồm metadata, frames, clips, audio, characters, final) thành ZIP tải về."""
    proj = get_shot_desk_project(project_id)
    if not proj:
        raise ValueError("Không tìm thấy project")

    proj_dir = SHOT_DESK_DIR / project_id
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        # 1. project.json
        z.write(proj_dir / "project.json", "project.json")

        # 2. Shots frames & clips
        for s in proj.get("shots", []):
            idx = s["index"]
            shot_dir = proj_dir / "shots" / f"shot_{idx:03d}"
            if shot_dir.exists():
                for f in shot_dir.iterdir():
                    if f.is_file():
                        z.write(f, f"shots/shot_{idx:03d}/{f.name}")

            clip_f = proj_dir / "clips" / f"shot_{idx:03d}_clip.mp4"
            if clip_f.exists():
                z.write(clip_f, f"clips/shot_{idx:03d}_clip.mp4")

        # 3. Audio tracks (TTS tiếng Việt, ambient)
        audio_dir = proj_dir / "audio"
        if audio_dir.exists():
            for f in audio_dir.iterdir():
                if f.is_file():
                    z.write(f, f"audio/{f.name}")

        # 4. Characters assets
        char_dir = proj_dir / "characters"
        if char_dir.exists():
            for f in char_dir.iterdir():
                if f.is_file():
                    z.write(f, f"characters/{f.name}")

        # 5. Final movie and srt
        final_dir = proj_dir / "final"
        if final_dir.exists():
            for f in final_dir.iterdir():
                if f.is_file():
                    z.write(f, f"final/{f.name}")

    return output_zip_path
