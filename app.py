"""
app.py  –  AI Movie Studio
--------------------------
FastAPI backend:
  - Web UI tổng quan (static/index.html)
  - REST API tạo phim hàng loạt batch
  - Cập nhật tiến độ thời gian thực (SSE)
  - Stream / download video thành phẩm
"""

import asyncio
import base64
import json
import logging
import os
import mimetypes
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from batch_engine import run_batch, generate_keyframe_image, heal_prompt_with_ai, apply_style_locks, sanitize_prompt_heuristics
from composer import compose_full_movie, audit_movie_scenes, compose_scene_video
from tts_manager import list_all_voices, synthesize_voice, VIENEU_VOICES_CONFIG, OMNIVOICE_VOICES_CONFIG
from affiliate_engine import (
    AFFILIATE_CATEGORIES,
    AFFILIATE_FRAMEWORKS,
    KOC_PERSONAS,
    AFFILIATE_SHOOTING_STYLES,
    ProductAffiliateRequest,
    AffiliateScriptResponse,
    generate_affiliate_script_ai,
    generate_affiliate_script_local,
)


load_dotenv()

logger = logging.getLogger("ai_movie_studio")

# ── Cấu hình ──────────────────────────────────────────────────────────────
MUSE_API_URL = os.getenv("MUSE2API_URL", "http://localhost:18610")
# SECURITY (audit §2.2): KHÔNG còn default key hardcode. Key chỉ lấy từ env
# MUSE2API_KEY (fallback MUSE_API_KEY). Nếu thiếu, MUSE_API_KEY=None và:
#  - endpoint gọi Muse trực tiếp trả 503 rõ ràng (xem require_muse_key()),
#  - pipeline render (film/retry/single) đánh dấu job error với message rõ ràng
#    thay vì gọi gateway bằng key giả. KHÔNG dùng key mặc định dưới mọi hình thức.
MUSE_API_KEY: Optional[str] = os.getenv("MUSE2API_KEY") or os.getenv("MUSE_API_KEY") or None
if not MUSE_API_KEY:
    logger.warning(
        "MUSE2API_KEY/MUSE_API_KEY chưa được đặt — các endpoint gọi Muse sẽ trả "
        "503 (config error) cho tới khi cấu hình key. KHÔNG dùng key mặc định.")


def require_muse_key() -> str:
    """Trả về Muse API key, hoặc raise 503 nếu chưa cấu hình (không key giả)."""
    if not MUSE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Muse API key chưa được cấu hình: đặt MUSE2API_KEY trong môi trường rồi khởi động lại server.",
        )
    return MUSE_API_KEY


# ── Concurrency guards (audit §1.6) ───────────────────────────────────────
# Một threading.Lock duy nhất bảo vệ mọi mutation của JOBS (cả async path và
# sync/thread path đều chạm vào dict này). Chỉ giữ lock trong đoạn ngắn,
# KHÔNG await khi đang giữ lock.
JOBS_LOCK = threading.Lock()
# job_id -> asyncio.Task của pipeline đang chạy (để cancel thật, xem api_cancel_job).
JOB_TASKS: Dict[str, "asyncio.Task"] = {}
# Rate-limit: tối đa _RATE_LIMIT_REQUESTS request /api/* mỗi _RATE_LIMIT_WINDOW_S
# cho mỗi IP (bộ nhớ tiến trình; không thêm dependency slowapi/redis).
_RATE_LIMIT_REQUESTS = 300
_RATE_LIMIT_WINDOW_S = 60.0
_rate_limit_buckets: Dict[str, list] = {}  # ip -> [window_start_monotonic, count]
_RATE_LIMIT_LOCK = threading.Lock()
_AUTH_WARNING_LOGGED = False


async def _send_json_via_asgi(send, status: int, payload: Dict[str, Any], extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [[b"content-type", b"application/json"]]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _RateLimitMiddleware:
    """Giới hạn 300 request/phút/IP cho /api/* (bộ nhớ tiến trình), vượt -> 429.
    Pure-ASGI passthrough: không đọc/đệm body nên không ảnh hưởng SSE/stream."""

    def __init__(self, app, limit: int = _RATE_LIMIT_REQUESTS, window: float = _RATE_LIMIT_WINDOW_S):
        self.app = app
        self.limit = limit
        self.window = window

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client") or ("unknown", 0)
        ip = str(client[0]) if client else "unknown"
        now = time.monotonic()
        with _RATE_LIMIT_LOCK:
            bucket = _rate_limit_buckets.get(ip)
            if bucket is None or now - bucket[0] >= self.window:
                _rate_limit_buckets[ip] = [now, 1]
                allowed = True
            else:
                bucket[1] += 1
                allowed = bucket[1] <= self.limit
        if not allowed:
            await _send_json_via_asgi(
                send, 429, {"error": "Too many requests, thử lại sau ít phút."},
                extra_headers=[[b"retry-after", b"60"]])
            return
        await self.app(scope, receive, send)


class _ApiKeyAuthMiddleware:
    """Auth tối thiểu cho /api/* (audit §2.1).
    - Nếu env STUDIO_API_KEY được đặt: bắt buộc header X-API-Key khớp, sai/thiếu -> 401.
    - Nếu chưa đặt: cho qua + log warning 1 lần ("auth disabled for local dev").
      Frontend tĩnh gửi header này qua apiFetch() (static/js/app.js, lấy key
      từ localStorage 'ams-api-key'); xem GET /api/auth/status để biết
      server có bắt buộc key không.
    - Miễn: "/" , "/health", "/api/health và "/api/jobs/{id}/sse" (EventSource của
      trình duyệt không gửi được custom header; miễn SSE để khỏi vỡ frontend —
      SSE chỉ lộ tiến độ/log của job_id, chấp nhận rủi ro này; TODO: cho SSE
      nhận api_key qua query param khi frontend hỗ trợ).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        if path in ("/api/health", "/api/auth/status") or (path.startswith("/api/jobs/") and path.endswith("/sse")):
            await self.app(scope, receive, send)
            return
        expected = os.getenv("STUDIO_API_KEY")
        if not expected:
            global _AUTH_WARNING_LOGGED
            if not _AUTH_WARNING_LOGGED:
                _AUTH_WARNING_LOGGED = True
                logger.warning(
                    "STUDIO_API_KEY chưa được đặt — auth cho /api/* đang TẮT "
                    "(local dev). Đặt STUDIO_API_KEY để bắt buộc header X-API-Key.")
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if headers.get("x-api-key") != expected:
            await _send_json_via_asgi(
                send, 401, {"error": "Thiếu hoặc sai header X-API-Key"})
            return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Một httpx.AsyncClient dùng chung (connection pooling, audit §3.3) +
    load persisted jobs 1 lần lúc startup (audit §1.5)."""
    app.state.http_client = httpx.AsyncClient()
    try:
        load_all_persisted_jobs()
    except Exception as exc:  # startup không được chết vì job_state hỏng
        logger.warning("Không load được persisted jobs lúc startup: %s", exc)
    yield
    client = getattr(app.state, "http_client", None)
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass


def get_http_client() -> httpx.AsyncClient:
    """Client dùng chung từ lifespan. Fallback: tạo 1 client lười khi chạy ngoài
    lifespan (vd TestClient không dùng context manager, gọi hàm trực tiếp).
    KHÔNG dùng `async with get_http_client()` — sẽ đóng client dùng chung."""
    client = getattr(app.state, "http_client", None)
    if client is None:
        client = httpx.AsyncClient()
        app.state.http_client = client
    return client


@asynccontextmanager
async def _shared_client_ctx():
    """Yield client dùng chung mà KHÔNG đóng nó khi rời context (để dùng chung
    với code cũ dạng `async with ... as client:` mà không sửa indent).
    Timeout phải truyền theo từng request (vd timeout=45.0), vì client dùng
    chung không mang timeout riêng của từng call site cũ."""
    yield get_http_client()
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "12"))
WORKSPACE = Path("workspace")
WORKSPACE.mkdir(exist_ok=True)

# ── Safe Containment & Path Traversal Guards ────────────────────────────────
UPLOAD_DIR = WORKSPACE / "_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MUSIC_DIR = WORKSPACE / "_music"
MUSIC_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR = WORKSPACE / "_projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

JOB_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")
PROTECTED_WORKSPACE_NAMES = {"_uploads", "_music", "_projects", "_shot_desk_projects", "_voice_previews", "_characters.json"}

def get_safe_job_dir(job_id: str) -> Path:
    """Kiểm tra và trả về thư mục job an toàn bên trong WORKSPACE, chống path traversal."""
    if not job_id or not isinstance(job_id, str) or not JOB_ID_REGEX.match(job_id) or job_id in PROTECTED_WORKSPACE_NAMES or job_id.startswith("_"):
        raise HTTPException(status_code=400, detail="job_id không hợp lệ hoặc thuộc thư mục được bảo vệ")
    job_dir = (WORKSPACE / job_id).resolve()
    if not job_dir.is_relative_to(WORKSPACE.resolve()):
        raise HTTPException(status_code=400, detail="Đường dẫn job_id không an toàn (path traversal)")
    return job_dir

def resolve_safe_job_path(job_id: str, relative_path: str) -> Path:
    """Resolve và đảm bảo relative_path nằm hoàn toàn bên trong thư mục job."""
    job_dir = get_safe_job_dir(job_id)
    if not relative_path or not isinstance(relative_path, str):
        raise HTTPException(status_code=400, detail="Đường dẫn không hợp lệ")
    norm = relative_path.replace("\\", "/")
    if norm.startswith("/") or ":" in norm or ".." in Path(norm).parts:
        raise HTTPException(status_code=400, detail="Đường dẫn chứa ký tự điều hướng hoặc tuyệt đối không an toàn")
    target = (job_dir / norm).resolve()
    if not target.is_relative_to(job_dir):
        raise HTTPException(status_code=400, detail="Đường dẫn tài nguyên nằm ngoài thư mục job")
    return target

def resolve_safe_upload_path(filename: str) -> Path:
    """Resolve và đảm bảo filename nằm hoàn toàn bên trong UPLOAD_DIR."""
    if not filename or not isinstance(filename, str):
        raise HTTPException(status_code=400, detail="Tên file upload không hợp lệ")
    norm = filename.replace("\\", "/")
    if norm.startswith("/") or ":" in norm or ".." in Path(norm).parts or "/" in norm:
        raise HTTPException(status_code=400, detail="Tên file upload không hợp lệ (path traversal)")
    target = (UPLOAD_DIR / norm).resolve()
    if not target.is_relative_to(UPLOAD_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Đường dẫn file upload nằm ngoài thư mục upload")
    return target

def resolve_safe_music_path(filename: str) -> Path:
    """Resolve và đảm bảo filename nằm hoàn toàn bên trong MUSIC_DIR."""
    if not filename or not isinstance(filename, str):
        raise HTTPException(status_code=400, detail="Tên file nhạc không hợp lệ")
    norm = filename.replace("\\", "/")
    if norm.startswith("/") or ":" in norm or ".." in Path(norm).parts or "/" in norm:
        raise HTTPException(status_code=400, detail="Tên file nhạc không hợp lệ (path traversal)")
    target = (MUSIC_DIR / norm).resolve()
    if not target.is_relative_to(MUSIC_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Đường dẫn file nhạc nằm ngoài thư mục nhạc")
    return target

# ── FastAPI App ────────────────────────────────────────────────────────────
app = FastAPI(title="AI Movie Studio", version="1.0.0", lifespan=lifespan)
app.add_middleware(_ApiKeyAuthMiddleware)
app.add_middleware(_RateLimitMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")

class StrKeyDict(dict):
    """Dict mà mọi key đều được chuẩn hoá về str khi truy cập.

    JSON đổi key int -> str khi lưu job_state.json (audit §1.2), nên
    job["scenes"][1] và job["scenes"]["1"] phải trỏ cùng một entry dù job
    đang ở bộ nhớ (key int) hay vừa restore từ đĩa (key str). Dùng class này
    cho job["scenes"] thay vì dict thường để mọi code cũ truy cập trực tiếp
    bằng int hay str đều đúng, không cần sửa từng call site.
    """

    @staticmethod
    def _norm(key):
        return str(key)

    def __getitem__(self, key):
        return super().__getitem__(self._norm(key))

    def __setitem__(self, key, value):
        super().__setitem__(self._norm(key), value)

    def __delitem__(self, key):
        super().__delitem__(self._norm(key))

    def __contains__(self, key):
        return super().__contains__(self._norm(key))

    def get(self, key, default=None):
        return super().get(self._norm(key), default)

    def pop(self, key, *args):
        return super().pop(self._norm(key), *args)

    def setdefault(self, key, default=None):
        return super().setdefault(self._norm(key), default)


# ── Job State ──────────────────────────────────────────────────────────────
# job_id → state dict
JOBS: Dict[str, Dict[str, Any]] = {}


def new_job(job_id: str, total_scenes: int, req: Optional[Any] = None) -> Dict:
    state = {
        "id": job_id,
        "status": "running",          # running | done | error
        "progress": 0,                # 0-100
        "scenes": StrKeyDict(),       # idx → {status, msg} (key luôn str, xem StrKeyDict)
        "total": total_scenes,
        "logs": [],
        "final_video": None,
        "failed_scenes": [],
        "created_at": time.time(),
        "req": req,
        "results": [],
    }
    with JOBS_LOCK:
        JOBS[job_id] = state
    return state


def job_log(job: Dict, msg: str):
    ts = time.strftime("%H:%M:%S")
    job["logs"].append(f"[{ts}] {msg}")
    print(f"[Job {job['id'][:8]}] {msg}")


# ── Pydantic Models ────────────────────────────────────────────────────────
class VoiceSegment(BaseModel):
    """Một đoạn thoại của 1 nhân vật trong cảnh."""
    character: str = Field(default="", max_length=200)  # Tên nhân vật (hiển thị)
    text: str = Field(max_length=5000)                  # Nội dung lời thoại
    voice: str = Field(max_length=200)                  # Giọng edge-tts cho nhân vật này


class SceneItem(BaseModel):
    index: int
    prompt: str = Field(max_length=5000)
    voiceover: str = Field(default="", max_length=5000)  # Lời thuyết minh đơn giản (dùng khi không có voice_segments)
    voice: str = Field(default="", max_length=200)       # Giọng riêng cho cảnh này (override global voice)
    voice_mode: Optional[str] = None # "tts" | "ai_voice"
    voice_segments: List[VoiceSegment] = Field(default=[], max_length=20)  # Nhiều nhân vật với giọng khác nhau
    # duration: engine (batch_engine/process_scene) nhận int giây cho /v1/videos;
    # giới hạn 1-60s để 1 request không kéo tài nguyên quá mức (audit §2.9).
    duration: int = Field(default=5, ge=1, le=60)
    size: str = Field(default="16:9", max_length=10)
    reference_image: Optional[str] = None    # Ảnh tham chiếu nhân vật (URL hoặc base64 Data URL)
    chain_from_previous: Optional[bool] = False # First/Last Frame Chaining: kế thừa khung hình cuối của cảnh trước
    sheet_angle: Optional[str] = "front"        # "front" | "side" | "closeup" | "fullbody"
    last_frame_url: Optional[str] = None


class MovieRequest(BaseModel):
    title: str = Field(default="my_movie", max_length=200)
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=200)  # Giọng mặc định toàn phim
    voice_mode: Optional[str] = "tts"    # "tts" | "ai_voice" (đưa thoại vào prompt để AI tự sinh tiếng)
    font_size: int = Field(default=22, ge=8, le=72)
    aspect_ratio: Optional[str] = Field(default="16:9", max_length=10)  # Tỉ lệ khung hình toàn phim (16:9, 9:16, 1:1, 21:9, 4:3)
    export_mode: str = Field(default="full", max_length=20)  # "full" | "batch_clips"
    max_concurrent: Optional[int] = Field(default=12, ge=1, le=12)
    chain_mode: Optional[str] = "none"   # "none" | "sequential"
    chain_fallback_mode: Optional[str] = "independent" # "independent" | "fallback_image" | "stop"
    bg_music_path: Optional[str] = None
    music_volume: Optional[float] = Field(default=0.25, ge=0.0, le=2.0)
    voice_volume: Optional[float] = Field(default=1.0, ge=0.0, le=2.0)
    subtitle_mode: Optional[str] = Field(default="burn_in", max_length=20)  # "burn_in" | "srt_only" | "none"
    quality: Optional[str] = Field(default="720p", max_length=20)           # "preview" | "720p" | "1080p"
    transition: Optional[str] = Field(default="none", max_length=20)        # "none" | "fade" | "dissolve"
    style_locks: Optional[Dict[str, str]] = None
    characters: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=100)
    start_scene_idx: Optional[int] = Field(default=1, ge=1)
    allow_partial: Optional[bool] = False
    scenes: List[SceneItem] = Field(max_length=200)  # audit §2.9: chặn DoS 1 request nghìn cảnh



def _atomic_write_json(path: Path, data: Any) -> None:
    """Ghi JSON atomic: tmp file cùng thư mục + fsync + os.replace (audit §1.3).
    Crash/mất điện giữa chừng không để lại file rỗng/cắt nửa. Raise khi lỗi."""
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _normalize_scene_keys(job: Dict) -> None:
    """Chuẩn hoá key của job['scenes'] về str (JSON đổi int->str khi lưu, audit §1.2)."""
    scenes = job.get("scenes")
    if isinstance(scenes, dict):
        job["scenes"] = StrKeyDict({str(k): v for k, v in scenes.items()})


def _scene(job: Dict, idx) -> Dict:
    """Lấy entry trạng thái của 1 cảnh, chấp nhận cả key int/str (audit §1.2).
    Key int cũ (nếu có) được migrate sang str. Tạo entry rỗng dưới key str nếu chưa có."""
    scenes = job.setdefault("scenes", StrKeyDict())
    if not isinstance(scenes, StrKeyDict):
        scenes = job["scenes"] = StrKeyDict(scenes)
    sc = scenes.get(str(idx))
    if sc is None and isinstance(idx, int):
        sc = scenes.pop(idx, None)
    if sc is None:
        sc = {}
        scenes[str(idx)] = sc
    return sc


def _set_scene(job: Dict, idx, value: Dict) -> None:
    """Ghi entry trạng thái cảnh — CHỈ dùng key str (tránh đếm trùng khi tính progress)."""
    scenes = job.setdefault("scenes", StrKeyDict())
    if not isinstance(scenes, StrKeyDict):
        scenes = job["scenes"] = StrKeyDict(scenes)
    # StrKeyDict đã chuẩn hoá key về str nên không còn key int trùng để xoá
    # (nhánh del cũ sẽ tự xoá chính entry vừa ghi nếu scenes là StrKeyDict).
    scenes[str(idx)] = value


def _unwrap_final_path(final_path: Any, *, context: str = "compose_full_movie") -> str:
    """Unwrap kết quả compose_full_movie (dict {'video_path','video_filename'} hoặc str).
    Raise ValueError rõ ràng nếu không có đường dẫn hợp lệ — thay vì tạo URL
    video hỏng '/api/jobs/{id}/video/' (audit §1.17)."""
    if isinstance(final_path, dict):
        path = final_path.get("video_path") or final_path.get("video_filename") or ""
    elif isinstance(final_path, str):
        path = final_path
    else:
        path = ""
    path = str(path).strip()
    if not path:
        raise ValueError(
            f"{context} không trả về đường dẫn video hợp lệ "
            f"(nhận được {type(final_path).__name__}: {str(final_path)[:200]})")
    return path


def save_job(job: Dict):
    """Ghi job_state.json atomic (tmp + fsync + os.replace). Lỗi được log đầy đủ
    và raise để caller biết — KHÔNG nuốt lặng (audit §1.3). Các progress callback
    và except-handler trong pipeline dùng _save_job_best_effort() để lỗi ghi đĩa
    không giết pipeline đang render."""
    job_dir = get_safe_job_dir(job["id"])
    job_dir.mkdir(parents=True, exist_ok=True)
    state_file = job_dir / "job_state.json"
    to_save = dict(job)
    if hasattr(to_save.get("req"), "model_dump"):
        to_save["req"] = to_save["req"].model_dump()
    try:
        _atomic_write_json(state_file, to_save)
    except Exception:
        logger.exception("Không ghi được job_state.json cho job %s", job.get("id"))
        raise


def _save_job_best_effort(job: Dict) -> None:
    """Save dùng trong progress callback / except-handler của pipeline:
    log warning khi lỗi, không raise (pipeline đang render không được chết vì đĩa)."""
    try:
        save_job(job)
    except Exception as exc:
        logger.warning("save_job (best-effort) thất bại cho job %s: %s", job.get("id"), exc)


def _save_job_or_http500(job: Dict) -> None:
    """Save dùng trong endpoint: lỗi ghi đĩa -> HTTP 500 có message rõ ràng."""
    try:
        save_job(job)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Không ghi được trạng thái job: {exc}")


_JOBS_LOADED_ONCE = False


def load_all_persisted_jobs(force: bool = False):
    """Khôi phục jobs đã lưu vào bộ nhớ — CHỈ 1 lần (startup/lần đầu cần),
    trừ khi force=True. KHÔNG ghi đè job đang có trong bộ nhớ (đặc biệt job
    đang running — pipeline mutate JOBS liên tục, audit §1.5). Job nào trên đĩa
    còn 'running' sau restart được đánh dấu interrupted/error rõ ràng thay vì
    để SSE poll vô hạn."""
    global _JOBS_LOADED_ONCE
    if _JOBS_LOADED_ONCE and not force:
        return
    _JOBS_LOADED_ONCE = True
    for item in WORKSPACE.iterdir():
        if item.is_dir() and not item.name.startswith("_") and item.name not in PROTECTED_WORKSPACE_NAMES:
            state_file = item / "job_state.json"
            if state_file.exists():
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    _normalize_scene_keys(data)
                    with JOBS_LOCK:
                        if item.name in JOBS:
                            continue  # bộ nhớ luôn mới hơn đĩa trong cùng 1 tiến trình
                        if data.get("status") == "running":
                            # Không còn pipeline nào chạy sau restart.
                            data["status"] = "error"
                            data["error"] = "interrupted"
                            data.setdefault("logs", []).append(
                                "[Hệ thống] Job bị gián đoạn do server khởi động lại "
                                "(interrupted). Bấm Resume để chạy tiếp từ cảnh chưa xong.")
                        JOBS[item.name] = data
                except Exception as exc:
                    logger.warning("Không load được job %s: %s", item.name, exc)


def get_or_restore_job(job_id: str) -> Optional[Dict]:
    # Ưu tiên trạng thái trong bộ nhớ: pipeline đang chạy mutate JOBS liên tục,
    # load lại từ đĩa sẽ clobber tiến độ/log chưa save (audit §1.5).
    existing = JOBS.get(job_id)
    if existing is not None:
        return existing
    try:
        job_dir = get_safe_job_dir(job_id)
    except Exception:
        return None
    state_file = job_dir / "job_state.json"
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            _normalize_scene_keys(data)
            if data.get("req") and isinstance(data["req"], dict):
                data["req"] = MovieRequest(**data["req"])
            if data.get("status") == "running":
                data["status"] = "error"
                data["error"] = "interrupted"
                data.setdefault("logs", []).append(
                    "[Hệ thống] Job bị gián đoạn do server khởi động lại "
                    "(interrupted). Bấm Resume để chạy tiếp từ cảnh chưa xong.")
            with JOBS_LOCK:
                # Re-check sau khi đọc đĩa: pipeline có thể vừa tạo job này.
                if job_id not in JOBS:
                    JOBS[job_id] = data
                return JOBS[job_id]
        except Exception as e:
            logger.warning("Không restore được job %s: %s", job_id, e)
    return JOBS.get(job_id)


def _is_job_running(job: Optional[Dict]) -> bool:
    """Job đang chạy thật hay không (status running hoặc còn task pipeline sống)."""
    if not job:
        return False
    if job.get("status") == "running":
        return True
    task = JOB_TASKS.get(job["id"])
    return task is not None and not task.done()


def _spawn_job_task(job_id: str, coro) -> "asyncio.Task":
    """Spawn pipeline task và lưu reference để cancel thật (audit §1.6).
    Thay BackgroundTasks ở các endpoint tạo phim: task vẫn chạy sau khi trả
    response, nhưng server giữ được handle để cancel + log exception khi task
    kết thúc bất thường."""
    task = asyncio.create_task(coro)
    with JOBS_LOCK:
        JOB_TASKS[job_id] = task

    def _done(t: "asyncio.Task", _jid=job_id):
        with JOBS_LOCK:
            if JOB_TASKS.get(_jid) is t:
                JOB_TASKS.pop(_jid, None)
        if t.cancelled():
            logger.info("Pipeline job %s đã bị cancel.", _jid)
        elif t.exception() is not None:
            logger.exception("Pipeline job %s lỗi chưa được xử lý.", _jid)

    task.add_done_callback(_done)
    return task


# ── Background Pipeline ────────────────────────────────────────────────────
async def film_pipeline(job: Dict, req: MovieRequest):
    try:
        job_id = job["id"]
        if not MUSE_API_KEY:
            # Không key -> config error rõ ràng trên job, không gọi gateway bằng key giả.
            job["status"] = "error"
            job_log(job, "❌ Muse API key chưa được cấu hình (MUSE2API_KEY). Không thể render.")
            _save_job_best_effort(job)
            return
        if job.get("cancelled") or job.get("status") == "cancelled":
            job_log(job, "🛑 Job đã bị hủy trước khi bắt đầu.")
            _save_job_best_effort(job)
            return
        output_dir = WORKSPACE / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "audio").mkdir(exist_ok=True)
        (output_dir / "scenes").mkdir(exist_ok=True)
        (output_dir / "final").mkdir(exist_ok=True)

        raw_concurrent = req.max_concurrent if (req.max_concurrent and req.max_concurrent > 0) else MAX_CONCURRENT
        concurrent = min(raw_concurrent, 12)
        total = len(req.scenes)
        if req.chain_mode == "sequential":
            job_log(job, f"🔗 Bắt đầu sản xuất {total} cảnh theo chế độ Nối tiếp tuần tự (Sequential Chaining: Kế thừa Last Frame liên tục qua từng cảnh)...")
        else:
            job_log(job, f"⚡ Bắt đầu xử lý {total} cảnh song song đa luồng (concurrent={concurrent}, phân bổ đồng thời qua các tài khoản Muse)...")

        global_size = req.aspect_ratio or "16:9"
        # Khởi tạo trạng thái từng cảnh
        for s in req.scenes:
            if not s.size:
                s.size = global_size
            _set_scene(job, s.index, {
                "status": "queued",
                "msg": "⌛ Đang chờ...",
                "prompt": s.prompt,
                "voiceover": s.voiceover,
                "video_url": None,
                "last_frame_url": None,
                "error": None,
            })

        job["progress"] = 5

        def progress_cb(idx: int, msg: str, extra: Optional[Dict] = None):
            st = "running"
            if "✅" in msg or "Hoàn thành" in msg:
                st = "done"
            elif "❌" in msg or "Thất bại" in msg:
                st = "error"
            sc = _scene(job, idx)
            sc["status"] = st
            sc["msg"] = msg
            if extra:
                sc.update(extra)
            _set_scene(job, idx, sc)
            done = sum(1 for v in job["scenes"].values() if v.get("status") == "done")
            job["progress"] = min(74, 5 + int(done / max(total, 1) * 70))
            _save_job_best_effort(job)

        scenes_dicts = [s.model_dump() for s in req.scenes]

        # ── Batch render hàng loạt ──
        results = await run_batch(
            scenes=scenes_dicts,
            base_url=MUSE_API_URL,
            api_key=MUSE_API_KEY,
            output_dir=str(output_dir),
            voice=req.voice,
            max_concurrent=concurrent,
            progress_cb=progress_cb,
            chain_mode=req.chain_mode or "none",
            chain_fallback_mode=req.chain_fallback_mode or "independent",
            style_locks=req.style_locks,
            start_scene_idx=req.start_scene_idx or 1,
            voice_mode=req.voice_mode or "tts",
        )

        job["results"] = results

        # Kiểm tra nếu job đã bị người dùng hủy.
        # TODO(batch_engine, ngoài phạm vi refactor app.py): run_batch/process_scene
        # chưa kiểm tra job["cancelled"] định kỳ nên batch đang chạy vẫn chạy hết;
        # api_cancel_job chỉ dừng được pipeline ở điểm await tiếp theo (task.cancel).
        # Muốn dừng render sâu hơn cần truyền cờ cancelled vào batch_engine.
        if job.get("cancelled") or job.get("status") == "cancelled":
            job_log(job, "🛑 Job đã bị hủy bởi người dùng.")
            _save_job_best_effort(job)
            return

        # Cập nhật trạng thái từng cảnh sau khi xong
        ok_count = 0
        job["failed_scenes"] = []
        for r in results:
            idx = r["index"]
            if r["status"] == "done":
                _scene(job, idx).update({
                    "status": "done",
                    "msg": "✅ Hoàn thành",
                    "video_url": f"/api/jobs/{job_id}/scenes/{idx}/video",
                    "last_frame_url": r.get("last_frame_url"),
                    "error": None,
                })
                ok_count += 1
            else:
                _scene(job, idx).update({
                    "status": "error",
                    "msg": f"❌ {r.get('error', '')}",
                    "error": r.get("error", ""),
                })
                job["failed_scenes"].append(idx)

        # Kiểm định tính toàn vẹn của tất cả các cảnh (Scene Audit)
        audit = audit_movie_scenes(scenes_dicts, results)
        job["audit"] = audit
        _save_job_best_effort(job)

        job_log(job, f"📊 Kết quả render: {audit['done_count']}/{audit['total']} cảnh thành công ({audit['failed_count']} lỗi, {audit['pending_count']} chờ)")
        job["progress"] = 75
        _save_job_best_effort(job)

        if ok_count == 0:
            job["status"] = "error"
            job_log(job, "❌ Không có cảnh nào thành công!")
            _save_job_best_effort(job)
            return

        # QUY TẮC CỐT LÕI: KHÔNG TỰ ĐỘNG BÁO THÀNH CÔNG VÀ KHÔNG TỰ XUẤT PHIM THIẾU CẢNH!
        # Nếu có cảnh lỗi hoặc thiếu, dừng lại ở trạng thái audit_pending để người dùng quyết định:
        # 1. Thử lại các cảnh lỗi
        # 2. Xuất bản nháp (Draft) bỏ qua cảnh thiếu
        # 3. Hủy xuất phim
        if not audit["is_complete"] and not req.allow_partial:
            job["status"] = "audit_pending"
            job_log(job, f"⚠️ Cảnh báo kiểm định: Phát hiện {audit['failed_count']} cảnh lỗi / thiếu. Đã tạm dừng xuất phim để bạn xem xét và quyết định!")
            _save_job_best_effort(job)
            return

        # Kiểm tra hủy lần nữa trước khi vào hậu kỳ (cancel có thể đến trong lúc render).
        if job.get("cancelled") or job.get("status") == "cancelled":
            job_log(job, "🛑 Job đã bị hủy bởi người dùng.")
            _save_job_best_effort(job)
            return

        # ── Hậu kỳ ──
        try:
            job_log(job, "🎞️ Đang hậu kỳ: ghép video, lồng tiếng, hòa âm nhạc nền, xử lý phụ đề...")
            job["progress"] = 80
            _save_job_best_effort(job)

            if req.export_mode == "batch_clips":
                # Xuất từng clip riêng biệt (mỗi cảnh là 1 video độc lập)
                clips = []
                for r in results:
                    if r["status"] != "done":
                        continue
                    idx = r["index"]
                    clip_out = str(output_dir / "final" / f"clip_{idx:03d}.mp4")
                    await asyncio.to_thread(
                        compose_scene_video,
                        scene_video=r["video_path"],
                        scene_audio=r["audio_path"],
                        output=clip_out,
                        audio_duration=float(r["audio_duration"]),
                        aspect_ratio=req.aspect_ratio or "16:9",
                    )
                    clips.append(f"/api/jobs/{job_id}/clips/clip_{idx:03d}.mp4")
                job["clips"] = clips
                job["progress"] = 100
                job["status"] = "done"
                job_log(job, f"✅ Đã xuất {len(clips)} clip riêng lẻ!")

            else:
                # Xuất 1 phim dài hoàn chỉnh
                final_path = _unwrap_final_path(
                    await asyncio.to_thread(
                        compose_full_movie,
                        results=results,
                        output_dir=str(output_dir),
                        project_title=req.title.replace(" ", "_"),
                        font_size=req.font_size,
                        bg_music_path=req.bg_music_path,
                        music_volume=req.music_volume if req.music_volume is not None else 0.25,
                        voice_volume=req.voice_volume if req.voice_volume is not None else 1.0,
                        subtitle_mode=req.subtitle_mode or "burn_in",
                        quality=req.quality or "720p",
                        transition=req.transition or "none",
                        aspect_ratio=req.aspect_ratio or "16:9",
                        allow_partial=bool(req.allow_partial),
                    )
                )
                rel = Path(final_path).name
                job["final_video"] = f"/api/jobs/{job_id}/video/{rel}"
                srt_path = output_dir / "final" / f"{req.title.replace(' ', '_')}.srt"
                if srt_path.exists():
                    job["srt_file"] = f"/api/jobs/{job_id}/subtitles"
                job["progress"] = 100
                job["status"] = "done"
                if not audit["is_complete"]:
                    job_log(job, f"⚠️ Đã xuất bản nháp (Draft - thiếu {audit['failed_count']} cảnh)! → {rel}")
                else:
                    job_log(job, f"🎉 Xuất phim thành công 100%! → {rel}")
            _save_job_best_effort(job)


        except Exception as e:
            job["status"] = "error"
            job_log(job, f"❌ Lỗi hậu kỳ: {e}")
            _save_job_best_effort(job)

    except Exception as e:
        job["status"] = "error"
        job_log(job, f"❌ Lỗi xử lý pipeline: {e}")
        _save_job_best_effort(job)


# ── API Routes ─────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(
        "static/index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@app.post("/api/movies")
async def create_movie(req: MovieRequest):
    """Tạo job mới. Trả về job_id để theo dõi."""
    if not req.scenes:
        return JSONResponse({"error": "Không có cảnh nào!"}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    job = new_job(job_id, len(req.scenes), req=req)
    _save_job_or_http500(job)
    job_log(job, f"📋 Dự án: '{req.title}' — {len(req.scenes)} cảnh — giọng: {req.voice}")
    _save_job_or_http500(job)
    # Spawn qua _spawn_job_task (giữ reference trong JOB_TASKS để cancel thật);
    # nếu thiếu MUSE_API_KEY thì film_pipeline đánh dấu job error rõ ràng.
    _spawn_job_task(job_id, film_pipeline(job, req))

    return {"job_id": job_id, "message": "Đã bắt đầu sản xuất phim hàng loạt"}


async def retry_failed_pipeline(job: Dict, auto_fix: bool = True):
    """Pipeline render lại CHỈ CÁC CẢNH BỊ LỖI rồi tự động ghép lại phim hoàn chỉnh."""
    # Cờ is_retrying đã được endpoint /retry-failed đặt NGAY trong request (dưới
    # JOBS_LOCK) trước khi spawn task — pipeline không kiểm tra lại ở đây: nếu
    # return sớm thì finally không chạy và cờ kẹt True vĩnh viễn (audit §1.6).
    job["is_retrying"] = True

    try:
        req: MovieRequest = job.get("req")
        if not req:
            job_log(job, "❌ Không tìm thấy thông tin cấu hình kịch bản gốc.")
            return
        if not MUSE_API_KEY:
            job["status"] = "error"
            job_log(job, "❌ Muse API key chưa được cấu hình (MUSE2API_KEY). Không thể render lại.")
            _save_job_best_effort(job)
            return

        failed_indices = list(job.get("failed_scenes", []))
        if not failed_indices:
            job_log(job, "ℹ️ Không có cảnh nào bị lỗi để render lại.")
            return

        output_dir = WORKSPACE / job["id"]
        job["status"] = "running"
        job_log(job, f"🔄 Bắt đầu xử lý {len(failed_indices)} cảnh bị lỗi: {failed_indices}...")

        # Đặt trạng thái các cảnh lỗi thành đang chạy lại
        for idx in failed_indices:
            _set_scene(job, idx, {"status": "running", "msg": "🔄 Đang chuẩn bị thử lại..."})

        failed_scenes = [s for s in req.scenes if s.index in failed_indices]

        if auto_fix:
            from batch_engine import heal_prompt_with_ai
            for s in failed_scenes:
                err_msg = _scene(job, s.index).get("msg", "")
                job_log(job, f"🪄 [Cảnh {s.index}] Đang tự động tối ưu Prompt theo phản hồi Muse AI...")
                healed = await heal_prompt_with_ai(s.prompt, err_msg, MUSE_API_URL, MUSE_API_KEY)
                if healed and healed != s.prompt:
                    job_log(job, f"✨ [Cảnh {s.index}] Đã chuyển đổi prompt sang phiên bản an toàn!")
                    s.prompt = healed

        scenes_dicts = [s.model_dump() for s in failed_scenes]

        def retry_progress_cb(idx: int, msg: str):
            _set_scene(job, idx, {"status": "running", "msg": msg})

        raw_concurrent = req.max_concurrent if (req and req.max_concurrent and req.max_concurrent > 0) else MAX_CONCURRENT
        concurrent = min(raw_concurrent, 12)
        new_results = await run_batch(
            scenes=scenes_dicts,
            base_url=MUSE_API_URL,
            api_key=MUSE_API_KEY,
            output_dir=str(output_dir),
            voice=req.voice,
            max_concurrent=concurrent,
            progress_cb=retry_progress_cb,
            voice_mode=req.voice_mode or "tts",
        )

        # Cập nhật kết quả vào danh sách results chung
        current_results = {r["index"]: r for r in job.get("results", [])}
        for nr in new_results:
            idx = nr["index"]
            current_results[idx] = nr
            if nr["status"] == "done":
                _set_scene(job, idx, {"status": "done", "msg": "✅ Hoàn thành (Sau khi thử lại)"})
                if idx in job["failed_scenes"]:
                    job["failed_scenes"].remove(idx)
            else:
                _set_scene(job, idx, {"status": "error", "msg": f"❌ {nr.get('error', '')}"})

        all_results = sorted(list(current_results.values()), key=lambda x: x["index"])
        job["results"] = all_results

        # Kiểm định lại toàn bộ các cảnh sau khi thử lại
        all_scene_dicts = [s.model_dump() for s in req.scenes]
        audit = audit_movie_scenes(all_scene_dicts, all_results)
        job["audit"] = audit
        _save_job_best_effort(job)

        if not audit["is_complete"]:
            job["status"] = "audit_pending"
            job_log(job, f"⚠️ Sau khi thử lại: Vẫn còn {audit['failed_count']} cảnh lỗi ({audit['done_count']}/{audit['total']} hoàn tất). Trạng thái: Chờ kiểm định.")
            _save_job_best_effort(job)
            return

        # Hậu kỳ ghép lại phim hoàn chỉnh
        try:
            job_log(job, "🎞️ Đang tự động ghép lại phim với các cảnh vừa tạo lại...")
            final_path = _unwrap_final_path(
                await asyncio.to_thread(
                    compose_full_movie,
                    results=all_results,
                    output_dir=str(output_dir),
                    project_title=req.title.replace(" ", "_"),
                    font_size=req.font_size,
                    bg_music_path=req.bg_music_path,
                    music_volume=req.music_volume if req.music_volume is not None else 0.25,
                    voice_volume=req.voice_volume if req.voice_volume is not None else 1.0,
                    subtitle_mode=req.subtitle_mode or "burn_in",
                    quality=req.quality or "720p",
                    transition=req.transition or "none",
                    aspect_ratio=req.aspect_ratio or "16:9",
                    allow_partial=False,
                )
            )
            rel = Path(final_path).name
            job["final_video"] = f"/api/jobs/{job['id']}/video/{rel}"
            srt_path = output_dir / "final" / f"{req.title.replace(' ', '_')}.srt"
            if srt_path.exists():
                job["srt_file"] = f"/api/jobs/{job['id']}/subtitles"
            job["progress"] = 100
            job["status"] = "done"
            job_log(job, f"🎉 Đã tái xuất phim hoàn chỉnh 100%! → {rel}")
            _save_job_best_effort(job)
        except Exception as e:
            job["status"] = "error"
            job_log(job, f"❌ Lỗi hậu kỳ sau khi render lại: {e}")
            _save_job_best_effort(job)
    finally:
        job["is_retrying"] = False



@app.post("/api/jobs/{job_id}/retry-failed")
async def retry_failed_scenes(job_id: str, auto_fix: bool = True):
    """API kích hoạt render lại các cảnh bị lỗi, mặc định tự động sửa prompt nếu bị vi phạm."""
    job = get_or_restore_job(job_id)
    if not job:
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    if not job.get("failed_scenes"):
        return JSONResponse({"message": "Không có cảnh nào bị lỗi", "failed_count": 0})
    with JOBS_LOCK:
        if job.get("is_retrying"):
            return JSONResponse({"error": "Tiến trình render lại đang chạy trong nền, vui lòng đợi hoàn tất trước khi bấm tiếp!"}, status_code=400)
        # Đặt cờ NGAY trong request, dưới lock, TRƯỚC khi trả response (audit §1.6):
        # 2 request liên tiếp không thể cùng qua gate này nữa.
        job["is_retrying"] = True
    failed = list(job["failed_scenes"])
    _spawn_job_task(job_id, retry_failed_pipeline(job, auto_fix))
    _save_job_best_effort(job)
    return {
        "message": f"Đang bắt đầu render lại {len(failed)} cảnh lỗi (Auto-Fix: {auto_fix})",
        "failed_scenes": failed
    }


class UpdateScenePromptRequest(BaseModel):
    scene_index: int
    new_prompt: str = Field(max_length=5000)


@app.post("/api/jobs/{job_id}/scenes/update-prompt")
def update_scene_prompt(job_id: str, payload: UpdateScenePromptRequest):
    """Cập nhật trực tiếp prompt mới cho 1 cảnh trong job."""
    job = get_or_restore_job(job_id)
    if not job or not job.get("req"):
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    req: MovieRequest = job["req"]
    for s in req.scenes:
        if s.index == payload.scene_index:
            s.prompt = payload.new_prompt
            sc = _scene(job, payload.scene_index)
            if sc:
                sc["prompt"] = payload.new_prompt
            job_log(job, f"✏️ [Cảnh {payload.scene_index}] Người dùng đã cập nhật Prompt mới: {payload.new_prompt[:50]}...")
            # audit §1.4: trước đây chỉ gán trong bộ nhớ, load lại từ đĩa là mất.
            _save_job_or_http500(job)
            return {"success": True, "scene_index": payload.scene_index, "prompt": payload.new_prompt}
    return JSONResponse({"error": f"Không tìm thấy cảnh {payload.scene_index}"}, status_code=404)


class FixPromptRequest(BaseModel):
    prompt: str = Field(max_length=5000)
    error: str = Field(default="", max_length=5000)
    job_id: Optional[str] = None
    scene_index: Optional[int] = None


@app.post("/api/ai/fix-prompt")
async def api_fix_prompt(req: FixPromptRequest):
    """Tự động phân tích lỗi từ chối của Muse AI và viết lại prompt an toàn chuẩn Hollywood."""
    from batch_engine import heal_prompt_with_ai
    fixed = await heal_prompt_with_ai(
        prompt=req.prompt,
        error_msg=req.error,
        base_url=MUSE_API_URL,
        api_key=MUSE_API_KEY
    )
    if req.job_id and req.scene_index is not None:
        job = get_or_restore_job(req.job_id)
        if job and job.get("req"):
            for s in job["req"].scenes:
                if s.index == req.scene_index:
                    s.prompt = fixed
                    break
            if str(req.scene_index) in job.get("scenes", {}):
                _scene(job, req.scene_index)["prompt"] = fixed
            _save_job_best_effort(job)
    return {
        "original_prompt": req.prompt,
        "fixed_prompt": fixed,
        "message": "Đã tự động loại bỏ yếu tố bạo lực/nhạy cảm và chuyển thành cảnh quay điện ảnh an toàn!"
    }


class RetrySingleSceneRequest(BaseModel):
    prompt: Optional[str] = Field(default=None, max_length=5000)
    voiceover: Optional[str] = Field(default=None, max_length=5000)


# ── Timeline Editor: reorder / rerender helpers ────────────────────────────
def _scene_file_relpaths(idx: int) -> Dict[str, str]:
    """Đường dẫn tương đối (trong thư mục job) của mọi file gắn với 1 cảnh."""
    n = f"{idx:03d}"
    return {
        "clip": f"final/clip_{n}.mp4",
        "video": f"scenes/scene_{n}.mp4",
        "raw": f"scenes/scene_{n}_raw.mp4",
        "audio": f"audio/scene_{n}.mp3",
        "last_frame": f"scenes/scene_{n}_last_frame.jpg",
        "prev_clip": f"previous/clip_{n}.mp4",
        "prev_video": f"previous/scene_{n}.mp4",
        "prev_audio": f"previous/scene_{n}.mp3",
        "prev_last_frame": f"previous/scene_{n}_last_frame.jpg",
    }


def _rename_scene_files(job_id: str, mapping: Dict[int, int]) -> None:
    """Đổi tên file của các cảnh theo mapping old_idx -> new_idx.
    2 pha qua thư mục staging để không va chạm khi hoán đổi (vd 1<->2).
    File nào không tồn tại thì bỏ qua; lỗi 1 file chỉ log, không dừng cả reorder."""
    job_dir = get_safe_job_dir(job_id)
    staging = job_dir / ".reorder_tmp"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        staged: Dict[str, Path] = {}
        for old_idx, new_idx in mapping.items():
            if old_idx == new_idx:
                continue
            for role, rel in _scene_file_relpaths(old_idx).items():
                src = job_dir / rel
                if src.is_file():
                    dst_stage = staging / f"{role}_{old_idx}.tmp"
                    try:
                        shutil.move(str(src), str(dst_stage))
                        staged[f"{role}_{old_idx}"] = dst_stage
                    except Exception as exc:
                        logger.warning("Reorder: không dời được %s: %s", src, exc)
        for key, stage_path in staged.items():
            role, old_idx_s = key.rsplit("_", 1)
            new_idx = mapping[int(old_idx_s)]
            dst = job_dir / _scene_file_relpaths(new_idx)[role]
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stage_path), str(dst))
            except Exception as exc:
                logger.warning("Reorder: không đặt lại được %s -> %s: %s", stage_path, dst, exc)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _previous_clip_dir(job_id: str) -> Path:
    d = get_safe_job_dir(job_id) / "previous"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_previous_clip(job: Dict, idx: int) -> Optional[Dict]:
    """Sao chép clip hiện tại của cảnh sang previous/ (giữ tối đa 1 bản cũ
    để khỏi phình đĩa — bản cũ hơn nữa bị xóa). Trả về metadata
    previous_clip, hoặc None nếu cảnh chưa có file nào."""
    job_id = job["id"]
    job_dir = get_safe_job_dir(job_id)
    prev_dir = _previous_clip_dir(job_id)
    for role, rel in _scene_file_relpaths(idx).items():
        if not role.startswith("prev_"):
            continue
        p = job_dir / rel
        if p.is_file():
            try:
                p.unlink()
            except Exception:
                pass
    copied = []
    for role, rel in _scene_file_relpaths(idx).items():
        if role.startswith("prev_"):
            continue
        src = job_dir / rel
        if src.is_file():
            try:
                shutil.copy2(str(src), str(prev_dir / src.name))
                copied.append(src.name)
            except Exception as exc:
                logger.warning("Rerender: không sao chép được %s: %s", src, exc)
    if not copied:
        return None
    meta = {
        "prompt": _scene(job, idx).get("prompt", ""),
        "created_at": time.time(),
        "has_video": any(n.endswith(".mp4") for n in copied),
        "files": copied,
    }
    _scene(job, idx)["previous_clip"] = meta
    return meta


def _delete_previous_clip(job: Dict, idx: int) -> None:
    """Xóa toàn bộ file bản cũ + metadata previous_clip của 1 cảnh."""
    job_dir = get_safe_job_dir(job["id"])
    for role, rel in _scene_file_relpaths(idx).items():
        if not role.startswith("prev_"):
            continue
        p = job_dir / rel
        if p.is_file():
            try:
                p.unlink()
            except Exception:
                pass
    _scene(job, idx).pop("previous_clip", None)


def _swap_previous_clip(job: Dict, idx: int) -> Optional[Dict]:
    """Hoán đổi clip hiện tại <-> bản previous. Trả về metadata previous_clip
    MỚI sau hoán đổi (chính là bản vừa bị thay ra để vẫn đổi lại được),
    hoặc None nếu trước đó không có clip hiện tại (sau restore không còn
    bản cũ nào để quay lại)."""
    job_id = job["id"]
    job_dir = get_safe_job_dir(job_id)
    prev_dir = _previous_clip_dir(job_id)
    staging = job_dir / ".restore_tmp"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        staged = []
        for role, rel in _scene_file_relpaths(idx).items():
            if role.startswith("prev_"):
                continue
            src = job_dir / rel
            if src.is_file():
                shutil.move(str(src), str(staging / src.name))
                staged.append(src.name)
        for role, rel in _scene_file_relpaths(idx).items():
            if not role.startswith("prev_"):
                continue
            src = job_dir / rel
            if src.is_file():
                dst = job_dir / _scene_file_relpaths(idx)[role[5:]]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        for name in staged:
            shutil.move(str(staging / name), str(prev_dir / name))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    sc = _scene(job, idx)
    if not staged:
        sc.pop("previous_clip", None)
        return None
    meta = {
        "prompt": sc.get("prompt", ""),
        "created_at": time.time(),
        "has_video": any(n.endswith(".mp4") for n in staged),
        "files": staged,
    }
    sc["previous_clip"] = meta
    return meta


def _clear_rerendering(job: Dict, idx) -> None:
    """Xóa cờ 'đang render lại' của 1 cảnh (gọi ở mọi đường ra của pipeline)."""
    try:
        _scene(job, idx).pop("rerendering", None)
    except Exception:
        pass


async def single_scene_pipeline(job: Dict, target_scene: SceneItem, recompose_final: bool = True):
    """Pipeline render lại DUY NHẤT một cảnh độc lập.
    recompose_final=False (dùng cho rerender từng shot ở Timeline Editor):
    không tự ghép lại cả phim — chỉ đánh dấu job['final_stale']=True để người
    dùng chủ động bấm Ghép lại khi đã chốt các shot."""
    idx = target_scene.index
    job_id = job["id"]
    output_dir = WORKSPACE / job_id
    req: MovieRequest = job["req"]

    job_log(job, f"🔄 [Cảnh {idx}] Bắt đầu render lại cảnh độc lập...")
    if not MUSE_API_KEY:
        _scene(job, idx).update({
            "status": "error",
            "msg": "❌ Muse API key chưa được cấu hình (MUSE2API_KEY).",
            "error": "Muse API key chưa được cấu hình (MUSE2API_KEY).",
        })
        _clear_rerendering(job, idx)
        job_log(job, "❌ Muse API key chưa được cấu hình (MUSE2API_KEY). Không thể render lại cảnh.")
        _save_job_best_effort(job)
        return

    def single_cb(i: int, msg: str, extra: Optional[Dict] = None):
        st = "running"
        if "✅" in msg or "Hoàn thành" in msg:
            st = "done"
        elif "❌" in msg or "Thất bại" in msg:
            st = "error"
        sc = _scene(job, idx)
        sc["status"] = st
        sc["msg"] = msg
        if extra:
            sc.update(extra)
        _set_scene(job, idx, sc)
        _save_job_best_effort(job)

    try:
        from batch_engine import process_scene
        sem = asyncio.Semaphore(1)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            res = await process_scene(
                client=client,
                scene=target_scene.model_dump(),
                sem=sem,
                base_url=MUSE_API_URL,
                api_key=MUSE_API_KEY,
                output_dir=str(output_dir),
                voice=req.voice,
                progress_cb=single_cb,
                voice_mode=req.voice_mode or "tts",
            )

        current_results = {r["index"]: r for r in job.get("results", [])}
        current_results[idx] = res
        all_results = sorted(list(current_results.values()), key=lambda x: x["index"])
        job["results"] = all_results

        if res.get("status") == "done":
            _scene(job, idx).update({
                "status": "done",
                "msg": "✅ Hoàn thành (Đã tạo lại)",
                "video_url": f"/api/jobs/{job_id}/scenes/{idx}/video",
                "last_frame_url": res.get("last_frame_url"),
                "error": None,
            })
            if idx in job.get("failed_scenes", []):
                job["failed_scenes"].remove(idx)
            job_log(job, f"🎉 [Cảnh {idx}] Render lại thành công!")

            # Ghép clip hoặc tái xuất full movie
            if req.export_mode == "batch_clips":
                try:
                    from composer import compose_scene_video
                    clip_out = str(output_dir / "final" / f"clip_{idx:03d}.mp4")
                    await asyncio.to_thread(
                        compose_scene_video,
                        scene_video=res["video_path"],
                        scene_audio=res["audio_path"],
                        output=clip_out,
                        audio_duration=float(res["audio_duration"]),
                        aspect_ratio=req.aspect_ratio or "16:9",
                    )
                    clip_url = f"/api/jobs/{job_id}/clips/clip_{idx:03d}.mp4"
                    if "clips" not in job or not job["clips"]:
                        job["clips"] = []
                    if clip_url not in job["clips"]:
                        job["clips"].append(clip_url)
                except Exception as ce:
                    job_log(job, f"⚠️ Lỗi ghép clip cảnh {idx}: {ce}")
            else:
                scenes_dicts = [s.model_dump() for s in req.scenes]
                audit = audit_movie_scenes(scenes_dicts, all_results)
                job["audit"] = audit
                if audit["is_complete"] and not recompose_final:
                    # Rerender từng shot: KHÔNG tự ghép lại cả phim. Giữ nguyên
                    # final cũ nhưng đánh dấu đã cũ để UI hiện "cần ghép lại".
                    job["final_stale"] = True
                    job_log(job, f"ℹ️ [Cảnh {idx}] Render lại xong. Phim đã ghép trước đó đã cũ — bấm Ghép lại ở panel Monitor để xuất bản mới.")
                elif audit["is_complete"]:
                    job_log(job, f"🎞️ Toàn bộ {audit['total']} cảnh đã hoàn tất! Đang ghép lại phim hoàn chỉnh...")
                    try:
                        final_path = _unwrap_final_path(
                            await asyncio.to_thread(
                                compose_full_movie,
                                results=all_results,
                                output_dir=str(output_dir),
                                project_title=req.title.replace(" ", "_"),
                                font_size=req.font_size,
                                bg_music_path=req.bg_music_path,
                                music_volume=req.music_volume if req.music_volume is not None else 0.25,
                                voice_volume=req.voice_volume if req.voice_volume is not None else 1.0,
                                subtitle_mode=req.subtitle_mode or "burn_in",
                                quality=req.quality or "720p",
                                transition=req.transition or "none",
                                aspect_ratio=req.aspect_ratio or "16:9",
                                allow_partial=False,
                            )
                        )
                        rel = Path(final_path).name
                        job["final_video"] = f"/api/jobs/{job_id}/video/{rel}"
                        srt_path = output_dir / "final" / f"{req.title.replace(' ', '_')}.srt"
                        if srt_path.exists():
                            job["srt_file"] = f"/api/jobs/{job_id}/subtitles"
                        job["progress"] = 100
                        job["status"] = "done"
                        job_log(job, f"🎉 Đã tái xuất phim hoàn chỉnh 100%! → {rel}")
                    except Exception as e:
                        job_log(job, f"⚠️ Lỗi ghép phim: {e}")
                else:
                    job["status"] = "audit_pending"
                    job_log(job, f"ℹ️ [Cảnh {idx}] Đã tạo lại xong. Hiện có {audit['done_count']}/{audit['total']} cảnh hoàn tất.")

        else:
            _scene(job, idx).update({
                "status": "error",
                "msg": f"❌ {res.get('error', 'Lỗi không xác định')}",
                "error": res.get("error", ""),
            })
            if idx not in job.get("failed_scenes", []):
                job.setdefault("failed_scenes", []).append(idx)
            job_log(job, f"❌ [Cảnh {idx}] Thử lại thất bại: {res.get('error')}")

        _clear_rerendering(job, idx)
        _save_job_best_effort(job)

    except Exception as exc:
        _scene(job, idx).update({
            "status": "error",
            "msg": f"❌ Lỗi: {exc}",
            "error": str(exc),
        })
        _clear_rerendering(job, idx)
        job_log(job, f"❌ [Cảnh {idx}] Exception: {exc}")
        _save_job_best_effort(job)


@app.post("/api/jobs/{job_id}/scenes/{scene_idx}/retry")
async def retry_single_scene_endpoint(
    job_id: str,
    scene_idx: int,
    payload: Optional[RetrySingleSceneRequest] = None,
):
    """Render lại DUY NHẤT một cảnh với prompt đã chỉnh sửa."""
    job = get_or_restore_job(job_id)
    if not job or not job.get("req"):
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)

    req: MovieRequest = job["req"]
    target_scene = None
    for s in req.scenes:
        if s.index == scene_idx:
            target_scene = s
            break
    if not target_scene:
        return JSONResponse({"error": f"Không tìm thấy cảnh {scene_idx}"}, status_code=404)

    sc_entry = _scene(job, scene_idx)
    if payload and payload.prompt:
        target_scene.prompt = payload.prompt
        sc_entry["prompt"] = payload.prompt
    if payload and payload.voiceover is not None:
        target_scene.voiceover = payload.voiceover
        sc_entry["voiceover"] = payload.voiceover

    sc_entry["status"] = "running"
    sc_entry["msg"] = "🔄 Đang chuẩn bị render lại..."
    sc_entry["error"] = None
    _set_scene(job, scene_idx, sc_entry)
    _save_job_or_http500(job)

    _spawn_job_task(job_id, single_scene_pipeline(job, target_scene))
    return {
        "success": True,
        "message": f"Đang bắt đầu render lại Cảnh {scene_idx}",
        "scene_index": scene_idx,
        "prompt": target_scene.prompt,
    }


# ── Timeline Editor: reorder + rerender từng shot ──────────────────────────
class ReorderScenesRequest(BaseModel):
    order: List[int] = Field(min_length=1, max_length=200)


@app.post("/api/jobs/{job_id}/scenes/reorder")
def reorder_job_scenes(job_id: str, payload: ReorderScenesRequest):
    """Đổi thứ tự các cảnh của job (Timeline Editor).
    Body: {"order": [3, 1, 2]} = chỉ số server CŨ theo thứ tự MỚI (hoán vị 1..N).
    Đổi tên file clip/audio/last-frame theo chỉ số mới, đánh dấu final cũ."""
    job = get_or_restore_job(job_id)
    if not job or not job.get("req"):
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    req: MovieRequest = job["req"]
    n = len(req.scenes)
    order = payload.order
    if len(order) != n or sorted(order) != list(range(1, n + 1)):
        return JSONResponse(
            {"error": f"Thứ tự không hợp lệ: phải là hoán vị của 1..{n} (đủ số lượng, không trùng, không thiếu)"},
            status_code=400)
    if _is_job_running(job):
        return JSONResponse(
            {"error": "Job đang render — không đổi được thứ tự lúc này. Hãy đợi render xong hoặc hủy job trước."},
            status_code=409)
    with JOBS_LOCK:
        old_scenes = list(req.scenes)
        new_scenes = [old_scenes[i - 1] for i in order]
        for new_idx, s in enumerate(new_scenes, start=1):
            s.index = new_idx
        req.scenes = new_scenes
        mapping = {old_idx: new_pos for new_pos, old_idx in enumerate(order, start=1)}
        _rename_scene_files(job_id, mapping)
        old_entries = job.get("scenes", {})
        new_entries = StrKeyDict()
        for new_pos, old_idx in enumerate(order, start=1):
            entry = dict(old_entries.get(str(old_idx), old_entries.get(old_idx, {})) or {})
            if entry.get("video_url"):
                entry["video_url"] = f"/api/jobs/{job_id}/scenes/{new_pos}/video"
            if entry.get("last_frame_url"):
                entry["last_frame_url"] = f"/api/jobs/{job_id}/last-frame/{new_pos}"
            new_entries[str(new_pos)] = entry
        job["scenes"] = new_entries
        job_dir = get_safe_job_dir(job_id)
        new_results = []
        for new_pos, old_idx in enumerate(order, start=1):
            for r in job.get("results", []):
                if r.get("index") == old_idx:
                    r = dict(r)
                    r["index"] = new_pos
                    if r.get("video_path"):
                        r["video_path"] = str(job_dir / "scenes" / f"scene_{new_pos:03d}.mp4")
                    if r.get("audio_path"):
                        r["audio_path"] = str(job_dir / "audio" / f"scene_{new_pos:03d}.mp3")
                    if r.get("last_frame_path"):
                        r["last_frame_path"] = str(job_dir / "scenes" / f"scene_{new_pos:03d}_last_frame.jpg")
                    if r.get("last_frame_url"):
                        r["last_frame_url"] = f"/api/jobs/{job_id}/last-frame/{new_pos}"
                    new_results.append(r)
        job["results"] = sorted(new_results, key=lambda x: x["index"])
        if job.get("clips"):
            new_clips = []
            for url in job["clips"]:
                m = re.search(r"clip_(\d+)\.mp4$", url)
                if m and int(m.group(1)) in mapping:
                    new_clips.append(url[:m.start(1)] + f"{mapping[int(m.group(1))]:03d}" + url[m.end(1):])
                else:
                    new_clips.append(url)
            job["clips"] = new_clips
        job["final_stale"] = True
        job_log(job, f"🔀 Đã đổi thứ tự cảnh: {order} (phim đã ghép trước đó cần ghép lại)")
        _save_job_or_http500(job)
    total_duration = sum(s.duration for s in req.scenes)
    return {
        "success": True,
        "order": order,
        "total_duration": total_duration,
        "final_stale": True,
        "scenes": [{"index": s.index, "prompt": s.prompt, "duration": s.duration} for s in req.scenes],
    }


class RerenderSceneRequest(BaseModel):
    """Body rerender 1 shot: mọi field đều tùy chọn. Chỉ field nào gửi lên mới
    đổi — nhân vật/giọng/reference image giữ nguyên trừ khi đổi rõ ràng
    (tôn trọng Character Block / voice lock đã có)."""
    prompt: Optional[str] = Field(default=None, max_length=5000)
    voiceover: Optional[str] = Field(default=None, max_length=5000)
    voice: Optional[str] = Field(default=None, max_length=200)
    duration: Optional[int] = Field(default=None, ge=1, le=60)
    size: Optional[str] = Field(default=None, max_length=10)
    reference_image: Optional[str] = None


@app.post("/api/jobs/{job_id}/scenes/{scene_idx}/rerender")
async def rerender_single_scene_endpoint(
    job_id: str,
    scene_idx: int,
    payload: Optional[RerenderSceneRequest] = None,
):
    """Render lại DUY NHẤT 1 shot (Timeline Editor), tái dùng single_scene_pipeline.
    Trước khi render đè, clip cũ được sao chép sang previous/ (giữ tối đa 1 bản)
    để so sánh A/B / quay lại. Không tự ghép lại cả phim (final_stale=True)."""
    job = get_or_restore_job(job_id)
    if not job or not job.get("req"):
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    req: MovieRequest = job["req"]
    target_scene = next((s for s in req.scenes if s.index == scene_idx), None)
    if not target_scene:
        return JSONResponse({"error": f"Không tìm thấy cảnh {scene_idx}"}, status_code=404)
    if _is_job_running(job) or _scene(job, scene_idx).get("rerendering"):
        return JSONResponse(
            {"error": "Job đang render (hoặc cảnh này đang được render lại) — hãy đợi xong rồi thử lại."},
            status_code=409)
    with JOBS_LOCK:
        prev = _save_previous_clip(job, scene_idx)
        if payload:
            for field, value in payload.model_dump(exclude_unset=True).items():
                if value is not None:
                    setattr(target_scene, field, value)
                    _scene(job, scene_idx)[field] = value
        sc = _scene(job, scene_idx)
        sc["status"] = "running"
        sc["msg"] = "🔄 Đang render lại shot này..."
        sc["error"] = None
        sc["rerendering"] = True
        _save_job_or_http500(job)
    _spawn_job_task(job_id, single_scene_pipeline(job, target_scene, recompose_final=False))
    return {
        "success": True,
        "scene_index": scene_idx,
        "status": "rerendering",
        "prompt": target_scene.prompt,
        "previous_clip": prev,
    }


@app.post("/api/jobs/{job_id}/scenes/{scene_idx}/restore-previous")
def restore_previous_scene_endpoint(job_id: str, scene_idx: int):
    """Quay lại bản cũ: hoán đổi clip hiện tại <-> bản previous (bản mới vừa
    render thành bản cũ để vẫn đổi lại được). Khôi phục cả prompt cũ."""
    job = get_or_restore_job(job_id)
    if not job or not job.get("req"):
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    req: MovieRequest = job["req"]
    target_scene = next((s for s in req.scenes if s.index == scene_idx), None)
    if not target_scene:
        return JSONResponse({"error": f"Không tìm thấy cảnh {scene_idx}"}, status_code=404)
    sc = _scene(job, scene_idx)
    if _is_job_running(job) or sc.get("rerendering"):
        return JSONResponse({"error": "Job đang render — hãy đợi xong rồi thử lại."}, status_code=409)
    prev = sc.get("previous_clip")
    if not prev:
        return JSONResponse({"error": f"Cảnh {scene_idx} không có bản clip cũ để quay lại"}, status_code=404)
    with JOBS_LOCK:
        old_prompt = target_scene.prompt
        prev_prompt = prev.get("prompt", "")
        new_prev = _swap_previous_clip(job, scene_idx)
        target_scene.prompt = prev_prompt
        sc["prompt"] = prev_prompt
        job["final_stale"] = True
        job_log(job, f"↩️ [Cảnh {scene_idx}] Đã quay lại bản clip cũ (prompt cũ đã khôi phục).")
        _save_job_or_http500(job)
    return {
        "success": True,
        "scene_index": scene_idx,
        "prompt": prev_prompt,
        "previous_clip": new_prev,
        "final_stale": True,
    }


@app.post("/api/jobs/{job_id}/scenes/{scene_idx}/discard-previous")
def discard_previous_scene_endpoint(job_id: str, scene_idx: int):
    """Giữ bản mới: xóa file + metadata bản cũ (xác nhận A/B xong)."""
    job = get_or_restore_job(job_id)
    if not job or not job.get("req"):
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    req: MovieRequest = job["req"]
    if not any(s.index == scene_idx for s in req.scenes):
        return JSONResponse({"error": f"Không tìm thấy cảnh {scene_idx}"}, status_code=404)
    if _is_job_running(job) or _scene(job, scene_idx).get("rerendering"):
        return JSONResponse({"error": "Job đang render — hãy đợi xong rồi thử lại."}, status_code=409)
    with JOBS_LOCK:
        if not _scene(job, scene_idx).get("previous_clip"):
            return JSONResponse({"error": f"Cảnh {scene_idx} không có bản clip cũ để xóa"}, status_code=404)
        _delete_previous_clip(job, scene_idx)
        job_log(job, f"✅ [Cảnh {scene_idx}] Đã giữ bản mới, xóa bản clip cũ.")
        _save_job_or_http500(job)
    return {"success": True, "scene_index": scene_idx}


@app.get("/api/jobs/{job_id}/scenes/{scene_idx}/previous-video")
def get_scene_previous_video(job_id: str, scene_idx: int):
    """Stream bản clip CŨ của 1 cảnh (phục vụ so sánh A/B sau rerender)."""
    idx = int(scene_idx)
    clip_path = resolve_safe_job_path(job_id, f"previous/clip_{idx:03d}.mp4")
    if clip_path.exists() and clip_path.is_file():
        return FileResponse(str(clip_path), media_type="video/mp4")
    scene_path = resolve_safe_job_path(job_id, f"previous/scene_{idx:03d}.mp4")
    if scene_path.exists() and scene_path.is_file():
        return FileResponse(str(scene_path), media_type="video/mp4")
    return JSONResponse({"error": "Không có bản clip cũ cho cảnh này"}, status_code=404)


@app.get("/api/jobs/{job_id}/scenes/{scene_idx}/video")
def get_scene_video(job_id: str, scene_idx: int):
    """Stream video của từng cảnh ngay khi cảnh đó vừa render xong an toàn."""
    idx = int(scene_idx)
    # Kiểm tra clip hoàn thiện trước (nếu có)
    clip_path = resolve_safe_job_path(job_id, f"final/clip_{idx:03d}.mp4")
    if clip_path.exists() and clip_path.is_file():
        return FileResponse(str(clip_path), media_type="video/mp4")
    # Kiểm tra raw scene video
    scene_path = resolve_safe_job_path(job_id, f"scenes/scene_{idx:03d}.mp4")
    if scene_path.exists() and scene_path.is_file():
        return FileResponse(str(scene_path), media_type="video/mp4")
    return JSONResponse({"error": "Video cảnh chưa sẵn sàng"}, status_code=404)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """Lấy trạng thái job."""
    job = get_or_restore_job(job_id)
    if not job:
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    return job


@app.get("/api/jobs/{job_id}/sse")
async def job_sse(job_id: str, request: Request):
    """Server-Sent Events: đẩy cập nhật tiến độ real-time.
    - Đọc trạng thái từ bộ nhớ (get_or_restore_job chỉ chạm đĩa khi job chưa có
      trong bộ nhớ) thay vì reload toàn bộ job_state.json mỗi 2s (audit §3.4).
    - Ngắt khi client disconnect và timeout tối đa 30 phút/stream (EventSource
      của trình duyệt tự reconnect); job chạy xong (done/error) thì kết thúc."""
    async def event_stream():
        last_log_count = 0
        started = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                if time.monotonic() - started > 1800:
                    yield f"data: {json.dumps({'event': 'timeout', 'message': 'SSE timeout, client sẽ reconnect'})}\n\n"
                    break
                job = get_or_restore_job(job_id)
                if not job:
                    yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                    break

                last_frames = {
                    r["index"]: r["last_frame_url"]
                    for r in job.get("results", [])
                    if isinstance(r, dict) and r.get("last_frame_url")
                }
                payload = {
                    "status": job["status"],
                    "progress": job["progress"],
                    "scenes": job["scenes"],
                    "logs": job["logs"][last_log_count:],
                    "final_video": job.get("final_video"),
                    "srt_file": job.get("srt_file"),
                    "audit": job.get("audit"),
                    "clips": job.get("clips", []),
                    "failed_scenes": job.get("failed_scenes", []),
                    "is_retrying": job.get("is_retrying", False),
                    "last_frames": last_frames,
                }
                last_log_count = len(job["logs"])
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                if job["status"] in ("done", "error") and not job.get("is_retrying"):
                    break
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/video/{filename}")
def stream_video(job_id: str, filename: str):
    """Stream video thành phẩm an toàn."""
    clean_name = Path(filename).name
    if not clean_name or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    path = resolve_safe_job_path(job_id, f"final/{clean_name}")
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "File không tồn tại"}, status_code=404)
    return FileResponse(str(path), media_type="video/mp4")


@app.get("/api/jobs/{job_id}/clips/{filename}")
def stream_clip(job_id: str, filename: str):
    """Stream clip riêng lẻ an toàn."""
    clean_name = Path(filename).name
    if not clean_name or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    path = resolve_safe_job_path(job_id, f"final/{clean_name}")
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "File không tồn tại"}, status_code=404)
    return FileResponse(str(path), media_type="video/mp4")


@app.get("/api/jobs/{job_id}/download/{filename}")
def download_video(job_id: str, filename: str):
    """Download video thành phẩm an toàn."""
    import urllib.parse
    clean_name = Path(filename).name
    if not clean_name or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    path = resolve_safe_job_path(job_id, f"final/{clean_name}")
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "File không tồn tại"}, status_code=404)
    safe_ascii = urllib.parse.quote(path.name)
    return FileResponse(
        str(path), media_type="video/mp4",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{safe_ascii}"},
    )


@app.get("/api/jobs")
@app.get("/api/library")
def list_jobs():
    """Lấy danh sách tất cả jobs đã sản xuất trong Workspace."""
    # load_all_persisted_jobs() giờ chỉ load 1 lần (startup/lần đầu) — không đọc
    # toàn bộ job_state.json mỗi request (audit §3.4).
    load_all_persisted_jobs()
    items = []
    for j in sorted(JOBS.values(), key=lambda x: x.get("created_at", 0), reverse=True):
        req_data = j.get("req")
        title = ""
        if isinstance(req_data, dict):
            title = req_data.get("title", "")
        elif hasattr(req_data, "title"):
            title = req_data.title
        items.append({
            "id": j["id"],
            "title": title or j["id"],
            "status": j["status"],
            "progress": j["progress"],
            "total": j.get("total", 0),
            "created_at": j.get("created_at", 0),
            "final_video": j.get("final_video"),
        })
    return items


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """Xoá job và toàn bộ workspace của job đó an toàn."""
    job = get_or_restore_job(job_id)
    if job is not None and _is_job_running(job):
        return JSONResponse(
            {"error": "Job đang chạy, không thể xoá. Hãy hủy job trước."},
            status_code=409)
    job_dir = get_safe_job_dir(job_id)
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if not job and not job_dir.exists():
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    if job_dir.exists() and job_dir.is_dir():
        shutil.rmtree(job_dir)
    return {"message": f"Đã xoá job {job_id}"}


# ── Filmmaking Pipeline Control & Project Management Endpoints ───────────────

class ComposeJobRequest(BaseModel):
    allow_partial: bool = False
    quality: Optional[str] = Field(default="720p", max_length=20)
    subtitle_mode: Optional[str] = Field(default="burn_in", max_length=20)
    transition: Optional[str] = Field(default="none", max_length=20)
    bg_music_path: Optional[str] = None
    music_volume: Optional[float] = Field(default=0.25, ge=0.0, le=2.0)
    voice_volume: Optional[float] = Field(default=1.0, ge=0.0, le=2.0)


@app.post("/api/jobs/{job_id}/compose")
async def api_compose_job(job_id: str, payload: Optional[ComposeJobRequest] = None):
    """Kích hoạt xuất phim theo yêu cầu (hỗ trợ xuất hoàn chỉnh hoặc xuất nháp với allow_partial=True)."""
    job = get_or_restore_job(job_id)
    if not job:
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    req: Optional[MovieRequest] = job.get("req")
    results = job.get("results", [])
    if not results:
        return JSONResponse({"error": "Không có dữ liệu cảnh nào để xuất phim"}, status_code=400)

    output_dir = WORKSPACE / job_id
    title = (req.title if req else "movie").replace(" ", "_")
    font_size = req.font_size if req else 22

    allow_partial = payload.allow_partial if payload else False
    quality = (payload.quality if payload and payload.quality else (req.quality if req else "720p"))
    subtitle_mode = (payload.subtitle_mode if payload and payload.subtitle_mode else (req.subtitle_mode if req else "burn_in"))
    transition = (payload.transition if payload and payload.transition else (req.transition if req else "none"))
    bg_music_path = payload.bg_music_path if payload and payload.bg_music_path is not None else (req.bg_music_path if req else None)
    music_volume = payload.music_volume if payload and payload.music_volume is not None else (req.music_volume if req and req.music_volume is not None else 0.25)
    voice_volume = payload.voice_volume if payload and payload.voice_volume is not None else (req.voice_volume if req and req.voice_volume is not None else 1.0)

    try:
        job_log(job, f"🎞️ Đang tiến hành xuất phim ({'Xuất bản nháp' if allow_partial else 'Xuất hoàn chỉnh'}, quality={quality}, sub={subtitle_mode}, trans={transition})...")
        aspect_ratio = req.aspect_ratio or "16:9" if req else "16:9"
        final_path = _unwrap_final_path(
            await asyncio.to_thread(
                compose_full_movie,
                results=results,
                output_dir=str(output_dir),
                project_title=title,
                font_size=font_size,
                bg_music_path=bg_music_path,
                music_volume=music_volume,
                voice_volume=voice_volume,
                subtitle_mode=subtitle_mode,
                quality=quality,
                transition=transition,
                aspect_ratio=aspect_ratio,
                allow_partial=allow_partial,
            )
        )
        rel = Path(final_path).name
        job["final_video"] = f"/api/jobs/{job_id}/video/{rel}"
        srt_path = output_dir / "final" / f"{title}.srt"
        if srt_path.exists():
            job["srt_file"] = f"/api/jobs/{job_id}/subtitles"
        job["progress"] = 100
        job["status"] = "done"
        job_log(job, f"🎉 Xuất phim thành công! → {rel}")
        _save_job_best_effort(job)
        return {"success": True, "video_url": job["final_video"], "srt_file": job.get("srt_file")}
    except Exception as e:
        job_log(job, f"❌ Lỗi xuất phim: {e}")
        _save_job_best_effort(job)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    """Dừng hoặc hủy job đang chạy (cancel thật task pipeline + cờ cancelled)."""
    job = get_or_restore_job(job_id)
    if not job:
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    with JOBS_LOCK:
        task = JOB_TASKS.get(job_id)
        job["cancelled"] = True
        job["status"] = "cancelled"
    # Cancel thật task pipeline (nếu còn chạy): task dừng ở điểm await tiếp theo,
    # pipeline kiểm tra cờ cancelled ở các mốc giai đoạn rồi thoát.
    if task is not None and not task.done():
        task.cancel()
    job_log(job, "🛑 Người dùng đã yêu cầu dừng/hủy job!")
    _save_job_best_effort(job)
    return {"success": True, "message": "Đã dừng job thành công"}


class ResumeJobRequest(BaseModel):
    from_scene_idx: Optional[int] = Field(default=1, ge=1)


@app.post("/api/jobs/{job_id}/resume")
async def api_resume_job(job_id: str, payload: Optional[ResumeJobRequest] = None):
    """Chạy tiếp job từ cảnh N mà không render lại các cảnh trước đã xong."""
    job = get_or_restore_job(job_id)
    if not job or not job.get("req"):
        return JSONResponse({"error": "Không tìm thấy job hoặc cấu hình ban đầu"}, status_code=404)
    req: MovieRequest = job["req"]
    start_idx = payload.from_scene_idx if payload and payload.from_scene_idx else 1
    req.start_scene_idx = start_idx
    with JOBS_LOCK:
        if _is_job_running(job):
            return JSONResponse(
                {"error": "Job đang chạy, không thể resume chồng lên tiến trình hiện tại."},
                status_code=409)
        job["status"] = "running"
        job["cancelled"] = False
    job_log(job, f"▶️ Tiếp tục chạy job từ Cảnh {start_idx}...")
    _save_job_or_http500(job)
    _spawn_job_task(job_id, film_pipeline(job, req))
    return {"success": True, "message": f"Đang tiếp tục chạy job từ Cảnh {start_idx}"}


class KeyframePreviewRequest(BaseModel):
    prompt: str = Field(max_length=5000)
    aspect_ratio: Optional[str] = Field(default="16:9", max_length=10)
    reference_image: Optional[str] = None
    style_locks: Optional[Dict[str, str]] = None


@app.post("/api/scenes/preview-keyframe")
async def api_preview_keyframe(req: KeyframePreviewRequest):
    """Sinh ảnh keyframe xem trước bằng muse-image trước khi tốn lượt render video."""
    require_muse_key()  # 503 rõ ràng nếu chưa cấu hình key
    try:
        styled = apply_style_locks(req.prompt, req.style_locks)
        fname = f"keyframe_{uuid.uuid4().hex[:8]}.png"
        out_path = str(WORKSPACE / "_uploads" / fname)
        (WORKSPACE / "_uploads").mkdir(parents=True, exist_ok=True)
        res = await generate_keyframe_image(
            prompt=styled,
            base_url=MUSE_API_URL,
            api_key=MUSE_API_KEY,
            output_path=out_path,
            aspect_ratio=req.aspect_ratio or "16:9",
            reference_image=req.reference_image,
        )
        return {"success": True, "url": f"/api/uploads/{fname}", "prompt": styled}
    except Exception as e:
        import traceback
        err_msg = str(e) or repr(e) or "Lỗi không xác định khi tạo keyframe"
        print(f"[PreviewKeyframeError] {err_msg}")
        traceback.print_exc()
        return JSONResponse({"success": False, "error": err_msg}, status_code=500)


@app.get("/api/jobs/{job_id}/subtitles")
def api_download_subtitles(job_id: str):
    """Tải file phụ đề SRT của phim an toàn."""
    job = get_or_restore_job(job_id)
    if not job:
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    req: Optional[MovieRequest] = job.get("req")
    title = (req.title if req else "movie").replace(" ", "_")
    clean_title = "".join(c for c in title if c.isalnum() or c in ("-", "_")).strip() or "movie"
    srt_path = resolve_safe_job_path(job_id, f"final/{clean_title}.srt")
    if not srt_path.exists() or not srt_path.is_file():
        final_dir = resolve_safe_job_path(job_id, "final")
        srts = [f for f in final_dir.glob("*.srt") if f.is_file()] if final_dir.exists() else []
        if srts:
            srt_path = srts[0]
        else:
            return JSONResponse({"error": "Không tìm thấy file phụ đề SRT"}, status_code=404)
    return FileResponse(
        str(srt_path),
        media_type="application/x-subrip",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{srt_path.name}"}
    )


@app.get("/api/jobs/{job_id}/metadata")
def api_download_metadata(job_id: str):
    """Tải toàn bộ metadata dự án (prompts, audio, logs, settings) dưới dạng JSON."""
    job = get_or_restore_job(job_id)
    if not job:
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    to_save = dict(job)
    if hasattr(to_save.get("req"), "model_dump"):
        to_save["req"] = to_save["req"].model_dump()
    return JSONResponse(to_save)


@app.get("/api/prompt-sheet")
def api_get_prompt_sheet():
    """Lấy danh sách các kịch bản mẫu, system prompt cho ChatGPT và từ điển prompt điện ảnh."""
    from prompt_templates import get_prompt_sheet_data
    return get_prompt_sheet_data()


# ── Shot Desk Studio Standard Endpoints ─────────────────────────────────────
from shot_desk import (
    import_shot_desk_zip,
    get_shot_desk_project,
    save_shot_desk_project,
    list_shot_desk_projects,
    render_single_shot_desk,
    approve_shot_desk_shot,
    approve_all_shot_desk_shots,
    edit_shot_desk_shot,
    update_shot_desk_project_settings,
    compose_shot_desk_timeline,
    export_shot_desk_zip,
    SHOT_DESK_DIR
)


# ── Upload guards (audit §2.6) ─────────────────────────────────────────────
# Giới hạn size: ảnh 50MB, nhạc 100MB, ZIP Shot Desk 500MB (khớp giới hạn của
# shot_desk.import_shot_desk_zip). Đọc theo chunk 1MB thay vì file.read() toàn bộ.
_MAX_IMAGE_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_MUSIC_UPLOAD_BYTES = 100 * 1024 * 1024
_MAX_ZIP_UPLOAD_BYTES = 500 * 1024 * 1024


async def _read_upload_capped(file: UploadFile, *, max_bytes: int, label: str) -> bytes:
    """Đọc upload theo chunk 1MB, raise 413 khi vượt giới hạn (không file.read() toàn bộ)."""
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{label} vượt quá giới hạn {max_bytes // (1024 * 1024)}MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _sniff_image_kind(head: bytes) -> Optional[str]:
    """Verify magic bytes ảnh: PNG/JPEG/WebP. Trả về kind hoặc None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "webp"
    return None


def _sniff_audio_kind(head: bytes, suffix: str) -> bool:
    """Verify magic bytes nhạc khớp với suffix đã khai báo."""
    if suffix == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if suffix == ".wav":
        return head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WAVE"
    if suffix in (".m4a", ".aac"):
        return (len(head) >= 8 and head[4:8] == b"ftyp") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF0) == 0xF0)
    if suffix == ".ogg":
        return head.startswith(b"OggS")
    return False


@app.post("/api/shot-desk/import-zip")
async def api_shot_desk_import_zip(file: UploadFile = File(...)):
    """Upload và import gói ZIP chuẩn Shot Desk."""
    content = await _read_upload_capped(file, max_bytes=_MAX_ZIP_UPLOAD_BYTES, label="File ZIP")
    try:
        project = import_shot_desk_zip(content, file.filename)
        return {"success": True, "project": project}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/shot-desk/load-sample")
def api_shot_desk_load_sample():
    """Tải nhanh dự án mẫu 'Bức Thư Chưa Gửi' từ workspace/_projects/."""
    sample_zip = WORKSPACE / "_projects" / "buc-thu-chua-gui-shot-desk.zip"
    if not sample_zip.exists():
        return JSONResponse({"error": "Không tìm thấy file mẫu buc-thu-chua-gui-shot-desk.zip"}, status_code=404)
    with open(sample_zip, "rb") as f:
        project = import_shot_desk_zip(f.read(), "buc-thu-chua-gui-shot-desk.zip")
    return {"success": True, "project": project}


@app.get("/api/shot-desk/projects")
def api_shot_desk_list_projects():
    """Liệt kê tất cả các dự án Shot Desk."""
    return list_shot_desk_projects()


PROJECT_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")

def get_safe_project_dir(project_id: str) -> Path:
    """Kiểm tra và trả về đường dẫn thư mục dự án an toàn, chống path traversal."""
    if not project_id or not PROJECT_ID_REGEX.match(project_id):
        raise HTTPException(status_code=400, detail="project_id không hợp lệ (chỉ chấp nhận ký tự chữ, số, gạch dưới và gạch ngang)")
    proj_dir = (SHOT_DESK_DIR / project_id).resolve()
    if not proj_dir.is_relative_to(SHOT_DESK_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Đường dẫn project_id không an toàn (path traversal)")
    return proj_dir

def resolve_safe_project_path(project_id: str, relative_path: str) -> Path:
    """Resolve và đảm bảo relative_path nằm hoàn toàn bên trong thư mục dự án."""
    proj_dir = get_safe_project_dir(project_id)
    if not relative_path or not isinstance(relative_path, str):
        raise HTTPException(status_code=400, detail="Đường dẫn không hợp lệ")
    norm = relative_path.replace("\\", "/")
    if norm.startswith("/") or ":" in norm or ".." in Path(norm).parts:
        raise HTTPException(status_code=400, detail="Đường dẫn chứa ký tự điều hướng hoặc tuyệt đối không an toàn")
    target = (proj_dir / norm).resolve()
    if not target.is_relative_to(proj_dir):
        raise HTTPException(status_code=400, detail="Đường dẫn tài nguyên nằm ngoài thư mục dự án")
    return target


@app.get("/api/shot-desk/projects/{project_id}")
def api_shot_desk_get_project(project_id: str):
    """Lấy chi tiết dự án Shot Desk kèm danh sách shots, character blocks, và review status."""
    get_safe_project_dir(project_id)
    p = get_shot_desk_project(project_id)
    if not p:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)
    return p


ACTIVE_SHOT_TASKS: dict[str, set[int]] = {}
ACTIVE_SHOT_DESK_COMPOSE: set[str] = set()


@app.get("/api/shot-desk/projects/{project_id}/status")
def api_shot_desk_get_project_status(project_id: str):
    """Endpoint siêu nhẹ để frontend poll trạng thái render không làm giật lag video hay quá tải mạng."""
    get_safe_project_dir(project_id)
    p = get_shot_desk_project(project_id)
    if not p:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)

    active_set = ACTIVE_SHOT_TASKS.get(project_id, set())
    proj_dir = get_safe_project_dir(project_id)
    clips_dir = proj_dir / "clips"

    # GET KHÔNG có side-effect (audit §1.7): chỉ TÍNH trạng thái hiển thị từ clip
    # trên đĩa + ACTIVE_SHOT_TASKS, KHÔNG sửa project và KHÔNG save. Việc chuyển
    # trạng thái thật chỉ làm ở POST /reset-stuck hoặc khi worker render xong.
    # (Lưu ý: get_shot_desk_project() trong shot_desk.py vẫn có thể save khi nó
    # tự đồng bộ URL frame — nằm ngoài phạm vi refactor app.py, file đó bị đóng.)
    shots_summary = []
    is_rendering = False
    for s in p.get("shots", []):
        st = s.get("status", "ready")
        idx = s.get("index")
        display_status = st
        display_error = s.get("error")
        display_video_url = s.get("video_url")

        # Trạng thái kẹt (orphaned rendering): hiển thị suy ra từ đĩa, không ghi.
        if st == "rendering" and idx not in active_set:
            clip_p = clips_dir / f"shot_{idx:03d}_clip.mp4"
            if clip_p.exists() and clip_p.stat().st_size > 500:
                display_status = "review"
                if not display_video_url:
                    display_video_url = f"/api/shot-desk/projects/{project_id}/shots/{idx}/video"
            else:
                display_status = "ready"
                if not display_error:
                    display_error = "Tiến trình render trước bị gián đoạn do server khởi động lại. Hãy bấm Render lại."

        if display_status == "rendering":
            is_rendering = True

        shots_summary.append({
            "index": idx,
            "status": display_status,
            "video_url": display_video_url,
            "audio_url": s.get("audio_url"),
            "error": display_error,
            "audit": s.get("audit"),
        })

    return {
        "id": p.get("id"),
        "is_rendering": is_rendering,
        "is_composing": project_id in ACTIVE_SHOT_DESK_COMPOSE,
        "shots": shots_summary
    }


@app.post("/api/shot-desk/projects/{project_id}/reset-stuck")
def api_shot_desk_reset_stuck(project_id: str):
    """Giải phóng toàn bộ các shot đang bị kẹt ở trạng thái rendering do sự cố gián đoạn."""
    proj = get_shot_desk_project(project_id)
    if not proj:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)
    active_set = ACTIVE_SHOT_TASKS.get(project_id, set())
    reset_count = 0
    for s in proj.get("shots", []):
        if s.get("status") == "rendering" and s.get("index") not in active_set:
            s["status"] = "ready"
            s["error"] = None
            reset_count += 1
    if reset_count > 0:
        save_shot_desk_project(proj)
    return {"success": True, "reset_count": reset_count, "message": f"Đã giải phóng {reset_count} cảnh bị kẹt!"}



@app.delete("/api/shot-desk/projects/{project_id}")
def api_shot_desk_delete_project(project_id: str):
    """Xóa dự án Shot Desk an toàn, chống path traversal."""
    if ACTIVE_SHOT_TASKS.get(project_id) or project_id in ACTIVE_SHOT_DESK_COMPOSE:
        return JSONResponse(
            {"error": "Dự án đang render/dựng, không thể xoá. Hãy đợi hoàn tất trước."},
            status_code=409)
    target = get_safe_project_dir(project_id)
    if target.exists():
        shutil.rmtree(target)
        return {"success": True, "message": f"Đã xóa dự án {project_id}"}
    return JSONResponse({"error": "Dự án không tồn tại"}, status_code=404)


@app.get("/api/shot-desk/projects/{project_id}/shots/{shot_index}/frames/{frame_name}")
def api_shot_desk_get_frame(project_id: str, shot_index: int, frame_name: str):
    """Phục vụ file ảnh frame của từng shot an toàn (hỗ trợ .png, .jpg, .jpeg, .webp)."""
    clean_frame = Path(frame_name).name
    if not clean_frame or ".." in frame_name or "/" in frame_name or "\\" in frame_name:
        raise HTTPException(status_code=400, detail="Tên frame ảnh không hợp lệ")

    shot_dir = resolve_safe_project_path(project_id, f"shots/shot_{shot_index:03d}")
    p = (shot_dir / clean_frame).resolve()
    if not p.is_relative_to(shot_dir):
        raise HTTPException(status_code=400, detail="Đường dẫn không hợp lệ")

    # 1. Nếu file chưa khớp chính xác, tìm theo tên không phân biệt đuôi mở rộng (first_frame.*, last_frame.*, middle_frame.*)
    if not p.exists() and shot_dir.exists():
        stem = Path(clean_frame).stem
        candidates = [f for f in shot_dir.glob(f"{stem}.*") if f.is_file() and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.avif')]
        if candidates:
            p = candidates[0]

    # 2. Thử tìm qua đường dẫn tham chiếu trong project.json nếu có
    if not p.exists():
        proj = get_shot_desk_project(project_id)
        if proj:
            for s in proj.get("shots", []):
                if s["index"] == shot_index:
                    for k in ("first_frame", "middle_frame", "last_frame"):
                        ref = s.get(k)
                        if ref and Path(ref).stem == Path(clean_frame).stem:
                            try:
                                cand = resolve_safe_project_path(project_id, ref)
                                if cand.exists() and cand.is_file():
                                    p = cand
                                    break
                            except Exception:
                                pass
                    break

    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "Không tìm thấy frame ảnh"}, status_code=404)

    m = mimetypes.guess_type(str(p))[0] or "image/png"
    return FileResponse(str(p), media_type=m)


@app.get("/api/shot-desk/projects/{project_id}/assets/{asset_path:path}")
def api_shot_desk_get_asset(project_id: str, asset_path: str):
    """Phục vụ asset tĩnh trong project an toàn, chống path traversal."""
    p = resolve_safe_project_path(project_id, asset_path)
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "Không tìm thấy asset"}, status_code=404)
    m = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    return FileResponse(str(p), media_type=m)


@app.get("/api/shot-desk/projects/{project_id}/shots/{shot_index}/video")
def api_shot_desk_get_shot_video(project_id: str, shot_index: int):
    """Stream video clip của shot."""
    p = resolve_safe_project_path(project_id, f"clips/shot_{shot_index:03d}_clip.mp4")
    if not p.exists():
        p = resolve_safe_project_path(project_id, f"clips/shot_{shot_index:03d}_raw.mp4")
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "Video clip chưa được render"}, status_code=404)
    return FileResponse(str(p), media_type="video/mp4")


@app.get("/api/shot-desk/projects/{project_id}/shots/{shot_index}/audio")
def api_shot_desk_get_shot_audio(project_id: str, shot_index: int):
    """Stream audio thoại của shot."""
    p = resolve_safe_project_path(project_id, f"audio/shot_{shot_index:03d}_tts.mp3")
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "Audio chưa có"}, status_code=404)
    return FileResponse(str(p), media_type="audio/mpeg")


class ShotRenderRequest(BaseModel):
    use_compliance: bool = False
    custom_prompt: Optional[str] = Field(default=None, max_length=5000)
    background: bool = True


@app.post("/api/shot-desk/projects/{project_id}/shots/{shot_index}/render")
async def api_shot_desk_render_shot(
    project_id: str,
    shot_index: int,
    background_tasks: BackgroundTasks,
    payload: Optional[ShotRenderRequest] = None
):
    """Render 1 shot đơn lẻ (hỗ trợ middle-frame splitting, compliance fallback, & background render)."""
    try:
        use_compliance = payload.use_compliance if payload else False
        custom_prompt = payload.custom_prompt if payload else None
        bg = payload.background if payload else True

        proj = get_shot_desk_project(project_id)
        if not proj:
            return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)

        target = None
        for s in proj.get("shots", []):
            if s["index"] == shot_index:
                target = s
                break
        if not target:
            return JSONResponse({"error": f"Không tìm thấy Shot {shot_index}"}, status_code=404)

        active_set = ACTIVE_SHOT_TASKS.setdefault(project_id, set())
        if shot_index in active_set:
            return {"success": True, "message": f"Shot {shot_index} đang được xử lý, đã chặn request trùng lặp!", "status": "rendering"}
        active_set.add(shot_index)

        if bg:
            target["status"] = "rendering"
            target["error"] = None
            save_shot_desk_project(proj)

            async def _bg_run():
                try:
                    await render_single_shot_desk(
                        project_id=project_id,
                        shot_index=shot_index,
                        base_url=MUSE_API_URL,
                        api_key=MUSE_API_KEY,
                        use_compliance=use_compliance,
                        custom_prompt=custom_prompt,
                    )
                except Exception as ex:
                    print(f"[ShotDesk Bg Render Error shot {shot_index}] {ex}")
                finally:
                    ACTIVE_SHOT_TASKS.get(project_id, set()).discard(shot_index)

            asyncio.create_task(_bg_run())
            return {"success": True, "message": f"Đang render Shot {shot_index} trong nền...", "status": "rendering"}
        else:
            try:
                shot = await render_single_shot_desk(
                    project_id=project_id,
                    shot_index=shot_index,
                    base_url=MUSE_API_URL,
                    api_key=MUSE_API_KEY,
                    use_compliance=use_compliance,
                    custom_prompt=custom_prompt,
                )
                return {"success": True, "shot": shot}
            finally:
                ACTIVE_SHOT_TASKS.get(project_id, set()).discard(shot_index)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


class RenderAllRequest(BaseModel):
    concurrent: int = Field(default=6, ge=1, le=24)


@app.post("/api/shot-desk/projects/{project_id}/render-all")
async def api_shot_desk_render_all(project_id: str, payload: Optional[RenderAllRequest] = None):
    """Kích hoạt render hàng loạt đa luồng (song song) qua toàn bộ các tài khoản Muse khả dụng."""
    proj = get_shot_desk_project(project_id)
    if not proj:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)

    # Tự động phát hiện số tài khoản Muse đang hoạt động để phân bổ tối đa toàn bộ tài khoản
    num_accounts = 6
    try:
        _cl = get_http_client()
        hr = await _cl.get(f"{MUSE_API_URL}/admin/accounts", headers={"Authorization": f"Bearer {MUSE_API_KEY}"}, timeout=3.0)
        if hr.status_code == 200:
            d = hr.json()
            accs = d.get("accounts", []) if isinstance(d, dict) else d
            enabled_accs = [a for a in accs if a.get("enabled", True)]
            if enabled_accs:
                num_accounts = len(enabled_accs)
    except Exception as acc_e:
        print(f"[RenderAll] Warning querying active accounts: {acc_e}")

    if payload and payload.concurrent and payload.concurrent > 0:
        concurrent = payload.concurrent
    else:
        concurrent = num_accounts

    concurrent = max(1, min(24, concurrent))

    active_set = ACTIVE_SHOT_TASKS.setdefault(project_id, set())
    proj_dir = get_safe_project_dir(project_id)
    clips_dir = proj_dir / "clips"
    eligible = []
    for s in proj.get("shots", []):
        idx = s.get("index")
        st = s.get("status")
        # 1. Bỏ qua các cảnh đang chạy thực tế trong RAM
        if idx in active_set:
            continue
        # 2. Bỏ qua các cảnh đã được duyệt (Approved / Done)
        if st in ("approved", "done"):
            continue
        # 3. Bỏ qua các cảnh đã render xong đang chờ duyệt ở Review Desk
        if st == "review":
            continue
        # 4. Bỏ qua nếu file clip MP4 thành phẩm đã có sẵn trên đĩa
        clip_p = clips_dir / f"shot_{idx:03d}_clip.mp4"
        if clip_p.exists() and clip_p.stat().st_size > 1000:
            s["status"] = "review"
            s["video_url"] = f"/api/shot-desk/projects/{project_id}/shots/{idx}/video"
            continue
        eligible.append(s)
    if not eligible:
        return {"success": True, "message": "Tất cả các cảnh đã hoàn thành hoặc đang được xử lý trong nền!"}

    # Đánh dấu tất cả cảnh eligible thành 'rendering' ngay lập tức và đưa vào active_set
    for s in eligible:
        s["status"] = "rendering"
        s["error"] = None
        active_set.add(s["index"])
    save_shot_desk_project(proj)

    sem = asyncio.Semaphore(concurrent)
    print(f"[ShotDesk Concurrent] Kích hoạt render song song {len(eligible)} cảnh qua {concurrent} luồng (chia đều qua toàn bộ {num_accounts} tài khoản Muse)...")

    async def _render_one(shot_idx: int):
        async with sem:
            try:
                await render_single_shot_desk(
                    project_id=project_id,
                    shot_index=shot_idx,
                    base_url=MUSE_API_URL,
                    api_key=MUSE_API_KEY
                )
            except Exception as ex:
                print(f"[ShotDesk Concurrent Error shot {shot_idx}] {ex}")
            finally:
                ACTIVE_SHOT_TASKS.get(project_id, set()).discard(shot_idx)

    async def _run_batch():
        tasks = [_render_one(s["index"]) for s in eligible]
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.create_task(_run_batch())
    return {
        "success": True,
        "message": f"Đã kích hoạt render đa luồng cho {len(eligible)} cảnh (song song {concurrent} luồng qua {num_accounts} tài khoản)!",
        "count": len(eligible),
        "concurrent": concurrent,
        "accounts": num_accounts
    }


@app.post("/api/shot-desk/projects/{project_id}/shots/{shot_index}/approve")
def api_shot_desk_approve_shot(project_id: str, shot_index: int):
    """Đạo diễn duyệt (Approve) shot sau khi xem Review Desk."""
    try:
        res = approve_shot_desk_shot(project_id, shot_index)
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


class ApproveAllRequest(BaseModel):
    only_with_video: bool = True


@app.post("/api/shot-desk/projects/{project_id}/approve-all")
def api_shot_desk_approve_all(project_id: str, payload: Optional[ApproveAllRequest] = None):
    """Phê duyệt hàng loạt các shot (mặc định chỉ duyệt các cảnh đã có video clip thành phẩm)."""
    try:
        only_vid = payload.only_with_video if payload else True
        res = approve_all_shot_desk_shots(project_id, only_with_video=only_vid)
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


class ShotEditRequest(BaseModel):
    prompt: Optional[str] = Field(default=None, max_length=5000)
    compliance_prompt: Optional[str] = Field(default=None, max_length=5000)
    voiceover: Optional[str] = Field(default=None, max_length=5000)
    duration: Optional[int] = Field(default=None, ge=1, le=60)
    transition_note: Optional[str] = Field(default=None, max_length=1000)
    reset_status: bool = True
    voice_mode: Optional[str] = None  # "narration" | "dialogue"
    speaker: Optional[str] = Field(default=None, max_length=200)
    voice: Optional[str] = Field(default=None, max_length=200)  # "vi-VN-HoaiMyNeural" | "vi-VN-NamMinhNeural"
    lip_sync: Optional[bool] = None


class ProjectSettingsRequest(BaseModel):
    default_voice: Optional[str] = Field(default=None, max_length=200)
    default_voice_mode: Optional[str] = Field(default=None, max_length=200)
    apply_to_all_shots: bool = False


@app.post("/api/shot-desk/projects/{project_id}/settings")
def api_shot_desk_update_settings(project_id: str, payload: ProjectSettingsRequest):
    """Cập nhật cấu hình giọng đọc và chế độ thoại toàn dự án."""
    try:
        res = update_shot_desk_project_settings(project_id, payload.model_dump(exclude_none=True))
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/shot-desk/projects/{project_id}/shots/{shot_index}/edit")
def api_shot_desk_edit_shot(project_id: str, shot_index: int, payload: ShotEditRequest):
    """Chỉnh sửa prompt, compliance_prompt, thoại hoặc thông số của shot."""
    try:
        res = edit_shot_desk_shot(project_id, shot_index, payload.model_dump(exclude_none=True))
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/shot-desk/projects/{project_id}/shots/{shot_index}/auto-heal")
async def api_shot_desk_auto_heal_shot(
    project_id: str,
    shot_index: int,
    background_tasks: BackgroundTasks,
):
    """Tự động làm sạch prompt bị vi phạm chính sách / lỗi, cập nhật prompt an toàn và render lại ngay lập tức."""
    get_safe_project_dir(project_id)
    proj = get_shot_desk_project(project_id)
    if not proj:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)
    target = None
    for s in proj.get("shots", []):
        if s["index"] == shot_index:
            target = s
            break
    if not target:
        return JSONResponse({"error": "Không tìm thấy shot"}, status_code=404)

    curr_p = target.get("prompt", "")
    err = target.get("error", "")
    safe_p = sanitize_prompt_heuristics(curr_p, err, aggressive=True)

    # Cập nhật prompt an toàn cho shot
    target["prompt"] = safe_p
    target["compliance_prompt"] = safe_p
    target["status"] = "rendering"
    target["error"] = None
    save_shot_desk_project(proj)

    active_set = ACTIVE_SHOT_TASKS.setdefault(project_id, set())
    active_set.add(shot_index)

    async def _bg_heal_one():
        try:
            await render_single_shot_desk(
                project_id=project_id,
                shot_index=shot_index,
                base_url=MUSE_API_URL,
                api_key=MUSE_API_KEY,
                custom_prompt=safe_p,
                use_compliance=True,
            )
        except Exception as ex:
            print(f"[ShotDesk AutoHeal Error shot {shot_index}] {ex}")
        finally:
            ACTIVE_SHOT_TASKS.get(project_id, set()).discard(shot_index)

    background_tasks.add_task(_bg_heal_one)

    return {
        "success": True,
        "message": f"Đã tự động làm sạch prompt và đang kích hoạt render lại an toàn cho Shot {shot_index}!",
        "new_prompt": safe_p,
    }


@app.post("/api/shot-desk/projects/{project_id}/auto-heal-all")
async def api_shot_desk_auto_heal_all(
    project_id: str,
    background_tasks: BackgroundTasks,
):
    """Tự động làm sạch prompt và kích hoạt render lại toàn bộ các shot đang bị lỗi trong dự án."""
    get_safe_project_dir(project_id)
    proj = get_shot_desk_project(project_id)
    if not proj:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)

    shots_to_render = []
    for s in proj.get("shots", []):
        if s.get("error") and not s.get("video_url"):
            curr_p = s.get("prompt", "")
            err = s.get("error", "")
            safe_p = sanitize_prompt_heuristics(curr_p, err, aggressive=True)
            s["prompt"] = safe_p
            s["compliance_prompt"] = safe_p
            s["status"] = "rendering"
            s["error"] = None
            shots_to_render.append((s["index"], safe_p))

    if not shots_to_render:
        return {"success": True, "healed_count": 0, "message": "Không có shot nào bị lỗi cần sửa!"}

    save_shot_desk_project(proj)

    active_set = ACTIVE_SHOT_TASKS.setdefault(project_id, set())
    for idx, _ in shots_to_render:
        active_set.add(idx)

    async def _run_healed_queue():
        for idx, safe_p in shots_to_render:
            try:
                await render_single_shot_desk(
                    project_id=project_id,
                    shot_index=idx,
                    base_url=MUSE_API_URL,
                    api_key=MUSE_API_KEY,
                    custom_prompt=safe_p,
                    use_compliance=True,
                )
            except Exception as e:
                print(f"[AutoHealAll] Error shot {idx}: {e}")
            finally:
                ACTIVE_SHOT_TASKS.get(project_id, set()).discard(idx)

    background_tasks.add_task(_run_healed_queue)

    return {
        "success": True,
        "healed_count": len(shots_to_render),
        "message": f"Đã tự động sửa prompt an toàn và đang kích hoạt render lại {len(shots_to_render)} shot bị lỗi!",
    }


class ShotDeskComposeRequest(BaseModel):
    allow_partial: bool = False
    bg_music_path: Optional[str] = None
    music_volume: float = Field(default=0.25, ge=0.0, le=2.0)
    voice_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    subtitle_mode: str = Field(default="burn_in", max_length=20)
    quality: str = Field(default="720p", max_length=20)
    sound_bridge_duration: float = Field(default=0.75, ge=0.0, le=5.0)


COMPOSE_STATUS: dict[str, dict] = {}


def _background_shot_desk_compose(clean_id: str, opts: dict):
    def _progress_cb(pct: int, msg: str):
        if clean_id in COMPOSE_STATUS:
            COMPOSE_STATUS[clean_id]["progress"] = pct
            COMPOSE_STATUS[clean_id]["step"] = msg

    try:
        res = compose_shot_desk_timeline(clean_id, progress_cb=_progress_cb, **opts)
        if clean_id in COMPOSE_STATUS:
            COMPOSE_STATUS[clean_id]["status"] = "done"
            COMPOSE_STATUS[clean_id]["progress"] = 100
            COMPOSE_STATUS[clean_id]["step"] = "🎉 Dựng phim hoàn tất 100%!"
            COMPOSE_STATUS[clean_id]["result"] = res
    except Exception as e:
        import traceback
        traceback.print_exc()
        if clean_id in COMPOSE_STATUS:
            COMPOSE_STATUS[clean_id]["status"] = "error"
            COMPOSE_STATUS[clean_id]["error"] = str(e)
    finally:
        ACTIVE_SHOT_DESK_COMPOSE.discard(clean_id)


@app.post("/api/shot-desk/projects/{project_id}/compose")
def api_shot_desk_compose(project_id: str, payload: Optional[ShotDeskComposeRequest] = None):
    """Dựng phim hoàn chỉnh từ các shot đã duyệt (Approved) theo Timeline."""
    clean_id = project_id.strip()
    if clean_id in ACTIVE_SHOT_DESK_COMPOSE:
        return JSONResponse({
            "error": "Tiến trình dựng phim đang chạy trong nền cho dự án này. Vui lòng chờ hoàn tất trước khi dựng lại!"
        }, status_code=409)

    ACTIVE_SHOT_DESK_COMPOSE.add(clean_id)
    opts = payload.model_dump() if payload else {}
    COMPOSE_STATUS[clean_id] = {
        "status": "composing",
        "progress": 0,
        "step": "Bắt đầu khởi tạo tiến trình dựng...",
        "error": None,
        "result": None,
    }

    import threading
    t = threading.Thread(target=_background_shot_desk_compose, args=(clean_id, opts), daemon=True)
    t.start()

    return {
        "success": True,
        "started": True,
        "project_id": clean_id,
        "message": "Tiến trình dựng phim đã bắt đầu chạy ngầm!"
    }


@app.get("/api/shot-desk/projects/{project_id}/compose-status")
def api_shot_desk_compose_status(project_id: str):
    """Lấy trạng thái và tiến độ % dựng phim timeline thời gian thực."""
    clean_id = project_id.strip()
    proj_dir = get_safe_project_dir(clean_id)
    final_dir = str((proj_dir / "final").resolve())

    st = COMPOSE_STATUS.get(clean_id, {
        "status": "idle",
        "progress": 0,
        "step": "",
        "error": None,
        "result": None,
    })
    return {
        "project_id": clean_id,
        "final_dir": final_dir,
        "is_composing": clean_id in ACTIVE_SHOT_DESK_COMPOSE,
        **st
    }


@app.post("/api/shot-desk/projects/{project_id}/open-folder")
def api_shot_desk_open_folder(project_id: str, request: Request):
    """Mở thư mục final/ chứa video thành phẩm trực tiếp trong Windows Explorer.
    CHỈ khả dụng khi truy cập từ localhost (127.0.0.1/::1); truy cập từ xa -> 403.
    Khi STUDIO_API_KEY được đặt thì request còn phải qua auth middleware (audit §2.8).
    Frontend vẫn gọi được như cũ khi chạy local; mở folder từ xa bị chặn có chủ đích."""
    _host = request.client.host if request.client else ""
    if _host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            status_code=403,
            detail="open-folder chỉ khả dụng khi truy cập từ localhost")
    import sys
    clean_id = project_id.strip()
    proj_dir = get_safe_project_dir(clean_id)
    final_dir = (proj_dir / "final").resolve()
    final_dir.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(final_dir))
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(final_dir)])
        return {"success": True, "path": str(final_dir)}
    except Exception as e:
        return JSONResponse({"error": f"Không thể mở thư mục: {e}"}, status_code=500)


@app.get("/api/shot-desk/projects/{project_id}/final-video/{filename}")
def api_shot_desk_get_final_video(project_id: str, filename: str):
    """Stream / Tải video thành phẩm an toàn."""
    clean_name = Path(filename).name
    if not clean_name or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Tên file video không hợp lệ")
    p = resolve_safe_project_path(project_id, f"final/{clean_name}")
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "File không tồn tại"}, status_code=404)
    return FileResponse(str(p), media_type="video/mp4")


@app.get("/api/shot-desk/projects/{project_id}/subtitles/{filename}")
def api_shot_desk_get_subtitles(project_id: str, filename: str):
    """Tải file phụ đề SRT của phim an toàn."""
    clean_name = Path(filename).name
    if not clean_name or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Tên file phụ đề không hợp lệ")
    p = resolve_safe_project_path(project_id, f"final/{clean_name}")
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "File phụ đề không tồn tại"}, status_code=404)
    return FileResponse(
        str(p),
        media_type="application/x-subrip",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{clean_name}"}
    )


@app.get("/api/shot-desk/projects/{project_id}/export-zip")
def api_shot_desk_export_zip(project_id: str):
    """Đóng gói và tải về gói ZIP dự án hoàn chỉnh an toàn."""
    get_safe_project_dir(project_id)
    p = get_shot_desk_project(project_id)
    if not p:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)
    zip_path = resolve_safe_project_path(project_id, f"{project_id}_export.zip")
    export_shot_desk_zip(project_id, str(zip_path))
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{zip_path.name}"}
    )


# ── Projects Persistence API ─────────────────────────────────────────────────
PROJECTS_DIR = WORKSPACE / "_projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


class ProjectSaveRequest(BaseModel):
    name: str = Field(max_length=100)
    title: str = Field(default="", max_length=200)
    script_text: str = Field(default="", max_length=100000)
    aspect_ratio: str = Field(default="16:9", max_length=10)
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=200)
    export_mode: str = Field(default="full", max_length=20)
    chain_mode: str = Field(default="none", max_length=20)
    chain_fallback_mode: str = Field(default="independent", max_length=20)
    subtitle_mode: str = Field(default="burn_in", max_length=20)
    quality: str = Field(default="720p", max_length=20)
    transition: str = Field(default="none", max_length=20)
    bg_music_path: Optional[str] = None
    music_volume: float = Field(default=0.25, ge=0.0, le=2.0)
    voice_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    scenes: List[Dict[str, Any]] = Field(default=[], max_length=200)
    characters: List[Dict[str, Any]] = Field(default=[], max_length=100)
    style_locks: Dict[str, str] = {}


@app.post("/api/projects/save")
def api_save_project(req: ProjectSaveRequest):
    """Lưu kịch bản, cảnh, nhân vật, prompt, cài đặt thành một Project."""
    clean_name = "".join(c for c in req.name if c.isalnum() or c in ("-", "_")).strip()
    if not clean_name:
        clean_name = f"project_{int(time.time())}"
    filepath = PROJECTS_DIR / f"{clean_name}.json"
    data = req.model_dump()
    data["saved_at"] = time.time()
    try:
        _atomic_write_json(filepath, data)
    except Exception as exc:
        logger.exception("Không lưu được project %s", clean_name)
        return JSONResponse({"error": f"Không lưu được project: {exc}"}, status_code=500)
    return {"success": True, "name": clean_name, "message": f"Đã lưu project '{clean_name}'"}


@app.get("/api/projects")
def api_list_projects():
    """Lấy danh sách các project đã lưu."""
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for f in sorted(PROJECTS_DIR.glob("*.json"), key=os.path.getmtime, reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                items.append({
                    "name": f.stem,
                    "title": data.get("title", f.stem),
                    "scenes_count": len(data.get("scenes", [])),
                    "saved_at": data.get("saved_at", os.path.getmtime(f)),
                })
        except Exception:
            items.append({"name": f.stem, "title": f.stem, "scenes_count": 0, "saved_at": os.path.getmtime(f)})
    return items


@app.get("/api/projects/{name}")
def api_get_project(name: str):
    """Mở lại cấu hình 1 project đã lưu an toàn."""
    clean_name = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
    if not clean_name or ".." in name:
        return JSONResponse({"error": f"Tên project không hợp lệ"}, status_code=400)
    filepath = (PROJECTS_DIR / f"{clean_name}.json").resolve()
    if not filepath.is_relative_to(PROJECTS_DIR.resolve()) or not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": f"Không tìm thấy project '{name}'"}, status_code=404)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        return JSONResponse(
            {"error": f"File project '{name}' bị hỏng (JSON không hợp lệ): {exc}"},
            status_code=500)
    except OSError as exc:
        return JSONResponse(
            {"error": f"Không đọc được file project '{name}': {exc}"},
            status_code=500)
    return data


@app.delete("/api/projects/{name}")
def api_delete_project(name: str):
    """Xóa 1 project đã lưu an toàn."""
    clean_name = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
    if not clean_name or ".." in name:
        return JSONResponse({"error": f"Tên project không hợp lệ"}, status_code=400)
    filepath = (PROJECTS_DIR / f"{clean_name}.json").resolve()
    if not filepath.is_relative_to(PROJECTS_DIR.resolve()):
        return JSONResponse({"error": "Đường dẫn không hợp lệ"}, status_code=400)
    if filepath.exists() and filepath.is_file():
        filepath.unlink()
        return {"success": True, "message": f"Đã xóa project '{name}'"}
    return JSONResponse({"error": "File không tồn tại"}, status_code=404)


# ── Music Management API ─────────────────────────────────────────────────────
MUSIC_DIR = WORKSPACE / "_music"
MUSIC_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/upload/music")
async def api_upload_music(file: UploadFile = File(...)):
    """Upload nhạc nền (MP3, WAV, AAC, M4A, OGG)."""
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename).suffix.lower()
    if suffix not in [".mp3", ".wav", ".m4a", ".aac", ".ogg"]:
        return JSONResponse({"error": "Định dạng âm thanh không hỗ trợ (chỉ nhận .mp3, .wav, .m4a, .aac, .ogg)"}, status_code=400)
    content = await _read_upload_capped(file, max_bytes=_MAX_MUSIC_UPLOAD_BYTES, label="File nhạc")
    if not _sniff_audio_kind(content[:16], suffix):
        return JSONResponse(
            {"error": "Nội dung file không khớp định dạng âm thanh đã khai báo"},
            status_code=400)
    safe_name = f"{uuid.uuid4().hex[:6]}_{Path(file.filename).name.replace(' ', '_')}"
    target = MUSIC_DIR / safe_name
    with open(target, "wb") as f:
        f.write(content)
    return {
        "success": True,
        "name": Path(file.filename).stem,
        "filename": safe_name,
        "path": str(target.resolve()),
        "url": f"/api/music/{safe_name}",
    }


@app.get("/api/music/list")
def api_list_music():
    """Liệt kê danh sách nhạc nền có sẵn trong kho."""
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for f in sorted(MUSIC_DIR.glob("*.*"), key=os.path.getmtime, reverse=True):
        if f.suffix.lower() in [".mp3", ".wav", ".m4a", ".aac", ".ogg"]:
            items.append({
                "filename": f.name,
                "name": f.stem.split("_", 1)[-1] if "_" in f.stem else f.stem,
                "path": str(f.resolve()),
                "url": f"/api/music/{f.name}",
            })
    return items


@app.get("/api/music/{filename}")
def api_get_music_file(filename: str):
    """Stream file nhạc nền an toàn."""
    path = resolve_safe_music_path(filename)
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "File nhạc không tồn tại"}, status_code=404)
    return FileResponse(str(path), media_type="audio/mpeg")


# ── Workspace Cleanup API ────────────────────────────────────────────────────
@app.post("/api/workspace/clean")
def api_clean_workspace(keep_projects: bool = True):
    """Dọn dẹp các thư mục job tạm, giải phóng dung lượng đĩa cứng và bảo toàn thư viện dự án."""
    with JOBS_LOCK:
        running = [jid for jid, j in JOBS.items() if j.get("status") == "running"]
        active_tasks = [jid for jid, t in JOB_TASKS.items() if not t.done()]
    still_running = sorted(set(running) | set(active_tasks))
    if still_running:
        return JSONResponse(
            {"error": f"Không thể dọn dẹp: còn job đang chạy ({', '.join(still_running)}). Hãy hủy/đợi hoàn tất trước."},
            status_code=409)
    deleted_jobs = 0
    bytes_freed = 0
    protected_dirs = {"_uploads", "_music", "_voice_previews"}
    if keep_projects:
        protected_dirs.update({"_projects", "_shot_desk_projects"})

    for item in WORKSPACE.iterdir():
        if item.is_dir() and item.name not in protected_dirs:
            try:
                for root, dirs, files in os.walk(item):
                    for file in files:
                        bytes_freed += os.path.getsize(os.path.join(root, file))
                shutil.rmtree(item)
                deleted_jobs += 1
            except Exception as e:
                print(f"[CleanWorkspaceError] {e}")
    mb_freed = round(bytes_freed / (1024 * 1024), 2)
    return {
        "success": True,
        "deleted_jobs": deleted_jobs,
        "mb_freed": mb_freed,
        "message": f"Đã dọn dẹp {deleted_jobs} thư mục tác vụ tạm thời, giải phóng {mb_freed} MB dung lượng!"
    }



UPLOAD_DIR = WORKSPACE / "_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/upload-image")
async def upload_image(file: UploadFile = File(...)):
    """Upload ảnh tham chiếu nhân vật trực tiếp từ máy tính."""
    # Đọc chunked + verify magic bytes TRƯỚC try (để 413/400 không bị nuốt thành 500).
    # Chỉ nhận PNG/JPEG/WebP thật; .html/.svg/.htm giả dạng ảnh bị từ chối.
    content = await _read_upload_capped(file, max_bytes=_MAX_IMAGE_UPLOAD_BYTES, label="Ảnh")
    kind = _sniff_image_kind(content[:16])
    if kind is None:
        return JSONResponse(
            {"error": "File không phải ảnh PNG/JPEG/WebP hợp lệ (đã kiểm tra magic bytes); không nhận .html/.svg/.htm"},
            status_code=400)
    mime = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}[kind]
    ext = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}[kind]
    try:
        import base64
        b64_data = base64.b64encode(content).decode("ascii")
        data_url = f"data:{mime};base64,{b64_data}"

        # Đuôi file lấy theo nội dung thật, tên chỉ giữ ký tự an toàn.
        stem = re.sub(r"[^A-Za-z0-9_\-]", "_", Path(file.filename).stem)[:50] or "image"
        safe_name = f"{uuid.uuid4().hex[:8]}_{stem}{ext}"
        save_path = UPLOAD_DIR / safe_name
        async with aiofiles.open(save_path, "wb") as f:
            await f.write(content)

        return {
            "success": True,
            "filename": file.filename,
            "saved_name": safe_name,
            "url": f"/api/uploads/{safe_name}",
            "data_url": data_url,
        }
    except Exception as e:
        return JSONResponse({"error": f"Lỗi tải ảnh: {str(e)}"}, status_code=500)


@app.get("/api/uploads/{filename}")
def get_uploaded_image(filename: str):
    """Phục vụ file ảnh đã tải lên an toàn."""
    path = resolve_safe_upload_path(filename)
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "File ảnh không tồn tại"}, status_code=404)
    # Chỉ phục vụ ảnh (upload mới đã verify magic bytes); header chống sniff/XSS
    # nếu có file lạ lọt vào từ trước. Giữ inline để <img> trong UI vẫn hiển thị.
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"):
        return JSONResponse({"error": "File không phải định dạng ảnh cho phép"}, status_code=404)
    import mimetypes
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return FileResponse(str(path), media_type=mime, headers={
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
    })


CHARACTERS_FILE = WORKSPACE / "_characters.json"

DEFAULT_CHARACTERS = [
    {
        "id": "char_luong_phong",
        "name": "Lương Phong",
        "aliases": ["Lương Phong", "luong phong", "kiếm sĩ", "swordsman"],
        "avatar": "",
        "description": "tall solitary swordsman, cold piercing gaze, wearing tattered dark hanfu robe and weathered bamboo conical hat covering his eyes, carrying ancient silver sword, cinematic lighting",
        "voice": "vi-VN-NamMinhNeural",
    },
    {
        "id": "char_phung_anh",
        "name": "Phụng Anh",
        "aliases": ["Phụng Anh", "phung anh", "nữ sát thủ", "masked woman"],
        "avatar": "",
        "description": "mysterious agile woman assassin in deep indigo silk robes, wearing half-face silver veil mask, sharp intense eyes, holding twin silver daggers, elegant and lethal",
        "voice": "vi-VN-HoaiMyNeural",
    },
    {
        "id": "char_giang_tran",
        "name": "Giang Trần",
        "aliases": ["Giang Trần", "giang tran", "thủ lĩnh", "lead assassin"],
        "avatar": "",
        "description": "fierce rugged assassin leader with scarred jaw, muscular build, dark leather battle armor, wielding giant heavy sabre, aggressive combat stance",
        "voice": "vi-VN-NamMinhNeural",
    },
]


def load_characters() -> List[Dict]:
    if not CHARACTERS_FILE.exists():
        try:
            save_characters(DEFAULT_CHARACTERS)
        except Exception:
            logger.warning("Không ghi được _characters.json mặc định; dùng dữ liệu trong bộ nhớ.")
        return DEFAULT_CHARACTERS
    try:
        with open(CHARACTERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_CHARACTERS


def save_characters(chars: List[Dict]):
    try:
        _atomic_write_json(CHARACTERS_FILE, chars)
    except Exception:
        logger.exception("Không ghi được _characters.json")
        raise


class CharacterModel(BaseModel):
    id: Optional[str] = None
    name: str = Field(max_length=200)
    aliases: Optional[List[str]] = []
    avatar: Optional[str] = ""
    description: Optional[str] = Field(default="", max_length=5000)
    voice: Optional[str] = Field(default="vi-VN-HoaiMyNeural", max_length=200)
    character_sheet: Optional[Dict[str, str]] = Field(default_factory=lambda: {
        "front": "", "side": "", "closeup": "", "fullbody": ""
    })


@app.get("/api/characters")
def api_get_characters():
    """Lấy danh sách hồ sơ nhân vật kèm Character Sheet (Production Bible)."""
    return load_characters()


@app.post("/api/characters")
def api_save_character(char: CharacterModel):
    """Tạo mới hoặc cập nhật hồ sơ nhân vật."""
    chars = load_characters()
    c_id = char.id or f"char_{uuid.uuid4().hex[:8]}"
    aliases = char.aliases or []
    if char.name and char.name not in aliases:
        aliases.insert(0, char.name)

    sheet = char.character_sheet or {"front": "", "side": "", "closeup": "", "fullbody": ""}
    for k in ["front", "side", "closeup", "fullbody"]:
        if k not in sheet:
            sheet[k] = ""

    avatar_val = char.avatar or ""
    if avatar_val and not sheet.get("front"):
        sheet["front"] = avatar_val
    elif sheet.get("front") and not avatar_val:
        avatar_val = sheet["front"]

    data = {
        "id": c_id,
        "name": char.name.strip(),
        "aliases": [a.strip() for a in aliases if a.strip()],
        "avatar": avatar_val,
        "description": char.description.strip() if char.description else "",
        "voice": char.voice or "vi-VN-HoaiMyNeural",
        "character_sheet": sheet,
    }

    found = False
    for i, c in enumerate(chars):
        if c.get("id") == c_id:
            chars[i] = data
            found = True
            break
    if not found:
        chars.insert(0, data)

    try:
        save_characters(chars)
    except Exception as exc:
        return JSONResponse({"error": f"Không lưu được hồ sơ nhân vật: {exc}"}, status_code=500)
    return {"success": True, "character": data}


@app.delete("/api/characters/{char_id}")
def api_delete_character(char_id: str):
    """Xóa hồ sơ nhân vật."""
    chars = load_characters()
    filtered = [c for c in chars if c.get("id") != char_id]
    try:
        save_characters(filtered)
    except Exception as exc:
        return JSONResponse({"error": f"Không xoá được hồ sơ nhân vật: {exc}"}, status_code=500)
    return {"success": True, "id": char_id}


class SheetGenRequest(BaseModel):
    angle: str = Field(default="front", max_length=20)  # "front" | "side" | "closeup" | "fullbody" | "all"
    prompt_override: Optional[str] = Field(default="", max_length=5000)

@app.post("/api/characters/{char_id}/generate-sheet")
async def api_generate_character_sheet(char_id: str, req: SheetGenRequest):
    """
    Module 1: Production Bible / Character Sheet Generator
    Sử dụng Muse2API /v1/images/generations (hỗ trợ reference_image) để tạo ra bảng nhân vật
    đa góc độ (chính diện, góc nghiêng, cận mặt, toàn thân).
    Sử dụng ảnh gốc làm reference_image để giữ nguyên khuôn mặt và tạo hình bất biến qua các góc!
    """
    chars = load_characters()
    target = None
    for c in chars:
        if c.get("id") == char_id:
            target = c
            break
    if not target:
        raise HTTPException(404, "Không tìm thấy hồ sơ nhân vật")
    require_muse_key()  # 503 rõ ràng nếu chưa cấu hình key

    char_sheet = target.get("character_sheet") or {"front": "", "side": "", "closeup": "", "fullbody": ""}
    for k in ["front", "side", "closeup", "fullbody"]:
        if k not in char_sheet:
            char_sheet[k] = ""

    angles = ["front", "side", "closeup", "fullbody"] if req.angle == "all" else [req.angle]

    angle_prompts = {
        "front": "master character sheet reference, direct front-facing portrait looking straight at camera, symmetrical cinematic lighting, photorealistic 8k, neutral studio background, ultra-sharp facial features, masterpiece, no text, no watermark",
        "side": "master character sheet reference, 3/4 angle side profile portrait, dramatic cinematic rim lighting, sharp jawline, 85mm portrait lens, photorealistic 8k, masterpiece, no text, no watermark",
        "closeup": "master character sheet reference, extreme close-up on eyes and intense facial expression, highly detailed skin texture, emotional gaze, macro 100mm lens, photorealistic 8k, masterpiece, no text, no watermark",
        "fullbody": "master character sheet reference, full body standing heroic action pose, head to toe visible, dynamic heroic stance, detailed warrior clothing, cape and weapon, volumetric cinematic lighting, photorealistic 8k, masterpiece, no text, no watermark",
    }

    base_ref = target.get("avatar") or char_sheet.get("front") or ""

    client = get_http_client()
    if True:  # giữ nguyên indent khối for bên dưới (client dùng chung, không đóng)
        for ang in angles:
            extra = req.prompt_override if req.prompt_override else angle_prompts.get(ang, angle_prompts["front"])
            full_prompt = f"{target.get('name', '')}, {target.get('description', '')}, {extra}"

            payload = {
                "prompt": full_prompt,
                "size": "1:1",
                "model": "muse-image"
            }
            if base_ref:
                ref_str = str(base_ref)
                if ref_str.startswith("/api/uploads/"):
                    local_p = WORKSPACE / "_uploads" / os.path.basename(ref_str)
                    if local_p.exists():
                        with open(local_p, "rb") as f:
                            payload["reference_image"] = f"data:image/png;base64,{base64.b64encode(f.read()).decode('ascii')}"
                    else:
                        payload["reference_image"] = f"http://localhost:8000{ref_str}"
                elif os.path.exists(ref_str) and os.path.isfile(ref_str):
                    with open(ref_str, "rb") as f:
                        payload["reference_image"] = f"data:image/png;base64,{base64.b64encode(f.read()).decode('ascii')}"
                else:
                    payload["reference_image"] = ref_str

            try:
                res = await client.post(
                    f"{MUSE_API_URL}/v1/images/generations",
                    json=payload,
                    headers={"Authorization": f"Bearer {MUSE_API_KEY}", "Content-Type": "application/json"},
                    timeout=180.0,
                )
                if res.status_code == 200:
                    data = res.json()
                    img_data = data.get("data", [])
                    if img_data:
                        img_url = img_data[0].get("url") or ""
                        if img_url:
                            if img_url.startswith("/"):
                                img_url = f"{MUSE_API_URL}{img_url}"
                            img_resp = await client.get(img_url, timeout=180.0)
                            if img_resp.status_code == 200:
                                fname = f"bible_{char_id}_{ang}_{uuid.uuid4().hex[:6]}.png"
                                save_path = WORKSPACE / "_uploads" / fname
                                (WORKSPACE / "_uploads").mkdir(parents=True, exist_ok=True)
                                with open(save_path, "wb") as f:
                                    f.write(img_resp.content)
                                local_url = f"/api/uploads/{fname}"
                                char_sheet[ang] = local_url
                                if ang == "front" and not target.get("avatar"):
                                    target["avatar"] = local_url
                                if not base_ref and ang == "front":
                                    base_ref = local_url
                else:
                    print(f"[GenerateSheetError] Angle {ang} status {res.status_code}: {res.text}")
            except Exception as exc:
                print(f"[GenerateSheetException] Angle {ang}: {exc}")

    target["character_sheet"] = char_sheet
    try:
        save_characters(chars)
    except Exception as exc:
        logger.warning("Không lưu được character sheet cho %s: %s", char_id, exc)
    return {"success": True, "character": target, "sheet": char_sheet}


class SheetAngleRequest(BaseModel):
    angle: str = Field(max_length=20)
    image_url: str


@app.post("/api/characters/{char_id}/set-sheet-angle")
def api_set_character_sheet_angle(char_id: str, req: SheetAngleRequest):
    """Gán thủ công ảnh cho 1 góc trong Production Bible."""
    chars = load_characters()
    for c in chars:
        if c.get("id") == char_id:
            if "character_sheet" not in c:
                c["character_sheet"] = {"front": "", "side": "", "closeup": "", "fullbody": ""}
            c["character_sheet"][req.angle] = req.image_url.strip()
            if req.angle == "front" and not c.get("avatar"):
                c["avatar"] = req.image_url.strip()
            try:
                save_characters(chars)
            except Exception as exc:
                return JSONResponse({"error": f"Không lưu được character sheet: {exc}"}, status_code=500)
            return {"success": True, "character": c}
    raise HTTPException(404, "Không tìm thấy hồ sơ nhân vật")


@app.get("/api/jobs/{job_id}/last-frame/{scene_idx}")
def api_get_scene_last_frame(job_id: str, scene_idx: int):
    """Lấy khung hình cuối cùng (last frame) đã trích xuất từ video của cảnh an toàn."""
    idx = int(scene_idx)
    frame_path = resolve_safe_job_path(job_id, f"scenes/scene_{idx:03d}_last_frame.jpg")
    if frame_path.exists() and frame_path.is_file():
        return FileResponse(str(frame_path), media_type="image/jpeg")
    raise HTTPException(404, "Không tìm thấy khung hình cuối")


class BeatBreakdownRequest(BaseModel):
    script_text: str = Field(max_length=50000)
    target_beat_duration: int = Field(default=4, ge=1, le=10)


@app.post("/api/ai/breakdown-beats")
async def api_breakdown_beats(req: BeatBreakdownRequest):
    """
    Module 2: Phân rã kịch bản thành các nhịp (beat) điện ảnh 2-5 giây,
    tập trung vào 1 hành động/góc máy duy nhất và chuẩn bị nối tiếp First/Last Frame Chaining.
    """
    raw = req.script_text.strip()
    if not raw:
        return {"success": True, "beats": []}

    beats = []
    prompt = f"""Bạn là Đạo diễn Điện ảnh và Chuyên gia Phân cảnh Hollywood.
Hãy phân rã kịch bản sau thành các "nhịp" (visual beats) điện ảnh ngắn dài 3-5 giây mỗi nhịp.
Mỗi nhịp đại diện cho 1 cú máy duy nhất (1 góc máy, 1 hành động cụ thể), chuẩn bị cho kỹ thuật nối tiếp khung hình First/Last Frame Chaining.

Kịch bản:
{raw}

Trả về JSON mảng các đối tượng:
[
  {{
    "title": "Tên nhịp (vd: Nhịp 1.1: Ánh mắt cảnh giác)",
    "prompt": "Prompt tiếng Anh điện ảnh chi tiết góc máy, ánh sáng, hành động ngắn duy nhất",
    "voiceover": "Câu thoại hoặc lời dẫn ngắn gọn cho nhịp này",
    "duration": 4,
    "camera_angle": "closeup | side | front | fullbody",
    "chain_from_previous": true
  }}
]
Chỉ trả về JSON thuần, không giải thích thêm."""

    try:
        if not MUSE_API_KEY:
            # Không có key thì bỏ qua hẳn lượt gọi AI, dùng fallback cục bộ bên dưới.
            raise RuntimeError("Muse API key chưa được cấu hình — dùng fallback cục bộ")
        async with _shared_client_ctx() as client:
            res = await client.post(
                f"{MUSE_API_URL}/v1/chat/completions",
                json={
                    "model": "muse-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.4
                },
                headers={"Authorization": f"Bearer {MUSE_API_KEY}"},
                timeout=45.0,
            )
            if res.status_code == 200:
                content = res.json()["choices"][0]["message"]["content"]
                m = re.search(r"\[\s*\{.*\}\s*\]", content, re.DOTALL)
                if m:
                    beats = json.loads(m.group(0))
    except Exception as e:
        print(f"[BeatBreakdownError] {e}")

    if not beats:
        lines = [l.strip() for l in raw.split("\n") if len(l.strip()) > 8]
        angles = ["front", "closeup", "side", "fullbody"]
        for i, line in enumerate(lines):
            ang = angles[i % len(angles)]
            beats.append({
                "title": f"Nhịp {i+1}",
                "prompt": f"Cinematic {ang} shot, {line}, photorealistic 8k, 35mm film grain, masterpiece",
                "voiceover": line,
                "duration": req.target_beat_duration,
                "camera_angle": ang,
                "chain_from_previous": (i > 0)
            })

    return {"success": True, "beats": beats}


@app.get("/api/character-library")
def get_character_library():
    """Lấy danh sách các ảnh nhân vật đã tải lên gần đây để tái sử dụng."""
    if not UPLOAD_DIR.exists():
        return []
    items = []
    for f in sorted(UPLOAD_DIR.iterdir(), key=os.path.getmtime, reverse=True):
        if f.is_file() and f.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp"]:
            orig_name = f.name.split("_", 1)[-1] if "_" in f.name else f.name
            items.append({
                "filename": orig_name,
                "saved_name": f.name,
                "url": f"/api/uploads/{f.name}",
            })
    return items[:20]


@app.get("/api/voices")
def list_voices():
    """Danh sách giọng đọc hỗ trợ đa engine (VieNeu-TTS, OmniVoice, Edge-TTS, AI Native)."""
    return list_all_voices()


@app.get("/api/voices/preview")
async def preview_voice(voice: str = "vi-VN-HoaiMyNeural", text: str = ""):
    """Sinh nhanh đoạn audio mẫu để người dùng nghe thử giọng nói trực tiếp trên web."""
    sample_text = text.strip()
    if voice in ("ai_voice", "native", "none", "no_dub"):
        sample_text = "Chế độ giọng gốc AI: Lời thoại sẽ được đưa thẳng vào Prompt để Muse AI tự sinh âm thanh và khẩu hình trực tiếp trong video."
        voice = "vi-VN-HoaiMyNeural"
    elif not sample_text:
        if voice.startswith("vieneu:"):
            cfg = next((c for c in VIENEU_VOICES_CONFIG if c["id"] == voice), None)
            sample_text = cfg["sample_text"] if cfg else "Xin chào, tôi là trợ lý thuyết minh giọng nói VieNeu TTS."
        elif voice.startswith("omnivoice:"):
            cfg = next((c for c in OMNIVOICE_VOICES_CONFIG if c["id"] == voice), None)
            sample_text = cfg["sample_text"] if cfg else "Xin chào, tôi là trợ lý giọng nói OmniVoice đa ngôn ngữ."
        elif voice.startswith("vi"):
            sample_text = "Xin chào, tôi là trợ lý thuyết minh giọng nói của AI Movie Studio."
        elif voice.startswith("zh"):
            sample_text = "你好，我是电影配音助理。"
        elif voice.startswith("ja"):
            sample_text = "こんにちは、AIムービースタジオのナレーションです。"
        elif voice.startswith("ko"):
            sample_text = "안녕하세요, AI 무비 스튜디오 나레이션입니다。"
        else:
            sample_text = "Hello, I am the voice actor for your cinematic movie."

    import hashlib
    cache_dir = WORKSPACE / "_voice_previews"
    cache_dir.mkdir(exist_ok=True)
    # Sanitize voice thành tên file an toàn (audit §2.7): chỉ [A-Za-z0-9_:-],
    # thay ':' bằng '_', cấm '/', '\\', '..'. Vd 'vieneu:AdamBua' -> 'vieneu_AdamBua'.
    safe_voice_filename = re.sub(r"[^A-Za-z0-9_:\-]", "_", voice or "")
    safe_voice_filename = safe_voice_filename.replace(":", "_").strip("._")[:80] or "voice"
    if text.strip():
        text_hash = hashlib.md5(sample_text.encode("utf-8")).hexdigest()[:8]
        cache_file = cache_dir / f"{safe_voice_filename}_{text_hash}.mp3"
    else:
        cache_file = cache_dir / f"{safe_voice_filename}.mp3"

    # Phòng thủ chiều sâu: đảm bảo path cuối vẫn nằm trong cache_dir.
    cache_file = cache_file.resolve()
    if not cache_file.is_relative_to(cache_dir.resolve()):
        raise HTTPException(status_code=400, detail="Tên giọng không hợp lệ")

    if not cache_file.exists() or cache_file.stat().st_size < 500:
        await synthesize_voice(sample_text, voice, str(cache_file))

    return FileResponse(str(cache_file), media_type="audio/mpeg")


@app.get("/api/auth/status")
async def auth_status():
    """Cho frontend biết server có bắt buộc X-API-Key không. Miễn auth.

    Chỉ trả boolean — không trả key hay bất kỳ secret nào.
    """
    return {"auth_required": bool(os.getenv("STUDIO_API_KEY"))}


@app.get("/api/health")
async def health_check():
    """Kiểm tra kết nối tới muse2api gateway."""
    muse_url = MUSE_API_URL
    studio_ok = True
    muse_ok = False
    muse_error = ""
    accounts = 0
    try:
        if not MUSE_API_KEY:
            raise RuntimeError("Muse API key chưa được cấu hình (MUSE2API_KEY)")
        async with _shared_client_ctx() as client:
            r = await client.get(
                f"{muse_url}/admin/accounts",
                headers={"Authorization": f"Bearer {MUSE_API_KEY}"},
                timeout=15.0,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    accounts = len(data)
                elif isinstance(data, dict):
                    accounts = data.get("stats", {}).get("total", len(data.get("accounts", [])))
                muse_ok = True
            else:
                muse_error = f"HTTP {r.status_code}"
    except Exception as e:
        muse_error = str(e)[:120]

    return {
        "studio": studio_ok,
        "muse2api": muse_ok,
        "muse_url": muse_url,
        "accounts": accounts,
        "muse_error": muse_error,
    }


class ScriptConvertRequest(BaseModel):
    raw_script: str = Field(max_length=100000)
    style_preset: Optional[str] = Field(default="shot on ARRI Alexa 35, Zeiss Master Prime, anamorphic widescreen, 4K, teal and orange", max_length=5000)


def local_convert_script(raw: str, style_preset: Optional[str] = "") -> str:
    """
    Bộ chuyển thể kịch bản ngoại tuyến thông minh (Local Storyboard Fallback).
    Hỗ trợ mọi định dạng: Timeline (0-10s:, 10-20s:), Cảnh 1, Shot 1, kịch bản tự do.
    Tự động bóc tách:
    - Thời lượng (duration)
    - Mô tả bối cảnh & chuyển động camera (PROMPT)
    - Tên nhân vật & lời thoại tiếng Việt (🎙️ Hội thoại tiếng Việt)
    """
    import re
    raw = raw.strip()
    if not raw:
        return ""

    # 1. Phân tách thành từng khối cảnh
    blocks = []
    if re.search(r'(?:^|\n)\s*\d+\s*[-–—]\s*\d+\s*s?\s*:', raw, re.IGNORECASE):
        # Định dạng mốc thời gian (0-10s:, 10-20s:)
        parts = re.split(r'(?=(?:^|\n)\s*\d+\s*[-–—]\s*\d+\s*s?\s*:)', raw, flags=re.IGNORECASE)
        blocks = [p.strip() for p in parts if p.strip()]
    elif re.search(r'(?:^|\n)\s*(?:cảnh|shot|hồi|phân cảnh|scene)\s*\d+', raw, re.IGNORECASE):
        parts = re.split(r'(?=(?:^|\n)\s*(?:cảnh|shot|hồi|phân cảnh|scene)\s*\d+)', raw, flags=re.IGNORECASE)
        blocks = [p.strip() for p in parts if p.strip()]
    elif "\n\n" in raw:
        blocks = [p.strip() for p in raw.split("\n\n") if p.strip()]
    else:
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        blocks = lines

    preset = style_preset or "shot on ARRI Alexa 35, Zeiss Master Prime, anamorphic 2.39:1, 4k, dramatic volumetric lighting, photorealistic 8k, masterpiece, no text, no watermark"

    camera_presets = [
        "Cinematic wide establishing shot, slow dramatic push in",
        "Dynamic medium tracking shot with subtle handheld movement",
        "Intense close-up shot, shallow depth of field, anamorphic bokeh",
        "Low-angle heroic tracking shot with volumetric rim lighting",
        "Over-the-shoulder dramatic perspective, atmospheric lighting",
        "Sweeping epic panoramic 360 camera arc, cinematic contrast"
    ]

    output_shots = []
    for idx, block in enumerate(blocks, 1):
        clean = block

        # Trích xuất thời lượng từ timeline 0-10s: hoặc duration:
        duration = 5
        range_m = re.search(r'(?:^|\n)\s*(\d+)\s*[-–—]\s*(\d+)\s*s?\s*:', clean, re.IGNORECASE)
        shot_title = f"Phân cảnh {idx}"
        if range_m:
            s_sec = int(range_m.group(1))
            e_sec = int(range_m.group(2))
            if e_sec > s_sec:
                duration = min(10, max(3, e_sec - s_sec))
                shot_title = f"Phân cảnh {idx} ({s_sec}-{e_sec}s)"
            clean = clean[:range_m.start()] + clean[range_m.end():]
        else:
            dur_m = re.search(r'duration:\s*(\d+)s?', clean, re.IGNORECASE)
            if dur_m:
                duration = min(10, max(3, int(dur_m.group(1))))
                clean = clean[:dur_m.start()] + clean[dur_m.end():]

        # Xóa header Cảnh/Shot cũ nếu có
        clean = re.sub(r'^(?:#\s*)?(?:🎥|🎬\s*)?(?:cảnh|shot|hồi|phân cảnh|scene)\s*[\d\.]+[\s–—:.-]*', '', clean.strip(), flags=re.IGNORECASE)

        # Trích xuất Màu sắc / Ánh sáng từ thẻ [Màu sắc: ...]
        color_note = ""
        color_m = re.search(r'\[Màu sắc:\s*([^\]]+)\]', clean, re.IGNORECASE)
        if color_m:
            color_note = color_m.group(1).strip()

        # Xóa các tag cấu trúc [...]
        without_tags = re.sub(r'\[[^\]]+\]', ' ', clean)
        without_tags = re.sub(r'\s+', ' ', without_tags).strip()

        # Trích xuất Lời thoại & Nhân vật theo mệnh đề cận kề
        dialogues = []
        speech_cuts = []
        quote_matches = list(re.finditer(r'["“]([^"”\n\r]+)["”]', without_tags))

        for qm in quote_matches:
            speech = qm.group(1).strip()
            q_idx = qm.start()
            before = without_tags[:q_idx].strip()

            spk_match = re.search(r'(?:^|[,\.!\?;\n])\s*([^,.\n:"]*?)\s*(?:nói|bảo|hỏi|đáp|thì thầm|gào thét|gầm lên|ngắt quãng|lẩm bẩm|hô|quát)?[^,.\n:"]*:\s*$', before, re.IGNORECASE)
            speaker = "Nhân vật"
            lead_in_start = q_idx

            if spk_match:
                full_match = spk_match.group(0)
                match_index = before.rfind(full_match)
                sep_m = re.match(r'^[,\.!\?;\n]\s*', full_match)
                sep_len = len(sep_m.group(0)) if sep_m else 0
                lead_in_start = match_index + sep_len

                clause = before[lead_in_start:]
                name_m = re.search(r'([A-ZÀ-Ỹ][a-zà-ỹ\w]+(?:\s+[A-ZÀ-Ỹ][a-zà-ỹ\w]+){0,3})', clause)
                if name_m:
                    speaker = name_m.group(1).strip()

            speech_cuts.append((lead_in_start, qm.end()))
            dialogues.append(f'{speaker}: "{speech}"')

        # Trích xuất Visual Prompt đầy đủ
        visual_prompt = ""
        last_idx = 0
        for start, end in speech_cuts:
            visual_prompt += without_tags[last_idx:start] + " "
            last_idx = end
        visual_prompt += without_tags[last_idx:]
        visual_prompt = re.sub(r'PROMPT:\s*', ' ', visual_prompt, flags=re.IGNORECASE)
        visual_prompt = re.sub(r'Duration:\s*\d+s?', ' ', visual_prompt, flags=re.IGNORECASE)
        visual_prompt = re.sub(r'(?:🎙️|🗣️)?\s*Hội thoại[^:]*:', ' ', visual_prompt, flags=re.IGNORECASE)
        visual_prompt = re.sub(r'\s+', ' ', visual_prompt).strip(' ,.')

        if color_note and color_note.lower() not in visual_prompt.lower():
            visual_prompt = f"{visual_prompt}, {color_note}" if visual_prompt else color_note

        if not visual_prompt:
            cam = camera_presets[(idx - 1) % len(camera_presets)]
            prompt_text = f"{cam}, {preset}"
        elif preset and not any(k in visual_prompt.lower() for k in ("arri alexa", "zeiss")):
            prompt_text = f"{visual_prompt}, {preset}"
        else:
            prompt_text = visual_prompt

        dialogue_block = "\n".join(dialogues) if dialogues else ""

        shot_block = (
            f"🎥 SHOT {idx} – {shot_title}\n"
            f"Duration: {duration}s\n\n"
            f"PROMPT:\n"
            f"{prompt_text}\n\n"
            f"🎙️ Hội thoại tiếng Việt:\n"
            f"{dialogue_block}\n"
        )
        output_shots.append(shot_block)

    return "\n---\n\n".join(output_shots)


@app.post("/api/ai/convert-script")
async def ai_convert_script(req: ScriptConvertRequest):
    """Sử dụng mô hình AI (qua muse2api) để tự động dịch/chuyển thể kịch bản thô thành kịch bản phân cảnh chuẩn Studio.
    Có fallback chuyển thể ngoại tuyến siêu tốc khi tài khoản Muse đang bận render."""
    raw = req.raw_script.strip()
    if not raw:
        return JSONResponse({"error": "Nội dung kịch bản trống"}, status_code=400)

    system_instruction = (
        "Bạn là một Đạo diễn điện ảnh Hollywood và Biên kịch gia cao cấp.\n"
        "Nhiệm vụ: Chuyển thể nội dung kịch bản của người dùng thành Kịch bản phân cảnh (Storyboard Shot-by-Shot) chuẩn kỹ thuật AI Movie Studio.\n"
        "Quy tắc bắt buộc:\n"
        "1. Xuất theo đúng định dạng sau cho từng cảnh:\n"
        "🎥 SHOT [Số cảnh] – [Tên cảnh]\n"
        "Duration: [5-10]s\n\n"
        "PROMPT:\n"
        "[Prompt tiếng Anh cực kỳ chi tiết: loại cảnh, góc quay, ánh sáng, chuyển động camera, chất lượng ARRI Alexa 35, 4k, no text, no watermark]\n\n"
        "🎙️ Hội thoại tiếng Việt:\n"
        "[Tên nhân vật]: \"[Lời thoại tiếng Việt]\"\n\n"
        "2. Không giải thích dông dài, trả về trực tiếp nội dung kịch bản phân cảnh."
    )

    try:
        if not MUSE_API_KEY:
            # Không có key thì bỏ qua hẳn lượt gọi AI, dùng fallback cục bộ bên dưới.
            raise RuntimeError("Muse API key chưa được cấu hình — dùng fallback cục bộ")
        async with _shared_client_ctx() as client:
            resp = await client.post(
                f"{MUSE_API_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {MUSE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "muse-spark",
                    "messages": [
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": f"Phong cách mong muốn: {req.style_preset}\n\nKịch bản thô cần chuyển thể:\n{raw}"}
                    ],
                    "temperature": 0.7
                },
                timeout=25.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                return {"standardized_script": reply, "engine": "ai"}
            else:
                print(f"[AI Convert] muse2api trả về HTTP {resp.status_code}, kích hoạt Local Fallback...")
    except Exception as e:
        print(f"[AI Convert Fallback] muse2api chat unavailable ({e}), using local parser engine...")

    # Chuyển đổi ngoại tuyến thông minh (Local Storyboard Fallback)
    local_script = local_convert_script(raw, req.style_preset)
    return {
        "standardized_script": local_script,
        "engine": "local",
        "notice": "Tài khoản Muse đang bận render video, hệ thống đã tự động chuyển thể kịch bản bằng Bộ công cụ ngoại tuyến thông minh!"
    }

# ── Quick Video (Proxy trực tiếp tới muse2api /v1/videos) ─────────────────

VI_CHARS = set("àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵ")


def has_vietnamese(text: str) -> bool:
    return any(c in VI_CHARS for c in (text or "").lower())


async def auto_enhance_prompt(prompt: str, size: str = "16:9") -> str:
    """
    Tự động chuẩn hóa và chuyển đổi prompt (đặc biệt là tiếng Việt hoặc mô tả ngắn)
    thành Cinematic English Video Prompt chuẩn Hollywood để Muse AI sinh video thành công 100%,
    tránh lỗi timeout '任务总等待时限已到'.
    """
    raw = prompt.strip()
    if not raw:
        return raw

    # Nếu đã là prompt tiếng Anh chi tiết, giữ nguyên
    if not has_vietnamese(raw) and len(raw.split()) >= 12 and any(k in raw.lower() for k in ("cinematic", "masterpiece", "shot", "lighting", "4k")):
        return raw

    # Thử dịch và nâng cấp qua Muse Chat
    system_inst = (
        "You are an expert Hollywood AI video prompt director. "
        "Convert the user's input (Vietnamese or short English) into a single, cohesive, vivid cinematic English video generation prompt. "
        "Include camera angle, subject actions, lighting, atmosphere, 4K resolution, photorealistic, masterpiece, no text, no watermark. "
        "Output ONLY the final English prompt as a single paragraph. Do not include markdown or explanations."
    )
    try:
        if not MUSE_API_KEY:
            # Không có key thì bỏ qua hẳn lượt gọi AI, dùng fallback heuristic bên dưới.
            raise RuntimeError("Muse API key chưa được cấu hình — dùng fallback cục bộ")
        async with _shared_client_ctx() as client:
            resp = await client.post(
                f"{MUSE_API_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {MUSE_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "muse-spark",
                    "messages": [
                        {"role": "system", "content": system_inst},
                        {"role": "user", "content": f"Aspect ratio: {size}. Prompt: {raw}"}
                    ],
                    "temperature": 0.4,
                },
                timeout=8.0,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                if content.startswith('"') and content.endswith('"'):
                    content = content[1:-1].strip()
                if len(content) > 10:
                    return content
    except Exception as e:
        print(f"[EnhancePrompt Fallback] {e}")

    # Fallback ngoại tuyến thông minh (Heuristic cinematic wrapper)
    is_vertical = any(k in size for k in ("9:16", "portrait"))
    cam = "Cinematic 9:16 vertical portrait shot" if is_vertical else "Cinematic 16:9 widescreen shot"
    return f"{cam} of {raw}, dramatic volumetric lighting, highly detailed, photorealistic 8k, 35mm film aesthetic, masterpiece, no text, no watermark"


class QuickVideoRequest(BaseModel):
    prompt: str = Field(max_length=5000)
    duration: int = Field(default=5, ge=1, le=60)
    size: str = Field(default="16:9", max_length=10)
    reference_image: Optional[str] = None


class EnhancePromptReq(BaseModel):
    prompt: str = Field(max_length=5000)
    size: str = Field(default="16:9", max_length=10)


@app.post("/api/quick-video/enhance-prompt")
async def api_quick_video_enhance_prompt(req: EnhancePromptReq):
    """Nâng cấp prompt tiếng Việt thành cinematic English prompt."""
    enhanced = await auto_enhance_prompt(req.prompt, req.size)
    return {"original": req.prompt, "enhanced": enhanced}


@app.post("/api/quick-video")
async def api_quick_video(req: QuickVideoRequest):
    """Tạo video ngắn trực tiếp qua muse2api /v1/videos, bỏ qua toàn bộ pipeline phim."""
    require_muse_key()  # 503 rõ ràng nếu chưa cấu hình key
    prompt_to_send = req.prompt.strip()
    # Tự động tối ưu hóa prompt nếu nhập tiếng Việt hoặc prompt quá ngắn để đảm bảo Muse AI nhận diện đúng
    if has_vietnamese(prompt_to_send) or len(prompt_to_send.split()) < 5:
        try:
            enhanced = await auto_enhance_prompt(prompt_to_send, req.size)
            if enhanced:
                prompt_to_send = enhanced
        except Exception:
            pass

    payload: Dict[str, Any] = {
        "prompt": prompt_to_send,
        "duration": req.duration,
        "size": req.size,
    }

    if req.reference_image:
        ref = req.reference_image.strip()
        if ref.startswith("/api/uploads/"):
            local_p = UPLOAD_DIR / os.path.basename(ref)
            if local_p.exists():
                with open(local_p, "rb") as f:
                    payload["reference_image"] = (
                        f"data:image/png;base64,{base64.b64encode(f.read()).decode('ascii')}"
                    )
            else:
                payload["reference_image"] = f"http://localhost:8000{ref}"
        else:
            payload["reference_image"] = ref

    payload["timeout"] = 600

    try:
        async with _shared_client_ctx() as client:
            r = await client.post(
                f"{MUSE_API_URL}/v1/videos",
                json=payload,
                headers={"Authorization": f"Bearer {MUSE_API_KEY}", "Content-Type": "application/json"},
                timeout=30.0,
            )
        if r.status_code in (200, 202):
            data = r.json()
            return {"success": True, **data}
        return JSONResponse(
            {"success": False, "error": f"muse2api HTTP {r.status_code}: {r.text[:300]}"},
            status_code=r.status_code,
        )
    except Exception as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)


@app.get("/api/quick-video/{task_id}")
async def api_quick_video_status(task_id: str):
    """Kiểm tra trạng thái task video nhanh từ muse2api."""
    require_muse_key()  # 503 rõ ràng nếu chưa cấu hình key
    try:
        async with _shared_client_ctx() as client:
            r = await client.get(
                f"{MUSE_API_URL}/v1/videos/{task_id}",
                headers={"Authorization": f"Bearer {MUSE_API_KEY}"},
                timeout=15.0,
            )
        if r.status_code == 200:
            data = r.json()
            # Chuẩn hóa trạng thái cho frontend
            raw_status = data.get("status", "")
            if raw_status in ("completed", "succeeded", "success", "done"):
                data["status"] = "done"
            elif raw_status in ("failed", "error"):
                data["status"] = "error"

            # Tìm URL video từ các trường có thể có của muse2api
            vurl = (
                data.get("video_url")
                or data.get("url")
                or (data.get("result") or {}).get("url")
                or (data.get("video") or {}).get("url")
            )
            if vurl:
                data["video_url"] = vurl
                data["video_proxy_url"] = f"/api/quick-video/{task_id}/stream"
            return data
        return JSONResponse({"error": f"HTTP {r.status_code}"}, status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/quick-video/{task_id}/stream")
async def api_quick_video_stream(task_id: str):
    """Proxy stream video nhanh từ muse2api về browser (tránh CORS)."""
    require_muse_key()  # 503 rõ ràng nếu chưa cấu hình key
    try:
        async with _shared_client_ctx() as status_client:
            r = await status_client.get(
                f"{MUSE_API_URL}/v1/videos/{task_id}",
                headers={"Authorization": f"Bearer {MUSE_API_KEY}"},
                timeout=15.0,
            )
        if r.status_code != 200:
            return JSONResponse({"error": "task not found"}, status_code=404)
        task_data = r.json()
        video_url = (
            task_data.get("video_url")
            or task_data.get("url")
            or (task_data.get("result") or {}).get("url")
            or (task_data.get("video") or {}).get("url")
        )
        if not video_url:
            return JSONResponse({"error": "Video chưa sẵn sàng"}, status_code=404)
        if not video_url.startswith("http"):
            video_url = f"{MUSE_API_URL}{video_url}"


        async def _stream():
            async with httpx.AsyncClient(timeout=120.0) as stream_client:
                async with stream_client.stream("GET", video_url) as resp:
                    async for chunk in resp.aiter_bytes(65536):
                        yield chunk

        return StreamingResponse(_stream(), media_type="video/mp4")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── AFFILIATE MARKETING (TIẾP THỊ LIÊN KẾT) ──────────────────────────────────

class AffiliateRenderRequest(BaseModel):
    title: str = Field(default="Video Tiếp Thị", max_length=200)
    scenes: List[Dict[str, Any]] = Field(max_length=200)
    voice: str = Field(default="vieneu:AdamBua", max_length=200)
    aspect_ratio: str = Field(default="9:16", max_length=10)
    bg_music_path: Optional[str] = None
    music_volume: Optional[float] = Field(default=0.20, ge=0.0, le=2.0)
    voice_volume: Optional[float] = Field(default=1.0, ge=0.0, le=2.0)
    quality: Optional[str] = Field(default="720p", max_length=20)
    transition: Optional[str] = Field(default="none", max_length=20)
    font_size: Optional[int] = Field(default=22, ge=8, le=72)


@app.get("/api/affiliate/templates")
async def get_affiliate_templates():
    """Trả về danh mục sản phẩm, framework kịch bản và preset mẫu tiếp thị."""
    return {
        "categories": AFFILIATE_CATEGORIES,
        "frameworks": AFFILIATE_FRAMEWORKS,
        "koc_personas": KOC_PERSONAS,
        "shooting_styles": AFFILIATE_SHOOTING_STYLES
    }


@app.post("/api/affiliate/generate-script")
async def api_generate_affiliate_script(req: ProductAffiliateRequest):
    """Sinh kịch bản video tiếp thị liên kết chuẩn chuyển đổi cao (TikTok Shop, Shopee, Reels)."""
    if not req.product_name or not req.product_name.strip():
        return JSONResponse({"error": "Vui lòng nhập tên sản phẩm"}, status_code=400)
    if MUSE_API_KEY:
        res = await generate_affiliate_script_ai(req, MUSE_API_URL, MUSE_API_KEY)
    else:
        # Không có key: bỏ qua lượt gọi AI, dùng bộ tạo cục bộ (không gọi gateway bằng key giả).
        res = generate_affiliate_script_local(req)
    return res.model_dump()


@app.post("/api/affiliate/render")
async def api_render_affiliate(req: AffiliateRenderRequest):
    """Bắt đầu sản xuất video tiếp thị liên kết dọc 9:16 hoàn chỉnh với kịch bản, lồng tiếng, phụ đề và nhạc nền."""
    if not req.scenes:
        return JSONResponse({"error": "Không có phân cảnh nào để render"}, status_code=400)

    scene_items = []
    for idx, s in enumerate(req.scenes, start=1):
        scene_items.append(SceneItem(
            index=idx,
            prompt=s.get("prompt", ""),
            voiceover=s.get("voiceover", ""),
            voice=s.get("voice") or req.voice or "vieneu:AdamBua",
            duration=int(s.get("duration", 5)),
            size=req.aspect_ratio or "9:16",
            reference_image=s.get("reference_image") or None
        ))

    movie_req = MovieRequest(
        title=req.title or "Video Tiếp Thị",
        voice=req.voice or "vieneu:AdamBua",
        voice_mode="tts",
        aspect_ratio=req.aspect_ratio or "9:16",
        font_size=req.font_size or 22,
        bg_music_path=req.bg_music_path,
        music_volume=req.music_volume if req.music_volume is not None else 0.20,
        voice_volume=req.voice_volume if req.voice_volume is not None else 1.0,
        quality=req.quality or "720p",
        transition=req.transition or "dissolve",
        scenes=scene_items
    )

    job_id = uuid.uuid4().hex[:12]
    job = new_job(job_id, len(movie_req.scenes), req=movie_req)
    _save_job_or_http500(job)
    job_log(job, f"🛍️ [Tiếp Thị Liên Kết] Dự án: '{movie_req.title}' — {len(movie_req.scenes)} cảnh (9:16) — Giọng: {movie_req.voice}")
    _save_job_or_http500(job)
    # Nếu thiếu MUSE_API_KEY thì film_pipeline đánh dấu job error rõ ràng.
    _spawn_job_task(job_id, film_pipeline(job, movie_req))

    return {"job_id": job_id, "message": "Đã bắt đầu sản xuất video tiếp thị liên kết", "total_scenes": len(movie_req.scenes)}


if __name__ == "__main__":

    import sys
    import uvicorn
    sys.stdout.reconfigure(encoding="utf-8")
    port = int(os.getenv("PORT", "8000"))
    print(f"\nAI Movie Studio running at: http://localhost:{port}\n")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
