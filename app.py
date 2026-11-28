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
import copy
import hmac
import ipaddress
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
from typing import Any, Dict, List, Literal, Optional, Set

import aiofiles
import httpx
from dotenv import load_dotenv

load_dotenv()
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, field_validator, model_validator

from batch_engine import run_batch, generate_keyframe_image, heal_prompt_with_ai, apply_style_locks, sanitize_prompt_heuristics
from composer import compose_full_movie, audit_movie_scenes, compose_scene_video, run_cancellable_compose
from consistency import run_consistency_checks
import muse_connection as muse_config
from muse_voice import MuseNarratorProfile, MuseVoiceProfile, plan_muse_speech
from tts_manager import list_all_voices, synthesize_voice, VIENEU_VOICES_CONFIG, OMNIVOICE_VOICES_CONFIG
from affiliate_engine import (
    AFFILIATE_CATEGORIES,
    AFFILIATE_FRAMEWORKS,
    KOC_PERSONAS,
    AFFILIATE_SHOOTING_STYLES,
    AFFILIATE_SCENE_DURATION,
    ProductAffiliateRequest,
    AffiliateScriptResponse,
    generate_affiliate_script_ai,
    generate_affiliate_script_local,
    import_affiliate_product_link,
    find_product_images,
)


logger = logging.getLogger("ai_movie_studio")

# ── Cấu hình ──────────────────────────────────────────────────────────────
MUSE_PROVIDER, MUSE_API_URL, MUSE_API_KEY = muse_config.muse_connection(muse_config.active_muse_provider())
_MUSE_KEY_NAME = "MUSE_BROWSER_KEY" if MUSE_PROVIDER == "browser" else "MUSE2API_KEY"
_MUSE_MAX_CONCURRENT = 12

if not MUSE_API_KEY:
    logger.warning("%s chưa được đặt. Các endpoint gọi Muse sẽ trả 503.", _MUSE_KEY_NAME)


def require_muse_key() -> str:
    if not MUSE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail=f"Muse API key chưa được cấu hình: đặt {_MUSE_KEY_NAME} rồi khởi động lại server.",
        )
    return MUSE_API_KEY


# ── Concurrency guards (audit §1.6) ───────────────────────────────────────
# Một threading.Lock duy nhất bảo vệ mọi mutation của JOBS (cả async path và
# sync/thread path đều chạm vào dict này). Chỉ giữ lock trong đoạn ngắn,
# KHÔNG await khi đang giữ lock.
JOBS_LOCK = threading.Lock()
# job_id -> asyncio.Task của pipeline đang chạy (để cancel thật, xem api_cancel_job).
JOB_TASKS: Dict[str, "asyncio.Task"] = {}
# job_id -> asyncio.Event hủy job (audit #8): film_pipeline /
# retry_failed_pipeline tạo event, truyền vào run_batch (kiểm tra trước
# mỗi cảnh + trong vòng poll); api_cancel_job set event này.
JOB_CANCEL_EVENTS: Dict[str, "asyncio.Event"] = {}
# Rate-limit: tối đa _RATE_LIMIT_REQUESTS request /api/* mỗi _RATE_LIMIT_WINDOW_S
# cho mỗi IP (bộ nhớ tiến trình; không thêm dependency slowapi/redis).
_RATE_LIMIT_REQUESTS = 300
_RATE_LIMIT_WINDOW_S = 60.0
# Audit #5a (2026-11-25): gioi han kich thuoc dict bucket — vuot nguong thi
# don cac bucket het han (truoc day khong bao gio xoa key IP cu -> ro ri RAM).
_RATE_LIMIT_MAX_BUCKETS = 10000
_rate_limit_buckets: Dict[str, list] = {}  # ip -> [window_start_monotonic, count]
_RATE_LIMIT_LOCK = threading.Lock()
_AUTH_WARNING_LOGGED = False


def _client_ip_from_scope(scope) -> str:
    """IP client that de tinh rate-limit / log.

    Audit #5a (2026-11-25): truoc day doc thang scope["client"] — sau
    proxy moi client chung 1 bucket (local thi tat ca la 127.0.0.1 nen
    limit 300/phut thanh global). Gio uu tien X-Forwarded-For (entry
    dau tien = client goc) NHUNG chi khi peer truc tiep KHONG phai IP
    cong khai (loopback / private / CGNAT — tuc la dang sau proxy tin
    cay hoac chay local). Peer cong khai ma gui XFF thi bo qua XFF de
    khong ai tu gia mao IP ne rate-limit. XFF khong hop le thi dung peer.
    """
    client = scope.get("client") or ("unknown", 0)
    peer = str(client[0]) if client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if peer_ip.is_global:
        return peer
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers", [])}
    xff = headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            pass
    return peer


async def _send_json_via_asgi(send, status: int, payload: Dict[str, Any], extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [[b"content-type", b"application/json"]]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _RateLimitMiddleware:
    """Giới hạn 300 request/phút/IP cho /api/* (bộ nhớ tiến trình), vượt -> 429.
    Pure-ASGI passthrough: không đọc/đệm body nên không ảnh hưởng SSE/stream.
    IP lay qua _client_ip_from_scope (uu tien X-Forwarded-For khi peer
    khong phai IP cong khai); bucket het han duoc don khi dict vuot nguong
    (audit #5a, 2026-11-25)."""

    def __init__(self, app, limit: int = _RATE_LIMIT_REQUESTS, window: float = _RATE_LIMIT_WINDOW_S):
        self.app = app
        self.limit = limit
        self.window = window

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        ip = _client_ip_from_scope(scope)
        now = time.monotonic()
        with _RATE_LIMIT_LOCK:
            bucket = _rate_limit_buckets.get(ip)
            if bucket is None or now - bucket[0] >= self.window:
                if bucket is None and len(_rate_limit_buckets) >= _RATE_LIMIT_MAX_BUCKETS:
                    # Don bucket het han de dict khong phi vo han.
                    for _old_ip in [k for k, v in _rate_limit_buckets.items()
                                    if now - v[0] >= self.window]:
                        del _rate_limit_buckets[_old_ip]
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


_STUDIO_SESSION_COOKIE = "ams_session"
_STUDIO_SESSION_SECONDS = 12 * 60 * 60


def _studio_key_matches(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _studio_session_signature(key: str, claims: str) -> str:
    return hmac.new(key.encode("utf-8"), f"studio-session:{claims}".encode("utf-8"), "sha256").hexdigest()


def _studio_session_valid(request: Request, key: str) -> bool:
    token = request.cookies.get(_STUDIO_SESSION_COOKIE, "")
    match = re.fullmatch(r"(\d{1,12}\.[a-f0-9]{32})\.([a-f0-9]{64})", token, flags=re.ASCII)
    if match is None:
        return False
    claims, signature = match.groups()
    expires_at = int(claims.split(".", 1)[0])
    return expires_at > time.time() and hmac.compare_digest(signature, _studio_session_signature(key, claims))


class _ApiKeyAuthMiddleware:
    """Authenticate API clients by header and browsers by session cookie."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        if path in ("/api/health", "/api/auth/status", "/api/auth/session"):
            await self.app(scope, receive, send)
            return
        expected = os.getenv("STUDIO_API_KEY")
        if not expected:
            global _AUTH_WARNING_LOGGED
            if not _AUTH_WARNING_LOGGED:
                _AUTH_WARNING_LOGGED = True
                logger.warning("STUDIO_API_KEY chưa được đặt, Studio chạy ở chế độ local.")
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        provided = request.headers.get("x-api-key")
        if provided is not None and _studio_key_matches(provided, expected):
            await self.app(scope, receive, send)
            return
        if _studio_session_valid(request, expected):
            origin = request.headers.get("origin")
            if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None:
                if origin != f"{request.url.scheme}://{request.url.netloc}":
                    await _send_json_via_asgi(send, 403, {"error": "Origin không hợp lệ."})
                    return
            await self.app(scope, receive, send)
            return
        await _send_json_via_asgi(send, 401, {"error": "Cần đăng nhập Studio hoặc gửi X-API-Key."})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Một httpx.AsyncClient dùng chung (connection pooling, audit §3.3) +
    load persisted jobs 1 lần lúc startup (audit §1.5)."""
    app.state.http_client = httpx.AsyncClient()
    try:
        load_all_persisted_jobs()
    except Exception as exc:  # startup không được chết vì job_state hỏng
        logger.warning("Không load được persisted jobs lúc startup: %s", exc)
    try:
        from batch_video_engine import start_batch_runtime

        await asyncio.to_thread(start_batch_runtime)
    except Exception as exc:
        logger.warning("Không thể khởi động lại hàng đợi Batch Video: %s", exc)
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
PROTECTED_WORKSPACE_NAMES = {
    "_uploads", "_music", "_projects", "_shot_desk_projects",
    "_batch_videos", "_story_projects", "_voice_previews",
    "_tts_cache", "_factory_cache", "_characters.json",
}
_ALLOWED_BGM_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def _validate_safe_studio_bgm_field(value: Optional[str]) -> Optional[str]:
    """Validate bg_music_path ở biên API: chỉ nhận file nằm trong workspace/_music hoặc workspace/_uploads."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return ""
    norm = raw.replace("\\", "/")
    if ".." in Path(norm).parts:
        raise ValueError("bg_music_path không được chứa '..'")
    music_root = MUSIC_DIR.resolve()
    upload_root = UPLOAD_DIR.resolve()
    ws_root = WORKSPACE.resolve()

    if norm.startswith("/api/music/"):
        cand = (MUSIC_DIR / Path(norm).name).resolve()
    elif norm.startswith("/api/uploads/"):
        cand = (UPLOAD_DIR / Path(norm).name).resolve()
    elif "/" not in norm and ":" not in norm:
        cand = (MUSIC_DIR / norm).resolve()
    else:
        p = Path(raw)
        cand = p.resolve() if p.is_absolute() else ((ws_root / norm).resolve() if norm.startswith(("_music/", "_uploads/")) else p.resolve())

    if not (cand.is_relative_to(music_root) or cand.is_relative_to(upload_root)):
        raise ValueError("bg_music_path chỉ cho phép file nằm trong workspace/_music hoặc workspace/_uploads")
    if cand.suffix.lower() not in _ALLOWED_BGM_EXTS:
        raise ValueError(f"bg_music_path không phải định dạng nhạc hỗ trợ ({sorted(_ALLOWED_BGM_EXTS)})")
    return raw


def _resolve_existing_studio_bgm(value: Optional[str]) -> Optional[str]:
    """Chuẩn hóa bg_music_path thành đường dẫn file thực tế tồn tại trong workspace/_music hoặc _uploads."""
    validated = _validate_safe_studio_bgm_field(value)
    if not validated:
        return None
    norm = validated.replace("\\", "/")
    ws_root = WORKSPACE.resolve()
    if norm.startswith("/api/music/"):
        cand = (MUSIC_DIR / Path(norm).name).resolve()
    elif norm.startswith("/api/uploads/"):
        cand = (UPLOAD_DIR / Path(norm).name).resolve()
    elif "/" not in norm and ":" not in norm:
        cand = (MUSIC_DIR / norm).resolve()
        if not cand.is_file():
            cand = (UPLOAD_DIR / norm).resolve()
    elif norm.startswith(("_music/", "_uploads/")):
        cand = (ws_root / norm).resolve()
    else:
        cand = Path(validated).resolve()
    if (cand.is_relative_to(MUSIC_DIR.resolve()) or cand.is_relative_to(UPLOAD_DIR.resolve())) and cand.is_file():
        return str(cand)
    return None

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

class _SelectiveGZipMiddleware:
    """Nén GZip cho HTML/CSS/JS/JSON >= 1KB, nhưng bỏ qua SSE (/sse) và các
    luồng media đã nén sẵn (.mp4/.webp/.mp3/.zip) để không buffer SSE hay tốn CPU vô ích."""

    _SKIP_SUFFIXES = (".mp4", ".webm", ".webp", ".png", ".jpg", ".jpeg", ".gif", ".mp3", ".wav", ".ogg", ".m4a", ".zip")

    def __init__(self, app):
        self.raw_app = app
        self.gzip_app = GZipMiddleware(app, minimum_size=1000, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = (scope.get("path") or "").lower()
            if (
                path.endswith("/sse")
                or "/video/" in path
                or "/clips/" in path
                or "/download/" in path
                or path.endswith(self._SKIP_SUFFIXES)
            ):
                await self.raw_app(scope, receive, send)
                return
        await self.gzip_app(scope, receive, send)


# ── FastAPI App ────────────────────────────────────────────────────────────
app = FastAPI(title="AI Movie Studio", version="1.0.0", lifespan=lifespan)
app.add_middleware(_SelectiveGZipMiddleware)
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
    delivery: Optional[Literal["on_screen", "offscreen", "radio", "phone", "speaker", "thought"]] = None


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
    keep_reference: bool = False
    characters: List[str] = Field(default_factory=list, max_length=30)
    setting: str = Field(default="", max_length=500)
    dialogue: str = Field(default="", max_length=3000)
    new_segment: bool = False
    transition_out: str = Field(default="", max_length=50)
    offscreen_narration: bool = False
    chain_from_previous: Optional[bool] = False # First/Last Frame Chaining: kế thừa khung hình cuối của cảnh trước
    sheet_angle: Optional[str] = "front"        # "front" | "side" | "closeup" | "fullbody"
    last_frame_url: Optional[str] = None
    # Nhóm 1 (render tự động): bản prompt an toàn đã chốt sẵn cho cảnh này.
    # Khi engine từ chối prompt gốc, pipeline tự thử ĐÚNG 1 lần bằng bản này
    # (dán nguyên văn, không sanitize thêm); không có thì fallback sanitize cũ.
    compliance_prompt: Optional[str] = Field(default=None, max_length=5000)


class CaptionSettings(BaseModel):
    subtitle_style: Literal["cinema", "headline", "boxed", "karaoke", "word_box", "neon"] = "cinema"
    subtitle_position: Literal["bottom", "middle", "top"] = "bottom"


class MovieRequest(CaptionSettings):
    world_bible: Dict[str,str] = Field(default_factory=dict,max_length=100)

    def render_scene(self, scene):
        result = scene.model_dump()
        result["world_description"] = self.world_bible.get(scene.setting, "")
        if self.muse_narrator:
            result["muse_narrator"] = self.muse_narrator.model_dump()
        return result

    title: str = Field(default="my_movie", max_length=200)
    muse_narrator: MuseNarratorProfile | None = None
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=200)  # Giọng mặc định toàn phim
    voice_mode: Optional[str] = "tts"    # "tts" | "ai_voice" (đưa thoại vào prompt để AI tự sinh tiếng)
    font_size: int = Field(default=22, ge=8, le=72)
    aspect_ratio: Optional[str] = Field(default="16:9", max_length=10)  # Tỉ lệ khung hình toàn phim (16:9, 9:16, 1:1, 21:9, 4:3)
    export_mode: str = Field(default="full", max_length=20)  # "full" | "batch_clips"
    max_concurrent: Optional[int] = Field(default=12, ge=1, le=12)
    chain_mode: Optional[str] = "none"   # "none" | "sequential"
    chain_fallback_mode: Optional[str] = "independent" # "independent" | "fallback_image" | "stop"
    # Chế độ đồng nhất: "standard" (mặc định, tương thích API cũ) | "max"
    # (Đồng nhất tối đa: ép chain_mode="sequential" + bắt buộc preflight
    # kiểm tra khóa nhân vật/cầu nối/thời lượng trước khi tạo job).
    consistency_mode: str = Field(default="standard", max_length=20)
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

    @field_validator("bg_music_path")
    @classmethod
    def _check_bg_music_path(cls, v: Optional[str]) -> Optional[str]:
        return _validate_safe_studio_bgm_field(v)



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


def _sync_job_srt_file(job: Dict, compose_result: Any) -> None:
    """Đồng bộ job['srt_file'] với kết quả compose_full_movie (fix 2026-11-10):
    chỉ chế độ 'srt_only' mới có file SRT riêng trong final/ (burn_in đã
    khắc phụ đề vào video, 'none' thì không có SRT). Trước đây pipeline kiểm
    tra file final/{title}.srt — tên file đó không còn ai ghi nữa (compose
    ghi {safe_title}_{ts}.srt) nên job['srt_file'] không bao giờ được set,
    và sau PR #16 (ẩn nút tải SRT khi không có srt_file) thì nút tải biến
    mất luôn cả ở chế độ srt_only. Compose trả dict {'srt_path', ...} nên
    đọc trực tiếp từ đó; job cũ không có srt_path thì xóa cờ cũ đi."""
    srt = compose_result.get("srt_path") if isinstance(compose_result, dict) else None
    if srt and os.path.exists(str(srt)):
        job["srt_file"] = f"/api/jobs/{job['id']}/subtitles"
    else:
        job.pop("srt_file", None)


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


_LAST_PROGRESS_SAVE: Dict[str, float] = {}
_PROGRESS_SAVE_LOCK = threading.Lock()


def _save_job_best_effort(job: Dict) -> None:
    """Save dùng trong progress callback / except-handler của pipeline:
    log warning khi lỗi, không raise (pipeline đang render không được chết vì đĩa)."""
    try:
        save_job(job)
        jid = job.get("id")
        if jid:
            with _PROGRESS_SAVE_LOCK:
                _LAST_PROGRESS_SAVE[jid] = time.monotonic()
    except Exception as exc:
        logger.warning("save_job (best-effort) thất bại cho job %s: %s", job.get("id"), exc)


def _save_job_progress_throttled(job: Dict, *, force: bool = False, min_interval: float = 1.5) -> None:
    """Giảm tải fsync đĩa trong vòng lặp poll tiến độ đa luồng: các tick trung gian
    ('running') chỉ flush tối đa 1 lần / 1.5s mỗi job; mốc chuyển trạng thái
    ('done' / 'error' / force=True) luôn ghi ngay lập tức."""
    jid = job.get("id") or ""
    now = time.monotonic()
    if not force and jid:
        with _PROGRESS_SAVE_LOCK:
            last = _LAST_PROGRESS_SAVE.get(jid, 0.0)
            if now - last < min_interval:
                return
            _LAST_PROGRESS_SAVE[jid] = now
    _save_job_best_effort(job)


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
                    if isinstance(data.get("req"), dict) and data["req"]:
                        data["req"] = MovieRequest(**data["req"])
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
                        if data.get("status") == "queued":
                            # Nhóm 1: hàng đợi chỉ sống trong tiến trình — sau
                            # restart không còn worker nào lấy job này nữa.
                            data["status"] = "error"
                            data["error"] = "interrupted"
                            data["queue_position"] = 0
                            data.setdefault("logs", []).append(
                                "[Hệ thống] Job đang chờ trong hàng đợi thì server khởi động lại "
                                "(interrupted). Bấm Resume để xếp hàng render lại.")
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
            if data.get("status") == "queued":
                # Nhóm 1: như load_all_persisted_jobs — hàng đợi không sống
                # qua restart, đánh dấu interrupted để bấm Resume xếp hàng lại.
                data["status"] = "error"
                data["error"] = "interrupted"
                data["queue_position"] = 0
                data.setdefault("logs", []).append(
                    "[Hệ thống] Job đang chờ trong hàng đợi thì server khởi động lại "
                    "(interrupted). Bấm Resume để xếp hàng render lại.")
            with JOBS_LOCK:
                # Re-check sau khi đọc đĩa: pipeline có thể vừa tạo job này.
                if job_id not in JOBS:
                    JOBS[job_id] = data
                return JOBS[job_id]
        except Exception as e:
            logger.warning("Không restore được job %s: %s", job_id, e)
    return JOBS.get(job_id)


def _is_job_running(job: Optional[Dict]) -> bool:
    """Check whether the job still has an active render or export."""
    if not job:
        return False
    job_id = job["id"]
    if (job.get("status") == "running" or job_id in _MANUAL_COMPOSE_JOBS
            or job_id == RENDER_CURRENT_JOB_ID):
        return True
    task = JOB_TASKS.get(job_id)
    return task is not None and not task.done()


def _spawn_job_task(job_id: str, coro) -> "asyncio.Task":
    """Spawn pipeline task và lưu reference để cancel thật (audit §1.6).
    Thay BackgroundTasks ở các endpoint tạo phim: task vẫn chạy sau khi trả
    response, nhưng server giữ được handle để cancel + log exception khi task
    kết thúc bất thường.

    Nhóm C (audit #5): nếu đã có task sống cho job này thì KHÔNG spawn chồng
    (2 pipeline cùng ghi results/clip của 1 job) — trả về task cũ và đóng
    coroutine chưa chạy, để caller không mất handle task đang chạy.
    """
    with JOBS_LOCK:
        existing = JOB_TASKS.get(job_id)
        if existing is not None and not existing.done():
            logger.warning(
                "Bỏ qua spawn pipeline chồng cho job %s (đã có task đang chạy).",
                job_id)
            try:
                coro.close()
            except Exception:
                pass
            return existing
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


# ── Hàng đợi render (Nhóm 1: render tự động) ──────────────────────────────
# Nhiều phim bấm Sản xuất cùng lúc thì xếp hàng nối tiếp thay vì render chồng
# lên nhau: job vào hàng đợi, worker lấy từng job chạy film_pipeline đến khi
# hàng rỗng. Job đầu tiên (hàng trống) chạy ngay; các job sau thấy vị trí của
# mình trong Monitor. Hàng đợi chỉ sống trong tiến trình — restart server thì
# job còn "queued" được đánh dấu interrupted ở load_all_persisted_jobs (như
# job "running" cũ) để bấm Resume chạy tiếp.
RENDER_QUEUE: List[str] = []  # job_id chờ render, theo thứ tự vào hàng
RENDER_QUEUE_WORKER: Optional["asyncio.Task"] = None
RENDER_CURRENT_JOB_ID: Optional[str] = None
# Nhóm C (audit #5): job_id đang compose thủ công qua /api/jobs/{id}/compose.
# Chỉ sống trong bộ nhớ (không lưu job_state.json) — giữ dưới JOBS_LOCK, để
# gate chống chạy chồng thấy được lần compose thủ công đang chạy (nó không nằm
# trong JOB_TASKS như pipeline).
_MANUAL_COMPOSE_JOBS: Set[str] = set()


def _job_title(job: Optional[Dict]) -> str:
    if not job:
        return ""
    req_data = job.get("req")
    if isinstance(req_data, dict):
        return req_data.get("title", "") or job.get("id", "")
    if hasattr(req_data, "title"):
        return getattr(req_data, "title") or job.get("id", "")
    return job.get("id", "")


def _refresh_queue_positions() -> None:
    """Cập nhật queue_position (1-based) cho mọi job đang chờ; job đang chạy = 0."""
    with JOBS_LOCK:
        snapshot = list(RENDER_QUEUE)
    for pos, job_id in enumerate(snapshot, start=1):
        job = JOBS.get(job_id)
        if job is not None:
            job["queue_position"] = pos


def _render_queue_snapshot() -> Dict[str, Any]:
    """Ảnh chụp hàng đợi cho Monitor/API: job đang chạy + từng job chờ + vị trí."""
    with JOBS_LOCK:
        queued_ids = list(RENDER_QUEUE)
        current_id = RENDER_CURRENT_JOB_ID
    current = None
    if current_id:
        job = JOBS.get(current_id)
        if job is not None:
            current = {
                "job_id": current_id,
                "title": _job_title(job),
                "status": job.get("status"),
                "progress": job.get("progress", 0),
                "total": job.get("total", 0),
            }
    queued = []
    for pos, job_id in enumerate(queued_ids, start=1):
        job = JOBS.get(job_id)
        if job is None:
            continue
        queued.append({
            "job_id": job_id,
            "title": _job_title(job),
            "status": job.get("status"),
            "total": job.get("total", 0),
            "queue_position": pos,
            "created_at": job.get("created_at", 0),
        })
    return {"current": current, "queued": queued, "queue_length": len(queued)}


def _ensure_render_queue_worker() -> None:
    """Đảm bảo có 1 worker hàng đợi đang chạy (gọi từ endpoint async)."""
    global RENDER_QUEUE_WORKER
    if RENDER_QUEUE_WORKER is None or RENDER_QUEUE_WORKER.done():
        RENDER_QUEUE_WORKER = asyncio.create_task(_render_queue_worker())


def enqueue_render_job(job: Dict) -> int:
    """Cho job vào hàng đợi render, trả về vị trí (1-based). Job đầu hàng chạy
    ngay khi worker lấy; job đã trong hàng thì không xếp trùng. Gọi từ endpoint
    async (create/resume) vì _ensure_render_queue_worker cần event loop."""
    job_id = job["id"]
    with JOBS_LOCK:
        if job_id not in RENDER_QUEUE:
            RENDER_QUEUE.append(job_id)
        position = RENDER_QUEUE.index(job_id) + 1
    job["status"] = "queued"
    job["queue_position"] = position
    job_log(job, f"⏳ Xếp hàng chờ render (vị trí {position} trong hàng đợi)...")
    _save_job_best_effort(job)
    _ensure_render_queue_worker()
    return position


def dequeue_render_job(job_id: str) -> bool:
    """Gỡ job khỏi hàng đợi (khi hủy/xóa lúc còn chờ). Trả về True nếu đã gỡ."""
    with JOBS_LOCK:
        if job_id in RENDER_QUEUE:
            RENDER_QUEUE.remove(job_id)
            removed = True
        else:
            removed = False
    if removed:
        _refresh_queue_positions()
    return removed


async def _render_queue_worker() -> None:
    """Worker hàng đợi: lấy từng job chạy film_pipeline nối tiếp đến khi hết.
    Job nào lỗi/cancel cũng không chặn job sau (film_pipeline tự bắt lỗi của
    nó; đây chỉ bọc thêm để worker không chết vì exception lọt ra)."""
    global RENDER_QUEUE_WORKER, RENDER_CURRENT_JOB_ID
    try:
        while True:
            with JOBS_LOCK:
                if not RENDER_QUEUE:
                    break
                job_id = RENDER_QUEUE.pop(0)
            _refresh_queue_positions()
            job = JOBS.get(job_id) or get_or_restore_job(job_id)
            if not job or job.get("cancelled") or job.get("status") == "cancelled":
                continue
            req = job.get("req")
            if isinstance(req, dict):
                try:
                    req = MovieRequest(**req)
                    job["req"] = req
                except Exception as exc:
                    job["status"] = "error"
                    job_log(job, f"❌ Không khôi phục được cấu hình job để render: {exc}")
                    _save_job_best_effort(job)
                    continue
            if req is None:
                job["status"] = "error"
                job_log(job, "❌ Job thiếu cấu hình (req) nên không thể render.")
                _save_job_best_effort(job)
                continue
            RENDER_CURRENT_JOB_ID = job_id
            job["status"] = "running"
            job["queue_position"] = 0
            job_log(job, "▶️ Bắt đầu render (đến lượt trong hàng đợi).")
            _save_job_best_effort(job)
            try:
                await film_pipeline(job, req)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Render queue job %s lỗi chưa được xử lý.", job_id)
                job["status"] = "error"
                job_log(job, f"❌ Lỗi xử lý pipeline: {exc}")
                _save_job_best_effort(job)
            finally:
                RENDER_CURRENT_JOB_ID = None
                _refresh_queue_positions()
    finally:
        with JOBS_LOCK:
            if RENDER_QUEUE_WORKER is asyncio.current_task():
                RENDER_QUEUE_WORKER = None


def _mark_job_cancelled(job: Dict) -> None:
    """Đánh dấu job + các cảnh chưa xong là cancelled (audit #8) để Monitor
    không treo ở trạng thái "running" sau khi hủy."""
    job["status"] = "cancelled"
    for sc in list(job.get("scenes", {}).values()):
        if isinstance(sc, dict) and sc.get("status") not in ("done", "error"):
            sc["status"] = "cancelled"
            sc["msg"] = "🛑 Đã hủy"


def _job_cancel_requested(job: Dict, cancel_event: Optional["asyncio.Event"] = None) -> bool:
    """Job đã bị người dùng hủy hay chưa — cờ dict, status, hoặc asyncio.Event
    (audit #4). Dùng chung cho cả 3 pipeline sau mỗi bước compose."""
    if job.get("cancelled") or job.get("status") == "cancelled":
        return True
    return cancel_event is not None and cancel_event.is_set()


def _mark_cancelled_after_compose(job: Dict, cancel_event: Optional["asyncio.Event"] = None) -> bool:
    """Kiểm tra hủy SAU một bước compose (asyncio.to_thread): nếu job đã bị
    hủy trong lúc compose thì đánh dấu cancelled và KHÔNG ghi final_video/done
    (audit #4 — trước đây code gán done vô điều kiện, ghi đè trạng thái
    cancelled). Trả True khi đã hủy (caller return ngay), False khi tiếp tục."""
    if not _job_cancel_requested(job, cancel_event):
        return False
    _mark_job_cancelled(job)
    job_log(job, "🛑 Job đã bị hủy bởi người dùng (trong lúc hậu kỳ).")
    _save_job_best_effort(job)
    return True


def _job_has_active_rerender(job: Dict) -> bool:
    """Có cảnh nào đang được render lại (cờ rerendering) không (audit #5)."""
    scenes = job.get("scenes") or {}
    if isinstance(scenes, dict):
        for sc in scenes.values():
            if isinstance(sc, dict) and sc.get("rerendering"):
                return True
    return False


# ── Background Pipeline ────────────────────────────────────────────────────
async def film_pipeline(job: Dict, req: MovieRequest):
    job_id = job["id"]
    # Audit #8: cờ hủy dùng chung cho run_batch (kiểm tra trước mỗi cảnh +
    # trong vòng poll). api_cancel_job set event này; task.cancel() vẫn là
    # cơ chế dừng chính ở điểm await tiếp theo.
    cancel_event = asyncio.Event()
    with JOBS_LOCK:
        JOB_CANCEL_EVENTS[job_id] = cancel_event
    try:
        # Timing từng giai đoạn (đo tốc độ render): render/poll/TTS do run_batch
        # điền, compose/total điền ở dưới. Job cũ thiếu field thì mặc định 0.
        pipe_t0 = time.monotonic()
        job["timing"] = {"render_s": 0.0, "poll_s": 0.0, "tts_s": 0.0, "compose_s": 0.0, "total_s": 0.0}
        if not MUSE_API_KEY:
            # Không key -> config error rõ ràng trên job, không gọi gateway bằng key giả.
            job["status"] = "error"
            job_log(job, f"❌ Muse API key chưa được cấu hình ({_MUSE_KEY_NAME}). Không thể render.")
            job["timing"]["total_s"] = round(time.monotonic() - pipe_t0, 3)
            _save_job_best_effort(job)
            return
        if job.get("cancelled") or job.get("status") == "cancelled":
            job_log(job, "🛑 Job đã bị hủy trước khi bắt đầu.")
            job["timing"]["total_s"] = round(time.monotonic() - pipe_t0, 3)
            _save_job_best_effort(job)
            return
        # Phòng thủ cho chế độ Đồng nhất tối đa: job tạo trước khi có preflight
        # hoặc resume từ req cũ vẫn phải chạy sequential.
        if getattr(req, "consistency_mode", "standard") == "max" \
                and (req.chain_mode or "none") != "sequential":
            req.chain_mode = "sequential"
            job_log(job, "🎯 [Đồng nhất tối đa] Ép chain_mode='sequential' "
                         "trước khi render.")
        output_dir = WORKSPACE / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "audio").mkdir(exist_ok=True)
        (output_dir / "scenes").mkdir(exist_ok=True)
        (output_dir / "final").mkdir(exist_ok=True)

        raw_concurrent = req.max_concurrent if (req.max_concurrent and req.max_concurrent > 0) else MAX_CONCURRENT
        max_cap = await muse_config.query_muse_capacity_async(_MUSE_MAX_CONCURRENT) if MUSE_PROVIDER == "browser" else _MUSE_MAX_CONCURRENT
        concurrent = min(raw_concurrent, max_cap)
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
            _save_job_progress_throttled(job, force=(st != "running"))

        scenes_dicts = [req.render_scene(s) for s in req.scenes]

        # ── Batch render hàng loạt ──
        batch_timing: Dict[str, float] = {}
        with muse_config.muse_affinity_scope(job_id):
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
                timing=batch_timing,
                cancel_event=cancel_event,
                muse_provider=MUSE_PROVIDER,
                # Snapshot nhân vật CỦA PHIM này (đã lưu ở create_movie) —
                # scene nhắc tên ai thì dán đúng Character Block của người đó.
                characters=job.get("characters") or copy.deepcopy(req.characters or []),
            )

        job["results"] = results
        job["timing"].update({k: batch_timing.get(k, 0.0) for k in ("render_s", "poll_s", "tts_s")})
        job["timing"]["total_s"] = round(time.monotonic() - pipe_t0, 3)
        _save_job_best_effort(job)

        # Kiểm tra nếu job đã bị người dùng hủy.
        # Audit #8: run_batch đã kiểm tra cancel_event trước mỗi cảnh +
        # trong vòng poll nên batch dừng sớm; ở đây chỉ dọn trạng thái
        # (các cảnh chưa xong -> "cancelled") rồi thoát, không hậu kỳ.
        # GIỚI HẠN: ffmpeg đồng bộ trong asyncio.to_thread không bị giết
        # khi cancel — cảnh đang ghép xong bước đó rồi mới dừng hẳn.
        if job.get("cancelled") or job.get("status") == "cancelled" or cancel_event.is_set():
            _mark_job_cancelled(job)
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
                    # Audit #9: cảnh TTS lỗi vẫn "done" (video có, audio im
                    # lặng) nhưng msg + cờ tts_failed phải nói rõ để Monitor
                    # thấy ngay cảnh nào mất tiếng.
                    "msg": "✅ Hoàn thành ⚠️ TTS lỗi — cảnh mất tiếng (audio im lặng)" if r.get("tts_failed") else (
                        "✅ Hoàn thành 🔍 QC không đạt — xem chi tiết trong thẻ cảnh" if r.get("qc_failed") else "✅ Hoàn thành"),
                    "video_url": f"/api/jobs/{job_id}/scenes/{idx}/video",
                    "last_frame_url": r.get("last_frame_url"),
                    "error": None,
                    "tts_failed": bool(r.get("tts_failed")),
                    "tts_error": r.get("tts_error"),
                    # Nhóm 1: QC + compliance retry — Monitor hiện badge từ đây.
                    "qc_failed": bool(r.get("qc_failed")),
                    "qc_issues": list(r.get("qc_issues") or []),
                    "qc": r.get("qc"),
                    "used_compliance": bool(r.get("used_compliance")),
                    "compliance_failed": bool(r.get("compliance_failed")),
                })
                ok_count += 1
            else:
                _scene(job, idx).update({
                    "status": "error",
                    "msg": f"❌ {r.get('error', '')}",
                    "error": r.get("error", ""),
                    "used_compliance": bool(r.get("used_compliance")),
                    "compliance_failed": bool(r.get("compliance_failed")),
                })
                job["failed_scenes"].append(idx)

        # Nhóm 1: danh sách cảnh QC không đạt + cảnh đã thử compliance — để
        # Monitor hiện cảnh báo tổng + badge từng cảnh (clip KHÔNG bị xóa).
        job["qc_failed_scenes"] = [r["index"] for r in results
                                   if r.get("status") == "done" and r.get("qc_failed")]
        job["compliance_retried_scenes"] = [r["index"] for r in results
                                            if r.get("used_compliance")]
        if job["qc_failed_scenes"]:
            job_log(job, "🔍 QC không đạt: cảnh " + ", ".join(f"#{i}" for i in job["qc_failed_scenes"]) +
                    " — clip vẫn giữ nguyên, xem lý do trong thẻ từng cảnh rồi quyết định render lại hay giữ.")
        if job["compliance_retried_scenes"]:
            job_log(job, "🛡️ Đã tự thử compliance_prompt cho cảnh " +
                    ", ".join(f"#{i}" for i in job["compliance_retried_scenes"]) +
                    " (prompt gốc bị từ chối, chỉ thử đúng 1 lần).")

        # Audit #9: TTS lỗi vẫn giữ fallback im lặng cho scene đó, nhưng job
        # phải cảnh báo rõ — liệt kê scene mất tiếng trong log Monitor.
        job["tts_failed_scenes"] = [r["index"] for r in results if r.get("tts_failed")]
        if job["tts_failed_scenes"]:
            job_log(job, "⚠️ Cảnh báo TTS: cảnh " + ", ".join(f"#{i}" for i in job["tts_failed_scenes"]) +
                    " mất tiếng (lỗi synthesis, đã dùng audio im lặng) — kiểm tra lại giọng đọc trước khi xuất phim!")

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
        if job["qc_failed_scenes"] and not req.allow_partial:
            job["status"] = "audit_pending"
            job_log(job, "Đã dừng xuất tự động. Xem lại các cảnh có cảnh báo QC trước khi chọn xuất hoặc dựng lại.")
            _save_job_best_effort(job)
            return
        if not audit["is_complete"] and not req.allow_partial:
            job["status"] = "audit_pending"
            job_log(job, f"⚠️ Cảnh báo kiểm định: Phát hiện {audit['failed_count']} cảnh lỗi / thiếu. Đã tạm dừng xuất phim để bạn xem xét và quyết định!")
            _save_job_best_effort(job)
            return

        # Kiểm tra hủy lần nữa trước khi vào hậu kỳ (cancel có thể đến trong lúc render).
        if job.get("cancelled") or job.get("status") == "cancelled" or cancel_event.is_set():
            _mark_job_cancelled(job)
            job_log(job, "🛑 Job đã bị hủy bởi người dùng.")
            _save_job_best_effort(job)
            return

        # ── Hậu kỳ ──
        try:
            compose_t0 = time.monotonic()
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
                    # Nhóm C (audit #4): hủy giữa lúc ghép clip -> dừng, không gán done.
                    if _mark_cancelled_after_compose(job, cancel_event):
                        return
                    clips.append(f"/api/jobs/{job_id}/clips/clip_{idx:03d}.mp4")
                job["clips"] = clips
                job["progress"] = 100
                job["status"] = "done"
                job_log(job, f"✅ Đã xuất {len(clips)} clip riêng lẻ!")

            else:
                # Xuất 1 phim dài hoàn chỉnh
                compose_res = await asyncio.to_thread(
                    compose_full_movie,
                    results=results,
                    output_dir=str(output_dir),
                    project_title=req.title.replace(" ", "_"),
                    font_size=req.font_size,
                    bg_music_path=_resolve_existing_studio_bgm(req.bg_music_path),
                    music_volume=req.music_volume if req.music_volume is not None else 0.25,
                    voice_volume=req.voice_volume if req.voice_volume is not None else 1.0,
                    subtitle_mode=req.subtitle_mode or "burn_in",
                    subtitle_style=req.subtitle_style, subtitle_position=req.subtitle_position,
                    quality=req.quality or "720p",
                    transition=req.transition or "none",
                    aspect_ratio=req.aspect_ratio or "16:9",
                    allow_partial=bool(req.allow_partial),
                )
                # Nhóm C (audit #4): hủy trong lúc compose -> không ghi final_video/done.
                if _mark_cancelled_after_compose(job, cancel_event):
                    return
                final_path = _unwrap_final_path(compose_res)
                rel = Path(final_path).name
                job["final_video"] = f"/api/jobs/{job_id}/video/{rel}"
                _sync_job_srt_file(job, compose_res)
                job["progress"] = 100
                job["status"] = "done"
                if not audit["is_complete"]:
                    job_log(job, f"⚠️ Đã xuất bản nháp (Draft - thiếu {audit['failed_count']} cảnh)! → {rel}")
                else:
                    job_log(job, f"🎉 Xuất phim thành công 100%! → {rel}")
            job["timing"]["compose_s"] = round(time.monotonic() - compose_t0, 3)
            job["timing"]["total_s"] = round(time.monotonic() - pipe_t0, 3)
            _save_job_best_effort(job)


        except Exception as e:
            # Nhóm C (audit #4): compose lỗi đúng lúc vừa bị hủy -> giữ cancelled,
            # không ghi đè thành error.
            if _job_cancel_requested(job, cancel_event):
                _mark_job_cancelled(job)
                job_log(job, "🛑 Job đã bị hủy bởi người dùng (trong lúc hậu kỳ).")
            else:
                job["status"] = "error"
                job_log(job, f"❌ Lỗi hậu kỳ: {e}")
            try:
                job["timing"]["compose_s"] = round(time.monotonic() - compose_t0, 3)
                job["timing"]["total_s"] = round(time.monotonic() - pipe_t0, 3)
            except Exception:
                pass
            _save_job_best_effort(job)

    except asyncio.CancelledError:
        # CancelledError là BaseException nên except Exception không bắt được
        # (audit #8): cập nhật timing/log/trạng thái rồi raise lại để task
        # giữ trạng thái cancelled.
        job["status"] = "cancelled"
        job_log(job, "🛑 Job đã bị hủy (task cancel) — dừng render.")
        try:
            if "timing" in job:
                job["timing"]["total_s"] = round(time.monotonic() - pipe_t0, 3)
        except Exception:
            pass
        _save_job_best_effort(job)
        raise
    except Exception as e:
        job["status"] = "error"
        job_log(job, f"❌ Lỗi xử lý pipeline: {e}")
        try:
            if "timing" in job:
                job["timing"]["total_s"] = round(time.monotonic() - pipe_t0, 3)
        except Exception:
            pass
        _save_job_best_effort(job)
    finally:
        with JOBS_LOCK:
            if JOB_CANCEL_EVENTS.get(job_id) is cancel_event:
                JOB_CANCEL_EVENTS.pop(job_id, None)


# ── API Routes ─────────────────────────────────────────────────────────────

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0%" stop-color="#7c5cfc"/><stop offset="100%" stop-color="#22d3ee"/>'
    "</linearGradient></defs>"
    '<rect width="64" height="64" rx="16" fill="#0b0d14"/>'
    '<rect x="4" y="4" width="56" height="56" rx="13" fill="none" stroke="url(#g)" stroke-width="3"/>'
    '<polygon points="26,20 26,44 46,32" fill="url(#g)"/>'
    "</svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
def chrome_devtools_well_known():
    return JSONResponse({}, headers={"Cache-Control": "public, max-age=86400"})


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


def _apply_consistency_max(req: MovieRequest):
    """Áp dụng chế độ Đồng nhất tối đa cho req trước khi tạo job.

    - consistency_mode không hợp lệ -> (None, 422).
    - "standard" -> (None, None): không làm gì, tương thích API cũ.
    - "max" -> chạy preflight (consistency.run_consistency_checks):
      có lỗi -> (checks, 422) kèm danh sách check, KHÔNG tạo job;
      chỉ warning/info -> ép chain_mode="sequential" (ghi log rõ đã
      override) và trả (checks, None) để caller lưu warnings vào job.
    """
    mode = (req.consistency_mode or "standard").strip().lower()
    if mode not in ("standard", "max"):
        return None, JSONResponse(
            {"error": "consistency_mode chỉ nhận 'standard' hoặc 'max'."},
            status_code=422)
    if mode != "max":
        return None, None
    req.consistency_mode = "max"
    checks = run_consistency_checks(req.model_dump(), consistency_mode="max")
    if checks["errors"] > 0:
        return checks, JSONResponse(
            {"error": "Preflight đồng nhất chưa đạt — sửa lỗi trước khi sản xuất.",
             "checks": checks["checks"],
             "errors": checks["errors"],
             "warnings": checks["warnings"]},
            status_code=422)
    if (req.chain_mode or "none") != "sequential":
        print(f"[ConsistencyMax] Ép chain_mode {req.chain_mode!r} -> "
              f"'sequential' cho phim '{req.title}' (consistency_mode=max).")
        req.chain_mode = "sequential"
    return checks, None


@app.post("/api/consistency-check")
async def api_consistency_check(req: MovieRequest):
    """Preflight kiểm tra đồng nhất cho 1 kịch bản (không tạo job).

    Body là MovieRequest đầy đủ. Trả {passed, errors, warnings, checks}.
    UI gọi endpoint này khi bấm 'Kiểm tra đồng nhất' trước khi sản xuất.
    """
    mode = (req.consistency_mode or "standard").strip().lower()
    if mode not in ("standard", "max"):
        return JSONResponse(
            {"error": "consistency_mode chỉ nhận 'standard' hoặc 'max'."},
            status_code=422)
    checks = run_consistency_checks(req.model_dump(), consistency_mode=mode)
    return {"passed": checks["passed"], "errors": checks["errors"],
            "warnings": checks["warnings"], "checks": checks["checks"]}


@app.post("/api/jobs/{job_id}/consistency-check")
async def api_job_consistency_check(job_id: str):
    """Preflight kiểm tra đồng nhất cho req đã lưu của 1 job."""
    job = get_or_restore_job(job_id)
    if not job or not job.get("req"):
        return JSONResponse({"error": "Không tìm thấy job hoặc cấu hình ban đầu"},
                            status_code=404)
    stored = job["req"]
    data = stored.model_dump() if hasattr(stored, "model_dump") else stored
    if not isinstance(data, dict):
        return JSONResponse({"error": "Cấu hình job không đọc được"},
                            status_code=500)
    mode = str(data.get("consistency_mode") or "standard")
    checks = run_consistency_checks(data, consistency_mode=mode)
    return {"job_id": job_id, "consistency_mode": mode,
            "chain_mode": data.get("chain_mode"),
            "passed": checks["passed"], "errors": checks["errors"],
            "warnings": checks["warnings"], "checks": checks["checks"]}


@app.post("/api/movies")
async def create_movie(req: MovieRequest):
    """Tạo job mới. Trả về job_id để theo dõi."""
    if not req.scenes:
        return JSONResponse({"error": "Không có cảnh nào!"}, status_code=400)

    # Chế độ Đồng nhất tối đa: preflight + ép sequential trước khi tạo job.
    cm_checks, cm_error = _apply_consistency_max(req)
    if cm_error is not None:
        return cm_error

    job_id = uuid.uuid4().hex[:12]
    job = new_job(job_id, len(req.scenes), req=req)
    # Snapshot nhân vật CỦA PHIM này vào job (để Monitor/job detail thấy được;
    # save_job đã serialize req bằng model_dump nên snapshot list-dict lưu
    # cùng job_state.json an toàn).
    job["characters"] = copy.deepcopy(req.characters or [])
    if cm_checks is not None:
        job["consistency_mode"] = "max"
        job["consistency_checks"] = cm_checks["checks"]
        job["consistency_warnings"] = cm_checks["warnings"]
    _save_job_or_http500(job)
    job_log(job, f"📋 Dự án: '{req.title}' — {len(req.scenes)} cảnh — giọng: {req.voice}")
    if cm_checks is not None and cm_checks["warnings"] > 0:
        job_log(job, f"🎯 [Đồng nhất tối đa] Preflight: {cm_checks['warnings']} cảnh báo "
                     f"(chain đã ép sequential, xem consistency_checks).")
    _save_job_or_http500(job)
    # Nhóm 1 — hàng đợi render: job vào hàng đợi nối tiếp thay vì spawn
    # pipeline ngay (nhiều phim bấm cùng lúc không render chồng nhau).
    # Job đầu hàng chạy ngay; các job sau chờ tới lượt, thấy vị trí ở Monitor.
    # Nếu thiếu MUSE_API_KEY thì film_pipeline đánh dấu job error rõ ràng.
    queue_position = enqueue_render_job(job)

    resp: Dict[str, Any] = {
        "job_id": job_id,
        "message": "Đã bắt đầu sản xuất phim hàng loạt" if queue_position <= 1 else f"Đã xếp hàng chờ render (vị trí {queue_position})",
        "queue_position": queue_position,
        "queued": queue_position > 1,
    }
    if cm_checks is not None:
        resp["consistency_mode"] = "max"
        resp["chain_mode"] = req.chain_mode
        resp["consistency_warnings"] = cm_checks["warnings"]
        resp["consistency_checks"] = cm_checks["checks"]
    return resp


# ── Ước tính thời gian render (Nhóm 1) ──────────────────────────────────────
# Dựa trên timing ĐÃ ĐO từ các job trước (PR #6: job["timing"]["poll_s"] là
# tổng thời gian chờ API video của các scene). Đây chỉ là ƯỚC TÍNH — tốc độ
# thực tế còn tùy tải của engine, mạng, độ dài từng cảnh — KHÔNG cam kết.
ESTIMATE_FALLBACK_POLL_S_PER_SCENE = 60.0  # chưa có mẫu đo: 60s/cảnh
ESTIMATE_FALLBACK_COMPOSE_S = 30.0         # chưa có mẫu đo: 30s ghép phim


def estimate_render_time(req: "MovieRequest") -> Dict[str, Any]:
    """Ước tính tổng thời gian render cho 1 MovieRequest (chưa tạo job).

    Công thức: waves = ceil(n_scenes / concurrent_hiệu_dụng) — mỗi wave chờ
    trung bình poll_s/cảnh (đo từ job trước); cộng thời gian ghép phim trung
    bình. chain_mode=sequential -> concurrent hiệu dụng = 1 (nối tiếp từng
    cảnh, không song song). TTS chạy song song với poll nên không cộng thêm.
    """
    n = max(len(req.scenes or []), 0)
    raw_concurrent = req.max_concurrent if (req.max_concurrent and req.max_concurrent > 0) else MAX_CONCURRENT
    concurrent = 1 if (req.chain_mode or "none") == "sequential" else min(raw_concurrent, _MUSE_MAX_CONCURRENT)

    poll_total = 0.0
    poll_scenes = 0
    compose_samples: List[float] = []
    for job in list(JOBS.values()):
        timing = job.get("timing") or {}
        results = job.get("results") or []
        done = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "done")
        poll_s = timing.get("poll_s") or 0
        if done > 0 and poll_s > 0:
            poll_total += float(poll_s)
            poll_scenes += done
        compose_s = timing.get("compose_s") or 0
        if compose_s > 0:
            compose_samples.append(float(compose_s))

    if poll_scenes > 0:
        avg_poll = poll_total / poll_scenes
        basis = "measured"
    else:
        avg_poll = ESTIMATE_FALLBACK_POLL_S_PER_SCENE
        basis = "fallback"
    avg_compose = (sum(compose_samples) / len(compose_samples)) if compose_samples else ESTIMATE_FALLBACK_COMPOSE_S

    import math
    waves = math.ceil(n / max(concurrent, 1)) if n > 0 else 0
    est_render = waves * avg_poll
    est_total = est_render + (avg_compose if n > 0 else 0.0)
    return {
        "estimated_seconds": round(est_total, 1),
        "estimated_minutes": round(est_total / 60.0, 1),
        "render_s": round(est_render, 1),
        "compose_s": round(avg_compose, 1) if n > 0 else 0.0,
        "scenes": n,
        "concurrent": concurrent,
        "chain_mode": req.chain_mode or "none",
        "avg_poll_s_per_scene": round(avg_poll, 1),
        "samples": {"jobs_with_timing": len(compose_samples), "scenes_measured": poll_scenes},
        "basis": basis,
        "note": ("Ước tính dựa trên thời gian chờ API trung bình "
                 f"({avg_poll:.0f}s/cảnh, đo từ {poll_scenes} cảnh đã render)"
                 if basis == "measured" else
                 "Chưa có job nào đo được timing — dùng mức mặc định 60s/cảnh. "
                 "Ước tính sẽ chính xác hơn sau vài lần render.") +
                " Chỉ là ước tính, không cam kết thời gian thực tế.",
    }


@app.get("/api/render-queue")
def api_render_queue():
    """Hàng đợi render hiện tại: job đang chạy + từng job chờ + vị trí."""
    return _render_queue_snapshot()


@app.post("/api/estimate")
async def api_estimate_render_time(req: MovieRequest):
    """Ước tính tổng thời gian render TRƯỚC khi bấm Sản xuất (không tạo job)."""
    if not req.scenes:
        return JSONResponse({"error": "Không có cảnh nào để ước tính!"}, status_code=400)
    return estimate_render_time(req)


async def retry_failed_pipeline(job: Dict, auto_fix: bool = True):
    """Pipeline render lại CHỈ CÁC CẢNH BỊ LỖI rồi tự động ghép lại phim hoàn chỉnh."""
    # Cờ is_retrying đã được endpoint /retry-failed đặt NGAY trong request (dưới
    # JOBS_LOCK) trước khi spawn task — pipeline không kiểm tra lại ở đây: nếu
    # return sớm thì finally không chạy và cờ kẹt True vĩnh viễn (audit §1.6).
    job["is_retrying"] = True
    # Audit #8: cờ hủy riêng cho đợt retry (đăng ký chung vào
    # JOB_CANCEL_EVENTS để api_cancel_job set được).
    retry_cancel_event = asyncio.Event()
    with JOBS_LOCK:
        JOB_CANCEL_EVENTS[job["id"]] = retry_cancel_event

    try:
        req: MovieRequest = job.get("req")
        if not req:
            job_log(job, "❌ Không tìm thấy thông tin cấu hình kịch bản gốc.")
            return
        if not MUSE_API_KEY:
            job["status"] = "error"
            job_log(job, f"❌ Muse API key chưa được cấu hình ({_MUSE_KEY_NAME}). Không thể render lại.")
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

        scenes_dicts = [req.render_scene(s) for s in failed_scenes]

        def retry_progress_cb(idx: int, msg: str):
            _set_scene(job, idx, {"status": "running", "msg": msg})

        raw_concurrent = req.max_concurrent if (req and req.max_concurrent and req.max_concurrent > 0) else MAX_CONCURRENT
        max_cap = await muse_config.query_muse_capacity_async(_MUSE_MAX_CONCURRENT) if MUSE_PROVIDER == "browser" else _MUSE_MAX_CONCURRENT
        concurrent = min(raw_concurrent, max_cap)
        with muse_config.muse_affinity_scope(job["id"]):
            new_results = await run_batch(
                scenes=scenes_dicts,
                base_url=MUSE_API_URL,
                api_key=MUSE_API_KEY,
                output_dir=str(output_dir),
                voice=req.voice,
                max_concurrent=concurrent,
                progress_cb=retry_progress_cb,
                voice_mode=req.voice_mode or "tts",
                cancel_event=retry_cancel_event,
                muse_provider=MUSE_PROVIDER,
                characters=job.get("characters") or copy.deepcopy(req.characters or []),
            )

        # Audit #8: hủy trong lúc retry -> dừng, không ghép lại phim.
        if job.get("cancelled") or job.get("status") == "cancelled" or retry_cancel_event.is_set():
            _mark_job_cancelled(job)
            job_log(job, "🛑 Đợt render lại đã bị hủy bởi người dùng.")
            _save_job_best_effort(job)
            return

        # Cập nhật kết quả vào danh sách results chung
        current_results = {r["index"]: r for r in job.get("results", [])}
        for nr in new_results:
            idx = nr["index"]
            current_results[idx] = nr
            if nr["status"] == "done":
                _scene(job, idx).update({
                    "status": "done",
                    "msg": "✅ Hoàn thành (Sau khi thử lại)" + (" ⚠️ TTS lỗi — cảnh mất tiếng (audio im lặng)" if nr.get("tts_failed") else ""),
                    "tts_failed": bool(nr.get("tts_failed")),
                    "tts_error": nr.get("tts_error"),
                })
                if idx in job["failed_scenes"]:
                    job["failed_scenes"].remove(idx)
            else:
                _scene(job, idx).update({"status": "error", "msg": f"❌ {nr.get('error', '')}"})

        all_results = sorted(list(current_results.values()), key=lambda x: x["index"])
        job["results"] = all_results
        # Audit #9: đồng bộ lại danh sách cảnh mất tiếng sau khi thử lại.
        job["tts_failed_scenes"] = [r["index"] for r in all_results if r.get("tts_failed")]
        if job["tts_failed_scenes"]:
            job_log(job, "⚠️ Cảnh báo TTS: cảnh " + ", ".join(f"#{i}" for i in job["tts_failed_scenes"]) +
                    " mất tiếng (lỗi synthesis, đã dùng audio im lặng) — kiểm tra lại giọng đọc trước khi xuất phim!")

        # Kiểm định lại toàn bộ các cảnh sau khi thử lại
        all_scene_dicts = [req.render_scene(s) for s in req.scenes]
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
            compose_res = await asyncio.to_thread(
                compose_full_movie,
                results=all_results,
                output_dir=str(output_dir),
                project_title=req.title.replace(" ", "_"),
                font_size=req.font_size,
                bg_music_path=_resolve_existing_studio_bgm(req.bg_music_path),
                music_volume=req.music_volume if req.music_volume is not None else 0.25,
                voice_volume=req.voice_volume if req.voice_volume is not None else 1.0,
                subtitle_mode=req.subtitle_mode or "burn_in",
                subtitle_style=req.subtitle_style, subtitle_position=req.subtitle_position,
                quality=req.quality or "720p",
                transition=req.transition or "none",
                aspect_ratio=req.aspect_ratio or "16:9",
                allow_partial=False,
            )
            # Nhóm C (audit #4): hủy trong lúc compose -> không ghi final_video/done.
            if _mark_cancelled_after_compose(job, retry_cancel_event):
                return
            final_path = _unwrap_final_path(compose_res)
            rel = Path(final_path).name
            job["final_video"] = f"/api/jobs/{job['id']}/video/{rel}"
            _sync_job_srt_file(job, compose_res)
            job["progress"] = 100
            job["status"] = "done"
            job_log(job, f"🎉 Đã tái xuất phim hoàn chỉnh 100%! → {rel}")
            _save_job_best_effort(job)
        except Exception as e:
            # Nhóm C (audit #4): compose lỗi đúng lúc vừa bị hủy -> giữ cancelled.
            if _job_cancel_requested(job, retry_cancel_event):
                _mark_job_cancelled(job)
                job_log(job, "🛑 Đợt render lại đã bị hủy bởi người dùng (trong lúc hậu kỳ).")
            else:
                job["status"] = "error"
                job_log(job, f"❌ Lỗi hậu kỳ sau khi render lại: {e}")
            _save_job_best_effort(job)
    finally:
        job["is_retrying"] = False
        with JOBS_LOCK:
            if JOB_CANCEL_EVENTS.get(job["id"]) is retry_cancel_event:
                JOB_CANCEL_EVENTS.pop(job["id"], None)



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
        # Nhóm C (audit #5): không retry chồng lên pipeline đang chạy, job đang
        # chờ hàng đợi, cảnh đang rerender, hay lần compose thủ công đang chạy
        # (các gate nhìn thấy cờ của nhau nên _spawn_job_task không bao giờ phải
        # từ chối spawn sau khi đã set is_retrying).
        if (_is_job_running(job) or job.get("status") == "queued"
                or job_id in RENDER_QUEUE or _job_has_active_rerender(job)):
            return JSONResponse(
                {"error": "Job đang render — hãy đợi xong rồi thử lại."},
                status_code=409)
        # Đặt cờ NGAY trong request, dưới lock, TRƯỚC khi trả response (audit §1.6):
        # 2 request liên tiếp không thể cùng qua gate này nữa.
        job["is_retrying"] = True
        # Nhóm C (audit #4): thao tác mới của người dùng — xóa cờ hủy cũ để đợt
        # retry này chỉ dừng khi có lệnh hủy MỚI (kẻo check hủy sau run_batch
        # đọc cờ cũ rồi abort ngay).
        job["cancelled"] = False
    failed = list(job["failed_scenes"])
    _spawn_job_task(job_id, retry_failed_pipeline(job, auto_fix))
    _save_job_best_effort(job)
    return {
        "message": f"Đang bắt đầu render lại {len(failed)} cảnh lỗi (Auto-Fix: {auto_fix})",
        "failed_scenes": failed
    }


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
            "msg": f"❌ Muse API key chưa được cấu hình ({_MUSE_KEY_NAME}).",
            "error": f"Muse API key chưa được cấu hình ({_MUSE_KEY_NAME}).",
        })
        _clear_rerendering(job, idx)
        job_log(job, f"❌ Muse API key chưa được cấu hình ({_MUSE_KEY_NAME}). Không thể render lại cảnh.")
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
        _save_job_progress_throttled(job, force=(st != "running"))

    try:
        from batch_engine import process_scene
        sem = asyncio.Semaphore(1)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            res = await process_scene(
                client=client,
                scene=req.render_scene(target_scene),
                sem=sem,
                base_url=MUSE_API_URL,
                api_key=MUSE_API_KEY,
                output_dir=str(output_dir),
                voice=req.voice,
                progress_cb=single_cb,
                voice_mode=req.voice_mode or "tts",
                muse_provider=MUSE_PROVIDER,
                # Snapshot nhân vật CỦA PHIM này — render lại 1 shot vẫn
                # dán đúng Character Block như render cả phim.
                characters=job.get("characters") or copy.deepcopy(req.characters or []),
            )

        current_results = {r["index"]: r for r in job.get("results", [])}
        current_results[idx] = res
        all_results = sorted(list(current_results.values()), key=lambda x: x["index"])
        job["results"] = all_results

        if res.get("status") == "done":
            _scene(job, idx).update({
                "status": "done",
                "msg": "✅ Hoàn thành (Đã tạo lại)" + (" ⚠️ TTS lỗi — cảnh mất tiếng (audio im lặng)" if res.get("tts_failed") else "") + (" 🔍 QC không đạt — xem chi tiết trong thẻ cảnh" if res.get("qc_failed") else ""),
                "video_url": f"/api/jobs/{job_id}/scenes/{idx}/video",
                "last_frame_url": res.get("last_frame_url"),
                "error": None,
                "tts_failed": bool(res.get("tts_failed")),
                "tts_error": res.get("tts_error"),
                # Nhóm 1: QC + compliance retry (như film_pipeline).
                "qc_failed": bool(res.get("qc_failed")),
                "qc_issues": list(res.get("qc_issues") or []),
                "qc": res.get("qc"),
                "used_compliance": bool(res.get("used_compliance")),
                "compliance_failed": bool(res.get("compliance_failed")),
            })
            # Audit #9: đồng bộ lại danh sách cảnh mất tiếng.
            job["tts_failed_scenes"] = [r["index"] for r in all_results if r.get("tts_failed")]
            # Nhóm 1: đồng bộ lại danh sách cảnh QC không đạt / đã thử compliance.
            job["qc_failed_scenes"] = [r["index"] for r in all_results
                                       if r.get("status") == "done" and r.get("qc_failed")]
            job["compliance_retried_scenes"] = [r["index"] for r in all_results
                                                if r.get("used_compliance")]
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
                scenes_dicts = [req.render_scene(s) for s in req.scenes]
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
                        compose_res = await asyncio.to_thread(
                            compose_full_movie,
                            results=all_results,
                            output_dir=str(output_dir),
                            project_title=req.title.replace(" ", "_"),
                            font_size=req.font_size,
                            bg_music_path=_resolve_existing_studio_bgm(req.bg_music_path),
                            music_volume=req.music_volume if req.music_volume is not None else 0.25,
                            voice_volume=req.voice_volume if req.voice_volume is not None else 1.0,
                            subtitle_mode=req.subtitle_mode or "burn_in",
                            subtitle_style=req.subtitle_style, subtitle_position=req.subtitle_position,
                            quality=req.quality or "720p",
                            transition=req.transition or "none",
                            aspect_ratio=req.aspect_ratio or "16:9",
                            allow_partial=False,
                        )
                        # Nhóm C (audit #4): hủy trong lúc ghép lại -> không ghi
                        # final_video/done. Không return ở đây để cuối hàm vẫn
                        # clear cờ rerendering + save như mọi đường khác.
                        if _job_cancel_requested(job):
                            _mark_job_cancelled(job)
                            job_log(job, f"🛑 [Cảnh {idx}] Job đã bị hủy trong lúc ghép lại phim — không ghi final_video.")
                        else:
                            final_path = _unwrap_final_path(compose_res)
                            rel = Path(final_path).name
                            job["final_video"] = f"/api/jobs/{job_id}/video/{rel}"
                            _sync_job_srt_file(job, compose_res)
                            job["progress"] = 100
                            job["status"] = "done"
                            job_log(job, f"🎉 Đã tái xuất phim hoàn chỉnh 100%! → {rel}")
                    except Exception as e:
                        # Nhóm C (audit #4): lỗi ghép đúng lúc vừa bị hủy -> giữ cancelled.
                        if _job_cancel_requested(job):
                            _mark_job_cancelled(job)
                            job_log(job, f"🛑 [Cảnh {idx}] Job đã bị hủy trong lúc ghép lại phim.")
                        else:
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

    with JOBS_LOCK:
        # Nhóm C (audit #5): gate chống chạy chồng, giống rerender endpoint —
        # job đang render, đang retry-failed, đang chờ hàng đợi, đang compose
        # thủ công, hoặc cảnh này đang được render lại thì từ chối 409.
        # Check + set cờ trong cùng 1 lock để 2 request liên tiếp không cùng lọt.
        if (_is_job_running(job) or job.get("is_retrying")
                or job.get("status") == "queued" or job_id in RENDER_QUEUE
                or _scene(job, scene_idx).get("rerendering")):
            return JSONResponse(
                {"error": "Job đang render (hoặc cảnh này đang được render lại) — hãy đợi xong rồi thử lại."},
                status_code=409)
        # Thao tác mới của người dùng: xóa cờ hủy cũ để lần retry này chỉ dừng
        # khi có lệnh hủy MỚI (audit #4); job từng bị hủy quay về trạng thái
        # chờ kiểm định thay vì kẹt ở "cancelled".
        job["cancelled"] = False
        if job.get("status") == "cancelled":
            job["status"] = "audit_pending"
        sc_entry = _scene(job, scene_idx)
        sc_entry["rerendering"] = True
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
    # Nhóm C (audit #5): gate nhìn thấy cả cờ is_retrying / compose thủ công /
    # job chờ hàng đợi để không spawn chồng lên retry-failed hay compose
    # (tránh _spawn_job_task phải từ chối sau khi đã set cờ rerendering).
    if (_is_job_running(job) or job.get("is_retrying")
            or job.get("status") == "queued" or job_id in RENDER_QUEUE
            or _scene(job, scene_idx).get("rerendering")):
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
                    "source": job.get("source"),
                    "logs": job["logs"][last_log_count:],
                    "final_video": job.get("final_video"),
                    "srt_file": job.get("srt_file"),
                    "audit": job.get("audit"),
                    "clips": job.get("clips", []),
                    "failed_scenes": job.get("failed_scenes", []),
                    "is_retrying": job.get("is_retrying", False),
                    "last_frames": last_frames,
                    # Nhóm 1: QC + compliance + hàng đợi render cho Monitor.
                    "qc_failed_scenes": job.get("qc_failed_scenes", []),
                    "compliance_retried_scenes": job.get("compliance_retried_scenes", []),
                    "queue_position": job.get("queue_position", 0),
                    "render_queue": _render_queue_snapshot(),
                }
                last_log_count = len(job["logs"])
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                if job["status"] in ("done", "error", "cancelled") and not job.get("is_retrying"):
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
def list_jobs():
    """Lấy danh sách tất cả jobs đã sản xuất trong Workspace."""
    # audit 2026-11-08 #14: alias GET /api/library đã xóa (trùng 100% logic
    # với /api/jobs, 0 caller frontend/test/docs). Frontend dùng /api/jobs.
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
            "source": j.get("source") or ("affiliate" if any("[Tiếp Thị Liên Kết]" in msg for msg in j.get("logs", [])[:3]) else None),
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
    job_dir = get_safe_job_dir(job_id)
    removed_from_queue = False
    with JOBS_LOCK:
        live = JOBS.get(job_id)
        # Nhóm C (audit #6): chặn cả job đang chờ trong hàng đợi / job worker
        # vừa pop khỏi hàng nhưng chưa kịp set running (status vẫn "queued"
        # trong cửa sổ đó) / job worker đang chạy. Check + dequeue + pop JOBS
        # nguyên tử dưới cùng 1 lock — endpoint delete chạy ở threadpool nên
        # có thể xen vào giữa pop và set-running của worker nếu không khóa.
        if live is not None and (
            _is_job_running(live)
            or live.get("status") == "queued"
            or job_id in RENDER_QUEUE
            or job_id == RENDER_CURRENT_JOB_ID
        ):
            return JSONResponse(
                {"error": "Job đang chạy hoặc đang chờ trong hàng đợi, không thể xoá. Hãy hủy job trước."},
                status_code=409)
        if job_id in RENDER_QUEUE:
            RENDER_QUEUE.remove(job_id)
            removed_from_queue = True
        job = JOBS.pop(job_id, None)
    if removed_from_queue:
        _refresh_queue_positions()
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

    @field_validator("bg_music_path")
    @classmethod
    def _check_bg_music_path(cls, v: Optional[str]) -> Optional[str]:
        return _validate_safe_studio_bgm_field(v)


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

    # Nhóm C (audit #5): gate chống chạy chồng — compose thủ công không được
    # chạy đè lên pipeline đang render / retry / rerender / compose khác.
    with JOBS_LOCK:
        if (_is_job_running(job) or job.get("is_retrying")
                or job.get("status") == "queued" or job_id in RENDER_QUEUE
                or _job_has_active_rerender(job)):
            return JSONResponse(
                {"error": "Job đang render — hãy đợi xong rồi thử lại."},
                status_code=409)
        # A new export can be requested after cancelling an earlier one.
        job["cancelled"] = False
        if job.get("status") == "cancelled":
            job["status"] = "audit_pending"
        _MANUAL_COMPOSE_JOBS.add(job_id)

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
        compose_res = await asyncio.to_thread(
            run_cancellable_compose,
            compose_full_movie,
            lambda: _job_cancel_requested(job),
            results=results,
            output_dir=str(output_dir),
            project_title=title,
            font_size=font_size,
            bg_music_path=_resolve_existing_studio_bgm(bg_music_path),
            music_volume=music_volume,
            voice_volume=voice_volume,
            subtitle_mode=subtitle_mode,
            subtitle_style=req.subtitle_style if req else "cinema", subtitle_position=req.subtitle_position if req else "bottom",
            quality=quality,
            transition=transition,
            aspect_ratio=aspect_ratio,
            allow_partial=allow_partial,
        )
        # Nhóm C (audit #4): hủy trong lúc compose thủ công -> không ghi final_video/done.
        if _mark_cancelled_after_compose(job):
            final_dir = (get_safe_job_dir(job_id) / "final").resolve()
            result_paths = [_unwrap_final_path(compose_res)]
            if isinstance(compose_res, dict) and compose_res.get("srt_path"):
                result_paths.append(compose_res["srt_path"])
            for result_path in result_paths:
                path = Path(result_path).resolve()
                if path.parent == final_dir:
                    path.unlink(missing_ok=True)
            return JSONResponse({"error": "Job đã bị hủy trong lúc xuất phim."}, status_code=409)
        final_path = _unwrap_final_path(compose_res)
        rel = Path(final_path).name
        job["final_video"] = f"/api/jobs/{job_id}/video/{rel}"
        _sync_job_srt_file(job, compose_res)
        job["progress"] = 100
        job["status"] = "done"
        job_log(job, f"🎉 Xuất phim thành công! → {rel}")
        _save_job_best_effort(job)
        return {"success": True, "video_url": job["final_video"], "srt_file": job.get("srt_file")}
    except Exception as e:
        if _mark_cancelled_after_compose(job):
            return JSONResponse({"error": "Job đã bị hủy trong lúc xuất phim."}, status_code=409)
        job_log(job, f"❌ Lỗi xuất phim: {e}")
        _save_job_best_effort(job)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        # Nhóm C (audit #5): luôn gỡ cờ compose thủ công, kể cả khi lỗi/hủy.
        with JOBS_LOCK:
            _MANUAL_COMPOSE_JOBS.discard(job_id)


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str):
    """Dừng hoặc hủy job đang chạy (cancel thật task pipeline + cờ cancelled)."""
    job = get_or_restore_job(job_id)
    if not job:
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    # Nhóm 1: job còn chờ trong hàng đợi thì gỡ khỏi hàng (không render nữa).
    was_queued = dequeue_render_job(job_id)
    with JOBS_LOCK:
        task = JOB_TASKS.get(job_id)
        cancel_event = JOB_CANCEL_EVENTS.get(job_id)
        job["cancelled"] = True
        job["status"] = "cancelled"
        job["queue_position"] = 0
    # Set cờ hủy để run_batch dừng ở cảnh/poll tiếp theo (audit #8).
    if cancel_event is not None:
        cancel_event.set()
    # Cancel thật task pipeline (nếu còn chạy): task dừng ở điểm await tiếp theo,
    # pipeline kiểm tra cờ cancelled ở các mốc giai đoạn rồi thoát.
    if task is not None and not task.done():
        task.cancel()
    if was_queued:
        job_log(job, "🛑 Đã hủy job đang chờ trong hàng đợi (gỡ khỏi hàng render).")
    else:
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
    start_idx = payload.from_scene_idx if payload and payload.from_scene_idx else 1
    with JOBS_LOCK:
        if (_is_job_running(job) or job.get("is_retrying") or _job_has_active_rerender(job)):
            return JSONResponse(
                {"error": "Job đang chạy, không thể resume chồng lên tiến trình hiện tại."},
                status_code=409)
        # Nhóm 1: job đã xếp hàng chờ thì không xếp trùng.
        if job["id"] in RENDER_QUEUE:
            return JSONResponse(
                {"error": "Job đã xếp hàng chờ render, không thể resume chồng lên."},
                status_code=409)
        previous = job["req"], job.get("cancelled", False), job["status"]
        job["req"] = job["req"].model_copy(update={"start_scene_idx": start_idx})
        job["cancelled"] = False
        job["status"] = "queued"
    job_log(job, f"▶️ Tiếp tục chạy job từ Cảnh {start_idx}...")
    try:
        _save_job_or_http500(job)
    except HTTPException:
        with JOBS_LOCK:
            job["req"], job["cancelled"], job["status"] = previous
        raise
    # Nhóm 1: resume cũng qua hàng đợi render (nối tiếp, không chồng job khác).
    queue_position = enqueue_render_job(job)
    return {"success": True, "message": f"Đang tiếp tục chạy job từ Cảnh {start_idx}",
            "queue_position": queue_position}


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
    """Tải file phụ đề SRT của phim an toàn.
    Chỉ phục vụ file SRT đi kèm đúng video thành phẩm hiện tại của job
    (cùng basename, khác đuôi .srt) — fix 2026-11-10: trước đây endpoint
    fallback glob final/*.srt nên có thể phục vụ file SRT CŨ còn sót từ
    lần xuất trước (burn_in cũ), khiến trình phát lại hiện 2 phụ đề dù
    bản xuất mới đã không còn file SRT cạnh mp4."""
    job = get_or_restore_job(job_id)
    if not job:
        return JSONResponse({"error": "Không tìm thấy job"}, status_code=404)
    final_video = job.get("final_video") or ""
    video_name = final_video.rsplit("/", 1)[-1] if final_video else ""
    video_stem = video_name.rsplit(".", 1)[0] if "." in video_name else video_name
    candidates = []
    if video_stem:
        candidates.append(f"final/{video_stem}.srt")
    req: Optional[MovieRequest] = job.get("req")
    title = (req.title if req else "movie").replace(" ", "_")
    clean_title = "".join(c for c in title if c.isalnum() or c in ("-", "_")).strip() or "movie"
    candidates.append(f"final/{clean_title}.srt")
    for cand in candidates:
        srt_path = resolve_safe_job_path(job_id, cand)
        if srt_path.exists() and srt_path.is_file():
            return FileResponse(
                str(srt_path),
                media_type="application/x-subrip",
                headers={"Content-Disposition": f"attachment; filename*=utf-8''{srt_path.name}"}
            )
    return JSONResponse({"error": "Không tìm thấy file phụ đề SRT"}, status_code=404)


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
    reorder_shot_desk_shots,
    save_previous_shot_clip,
    restore_previous_shot_clip,
    discard_previous_shot_clip,
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

        # Timeline Editor Shot Desk (nhóm 2): render lại 1 shot thì lưu clip
        # cũ sang previous/ (giữ tối đa 1 bản) để so sánh A/B / quay lại —
        # tái dùng endpoint render sẵn có, không thêm endpoint rerender riêng.
        # Nạp lại proj sau khi lưu để target mang previous_clip mới nhất
        # (tránh save ở dưới đè mất meta).
        prev_meta = None
        try:
            prev_meta = save_previous_shot_clip(project_id, shot_index)
        except Exception as exc:
            print(f"[ShotDesk] Không lưu được clip cũ shot {shot_index}: {exc}")
        if prev_meta is not None:
            proj = get_shot_desk_project(project_id) or proj
            target = next((s for s in proj.get("shots", [])
                           if s["index"] == shot_index), target)

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
            return {"success": True, "message": f"Đang render Shot {shot_index} trong nền...", "status": "rendering",
                    "previous_clip": prev_meta}
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
                return {"success": True, "shot": shot, "previous_clip": prev_meta}
            finally:
                ACTIVE_SHOT_TASKS.get(project_id, set()).discard(shot_index)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


class ShotDeskReorderRequest(BaseModel):
    order: List[int] = Field(min_length=1, max_length=200)


@app.post("/api/shot-desk/projects/{project_id}/shots/reorder")
def api_shot_desk_reorder_shots(project_id: str, payload: ShotDeskReorderRequest):
    """Đổi thứ tự các shot (Timeline Editor Shot Desk) — cùng cơ chế với
    POST /api/jobs/{id}/scenes/reorder của panel Tạo phim (PR #2).
    Body: {"order": [3, 1, 2]} = chỉ số CŨ theo thứ tự MỚI (hoán vị 1..N).
    Đổi tên thư mục/file shot + clip/audio theo chỉ số mới, đánh dấu final cũ."""
    get_safe_project_dir(project_id)
    proj = get_shot_desk_project(project_id)
    if not proj:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)
    if ACTIVE_SHOT_TASKS.get(project_id) or project_id in ACTIVE_SHOT_DESK_COMPOSE:
        return JSONResponse(
            {"error": "Dự án đang render hoặc đang dựng phim — không đổi được thứ tự lúc này."},
            status_code=409)
    try:
        new_proj = reorder_shot_desk_shots(project_id, list(payload.order))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    total_duration = sum(int(s.get("duration") or 10) for s in new_proj.get("shots", []))
    return {
        "success": True,
        "order": list(payload.order),
        "total_duration": total_duration,
        "final_stale": True,
        "shots": [{"index": s["index"], "prompt": s.get("prompt", ""),
                   "duration": s.get("duration")} for s in new_proj.get("shots", [])],
    }


@app.get("/api/shot-desk/projects/{project_id}/shots/{shot_index}/previous-video")
def api_shot_desk_get_shot_previous_video(project_id: str, shot_index: int):
    """Stream bản clip CŨ của 1 shot Shot Desk (so sánh A/B sau render lại)."""
    p = resolve_safe_project_path(project_id, f"previous/shot_{shot_index:03d}_clip.mp4")
    if not p.exists():
        p = resolve_safe_project_path(project_id, f"previous/shot_{shot_index:03d}_raw.mp4")
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "Shot này không có bản clip cũ"}, status_code=404)
    return FileResponse(str(p), media_type="video/mp4")


@app.post("/api/shot-desk/projects/{project_id}/shots/{shot_index}/restore-previous")
def api_shot_desk_restore_previous(project_id: str, shot_index: int):
    """Quay lại bản cũ: hoán đổi clip hiện tại <-> bản previous (bản mới vừa
    render thành bản cũ để vẫn đổi lại được). Khôi phục cả prompt cũ."""
    get_safe_project_dir(project_id)
    proj = get_shot_desk_project(project_id)
    if not proj:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)
    target = next((s for s in proj.get("shots", [])
                   if s.get("index") == shot_index), None)
    if not target:
        return JSONResponse({"error": f"Không tìm thấy Shot {shot_index}"}, status_code=404)
    if shot_index in ACTIVE_SHOT_TASKS.get(project_id, set()):
        return JSONResponse({"error": "Shot đang render — hãy đợi xong rồi thử lại."}, status_code=409)
    if not target.get("previous_clip"):
        return JSONResponse({"error": f"Shot {shot_index} không có bản clip cũ để quay lại"}, status_code=404)
    try:
        new_prev = restore_previous_shot_clip(project_id, shot_index)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    proj = get_shot_desk_project(project_id)
    shot = next((s for s in proj.get("shots", [])
                 if s.get("index") == shot_index), target)
    return {
        "success": True,
        "shot_index": shot_index,
        "prompt": shot.get("prompt", ""),
        "previous_clip": new_prev,
        "final_stale": True,
    }


@app.post("/api/shot-desk/projects/{project_id}/shots/{shot_index}/discard-previous")
def api_shot_desk_discard_previous(project_id: str, shot_index: int):
    """Giữ bản mới: xóa file + metadata bản cũ (xác nhận A/B xong)."""
    get_safe_project_dir(project_id)
    proj = get_shot_desk_project(project_id)
    if not proj:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)
    target = next((s for s in proj.get("shots", [])
                   if s.get("index") == shot_index), None)
    if not target:
        return JSONResponse({"error": f"Không tìm thấy Shot {shot_index}"}, status_code=404)
    if shot_index in ACTIVE_SHOT_TASKS.get(project_id, set()):
        return JSONResponse({"error": "Shot đang render — hãy đợi xong rồi thử lại."}, status_code=409)
    if not target.get("previous_clip"):
        return JSONResponse({"error": f"Shot {shot_index} không có bản clip cũ để xóa"}, status_code=404)
    try:
        discard_previous_shot_clip(project_id, shot_index)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"success": True, "shot_index": shot_index}


class RenderAllRequest(BaseModel):
    chain: bool = True
    concurrent: int = Field(default=6, ge=1, le=24)


@app.post("/api/shot-desk/projects/{project_id}/render-all")
async def api_shot_desk_render_all(project_id: str, payload: Optional[RenderAllRequest] = None):
    """Kích hoạt render hàng loạt đa luồng (song song) qua toàn bộ các tài khoản Muse khả dụng."""
    proj = get_shot_desk_project(project_id)
    if not proj:
        return JSONResponse({"error": "Không tìm thấy dự án"}, status_code=404)

    # Tự động phát hiện số tài khoản Muse / Chrome Profile đang hoạt động để phân bổ song song
    num_accounts = 1 if MUSE_PROVIDER == "browser" else 6
    try:
        _cl = get_http_client()
        hr = await _cl.get(f"{MUSE_API_URL}/admin/accounts", headers={"Authorization": f"Bearer {MUSE_API_KEY}"}, timeout=3.0)
        if hr.status_code == 200:
            num_accounts = muse_config.muse_capacity_from_accounts(MUSE_PROVIDER, hr.json(), max_concurrent=24)
    except Exception as acc_e:
        print(f"[RenderAll] Warning querying active accounts: {acc_e}")

    if payload and payload.concurrent and payload.concurrent > 0:
        concurrent = payload.concurrent
    else:
        concurrent = num_accounts

    chain = payload is None or payload.chain
    max_lanes = num_accounts if MUSE_PROVIDER == "browser" else 24
    concurrent = 1 if chain else max(1, min(max_lanes, concurrent))

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
                    api_key=MUSE_API_KEY, chain=chain
                )
            except Exception as ex:
                from shot_desk import update_shot_state
                await update_shot_state(project_id,shot_idx,{"status":"ready","error":str(ex)})
                print(f"[ShotDesk Concurrent Error shot {shot_idx}] {ex}")
            finally:
                ACTIVE_SHOT_TASKS.get(project_id, set()).discard(shot_idx)

    async def _run_batch():
        if chain:
            for shot in sorted(eligible, key=lambda s: s["index"]):
                await _render_one(shot["index"])
            return
        tasks = [_render_one(s["index"]) for s in eligible]
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.create_task(_run_batch())
    return {
        "success": True,
        "message": f"Đã bắt đầu dựng {len(eligible)} cảnh · " + ("nối khung cuối tuần tự" if chain else f"{concurrent} luồng độc lập"),
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
    dialogue: Optional[str] = Field(default=None, max_length=3000)
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
    muse_narrator: MuseNarratorProfile | None = None
    apply_to_all_shots: bool = False


@app.post("/api/shot-desk/projects/{project_id}/settings")
def api_shot_desk_update_settings(project_id: str, payload: ProjectSettingsRequest):
    """Cập nhật cấu hình giọng đọc và chế độ thoại toàn dự án."""
    try:
        settings = payload.model_dump(exclude_none=True)
        if "muse_narrator" in payload.model_fields_set and payload.muse_narrator is None:
            settings["muse_narrator"] = None
        res = update_shot_desk_project_settings(project_id, settings)
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

# G3 — Lịch sử version project: giữ tối đa 20 snapshot/phim, bản cũ nhất
# tự xóa khi vượt.
MAX_PROJECT_VERSIONS = 20


class ProjectSaveRequest(BaseModel):
    name: str = Field(max_length=100)
    title: str = Field(default="", max_length=200)
    script_text: str = Field(default="", max_length=100000)
    aspect_ratio: str = Field(default="16:9", max_length=10)
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=200)
    voice_mode: str = Field(default="tts", max_length=20)
    muse_narrator: MuseNarratorProfile | None = None
    consistency_mode: str = Field(default="standard", max_length=20)
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
    # G3 — Lịch sử version: mỗi lần Lưu append 1 snapshot của đúng nội dung
    # vừa lưu vào cùng file project (key "versions"), giữ tối đa 20 bản,
    # bản cũ nhất tự xóa khi vượt. Lưu trong cùng file để không đổi format
    # file / không sinh thêm file cạnh nó; project cũ (không có "versions")
    # vẫn mở bình thường, bản lưu đầu tiên sau nâng cấp tạo versions=[v1].
    existing = _read_project_json(filepath)
    versions = list(existing.get("versions", [])) if isinstance(existing, dict) else []
    versions = [v for v in versions if isinstance(v, dict)]
    next_version = max((v.get("version", 0) for v in versions), default=0) + 1
    versions.append(_project_version_snapshot(data, next_version))
    versions = versions[-MAX_PROJECT_VERSIONS:]
    data["versions"] = versions
    try:
        _atomic_write_json(filepath, data)
    except Exception as exc:
        logger.exception("Không lưu được project %s", clean_name)
        return JSONResponse({"error": f"Không lưu được project: {exc}"}, status_code=500)
    return {"success": True, "name": clean_name, "version": next_version,
            "versions_count": len(versions),
            "message": f"Đã lưu project '{clean_name}' (bản #{next_version})"}


def _read_project_json(filepath: Path):
    """Đọc file project JSON, trả None khi chưa có / hỏng (không raise)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _project_version_snapshot(data: Dict[str, Any], version: int) -> Dict[str, Any]:
    """1 snapshot version: metadata + toàn bộ nội dung project (trừ key
    "versions" để không lồng nhau). Khôi phục = ghi đè tuyệt đối bằng
    snapshot.data, cùng đường khôi phục như applyProjectData ở frontend."""
    snap = {k: v for k, v in data.items() if k != "versions"}
    return {
        "version": version,
        "saved_at": data.get("saved_at", time.time()),
        "title": data.get("title", ""),
        "scenes_count": len(data.get("scenes", []) or []),
        "data": snap,
    }


def _clean_project_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()


def _project_filepath_or_error(name: str):
    """Trả (filepath, None) khi hợp lệ + file tồn tại, ngược lại
    (None, JSONResponse lỗi). Dùng chung cho endpoints versions."""
    clean_name = _clean_project_name(name)
    if not clean_name or ".." in name:
        return None, JSONResponse({"error": "Tên project không hợp lệ"}, status_code=400)
    filepath = (PROJECTS_DIR / f"{clean_name}.json").resolve()
    if not filepath.is_relative_to(PROJECTS_DIR.resolve()) or not filepath.exists() or not filepath.is_file():
        return None, JSONResponse({"error": f"Không tìm thấy project '{name}'"}, status_code=404)
    return filepath, None


@app.get("/api/projects/{name}/versions")
def api_list_project_versions(name: str):
    """Liệt kê các bản snapshot của 1 project (mới nhất trước)."""
    filepath, err = _project_filepath_or_error(name)
    if err is not None:
        return err
    data = _read_project_json(filepath)
    if not isinstance(data, dict):
        return JSONResponse({"error": f"File project '{name}' bị hỏng"}, status_code=500)
    versions = [v for v in data.get("versions", []) if isinstance(v, dict)]
    return [
        {"version": v.get("version"), "saved_at": v.get("saved_at"),
         "title": v.get("title", ""), "scenes_count": v.get("scenes_count", 0)}
        for v in sorted(versions, key=lambda v: v.get("version", 0), reverse=True)
    ]


@app.post("/api/projects/{name}/versions/{version}/restore")
def api_restore_project_version(name: str, version: int):
    """Khôi phục project về 1 bản snapshot cũ (ghi đè tuyệt đối nội dung
    hiện tại bằng snapshot.data). Bản đã khôi phục được ghi thêm thành 1
    version mới nên vẫn undo được bằng cách khôi phục lại bản trước đó."""
    filepath, err = _project_filepath_or_error(name)
    if err is not None:
        return err
    data = _read_project_json(filepath)
    if not isinstance(data, dict):
        return JSONResponse({"error": f"File project '{name}' bị hỏng"}, status_code=500)
    versions = [v for v in data.get("versions", []) if isinstance(v, dict)]
    snap = next((v for v in versions if v.get("version") == version), None)
    if snap is None or not isinstance(snap.get("data"), dict):
        return JSONResponse(
            {"error": f"Không tìm thấy bản #{version} của project '{name}'"},
            status_code=404)
    restored = copy.deepcopy(snap["data"])
    restored["saved_at"] = time.time()
    next_version = max((v.get("version", 0) for v in versions), default=0) + 1
    versions.append(_project_version_snapshot(restored, next_version))
    versions = versions[-MAX_PROJECT_VERSIONS:]
    restored["versions"] = versions
    try:
        _atomic_write_json(filepath, restored)
    except Exception as exc:
        logger.exception("Không khôi phục được project %s bản #%s", name, version)
        return JSONResponse({"error": f"Không khôi phục được project: {exc}"}, status_code=500)
    return {"success": True, "name": _clean_project_name(name),
            "restored_version": version, "version": next_version,
            "message": f"Đã khôi phục project '{name}' về bản #{version}",
            "project": restored}


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
                    "versions_count": len(data.get("versions", [])),
                })
        except Exception:
            items.append({"name": f.stem, "title": f.stem, "scenes_count": 0, "saved_at": os.path.getmtime(f), "versions_count": 0})
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
        running = [jid for jid, j in JOBS.items() if j.get("status") in ("running", "queued")]
        active_tasks = [jid for jid, t in JOB_TASKS.items() if not t.done()]
        queued_jobs = set(RENDER_QUEUE) | _MANUAL_COMPOSE_JOBS
    active_sd = [pid for pid, sset in ACTIVE_SHOT_TASKS.items() if sset] + list(ACTIVE_SHOT_DESK_COMPOSE)
    active_batch = []
    try:
        active_batch = [
            v["id"] for v in BATCH_MANAGER.list_videos()
            if v.get("status") in ("running", "queued")
        ]
    except Exception:
        pass
    still_running = sorted(set(running) | set(active_tasks) | set(queued_jobs) | set(active_sd) | set(active_batch))
    if still_running:
        return JSONResponse(
            {"error": f"Không thể dọn dẹp: còn tác vụ đang chạy/chờ ({', '.join(still_running[:10])}). Hãy hủy hoặc đợi hoàn tất trước."},
            status_code=409)
    deleted_jobs = 0
    bytes_freed = 0
    protected_dirs = {"_uploads", "_music", "_voice_previews", "_tts_cache", "_factory_cache"}
    if keep_projects:
        protected_dirs.update({"_projects", "_shot_desk_projects", "_batch_videos", "_story_projects"})

    # Nhóm C (audit #6): chụp snapshot thư mục TRƯỚC khi xóa (iterdir lazy có
    # thể thấy thư mục job mới tạo giữa chừng).
    items = [it for it in WORKSPACE.iterdir()
             if it.is_dir() and it.name not in protected_dirs]
    for item in items:
        # Chỉ xóa thư mục job đã biết: có job_state.json với id khớp tên thư
        # mục. Thư mục lạ/không rõ job thì để lại, không xóa bừa.
        state_file = item / "job_state.json"
        job_id = None
        try:
            if state_file.exists():
                with open(state_file, "r", encoding="utf-8") as f:
                    _state = json.load(f)
                if isinstance(_state, dict) and _state.get("id") == item.name:
                    job_id = item.name
        except Exception:
            job_id = None
        if job_id is None:
            continue
        # Re-check ngay trước khi xóa: job có thể vừa được resume/render lại
        # trong lúc dọn (check đầu hàm đã thả lock từ lâu).
        with JOBS_LOCK:
            _live = JOBS.get(job_id)
            if ((_live is not None and (_live.get("status") in ("running", "queued")
                                        or _is_job_running(_live)))
                    or job_id in RENDER_QUEUE
                    or job_id == RENDER_CURRENT_JOB_ID):
                continue
        try:
            for root, dirs, files in os.walk(item):
                for file in files:
                    bytes_freed += os.path.getsize(os.path.join(root, file))
            shutil.rmtree(item)
            deleted_jobs += 1
        except Exception as e:
            print(f"[CleanWorkspaceError] {e}")
            continue
        # Sau khi xóa thư mục thì xóa khỏi bộ nhớ + hàng đợi (không để lại job
        # ma). Re-check lần nữa: nếu job vừa trở nên active trong lúc xóa đĩa
        # thì giữ lại trong JOBS (pipeline sẽ tự tạo lại thư mục).
        _queue_changed = False
        with JOBS_LOCK:
            _live = JOBS.get(job_id)
            if _live is None or (
                _live.get("status") not in ("running", "queued")
                and not _is_job_running(_live)
                and job_id not in RENDER_QUEUE
                and job_id != RENDER_CURRENT_JOB_ID
            ):
                JOBS.pop(job_id, None)
                if job_id in RENDER_QUEUE:
                    RENDER_QUEUE.remove(job_id)
                    _queue_changed = True
        if _queue_changed:
            _refresh_queue_positions()
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
    muse_voice: MuseVoiceProfile | None = None
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
    previous = next((c for c in chars if c.get("id") == c_id), {})
    aliases = char.aliases or []
    if char.name and char.name not in aliases:
        aliases.insert(0, char.name)

    sheet = dict(
        (char.character_sheet if "character_sheet" in char.model_fields_set else previous.get("character_sheet")) or {}
    )
    for k in ["front", "side", "closeup", "fullbody"]:
        if k not in sheet:
            sheet[k] = ""

    avatar_val = (char.avatar if "avatar" in char.model_fields_set else previous.get("avatar")) or ""
    if "character_sheet" not in char.model_fields_set and "avatar" in char.model_fields_set:
        if avatar_val or sheet["front"] == previous.get("avatar"):
            sheet["front"] = avatar_val
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
            if "muse_voice" not in char.model_fields_set and c.get("muse_voice"):
                data["muse_voice"] = c["muse_voice"]
            chars[i] = data
            found = True
            break
    if not found:
        chars.insert(0, data)
    if char.muse_voice is not None:
        data["muse_voice"] = char.muse_voice.model_dump()

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
                ref_str = str(base_ref).strip()
                if ref_str.startswith(("http://", "https://", "data:image/")):
                    payload["reference_image"] = ref_str
                else:
                    from batch_engine import _resolve_safe_reference
                    safe_local = _resolve_safe_reference(ref_str)
                    if safe_local and safe_local.is_file():
                        mime = mimetypes.guess_type(str(safe_local))[0] or "image/png"
                        with open(safe_local, "rb") as f:
                            payload["reference_image"] = f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"
                    elif ref_str.startswith("/api/uploads/"):
                        payload["reference_image"] = f"http://localhost:8000{ref_str}"

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
                            media_headers = muse_config.browser_media_headers(img_url, MUSE_API_URL, MUSE_API_KEY)
                            img_resp = await client.get(img_url, timeout=180.0, **({"headers": media_headers} if media_headers else {}))
                            image_kind = _sniff_image_kind(img_resp.content[:16]) if img_resp.status_code == 200 else None
                            if image_kind:
                                extension = "jpg" if image_kind == "jpeg" else image_kind
                                fname = f"bible_{char_id}_{ang}_{uuid.uuid4().hex[:6]}.{extension}"
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
    """Lấy khung hình cuối cùng (last frame) đã trích xuất từ video của cảnh an toàn.

    GIỮ LẠI (audit 2026-11-08 #14): endpoint này KHÔNG chết — backend tự sinh
    URL trỏ tới nó trong response reorder (last_frame_url) và frontend dùng
    URL đó để hiển thị ảnh Last Frame ở Monitor/Shot Desk (app.js dùng
    s.last_frame_url trực tiếp làm src <img>). Ngoài ra còn phục vụ API ngoài.
    """
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
Giữ nguyên lời và thứ tự từng người nói. Voiceover chỉ dùng cho người kể ngoài hình.
Với thoại nhân vật, dùng voice_segments với nguồn tiếng on_screen, offscreen, radio, phone, speaker hoặc thought.
Chia lời kể và thoại nhân vật thành các nhịp riêng nếu cả hai xuất hiện cùng một đoạn.
Chỉ người đang nói trên hình mới mấp máy môi. Với giọng ngoài hình, dùng cảnh phản ứng im lặng hoặc góc cận thiết bị.

Kịch bản:
{raw}

Trả về JSON mảng các đối tượng:
[
  {{
    "title": "Tên nhịp (vd: Nhịp 1.1: Ánh mắt cảnh giác)",
    "prompt": "Prompt tiếng Anh điện ảnh chi tiết góc máy, ánh sáng, hành động ngắn duy nhất",
    "voiceover": "Lời kể ngoài hình hoặc chuỗi rỗng",
    "voice_segments": [{{"character": "Tên người nói", "text": "Lời thoại nguyên văn", "delivery": "on_screen"}}],
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


@app.get("/api/reference-images")
@app.get("/api/character-library", deprecated=True)
def get_reference_images():
    """Lấy danh sách các ảnh tham chiếu đã tải lên gần đây để tái sử dụng.

    audit 2026-11-08 #10: endpoint này trước đây tên /api/character-library,
    gây nhầm với GET /api/characters (hồ sơ nhân vật trong _characters.json)
    trong khi nó chỉ trả 20 ảnh upload gần đây. Đổi tên chính thành
    /api/reference-images; route cũ giữ làm alias deprecated để không gãy
    client ngoài, sẽ gỡ ở bản sau.
    """
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


# ── Voice preview cache: giới hạn 200 file, dọn LRU theo mtime ──────────
# audit 2026-11-08 #15: _voice_previews trước đây tích lũy vô hạn (mỗi giọng
# × mỗi câu mẫu một file). Làm theo mẫu _tts_cache_prune (batch_engine.py:
# 500 file/500MB) nhưng chỉ giới hạn số file vì preview rất nhỏ.
_VOICE_PREVIEW_MAX_FILES = 200


def _voice_preview_prune(cache_dir=None) -> None:
    """Dọn _voice_previews khi vượt giới hạn: xóa file cũ nhất (LRU theo mtime)."""
    try:
        cache_dir = Path(cache_dir) if cache_dir is not None else WORKSPACE / "_voice_previews"
        if not cache_dir.is_dir():
            return
        entries = [(p, p.stat()) for p in cache_dir.glob("*.mp3")]
        if len(entries) <= _VOICE_PREVIEW_MAX_FILES:
            return
        entries.sort(key=lambda ps: ps[1].st_mtime)  # cũ nhất trước
        for p, _s in list(entries):
            if len(entries) <= _VOICE_PREVIEW_MAX_FILES:
                break
            try:
                p.unlink()
                entries.remove((p, _s))
            except OSError:
                pass
    except Exception as exc:
        print(f"[Voice Preview] Prune thất bại (bỏ qua): {exc}")


@app.get("/api/voices/preview")
async def preview_voice(voice: str = "vi-VN-HoaiMyNeural", text: str = ""):
    """Sinh nhanh đoạn audio mẫu để người dùng nghe thử giọng nói trực tiếp trên web.

    audit 2026-11-08 #12: các voice đặc biệt (ai_voice/native/none/no_dub)
    KHÔNG có giọng TTS thật — trước đây backend âm thầm đổi thành
    vi-VN-HoaiMyNeural và phát giọng khác, gây hiểu lầm. Giờ trả 400 để
    frontend disable nút nghe thử kèm tooltip giải thích, không phát giọng khác.
    """
    if voice in ("ai_voice", "native", "none", "no_dub"):
        raise HTTPException(
            status_code=400,
            detail="Giọng này không có bản nghe thử: lời thoại được AI sinh giọng gốc trong video, không qua TTS.",
        )
    sample_text = text.strip()
    if not sample_text:
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

    # audit #15: dọn LRU mỗi lần preview (kể cả cache hit) để thư mục đã
    # phình từ trước vẫn được dọn dần về tối đa 200 file.
    _voice_preview_prune(cache_dir)

    return FileResponse(str(cache_file), media_type="audio/mpeg")


@app.get("/api/auth/status")
async def auth_status(request: Request):
    key = os.getenv("STUDIO_API_KEY")
    return JSONResponse(
        {"auth_required": bool(key), "authenticated": bool(key and _studio_session_valid(request, key))},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/auth/session")
async def create_studio_session(request: Request):
    key = os.getenv("STUDIO_API_KEY")
    if not key:
        return JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
    if not _studio_key_matches(request.headers.get("x-api-key", ""), key):
        return JSONResponse({"error": "Khóa API Studio không đúng."}, status_code=401)
    claims = f"{int(time.time()) + _STUDIO_SESSION_SECONDS}.{uuid.uuid4().hex}"
    response = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        _STUDIO_SESSION_COOKIE, f"{claims}.{_studio_session_signature(key, claims)}",
        max_age=_STUDIO_SESSION_SECONDS, path="/api", httponly=True,
        secure=request.url.scheme == "https", samesite="strict",
    )
    return response


@app.delete("/api/auth/session")
async def delete_studio_session(request: Request):
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        return JSONResponse({"error": "Origin không hợp lệ."}, status_code=403)
    response = JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(_STUDIO_SESSION_COOKIE, path="/api")
    return response


class MuseSettingsRequest(BaseModel):
    provider: Literal["muse2api", "browser"]


def _muse_settings():
    selected = muse_config.saved_muse_provider() or MUSE_PROVIDER
    return {
        "provider": selected,
        "active_provider": MUSE_PROVIDER,
        "restart_required": selected != MUSE_PROVIDER,
    }


@app.get("/api/settings/muse")
async def get_muse_settings():
    return JSONResponse(_muse_settings(), headers={"Cache-Control": "no-store"})


@app.put("/api/settings/muse")
async def update_muse_settings(payload: MuseSettingsRequest, request: Request):
    global MUSE_PROVIDER, MUSE_API_URL, MUSE_API_KEY, _MUSE_KEY_NAME, _MUSE_MAX_CONCURRENT
    _check_local_form_origin(request)
    connection = muse_config.muse_connection(payload.provider)
    if payload.provider != MUSE_PROVIDER and (
        RENDER_QUEUE or any(_is_job_running(job) or job.get("is_retrying") for job in JOBS.values())
        or any(ACTIVE_SHOT_TASKS.values())
        or BATCH_MANAGER.list_queue(status="running")["counts"]["running"]
    ):
        raise HTTPException(409, "Studio đang dựng. Hãy đợi tác vụ hoàn tất rồi đổi kết nối Muse.")
    path = muse_config.MUSE_SETTINGS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, {"provider": payload.provider})
    except OSError as error:
        logger.warning("Không lưu được cấu hình Muse: %s", error)
        raise HTTPException(status_code=500, detail="Không lưu được cấu hình Muse.") from error
    MUSE_PROVIDER, MUSE_API_URL, MUSE_API_KEY = connection
    _MUSE_KEY_NAME = "MUSE_BROWSER_KEY" if MUSE_PROVIDER == "browser" else "MUSE2API_KEY"
    _MUSE_MAX_CONCURRENT = 12
    muse_config.active_muse_provider.cache_clear()
    return _muse_settings()


class ChromeProfileSelection(BaseModel):
    id: uuid.UUID | None


class MuseBrowserChatMode(BaseModel):
    mode: Literal["side", "main"]


class MuseBrowserPoolMode(BaseModel):
    enabled: bool


async def _browser_profiles_request(method: str, path: str = "", **kwargs):
    try:
        _, url, key = muse_config.muse_connection("browser")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not key:
        raise HTTPException(status_code=503, detail="Chưa cấu hình MUSE_BROWSER_KEY.")
    try:
        async with _shared_client_ctx() as client:
            response = await client.request(
                method, f"{url}/admin/chrome-profiles{path}",
                headers={"Authorization": f"Bearer {key}"}, timeout=10.0, **kwargs,
            )
        data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Không kết nối được gateway Muse Browser.") from error
    if not response.is_success:
        detail = data.get("error") if isinstance(data, dict) else None
        raise HTTPException(status_code=response.status_code, detail=detail or "Gateway Browser chưa hỗ trợ profile Chrome.")
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


def _check_local_form_origin(request: Request):
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(status_code=403, detail="Origin không hợp lệ.")


@app.get("/api/settings/muse/chrome-profiles")
async def list_muse_chrome_profiles():
    return await _browser_profiles_request("GET")


@app.post("/api/settings/muse/chrome-profiles/pair")
async def create_muse_profile_pairing(request: Request):
    _check_local_form_origin(request)
    return await _browser_profiles_request("POST", "/pair")


@app.put("/api/settings/muse/chrome-profiles/selection")
async def select_muse_chrome_profile(payload: ChromeProfileSelection, request: Request):
    _check_local_form_origin(request)
    return await _browser_profiles_request("PUT", "/selection", json={"id": str(payload.id) if payload.id else None})


@app.put("/api/settings/muse/chrome-profiles/chat-mode")
async def select_muse_browser_chat_mode(payload: MuseBrowserChatMode, request: Request):
    _check_local_form_origin(request)
    return await _browser_profiles_request("PUT", "/chat-mode", json={"mode": payload.mode})


@app.put("/api/settings/muse/chrome-profiles/pool")
async def select_muse_browser_pool(payload: MuseBrowserPoolMode, request: Request):
    _check_local_form_origin(request)
    return await _browser_profiles_request("PUT", "/pool", json={"enabled": payload.enabled})


@app.delete("/api/settings/muse/chrome-profiles/{profile_id}")
async def remove_muse_chrome_profile(profile_id: uuid.UUID, request: Request):
    _check_local_form_origin(request)
    return await _browser_profiles_request("DELETE", f"/{profile_id}")


def _get_windows_desktop_name() -> str:
    if os.name != "nt":
        return ""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        hdesk = u32.GetThreadDesktop(k32.GetCurrentThreadId())
        if not hdesk:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        needed = ctypes.c_uint(0)
        if u32.GetUserObjectInformationW(hdesk, 2, buf, 512, ctypes.byref(needed)):
            return str(buf.value or "")
    except Exception:
        return ""
    return ""


@app.post("/api/settings/muse/chrome-profiles/launch-workers")
async def launch_muse_chrome_workers(request: Request):
    _check_local_form_origin(request)
    if os.name != "nt":
        raise HTTPException(status_code=400, detail="Tính năng chạy file .bat chỉ hỗ trợ trên Windows.")
    desktop_name = _get_windows_desktop_name()
    if desktop_name and desktop_name.lower() != "default":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Server Studio đang chạy trong phiên nền ẩn ({desktop_name}), không thể mở Chrome ra màn hình chính từ đây. "
                "Hãy mở trực tiếp file start_muse_browsers_silent.bat trong thư mục dự án, "
                "hoặc khởi động Studio từ terminal Windows của bạn để dùng nút này."
            ),
        )
    bat_path = (Path(__file__).resolve().parent / "start_muse_browsers_silent.bat").resolve()
    if not bat_path.is_file():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy {bat_path.name} trong thư mục ứng dụng.")
    import subprocess
    try:
        subprocess.Popen(["explorer.exe", str(bat_path)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Không thể khởi chạy {bat_path.name}: {exc}")
    return {
        "ok": True,
        "script": bat_path.name,
        "message": "Đang mở 5 Chrome Worker độc lập chạy nền (không tắt trình duyệt cá nhân của bạn)...",
    }


async def _check_muse_gateway(provider: str, url: str, key: Optional[str]):
    accounts = 0
    ready = False
    error = ""
    pool = False
    busy = 0
    try:
        if not key:
            key_name = "MUSE_BROWSER_KEY" if provider == "browser" else "MUSE2API_KEY"
            raise RuntimeError(f"Chưa cấu hình {key_name}.")
        async with _shared_client_ctx() as client:
            response = await client.get(
                f"{url}/admin/accounts",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15.0,
            )
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                accounts = len(data)
            elif isinstance(data, dict):
                accounts = data.get("stats", {}).get("total", len(data.get("accounts", [])))
                busy = int(data.get("stats", {}).get("busy", 0) or 0)
                pool = data.get("pool") is True
            if provider == "browser":
                ready = isinstance(data, dict) and data.get("ready") is True
                if not ready:
                    hint = data.get("error") if isinstance(data, dict) else None
                    error = str(hint)[:160] if hint else "Mở Chrome hoặc Edge của gateway và đăng nhập Muse."
            else:
                ready = True
        else:
            error = f"Gateway trả HTTP {response.status_code}."
    except Exception as exc:
        error = str(exc)[:120]
    return {"ready": ready, "accounts": accounts, "error": error, "pool": pool, "busy": busy}


@app.get("/api/settings/muse/health")
async def muse_settings_health(provider: Literal["muse2api", "browser"]):
    try:
        connection = muse_config.muse_connection(provider)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    result = await _check_muse_gateway(*connection)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def health_check():
    """Check whether the active Muse provider is ready."""
    result = await _check_muse_gateway(MUSE_PROVIDER, MUSE_API_URL, MUSE_API_KEY)
    return {
        "studio": True,
        "muse2api": result["ready"],
        "muse_provider": MUSE_PROVIDER,
        "muse_url": MUSE_API_URL,
        "accounts": result["accounts"],
        "muse_pool": result.get("pool", False),
        "muse_busy": result.get("busy", 0),
        "muse_error": result["error"],
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
        "[Tên nhân vật] [nguồn tiếng]: \"[Lời thoại tiếng Việt]\"\n\n"
        "2. Nguồn tiếng là on_screen, offscreen, radio, phone, speaker hoặc thought. "
        "Dùng 'Người kể chuyện [offscreen]' cho lời kể. Giữ đúng người, lời và thứ tự. "
        "Chỉ người đang nói trực tiếp mới mấp máy môi. Dùng cảnh phản ứng im lặng khi nghe giọng ngoài hình.\n"
        "3. Không giải thích dông dài, trả về trực tiếp nội dung kịch bản phân cảnh."
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
    if any(mark in raw for mark in ('"', "“", "”", "«", "»")) or re.search(
        r"\b(?:dialogue|voice[ -]?over|lời thoại|lời kể|độc thoại)\s*:", raw, re.I
    ):
        return raw

    # Nếu đã là prompt tiếng Anh chi tiết, giữ nguyên
    if not has_vietnamese(raw) and len(raw.split()) >= 12 and any(k in raw.lower() for k in ("cinematic", "masterpiece", "shot", "lighting", "4k")):
        return raw

    # Thử dịch và nâng cấp qua Muse Chat
    system_inst = (
        "You are an expert Hollywood AI video prompt director. "
        "Convert the user's input (Vietnamese or short English) into a single, cohesive, vivid cinematic English video generation prompt. "
        "Include camera angle, subject actions, lighting, atmosphere, 4K resolution, photorealistic, masterpiece, no text, no watermark. "
        "Keep any spoken words verbatim in the original language, with the same speaker and audio source. "
        "Never turn a narrator, a thought or a voice from a device into somebody speaking in the frame. "
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
    cast_ids: list[str] = Field(default_factory=list, max_length=6)
    visual_style: str = Field(default="",max_length=50)
    prompt: str = Field(max_length=5000)
    duration: int = Field(default=5, ge=1, le=60)
    size: str = Field(default="16:9", max_length=10)
    reference_image: Optional[str] = None
    dialogue: str = Field(default="", max_length=3000)
    voiceover: str = Field(default="", max_length=3000)
    muse_narrator: MuseNarratorProfile | None = None


class EnhancePromptReq(BaseModel):
    prompt: str = Field(max_length=5000)
    size: str = Field(default="16:9", max_length=10)


@app.post("/api/quick-video/enhance-prompt")
async def api_quick_video_enhance_prompt(req: EnhancePromptReq):
    """Nâng cấp prompt tiếng Việt thành cinematic English prompt."""
    enhanced = await auto_enhance_prompt(req.prompt, req.size)
    return {"original": req.prompt, "enhanced": enhanced}


# Provider ownership keeps polling and playback attached to the original job.
_quick_video_providers: Dict[str, str] = {}


@app.post("/api/quick-video")
async def api_quick_video(req: QuickVideoRequest):
    """Tạo video ngắn qua kết nối Muse đang hoạt động."""
    require_muse_key()  # 503 rõ ràng nếu chưa cấu hình key
    from batch_engine import character_block_of
    try:
        cast = _selected_cast(req.cast_ids)
        speech = plan_muse_speech(req.model_dump(), cast, req.muse_narrator, freeform=True)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

    prompt_to_send = req.prompt.strip()
    # Tự động tối ưu hóa prompt nếu nhập tiếng Việt hoặc prompt quá ngắn để đảm bảo Muse AI nhận diện đúng
    if has_vietnamese(prompt_to_send) or len(prompt_to_send.split()) < 5:
        try:
            enhanced = await auto_enhance_prompt(prompt_to_send, req.size)
            if enhanced:
                prompt_to_send = enhanced
        except Exception:
            pass

    visible_cast = [ch for ch in cast if ch["name"] not in speech.remote]
    for ch in visible_cast:
        block = character_block_of(ch)
        if not block:
            raise HTTPException(422, f"Nhân vật {ch['name']} thiếu mô tả nhận dạng.")
        if block not in prompt_to_send:
            prompt_to_send += "\n" + ch["name"] + ": " + block
    if req.visual_style:
        if req.visual_style not in BATCH_VISUAL_STYLES:
            raise HTTPException(422, "Phong cách không hợp lệ.")
        prompt_to_send += "\n" + BATCH_VISUAL_STYLES[req.visual_style]["prompt_suffix"]
    if visible_cast and not req.reference_image:
        from batch_video_engine import _cast_reference
        req.reference_image, order = await asyncio.to_thread(_cast_reference, visible_cast)
        if order:
            prompt_to_send += "\n" + order
    payload: Dict[str, Any] = {
        "prompt": speech.append_to(prompt_to_send),
        "duration": req.duration,
        "size": req.size,
    }

    if req.reference_image:
        ref = req.reference_image.strip()
        if ref.startswith("/api/uploads/"):
            local_p = UPLOAD_DIR / os.path.basename(ref)
            if local_p.exists():
                with open(local_p, "rb") as f:
                    image = f.read()
                    kind = _sniff_image_kind(image) or ("gif" if image.startswith((b"GIF87a", b"GIF89a")) else "png")
                    payload["reference_image"] = (
                        f"data:image/{kind};base64,{base64.b64encode(image).decode('ascii')}"
                    )
            else:
                raise HTTPException(422, "Ảnh tham chiếu không còn trong thư viện. Chọn lại ảnh trước khi tạo video.")
        else:
            payload["reference_image"] = ref

    payload["timeout"] = 600
    provider, url, key = MUSE_PROVIDER, MUSE_API_URL, MUSE_API_KEY

    try:
        async with _shared_client_ctx() as client:
            r = await client.post(
                f"{url}/v1/videos",
                json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=30.0,
            )
        if r.status_code in (200, 202):
            data = r.json()
            task_id = data.get("task_id") or data.get("id")
            if isinstance(task_id, str):
                if len(_quick_video_providers) >= 2048:
                    _quick_video_providers.pop(next(iter(_quick_video_providers)))
                _quick_video_providers[task_id] = provider
            return {"success": True, **data}
        return JSONResponse(
            {"success": False, "error": f"Muse HTTP {r.status_code}: {r.text[:300]}"},
            status_code=r.status_code,
        )
    except Exception as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)


@app.get("/api/quick-video/{task_id}")
async def api_quick_video_status(task_id: str):
    """Kiểm tra trạng thái task video nhanh từ kết nối Muse đang hoạt động."""
    provider = _quick_video_providers.get(task_id)
    _, url, key = muse_config.muse_connection(provider) if provider else (MUSE_PROVIDER, MUSE_API_URL, MUSE_API_KEY)
    if not key:
        raise HTTPException(503, "Chưa cấu hình khóa cho kết nối Muse của tác vụ.")
    try:
        async with _shared_client_ctx() as client:
            r = await client.get(
                f"{url}/v1/videos/{task_id}",
                headers={"Authorization": f"Bearer {key}"},
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
async def api_quick_video_stream(task_id: str, request: Request):
    """Proxy stream video nhanh từ muse2api về browser (tránh CORS)."""
    provider = _quick_video_providers.get(task_id)
    _, url, key = muse_config.muse_connection(provider) if provider else (MUSE_PROVIDER, MUSE_API_URL, MUSE_API_KEY)
    if not key:
        raise HTTPException(503, "Chưa cấu hình khóa cho kết nối Muse của tác vụ.")
    try:
        async with _shared_client_ctx() as status_client:
            r = await status_client.get(
                f"{url}/v1/videos/{task_id}",
                headers={"Authorization": f"Bearer {key}"},
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
            video_url = f"{url}{video_url}"


        headers = muse_config.browser_media_headers(video_url, url, key)
        headers["Accept-Encoding"] = "identity"
        if request.headers.get("range"):
            headers["Range"] = request.headers["range"]
        stream_client = httpx.AsyncClient(timeout=120.0)
        try:
            upstream = await stream_client.send(stream_client.build_request("GET", video_url, headers=headers), stream=True)
        except BaseException:
            await stream_client.aclose()
            raise

        async def close_stream():
            await upstream.aclose()
            await stream_client.aclose()

        if upstream.status_code not in (200, 206):
            code = upstream.status_code
            outgoing_headers = {"Content-Range": upstream.headers["content-range"]} if "content-range" in upstream.headers else {}
            await close_stream()
            return JSONResponse({"error": f"Không tải được video (HTTP {code})."}, status_code=code, headers=outgoing_headers)

        passthrough = {
            name: upstream.headers[name]
            for name in ("accept-ranges", "content-range", "content-length", "content-encoding")
            if name in upstream.headers
        }
        return StreamingResponse(
            upstream.aiter_raw(65536),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "video/mp4"),
            headers=passthrough,
            background=BackgroundTask(close_stream),
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── AFFILIATE MARKETING (TIẾP THỊ LIÊN KẾT) ──────────────────────────────────

class AffiliateRenderRequest(CaptionSettings):
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

    @field_validator("bg_music_path")
    @classmethod
    def _check_bg_music_path(cls, v: Optional[str]) -> Optional[str]:
        return _validate_safe_studio_bgm_field(v)


@app.get("/api/affiliate/templates")
async def get_affiliate_templates():
    """Trả về danh mục sản phẩm, framework kịch bản và preset mẫu tiếp thị."""
    return {
        "categories": AFFILIATE_CATEGORIES,
        "frameworks": AFFILIATE_FRAMEWORKS,
        "koc_personas": KOC_PERSONAS,
        "shooting_styles": AFFILIATE_SHOOTING_STYLES
    }


class AffiliateLinkImportRequest(BaseModel):
    url: str = Field(default="", max_length=2000)


@app.post("/api/affiliate/import-link")
async def api_affiliate_import_link(req: AffiliateLinkImportRequest):
    """Nhập thông tin sản phẩm từ link Shopee/TikTok để prefill form affiliate.

    422: link sai định dạng hoặc host không hỗ trợ. 502: không tải được link.
    Link hợp lệ nhưng không lấy được chi tiết vẫn trả 200 với fetched=False
    + message rõ ràng (UI prefill phần lấy được, còn lại nhập tay).
    Chạy fetch trong thread riêng để không chặn event loop (mỗi lượt ≤15s).
    """
    url = (req.url or "").strip()
    if not url:
        return JSONResponse(
            {"error": "Vui lòng dán link sản phẩm Shopee hoặc TikTok"},
            status_code=422,
        )
    try:
        result = await asyncio.to_thread(import_affiliate_product_link, url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse(
            {"error": f"Không tải được link: {e}"}, status_code=502
        )
    return result


class AffiliateFindImagesRequest(BaseModel):
    product_name: str = Field(default="", max_length=500)
    source_url: Optional[str] = Field(default=None, max_length=2000)


@app.post("/api/affiliate/find-product-images")
async def api_affiliate_find_product_images(req: AffiliateFindImagesRequest):
    """Tìm ảnh ứng viên cho sản phẩm (tối đa 8) để người dùng chọn làm ảnh Product Lock.

    Dùng khi ảnh sản phẩm hiện tại chỉ là thùng/vỏ (không thấy bên trong):
    nguồn ưu tiên là ảnh nền tảng gốc từ source_url (Shopee API nhiều ảnh,
    TikTok og_info 1 ảnh), sau đó là tìm kiếm ảnh web (DuckDuckGo, fallback
    Bing). Trả 200 với images=[] + message rõ ràng khi không tìm được ảnh
    nào (UI vẫn cho tải ảnh tay). 422 khi thiếu tên sản phẩm.
    Chạy fetch trong thread riêng để không chặn event loop.
    """
    name = (req.product_name or "").strip()
    if not name:
        return JSONResponse(
            {"error": "Vui lòng nhập tên sản phẩm để tìm ảnh"},
            status_code=422,
        )
    try:
        result = await asyncio.to_thread(
            find_product_images, name, req.source_url or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse(
            {"error": f"Không tìm được ảnh: {e}"}, status_code=502
        )
    return result


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
        # Thời lượng theo thoại từng cảnh (clip 5/6/8/10s): giữ duration kịch
        # bản gửi lên, chỉ kẹp về 1–60s cho an toàn. Script cũ gửi 10 vẫn
        # chạy đúng như trước (tương thích ngược).
        try:
            _dur = int(s.get("duration")) if s.get("duration") is not None else AFFILIATE_SCENE_DURATION
        except (TypeError, ValueError):
            _dur = AFFILIATE_SCENE_DURATION
        _dur = max(1, min(60, _dur))
        scene_items.append(SceneItem(
            index=idx,
            prompt=s.get("prompt", ""),
            voiceover=s.get("voiceover", ""),
            voice=s.get("voice") or req.voice or "vieneu:AdamBua",
            duration=_dur,
            size=req.aspect_ratio or "9:16",
            reference_image=s.get("reference_image") or None,
            keep_reference=bool(s.get("reference_image")),
            offscreen_narration=True,
        ))

    movie_req = MovieRequest(
        title=req.title or "Video Tiếp Thị",
        subtitle_style=req.subtitle_style, subtitle_position=req.subtitle_position,
        voice=req.voice or "vieneu:AdamBua",
        voice_mode="tts",
        aspect_ratio=req.aspect_ratio or "9:16",
        font_size=req.font_size or 22,
        bg_music_path=req.bg_music_path,
        music_volume=req.music_volume if req.music_volume is not None else 0.20,
        voice_volume=req.voice_volume if req.voice_volume is not None else 1.0,
        quality=req.quality or "720p",
        transition=req.transition or "dissolve",
        # The original product photo anchors every supplied scene.
        chain_mode="sequential",
        scenes=scene_items
    )

    job_id = uuid.uuid4().hex[:12]
    job = new_job(job_id, len(movie_req.scenes), req=movie_req)
    job["source"] = "affiliate"
    _save_job_or_http500(job)
    _total_s = sum(s.duration for s in movie_req.scenes)
    job_log(job, f"🛍️ [Tiếp Thị Liên Kết] Dự án: '{movie_req.title}' — {len(movie_req.scenes)} cảnh, tổng ~{_total_s}s ({movie_req.aspect_ratio}, chain sequential) — Giọng: {movie_req.voice}")
    _save_job_or_http500(job)
    # Xếp vào RENDER_QUEUE để không đụng độ khi đang render phim khác.
    queue_position = enqueue_render_job(job)

    return {
        "job_id": job_id,
        "message": "Đã bắt đầu sản xuất video tiếp thị liên kết" if queue_position <= 1 else f"Đã xếp hàng chờ render (vị trí {queue_position})",
        "total_scenes": len(movie_req.scenes),
        "queue_position": queue_position,
        "queued": queue_position > 1,
    }


# ── TẠO VIDEO HÀNG LOẠT (BATCH VIDEO CREATOR) ────────────────────────────────

from batch_video_engine import (
    BATCH_MANAGER,
    BATCH_PROMPT_TEMPLATES,
    BATCH_VISUAL_STYLES,
    BATCH_DIR,
)
import auto_factory
import story_engine

class BatchCreateRequest(BaseModel):
    render_mode: Literal["illustrated","ai_video"] = "illustrated"
    prompts: List[str] = Field(default=[], max_length=100)
    aspect_ratio: str = Field(default="9:16", max_length=10)
    scenes_count: int = Field(default=8, ge=3, le=12)
    visual_style: str = Field(default="anime", max_length=50)
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=100)
    mode: str = Field(default="free", max_length=50)
    character_id: Optional[str] = Field(default=None, max_length=100)
    auto_run: bool = True

    @field_validator("aspect_ratio")
    @classmethod
    def validate_aspect_ratio(cls, v: str) -> str:
        valid_ratios = {"9:16", "16:9", "1:1"}
        if v not in valid_ratios:
            raise ValueError(f"Tỷ lệ khung hình '{v}' không hợp lệ. Cho phép: {list(valid_ratios)}")
        return v

    @field_validator("visual_style")
    @classmethod
    def validate_visual_style(cls, v: str) -> str:
        if v not in BATCH_VISUAL_STYLES:
            raise ValueError(f"Phong cách '{v}' không hợp lệ. Cho phép: {list(BATCH_VISUAL_STYLES.keys())}")
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("free", "character"):
            raise ValueError("Chế độ tạo (mode) chỉ nhận 'free' hoặc 'character'")
        return v

    @field_validator("voice")
    @classmethod
    def validate_voice(cls, v: str) -> str:
        clean = v.strip()
        if clean in ("ai_voice", "native", "none", "no_dub"):
            raise ValueError(f"Chế độ '{clean}' không tạo file giọng đọc cho video ghép ảnh — hãy chọn giọng đọc TTS cụ thể")
        if not clean or len(clean) < 3 or not re.match(r"^[a-zA-Z0-9_\-:]+$", clean):
            raise ValueError(f"Tên giọng đọc '{v}' không hợp lệ")
        return clean


class BulkActionRequest(BaseModel):
    action: str = Field(..., max_length=20)
    video_ids: List[str] = Field(default=[], max_length=200)

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        clean = (v or "").strip().lower()
        if clean not in ("run", "delete"):
            raise ValueError(f"Hành động '{v}' không hợp lệ (chỉ hỗ trợ 'run' hoặc 'delete')")
        return clean

    @field_validator("video_ids")
    @classmethod
    def validate_video_ids(cls, v: List[str]) -> List[str]:
        cleaned = []
        for vid in v:
            vid_clean = (vid or "").strip()
            if not re.match(r"^bv_[a-zA-Z0-9_\-]+$", vid_clean):
                raise ValueError(f"Video ID '{vid}' không hợp lệ hoặc chứa ký tự nguy hiểm")
            cleaned.append(vid_clean)
        return cleaned


@app.get("/api/batch-videos/templates")
async def get_batch_templates():
    """Trả về danh mục mẫu prompt và các phong cách hình ảnh có sẵn."""
    return {
        "templates": BATCH_PROMPT_TEMPLATES,
        "styles": list(BATCH_VISUAL_STYLES.values())
    }


@app.get("/api/batch-videos/queue")
async def get_batch_queue(
    search: str = "",
    status: str = "all",
    sort: str = "newest"
):
    """Lấy danh sách hàng đợi video kèm bộ lọc và thống kê."""
    return BATCH_MANAGER.list_queue(search=search, status=status, sort=sort)


@app.post("/api/batch-videos/create")
async def create_batch_videos(req: BatchCreateRequest):
    """Tạo danh sách video hàng loạt từ các dòng prompt."""
    clean_prompts = [p.strip() for p in req.prompts if p.strip()]
    if not clean_prompts:
        return JSONResponse({"error": "Vui lòng nhập ít nhất một dòng prompt"}, status_code=400)

    created = BATCH_MANAGER.add_batch(
        prompts=clean_prompts,
        aspect_ratio=req.aspect_ratio,
        scenes_count=req.scenes_count,
        visual_style=req.visual_style,
        voice=req.voice,
        mode=req.mode,
        character_id=req.character_id,
        auto_run=req.auto_run,
        **({"extra":{"render_mode":"ai_video"}} if req.render_mode=="ai_video" else {})
    )
    return {
        "message": f"Đã thêm {len(created)} video vào hàng đợi",
        "created": created,
        "total": len(created)
    }


@app.post("/api/batch-videos/retry-failed")
async def retry_failed_batch():
    """Đưa toàn bộ video bị lỗi về hàng đợi để chạy lại."""
    count = BATCH_MANAGER.retry_failed()
    return {"message": f"Đã kích hoạt chạy lại {count} video bị lỗi", "count": count}


@app.post("/api/batch-videos/run-all")
async def run_all_batch():
    """Kích hoạt chạy toàn bộ video nháp và hàng đợi."""
    count = BATCH_MANAGER.run_all_queued()
    return {"message": f"Đã kích hoạt {count} video trong hàng đợi", "count": count}


@app.post("/api/batch-videos/{video_id}/run")
async def run_single_batch_video(video_id: str):
    """Kích hoạt chạy 1 video cụ thể."""
    ok = BATCH_MANAGER.run_video(video_id)
    if not ok:
        return JSONResponse({"error": "Không tìm thấy video"}, status_code=404)
    return {"message": "Đã bắt đầu xử lý video", "id": video_id}


@app.get("/api/batch-videos/{video_id}")
async def get_single_batch_video(video_id: str):
    """Lấy thông tin chi tiết của 1 video trong hàng đợi."""
    if not re.match(r"^bv_[a-zA-Z0-9_\-]+$", video_id):
        return JSONResponse({"error": "Video ID không hợp lệ"}, status_code=400)
    v = BATCH_MANAGER.get_video(video_id)
    if not v:
        return JSONResponse({"error": "Không tìm thấy video"}, status_code=404)
    return v


@app.delete("/api/batch-videos/{video_id}")
async def delete_single_batch_video(video_id: str):
    """Xóa 1 video khỏi hàng đợi và dọn dẹp file đĩa an toàn."""
    if not re.match(r"^bv_[a-zA-Z0-9_\-]+$", video_id):
        return JSONResponse({"error": "Video ID không hợp lệ"}, status_code=400)
    ok = BATCH_MANAGER.delete_video(video_id)
    if not ok:
        return JSONResponse({"error": "Không tìm thấy video"}, status_code=404)
    return {"message": "Đã xóa video thành công", "id": video_id}


@app.post("/api/batch-videos/{video_id}/open-shot-desk")
async def open_batch_in_shot_desk(video_id: str):
    """Chuyển đổi video hàng loạt sang dự án Shot Desk Studio để biên tập sâu."""
    res = BATCH_MANAGER.convert_to_shot_desk(video_id)
    if not res:
        return JSONResponse({"error": "Không tìm thấy video để chuyển đổi"}, status_code=404)
    return {"message": "Đã chuyển đổi sang Shot Desk thành công", **res}


@app.get("/api/batch-videos/{video_id}/video")
async def get_batch_video_file(video_id: str):
    """Tải / stream file video hoàn chỉnh final.mp4 của batch video."""
    item = BATCH_MANAGER.get_video(video_id)
    if item and item.get("guided") and not item.get("final_video_url"):
        raise HTTPException(409, "Cần xuất lại sau khi sửa phân cảnh.")
    video_path = (BATCH_DIR / video_id / "final.mp4").resolve()
    if not video_path.is_relative_to(BATCH_DIR.resolve()) or not video_path.is_file():
        return JSONResponse({"error": "Video chưa hoàn tất hoặc không tồn tại"}, status_code=404)
    return FileResponse(str(video_path), media_type="video/mp4")


@app.get("/api/batch-videos/{video_id}/subtitles")
async def get_batch_subtitles_file(video_id: str):
    """Tải file phụ đề SRT của batch video."""
    item = BATCH_MANAGER.get_video(video_id)
    if item and item.get("guided") and not item.get("srt_url"):
        raise HTTPException(409, "Cần xuất lại phụ đề.")
    srt_path = (BATCH_DIR / video_id / "subtitles.srt").resolve()
    if not srt_path.is_relative_to(BATCH_DIR.resolve()) or not srt_path.is_file():
        return JSONResponse({"error": "File phụ đề chưa sẵn sàng hoặc không tồn tại"}, status_code=404)
    return FileResponse(str(srt_path), media_type="text/plain; charset=utf-8", filename=f"{video_id}_subtitles.srt")


@app.post("/api/batch-videos/bulk-action")
async def bulk_action_batch_videos(req: BulkActionRequest):
    """Thực hiện thao tác hàng loạt (run hoặc delete) trên danh sách video được chọn."""
    if not req.video_ids:
        return JSONResponse({"error": "Danh sách video được chọn rỗng"}, status_code=400)

    if req.action == "run":
        count = BATCH_MANAGER.bulk_run(req.video_ids)
        return {"message": f"Đã kích hoạt chạy {count} video được chọn", "count": count, "action": "run"}
    elif req.action == "delete":
        count = BATCH_MANAGER.bulk_delete(req.video_ids)
        return {"message": f"Đã xóa thành công {count} video được chọn", "count": count, "action": "delete"}
    else:
        return JSONResponse({"error": f"Hành động '{req.action}' không hợp lệ (chỉ hỗ trợ 'run' hoặc 'delete')"}, status_code=400)


# ══════════════════════════════════════════════════════════════════════════
# XƯỞNG TỰ ĐỘNG (Auto Factory) — đặt hàng 1 cú bấm ra nhiều video
# (xem auto_factory.py: nguồn RSS, gói đăng bài; video chạy qua
# BatchVideoManager có sẵn, worker có sẵn tự chạy)
# ══════════════════════════════════════════════════════════════════════════

class FactoryOrderRequest(BaseModel):
    render_mode: Literal["illustrated","ai_video"] = "illustrated"
    source: str = Field(default="trending", max_length=20)
    topics: Optional[List[str]] = Field(default=None, max_length=10)
    count: int = Field(default=3, ge=1, le=10)
    scenes_count: int = Field(default=5, ge=3, le=8)
    visual_style: str = Field(default="cinematic", max_length=50)
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=100)
    aspect_ratio: str = Field(default="9:16", max_length=10)
    image_source: str = Field(default="muse", max_length=20)
    mode: str = Field(default="free", max_length=20)

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        if v not in ("trending", "news", "manual"):
            raise ValueError("Nguồn nội dung chỉ nhận 'trending', 'news' hoặc 'manual'")
        return v

    @field_validator("aspect_ratio")
    @classmethod
    def validate_aspect_ratio(cls, v: str) -> str:
        if v not in ("9:16", "16:9", "1:1"):
            raise ValueError(f"Tỷ lệ khung hình '{v}' không hợp lệ. Cho phép: 9:16, 16:9, 1:1")
        return v

    @field_validator("visual_style")
    @classmethod
    def validate_visual_style(cls, v: str) -> str:
        if v not in BATCH_VISUAL_STYLES:
            raise ValueError(f"Phong cách '{v}' không hợp lệ. Cho phép: {list(BATCH_VISUAL_STYLES.keys())}")
        return v

    @field_validator("voice")
    @classmethod
    def validate_voice(cls, v: str) -> str:
        clean = v.strip()
        if clean in ("ai_voice", "native", "none", "no_dub"):
            raise ValueError(f"Chế độ '{clean}' không tạo file giọng đọc cho video ghép ảnh — hãy chọn giọng đọc TTS cụ thể")
        if not clean or len(clean) < 3 or not re.match(r"^[a-zA-Z0-9_\-:]+$", clean):
            raise ValueError(f"Tên giọng đọc '{v}' không hợp lệ")
        return clean

    @field_validator("image_source")
    @classmethod
    def validate_image_source(cls, v: str) -> str:
        if v != "muse":
            raise ValueError("Nguồn tạo hình hiện chỉ hỗ trợ 'muse' (Muse AI)")
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("free", "character"):
            raise ValueError("Chế độ tạo (mode) chỉ nhận 'free' hoặc 'character'")
        return v

    @model_validator(mode="after")
    def validate_manual_topics(self):
        if self.source == "manual" or self.topics is not None:
            topics = list(dict.fromkeys(t.strip() for t in (self.topics or []) if t.strip()))
            if not topics:
                raise ValueError("Chọn hoặc nhập ít nhất 1 chủ đề.")
            self.topics = topics
        return self


@app.get("/api/factory/styles")
async def get_factory_styles():
    """11 phong cách hình ảnh của Xưởng tự động (theo đúng thứ tự hiển thị)."""
    return {"styles": auto_factory.factory_styles()}


@app.get("/api/factory/topics")
async def get_factory_topics(source: str = "trending", refresh: bool = False):
    """Lấy chủ đề tự động từ RSS (cache 30 phút; RSS lỗi thì fallback pool).
    refresh=true: bỏ qua cache, lấy mới từ RSS."""
    if source not in ("trending", "news"):
        return JSONResponse({"error": "Nguồn nội dung chỉ nhận 'trending' hoặc 'news'"}, status_code=422)
    result = await asyncio.to_thread(auto_factory.fetch_topics, source, None, not refresh)
    return {**result, "supports_topic_selection": True}


@app.post("/api/factory/order")
async def create_factory_order(req: FactoryOrderRequest):
    """Đặt hàng 1 cú bấm: lấy chủ đề tự động rồi tạo N video qua hàng đợi
    batch (worker có sẵn tự chạy, không tạo worker mới)."""
    fallback = False
    if req.topics is not None:
        topics = req.topics
    else:
        fetched = await asyncio.to_thread(auto_factory.fetch_topics, req.source)
        topics = fetched["topics"][: req.count]
        fallback = fetched["fallback"]
    if not topics:
        return JSONResponse({"error": "Không lấy được chủ đề nào để sản xuất"}, status_code=422)
    created = await asyncio.to_thread(
        BATCH_MANAGER.add_batch,
        prompts=topics,
        aspect_ratio=req.aspect_ratio,
        scenes_count=req.scenes_count,
        visual_style=req.visual_style,
        voice=req.voice,
        mode=req.mode,
        auto_run=True,
        **({"extra":{"render_mode":"ai_video"}} if req.render_mode=="ai_video" else {}),
    )
    for item in created:
        BATCH_MANAGER.update_video(item["id"], {"factory_source": req.source})
    return {
        "message": f"Đã đặt {len(created)} video vào xưởng (nguồn: {req.source})",
        "source": req.source,
        "fallback": fallback,
        "requested": req.count,
        "created": created,
        "total": len(created),
        "estimated_seconds": auto_factory.estimate_seconds(len(created), req.scenes_count),
    }


@app.get("/api/factory/publish-pack/{video_id}")
async def get_factory_publish_pack(video_id: str):
    """Tải gói đăng bài ZIP của 1 video đã hoàn tất (video + thumbnail +
    tiêu đề/mô tả/hashtag + phụ đề + metadata)."""
    try:
        zip_path = await asyncio.to_thread(auto_factory.build_publish_pack, video_id)
    except FileNotFoundError:
        return JSONResponse({"error": "Không tìm thấy video"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return FileResponse(str(zip_path), media_type="application/zip",
                        filename=f"publish-pack-{video_id}.zip")


@app.get("/api/factory/thumbnail/{video_id}")
async def get_factory_thumbnail(video_id: str):
    """Ảnh thumbnail của video (trích từ final.mp4, có cache trong thư mục video)."""
    thumb = await asyncio.to_thread(auto_factory.get_thumbnail, video_id)
    if not thumb:
        return JSONResponse({"error": "Chưa có thumbnail cho video này"}, status_code=404)
    return FileResponse(str(thumb), media_type="image/jpeg")




# ══════════════════════════════════════════════════════════════════════════
# TRUYỆN THÀNH VIDEO (Story-to-Video) — truyện dài → video từng chương
# (xem story_engine.py: tách chương, nguồn truyện, xào nấu deterministic;
# video chạy qua BatchVideoManager có sẵn, scenes inject sẵn, worker tự chạy)
# ══════════════════════════════════════════════════════════════════════════

class StoryPasteRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    author: str = Field(default="Không rõ", max_length=100)
    text: str = Field(..., min_length=50)
    chars_per_chapter: int = Field(default=6000, ge=1000, le=20000)
    style: str = Field(default="cinematic", max_length=50)

    @field_validator("style")
    @classmethod
    def validate_style(cls, v: str) -> str:
        if v not in BATCH_VISUAL_STYLES:
            raise ValueError(f"Phong cách '{v}' không hợp lệ")
        return v

    @model_validator(mode="after")
    def validate_text_length(self):
        if len(self.text) > story_engine.MAX_STORY_CHARS:
            raise ValueError(
                f"Nội dung vượt giới hạn {story_engine.MAX_STORY_CHARS:,} ký tự "
                f"(đang có {len(self.text):,} ký tự)")
        return self


class StoryImportUrlRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)
    title: str = Field(default="", max_length=200)
    chars_per_chapter: int = Field(default=6000, ge=1000, le=20000)
    style: str = Field(default="cinematic", max_length=50)

    @field_validator("style")
    @classmethod
    def validate_style(cls, v: str) -> str:
        if v not in BATCH_VISUAL_STYLES:
            raise ValueError(f"Phong cách '{v}' không hợp lệ")
        return v


class StoryAdaptRequest(BaseModel):
    mode: str = Field(default="short", max_length=20)
    chapter_index: Optional[int] = Field(default=None, ge=0)
    scenes_count: int = Field(default=5, ge=3, le=8)
    visual_style: str = Field(default="cinematic", max_length=50)
    aspect_ratio: str = Field(default="9:16", max_length=10)
    extra_req: str = Field(default="", max_length=500)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in story_engine.ADAPT_MODES:
            raise ValueError(f"Chế độ xào nấu '{v}' không hợp lệ. Cho phép: {sorted(story_engine.ADAPT_MODES)}")
        return v

    @field_validator("visual_style")
    @classmethod
    def validate_style(cls, v: str) -> str:
        if v not in BATCH_VISUAL_STYLES:
            raise ValueError(f"Phong cách '{v}' không hợp lệ")
        return v

    @field_validator("aspect_ratio")
    @classmethod
    def validate_aspect_ratio(cls, v: str) -> str:
        if v not in ("9:16", "16:9", "1:1"):
            raise ValueError(f"Tỷ lệ khung hình '{v}' không hợp lệ. Cho phép: 9:16, 16:9, 1:1")
        return v


class StoryCreateVideosRequest(CaptionSettings):
    guided: bool = False
    render_mode: Literal["ai_video", "illustrated"] = "ai_video"
    cast_ids: List[str] = Field(default_factory=list, max_length=30)
    chapter_indexes: List[int] = Field(..., min_length=1, max_length=20)
    visual_style: str = Field(default="cinematic", max_length=50)
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=100)
    aspect_ratio: str = Field(default="9:16", max_length=10)
    scenes_count: int = Field(default=5, ge=3, le=8)
    mode: str = Field(default="short", max_length=20)
    extra_req: str = Field(default="", max_length=500)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in story_engine.ADAPT_MODES:
            raise ValueError(f"Chế độ xào nấu '{v}' không hợp lệ")
        return v

    @field_validator("visual_style")
    @classmethod
    def validate_style(cls, v: str) -> str:
        if v not in BATCH_VISUAL_STYLES:
            raise ValueError(f"Phong cách '{v}' không hợp lệ")
        return v

    @field_validator("voice")
    @classmethod
    def validate_voice(cls, v: str) -> str:
        clean = v.strip()
        if clean in ("ai_voice", "native", "none", "no_dub"):
            raise ValueError(f"Chế độ '{clean}' không tạo file giọng đọc cho video ghép ảnh — hãy chọn giọng đọc TTS cụ thể")
        if not clean or len(clean) < 3 or not re.match(r"^[a-zA-Z0-9_\-:]+$", clean):
            raise ValueError(f"Tên giọng đọc '{v}' không hợp lệ")
        return clean

    @field_validator("aspect_ratio")
    @classmethod
    def validate_aspect_ratio(cls, v: str) -> str:
        if v not in ("9:16", "16:9", "1:1"):
            raise ValueError(f"Tỷ lệ khung hình '{v}' không hợp lệ. Cho phép: 9:16, 16:9, 1:1")
        return v


class StorySaveScriptRequest(BaseModel):
    scenes: List[Dict[str, Any]] = Field(..., min_length=1, max_length=12)
    mode: str = Field(default="short", max_length=20)
    visual_style: str = Field(default="cinematic", max_length=50)
    aspect_ratio: str = Field(default="9:16", max_length=10)

    @field_validator("visual_style")
    @classmethod
    def validate_style(cls, v: str) -> str:
        if v not in BATCH_VISUAL_STYLES:
            raise ValueError(f"Phong cách '{v}' không hợp lệ")
        return v

    @field_validator("aspect_ratio")
    @classmethod
    def validate_aspect_ratio(cls, v: str) -> str:
        if v not in ("9:16", "16:9", "1:1"):
            raise ValueError(f"Tỷ lệ khung hình '{v}' không hợp lệ. Cho phép: 9:16, 16:9, 1:1")
        return v

    @model_validator(mode="after")
    def validate_scenes(self):
        for i, sc in enumerate(self.scenes):
            vo = str(sc.get("voiceover") or "").strip()
            pr = str(sc.get("prompt") or "").strip()
            if not vo or not pr:
                raise ValueError(f"Cảnh {i + 1} thiếu lời thoại hoặc prompt hình ảnh")
            dur = sc.get("duration", 6)
            if not isinstance(dur, (int, float)) or not 3 <= dur <= 15:
                raise ValueError(f"Cảnh {i + 1} có thời lượng không hợp lệ (3–15 giây)")
        return self


def _story_project_or_none(project_id: str):
    return story_engine.load_project(project_id)


@app.get("/api/story/library")
async def get_story_library():
    """Danh sách truyện trong kho có sẵn (bản kể lại ngắn, public domain)."""
    return {"stories": story_engine.list_library()}


@app.get("/api/story/library/{story_id}")
async def get_story_library_item(story_id: str):
    """Toàn bộ 1 truyện trong kho (kèm các chương)."""
    story = story_engine.get_library_story(story_id)
    if not story:
        return JSONResponse({"error": f"Không tìm thấy truyện '{story_id}' trong kho"}, status_code=404)
    return story


@app.get("/api/story/adapt-modes")
async def get_story_adapt_modes():
    """5 chế độ xào nấu (chuyển thể deterministic, không dùng LLM ngoài)."""
    return {"modes": [
        {"id": k, "name": v["name"], "desc": v["desc"]}
        for k, v in story_engine.ADAPT_MODES.items()
    ]}


@app.get("/api/story/search")
async def search_story_web(q: str = ""):
    """Tìm truyện: kho có sẵn trước, rồi Wikipedia + Wikisource tiếng Việt."""
    if not q.strip():
        return JSONResponse({"error": "Từ khóa tìm kiếm không được để trống"}, status_code=422)
    try:
        return await asyncio.to_thread(story_engine.search_web, q)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)


@app.get("/api/story/fetch-web")
async def fetch_story_web(title: str = "", source: str = "wikipedia"):
    """Lấy toàn văn 1 bài từ Wikipedia/Wikisource/kho rồi tạo project."""
    if not title.strip():
        return JSONResponse({"error": "Tiêu đề không được để trống"}, status_code=422)
    if source not in ("library", "wikipedia", "wikisource"):
        return JSONResponse({"error": "Nguồn web chỉ nhận 'library', 'wikipedia' hoặc 'wikisource'"}, status_code=422)
    try:
        text = await asyncio.to_thread(story_engine.fetch_web_text, title, source)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    author = {"library": "Dân gian", "wikipedia": "Wikipedia tiếng Việt",
              "wikisource": "Wikisource tiếng Việt"}[source]
    try:
        project = await asyncio.to_thread(story_engine.create_project, title.strip(), author, text, "web")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return {"message": f"Đã tạo truyện '{project['title']}' ({project['chapter_count']} chương)", "project": project}


@app.post("/api/story/import-url")
async def import_story_url(req: StoryImportUrlRequest):
    """Nhập truyện từ 1 link: fetch rồi trích văn bản chính, tạo project."""
    try:
        page = await asyncio.to_thread(story_engine._safe_story_url_get, req.url.strip())
        if page.status_code != 200:
            raise ValueError(f"Link trả về HTTP {page.status_code}.")
        pages = story_engine.comic_pages(req.url, page.text)
        if pages:
            from html import unescape

            if MUSE_PROVIDER != "browser" or not MUSE_API_KEY:
                raise ValueError("Chọn Muse Browser trước khi tự động dựng truyện tranh từ ảnh.")
            heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", page.text, re.I | re.S)
            source_title = unescape(re.sub(r"<[^>]+>", "", heading[1])).strip() if heading else ""
            title = req.title.strip() or source_title[:200] or "Chương truyện tranh"
            scenes = [
                {"index": index, "page_url": url, "prompt": "", "voiceover": "", "duration": 8}
                for index, url in enumerate(pages, 1)
            ]
            items = await asyncio.to_thread(
                BATCH_MANAGER.add_batch, [title], scenes_count=len(scenes), visual_style=req.style,
                aspect_ratio="9:16", scripts=[{"scenes": scenes}], auto_run=True,
                extra={
                    "guided": True, "studio_source": "muse_studio", "render_mode": "ai_video",
                    "reference_mode": "approved", "comic": True, "comic_pages": pages,
                    "comic_pages_done": 0,
                    "operation": {"action": "comic", "allow_stills": False, "include_subtitles": True},
                },
            )
            return {"message": f"Đã xếp hàng tách khung và thoại từ {len(pages)} trang truyện.", "video_id": items[0]["id"]}
        text = await asyncio.to_thread(story_engine.extract_text_from_url, req.url, lambda _: page)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    title = req.title.strip()
    if not title:
        host = re.sub(r"^https?://", "", req.url).split("/")[0]
        title = f"Truyện từ {host}"
    try:
        project = await asyncio.to_thread(
            story_engine.create_project,
            title, "Không rõ", text, "url",
            req.style, req.chars_per_chapter,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return {"message": f"Đã tạo truyện '{project['title']}' ({project['chapter_count']} chương)", "project": project}


@app.post("/api/story/upload")
async def upload_story_file(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    author: str = Form(default="Không rõ"),
    style: str = Form(default="cinematic"),
    chars_per_chapter: int = Form(default=6000),
):
    """Tải file truyện lên (TXT/MD/DOCX/EPUB, tối đa 60MB). PDF chưa hỗ trợ."""
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return JSONResponse({"error": "PDF chưa được hỗ trợ — hãy chuyển sang DOCX hoặc TXT trước rồi tải lên (mở PDF bằng Word/Google Docs rồi lưu thành .docx, hoặc copy văn bản rồi dán trực tiếp)."}, status_code=400)
    if suffix not in (".txt", ".md", ".docx", ".epub"):
        return JSONResponse({"error": f"Định dạng '{suffix or 'không rõ'}' chưa được hỗ trợ — chỉ nhận TXT, MD, DOCX, EPUB."}, status_code=422)
    if style not in BATCH_VISUAL_STYLES:
        return JSONResponse({"error": f"Phong cách '{style}' không hợp lệ"}, status_code=422)
    chars_per_chapter = max(1000, min(20000, int(chars_per_chapter or 6000)))
    try:
        data = await _read_upload_capped(file, max_bytes=story_engine.MAX_UPLOAD_BYTES, label="File truyện")
    except HTTPException as e:
        if e.status_code == 413:
            return JSONResponse({"error": "File truyện vượt quá giới hạn 60MB — hãy chia nhỏ file rồi tải lên từng phần"}, status_code=422)
        raise
    try:
        text = story_engine.parse_text_bytes(data, filename)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    title = title.strip() or Path(filename).stem.replace("_", " ").replace("-", " ").strip() or "Truyện chưa đặt tên"
    try:
        project = story_engine.create_project(
            title, author.strip() or "Không rõ", text, source="upload",
            style=style, chars_per_chapter=chars_per_chapter)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return {"message": f"Đã tạo truyện '{project['title']}' ({project['chapter_count']} chương)", "project": project}


@app.post("/api/story/paste")
async def paste_story_text(req: StoryPasteRequest):
    """Dán văn bản truyện trực tiếp rồi tạo project (tối đa 500.000 ký tự)."""
    try:
        project = story_engine.create_project(
            req.title, req.author, req.text, source="paste",
            style=req.style, chars_per_chapter=req.chars_per_chapter)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return {"message": f"Đã tạo truyện '{project['title']}' ({project['chapter_count']} chương)", "project": project}


@app.post("/api/story/library/{story_id}/create-project")
async def create_story_project_from_library(story_id: str):
    """Tạo project từ 1 truyện trong kho có sẵn (giữ nguyên chương đã chia)."""
    try:
        project = story_engine.create_project_from_library(story_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"message": f"Đã tạo truyện '{project['title']}' ({project['chapter_count']} chương)", "project": project}


@app.get("/api/story/projects")
async def list_story_projects():
    """Danh sách truyện đã nhập (mới nhất trước)."""
    return {"projects": story_engine.list_projects()}


@app.get("/api/story/projects/{project_id}")
async def get_story_project(project_id: str):
    """Toàn bộ 1 project truyện (kèm các chương + kịch bản đã adapt)."""
    project = _story_project_or_none(project_id)
    if not project:
        return JSONResponse({"error": f"Không tìm thấy truyện '{project_id}'"}, status_code=404)
    return project


@app.post("/api/story/projects/{project_id}/adapt")
async def adapt_story_project(project_id: str, req: StoryAdaptRequest):
    """Xào nấu 1 chương (hoặc tất cả) thành kịch bản, lưu vào project để
    sửa trực tiếp ở UI trước khi tạo video."""
    project = _story_project_or_none(project_id)
    if not project:
        return JSONResponse({"error": f"Không tìm thấy truyện '{project_id}'"}, status_code=404)
    chapters = project.get("chapters") or []
    indexes = [req.chapter_index] if req.chapter_index is not None else list(range(len(chapters)))
    if req.chapter_index is not None and (req.chapter_index < 0 or req.chapter_index >= len(chapters)):
        return JSONResponse({"error": f"Chỉ số chương {req.chapter_index} không hợp lệ (truyện có {len(chapters)} chương)"}, status_code=422)
    adapted = []
    try:
        for ci in indexes:
            ch = chapters[ci]
            script = story_engine.adapt_chapter(
                ch["text"], req.mode, req.scenes_count, req.visual_style,
                extra_req=req.extra_req, aspect_ratio=req.aspect_ratio)
            script = {
                **script,
                "scenes_count": len(script["scenes"]),
                "requested_scenes_count": req.scenes_count,
                "visual_style": req.visual_style,
                "aspect_ratio": req.aspect_ratio,
                "extra_req": (req.extra_req or "").strip(),
                "user_edited": False,
            }
            project.setdefault("scripts", {})[str(ci)] = script
            adapted.append({
                "chapter_index": ci, "chapter_title": ch["title"],
                "mode": req.mode, "scene_count": len(script["scenes"]),
                "scenes": script["scenes"],
            })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    story_engine.save_project(project)
    return {"project_id": project_id, "adapted": adapted}


@app.put("/api/story/projects/{project_id}/scripts/{chapter_index}")
async def save_story_script(project_id: str, chapter_index: int, req: StorySaveScriptRequest):
    """Lưu lại kịch bản 1 chương sau khi sửa trực tiếp ở UI (lời thoại/prompt)."""
    project = _story_project_or_none(project_id)
    if not project:
        return JSONResponse({"error": f"Không tìm thấy truyện '{project_id}'"}, status_code=404)
    chapters = project.get("chapters") or []
    if chapter_index < 0 or chapter_index >= len(chapters):
        return JSONResponse({"error": f"Chỉ số chương {chapter_index} không hợp lệ (truyện có {len(chapters)} chương)"}, status_code=422)
    scenes = []
    for i, sc in enumerate(req.scenes):
        scenes.append({
            "index": i + 1,
            "role": sc.get("role") or ("hook" if i == 0 else "ending" if i == len(req.scenes) - 1 else "buildup"),
            "prompt": str(sc.get("prompt") or "").strip(),
            "voiceover": str(sc.get("voiceover") or "").strip(),
            "duration": sc.get("duration", 6),
        })
    project.setdefault("scripts", {})[str(chapter_index)] = {
        "mode": req.mode,
        "scenes": scenes,
        "scene_count": len(scenes),
        "scenes_count": len(scenes),
        "requested_scenes_count": len(scenes),
        "visual_style": req.visual_style,
        "aspect_ratio": req.aspect_ratio,
        "user_edited": True,
    }
    story_engine.save_project(project)
    return {"message": f"Đã lưu kịch bản chương {chapter_index + 1}", "project_id": project_id,
            "chapter_index": chapter_index, "scene_count": len(scenes)}


def _selected_cast(ids):
    selected = set(ids)
    cast = [c for c in load_characters() if c["id"] in selected] if selected else []
    if len(cast) != len(selected):
        raise ValueError("Hồ sơ nhân vật đã thay đổi. Hãy chọn lại diễn viên.")
    return cast


class StudioBeat(BaseModel):
    title: str = Field(default="", max_length=200)
    setting: str = Field(default="", max_length=500)
    prompt: str = Field(min_length=1, max_length=3000)
    voiceover: str = Field(min_length=1, max_length=3000)
    dialogue: str = Field(default="", max_length=3000)
    characters: List[str] = Field(default_factory=list, max_length=30)
    new_segment: bool = False
    transition_out: Literal["dissolve", "cut", "match cut"] = "dissolve"
    duration: Literal[5, 6, 8, 10, 12] = 6


class MuseStudioDraft(CaptionSettings):
    guided: bool = False
    title: str = Field(min_length=1, max_length=200)
    scenes: List[StudioBeat] = Field(min_length=1, max_length=40)
    cast_ids: List[str] = Field(default_factory=list, max_length=30)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    visual_style: str = Field(default="cinematic", max_length=50)
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=100)


def _studio_draft_settings(req: MuseStudioDraft):
    if not req.title.strip() or req.visual_style not in BATCH_VISUAL_STYLES:
        raise HTTPException(422, "Tên phim hoặc phong cách chưa hợp lệ.")
    try:
        cast = _selected_cast(req.cast_ids)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    suffix = BATCH_VISUAL_STYLES[req.visual_style]["prompt_suffix"]
    scenes = []
    for index, scene in enumerate(req.scenes, 1):
        if not scene.prompt.strip() or not scene.voiceover.strip():
            raise HTTPException(422, f"Cảnh {index} thiếu mô tả hoặc lời kể.")
        scenes.append({**scene.model_dump(), "index": index, "direction": scene.prompt.strip(),
                       "prompt": f"{scene.prompt.strip()}. {suffix}"})
    return scenes, {"guided": req.guided, "render_mode": "ai_video", "studio_source": "muse_studio", "cast": cast,
                    "subtitle_style": req.subtitle_style, "subtitle_position": req.subtitle_position}


@app.post("/api/muse-studio/drafts")
def create_muse_studio_draft(req: MuseStudioDraft):
    scenes, settings = _studio_draft_settings(req)
    items = BATCH_MANAGER.add_batch([req.title], scenes_count=len(scenes), voice=req.voice,
        aspect_ratio=req.aspect_ratio, visual_style=req.visual_style, auto_run=False,
        scripts=[{"title": req.title, "scenes": scenes}], extra=settings)
    return {"id": items[0]["id"]}


@app.put("/api/muse-studio/drafts/{video_id}")
def edit_muse_studio_draft(video_id: str, req: MuseStudioDraft):
    scenes, settings = _studio_draft_settings(req)
    if not BATCH_MANAGER.edit_studio_draft(video_id, {
        **settings, "title": req.title, "prompt": req.title, "scenes": scenes, "scenes_count": len(scenes),
        "voice": req.voice, "aspect_ratio": req.aspect_ratio, "visual_style": req.visual_style,
    }):
        raise HTTPException(409, "Bản dựng đã vào hàng đợi hoặc không còn tồn tại.")
    return {"id": video_id}


class SceneReview(BaseModel):
    approved: bool


@app.put("/api/muse-studio/{video_id}/scenes/{index}/review")
def review_studio_scene(video_id: str, index: int, req: SceneReview):
    if not BATCH_MANAGER.set_scene_approval(video_id, index, req.approved):
        raise HTTPException(409, "Chỉ duyệt được cảnh trong bản dựng đã hoàn tất.")
    return {"approved": req.approved}


@app.get("/api/muse-studio/{video_id}/scenes/{index}/clip")
def muse_studio_scene_clip(video_id: str, index: int):
    from batch_video_engine import is_safe_video_id
    if not is_safe_video_id(video_id) or not 1 <= index <= 200:
        raise HTTPException(404, "Cảnh không tồn tại.")
    folder = (BATCH_DIR / video_id).resolve()
    path = (folder / "clips" / f"clip_{index}.mp4").resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise HTTPException(404, "Clip chưa sẵn sàng.")
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/story/projects/{project_id}/create-videos")
async def create_story_project_videos(project_id: str, req: StoryCreateVideosRequest):
    """Tạo 1 video cho mỗi chương đã chọn (scenes inject sẵn, worker tự chạy)."""
    try:
        cast = _selected_cast(req.cast_ids)
        if req.guided:
            cast.extend(studio_routes.library(project_id))
        created = story_engine.create_story_videos(
            project_id, req.chapter_indexes, req.visual_style, req.voice,
            req.aspect_ratio, req.scenes_count, mode=req.mode,
            extra_req=req.extra_req, render_mode=req.render_mode, cast=cast, guided=req.guided and req.render_mode == "ai_video",
            subtitle_style=req.subtitle_style, subtitle_position=req.subtitle_position)
    except FileNotFoundError:
        return JSONResponse({"error": f"Không tìm thấy truyện '{project_id}'"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return {
        "message": f"Đã tạo {len(created)} bản dựng từ truyện",
        "project_id": project_id,
        "created": created,
        "total": len(created),
    }


import studio_routes

app.include_router(studio_routes.router)


@app.post("/api/muse-studio/workshop")
def studio_workshop(req: studio_routes.Workshop):
    try:
        cast = _selected_cast(req.cast_ids)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return studio_routes.create_workshop(req, cast)


@app.post("/api/muse-studio/assistant")
def studio_assistant(req: studio_routes.AssistantBrief):
    try:
        cast = _selected_cast(req.cast_ids)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return studio_routes.create_workshop(req, cast, count=req.count)


def _studio_bind_host() -> str:
    host = os.getenv("STUDIO_HOST", "127.0.0.1")
    try:
        is_local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_local = host.lower() == "localhost"
    if not is_local and not os.getenv("STUDIO_API_KEY"):
        raise SystemExit("Cần cấu hình STUDIO_API_KEY khi mở Studio ngoài loopback.")
    return host


if __name__ == "__main__":

    import sys
    import uvicorn
    sys.stdout.reconfigure(encoding="utf-8")
    port = int(os.getenv("PORT", "8000"))
    host = _studio_bind_host()
    uvicorn.run(app, host=host, port=port, reload=False)
