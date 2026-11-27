"""auto_factory.py — Xưởng tự động (Auto Factory)
-------------------------------------------------
Một cú bấm ra nhiều video hoàn chỉnh:
- Lấy chủ đề tự động từ RSS công khai (Google Trends VN / VnExpress),
  không cần key mới; RSS lỗi thì fallback về pool ý tưởng có sẵn.
- Đặt hàng 1 cú bấm: tạo N video qua BatchVideoManager có sẵn
  (worker có sẵn tự chạy, không tạo worker/pipeline dựng video mới).
- Gói đăng bài (publish pack): ZIP chứa video + thumbnail + tiêu đề /
  mô tả / hashtag + phụ đề + metadata cho từng video hoàn chỉnh.

Không upload YouTube thật (cần OAuth, ngoài phạm vi): nút "Đăng bài"
ở UI mở gói đăng bài để sao chép tiêu đề/mô tả và tải ZIP.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
import subprocess
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from batch_video_engine import (
    BATCH_DIR,
    BATCH_MANAGER,
    BATCH_PROMPT_TEMPLATES,
    BATCH_VISUAL_STYLES,
)
from composer import _resolve_ffmpeg, get_media_duration

logger = logging.getLogger("ai_movie_studio.auto_factory")

# ── 1. Nguồn nội dung ─────────────────────────────────────────────────────
TRENDING_RSS_URL = "https://trends.google.com/trending/rss?geo=VN"
TRENDING_RSS_URLS = [
    "https://trends.google.com/trending/rss?geo=VN",
    "https://trends.google.com/trends/trendingsearches/daily/rss?geo=VN",
]
NEWS_RSS_URLS = [
    "https://vnexpress.net/rss/tin-moi-nhat.rss",
    "https://tuoitre.vn/home.rss",
    "https://tuoitre.vn/rss/tin-moi-nhat.rss",  # fallback khi VnExpress lỗi
]

FACTORY_CACHE_DIR = Path(__file__).resolve().parent / "workspace" / "_factory_cache"
FACTORY_CACHE_TTL_S = 30 * 60  # cache chủ đề 30 phút
FACTORY_TOPICS_LIMIT = 100
FACTORY_RSS_TIMEOUT_S = 15

# 11 phong cách hình ảnh của Xưởng (thứ tự hiển thị ở UI). Hậu tố prompt
# thật nằm trong BATCH_VISUAL_STYLES (batch_video_engine.py) — mọi id ở
# đây BẮT BUỘC phải có trong đó, nếu không validator của order sẽ 422.
FACTORY_STYLE_IDS = [
    "cinematic", "donghua", "anime", "horror_dark", "comic", "3d_pixar",
    "ink_wash", "cyberpunk", "photoreal", "watercolor", "oil_painting",
]

# Thời gian ước tính cho mỗi cảnh (giây) — chỉ để hiển thị ước tính, ghi rõ
# là ước tính ở UI (render + TTS + vẽ ảnh thực tế phụ thuộc tải của AI).
ESTIMATE_SECONDS_PER_SCENE = 25


def factory_styles() -> List[Dict[str, str]]:
    """Danh sách 11 phong cách của Xưởng theo đúng thứ tự hiển thị."""
    out = []
    for style_id in FACTORY_STYLE_IDS:
        info = BATCH_VISUAL_STYLES.get(style_id)
        if info:
            out.append({
                "id": info["id"],
                "name": info["name"],
                "prompt_suffix": info["prompt_suffix"],
            })
    return out


def estimate_seconds(count: int, scenes_count: int) -> int:
    """Ước tính thời gian sản xuất (giây) — videos × scenes × ~25s."""
    return max(0, int(count) * int(scenes_count) * ESTIMATE_SECONDS_PER_SCENE)


# ── 2. Lấy chủ đề từ RSS (có cache + fallback) ─────────────────────────────

def _clean_topic_title(raw: str) -> str:
    """Làm sạch tiêu đề RSS thành chủ đề video: gỡ tag HTML, giải entity,
    gọn khoảng trắng, cắt tối đa 120 ký tự."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # Bỏ ký tự điều khiển (xuống dòng/tab còn sót, ký tự không in được)
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120].strip()


def _parse_rss_titles(xml_text: str, limit: int = FACTORY_TOPICS_LIMIT) -> List[str]:
    """Parse XML RSS, lấy tiêu đề các <item> (giữ thứ tự, khử trùng)."""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Không parse được RSS: %s", e)
        return []
    titles: List[str] = []
    seen = set()
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is None:
            continue
        title = _clean_topic_title("".join(title_el.itertext()))
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def _fallback_topics(limit: int = FACTORY_TOPICS_LIMIT) -> List[str]:
    """Pool ý tưởng dự phòng khi RSS lỗi: các chủ đề mẫu có sẵn trong
    batch_video_engine (BATCH_PROMPT_TEMPLATES)."""
    pool: List[str] = []
    for tpl in BATCH_PROMPT_TEMPLATES:
        for item in tpl.get("items", []):
            title = _clean_topic_title(str(item))
            if title and title not in pool:
                pool.append(title)
    return pool[:limit]


def _cache_path(source: str) -> Path:
    # Audit #5f (2026-11-25): loc ten file (defense-in-depth) — fetch_topics
    # da allowlist chi nhan "trending"/"news", nhung ham nay van khong duoc
    # de "../" thoat ra khoi FACTORY_CACHE_DIR.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(source or ""))
    if not safe:
        safe = "default"
    FACTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return FACTORY_CACHE_DIR / f"{safe}_catalog.json"


def _read_cache(source: str) -> Optional[List[str]]:
    """Đọc cache nếu còn hạn (TTL 30 phút); hết hạn/hỏng thì trả None."""
    path = _cache_path(source)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(data.get("fetched_at", 0))
        topics = data.get("topics") or []
        if time.time() - fetched_at > FACTORY_CACHE_TTL_S:
            return None
        if not isinstance(topics, list) or not topics:
            return None
        return [str(t) for t in topics]
    except Exception as e:
        logger.warning("Không đọc được cache chủ đề %s: %s", source, e)
        return None


def _write_cache(source: str, topics: List[str]) -> None:
    try:
        _cache_path(source).write_text(
            json.dumps({"fetched_at": time.time(), "topics": topics},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Không ghi được cache chủ đề %s: %s", source, e)


def _default_http_get(url: str):
    """GET một URL RSS bằng httpx (hàm riêng để test có thể mock)."""
    return httpx.get(
        url,
        timeout=FACTORY_RSS_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (AI Movie Studio Auto Factory)"},
    )


def fetch_topics(
    source: str,
    http_get: Optional[Callable[[str], Any]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Lấy danh sách chủ đề cho 1 nguồn.

    Trả về {"source", "topics", "fallback", "cached"}:
    - Ưu tiên cache còn hạn (cached=True, không gọi mạng) trừ khi
      use_cache=False (nút "Làm mới" ở UI).
    - RSS lỗi/rỗng thì fallback về pool ý tưởng có sẵn (fallback=True).
    - Không bao giờ raise vì lỗi mạng — luôn trả ít nhất pool dự phòng.
    """
    if source not in ("trending", "news"):
        raise ValueError(f"Nguồn nội dung '{source}' không hợp lệ (chỉ nhận 'trending' hoặc 'news')")

    if use_cache:
        cached = _read_cache(source)
        if cached:
            return {"source": source, "topics": cached, "fallback": False, "cached": True}

    get = http_get or _default_http_get
    urls = TRENDING_RSS_URLS if source == "trending" else NEWS_RSS_URLS
    def read_feed(url):
        try:
            res = get(url)
            if res.status_code == 200:
                return _parse_rss_titles(res.text)
            logger.warning("RSS %s trả status %s", url, res.status_code)
        except Exception as error:
            logger.warning("Không lấy được RSS %s: %s", url, error)
        return []

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        topics_by_title = {}
        for titles in pool.map(read_feed, urls):
            for title in titles:
                topics_by_title.setdefault(title.casefold(), title)
    if topics_by_title:
        topics = list(topics_by_title.values())[:FACTORY_TOPICS_LIMIT]
        _write_cache(source, topics)
        return {"source": source, "topics": topics, "fallback": False, "cached": False}

    topics = _fallback_topics()
    return {"source": source, "topics": topics, "fallback": True, "cached": False}


# ── 3. Gói đăng bài (publish pack) ──────────────────────────────────────────

def _video_dir(video_id: str) -> Path:
    """Thư mục của 1 batch video, có chặn path traversal."""
    video_path = (BATCH_DIR / video_id).resolve()
    if not video_path.is_relative_to(BATCH_DIR.resolve()):
        raise ValueError(f"ID video '{video_id}' không hợp lệ")
    return video_path


def _thumbnail_size(aspect_ratio: str) -> tuple:
    if aspect_ratio == "16:9":
        return (1280, 720)
    if aspect_ratio == "1:1":
        return (720, 720)
    return (720, 1280)  # 9:16 mặc định


def _extract_thumbnail(video_path: Path, out_path: Path, aspect_ratio: str) -> bool:
    """Trích 1 frame (giây 0.5) làm thumbnail, scale + pad về đúng khổ."""
    w, h = _thumbnail_size(aspect_ratio)
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    cmd = [
        _resolve_ffmpeg(), "-y", "-ss", "0.5", "-i", str(video_path),
        "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out_path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60)
        return res.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000
    except Exception as e:
        logger.warning("Không trích được thumbnail từ %s: %s", video_path, e)
        return False


def _topic_hashtags(title: str) -> List[str]:
    """Hashtag tự sinh từ tiêu đề: lấy từ dài có nghĩa, bỏ stopword."""
    stop = {"của", "và", "là", "cho", "với", "trong", "sau", "khi", "đã",
            "một", "các", "những", "đến", "từ", "về", "này", "kia", "ra",
            "the", "and", "for", "with"}
    tags: List[str] = []
    for word in re.findall(r"[A-Za-zÀ-ỹ0-9]+", title.lower()):
        # Audit #5e (2026-11-25): tags chua "#word" nen phai kiem
        # f"#{word}" — truoc day `word not in tags` luon True, tag lap lai.
        if len(word) >= 3 and word not in stop and f"#{word}" not in tags:
            tags.append(f"#{word}")
        if len(tags) >= 5:
            break
    return tags


def build_publish_pack(video_id: str, force: bool = False) -> Path:
    """Tạo gói đăng bài ZIP cho 1 video ĐÃ hoàn tất.

    ZIP gồm: final.mp4, thumbnail.jpg, title.txt, description.txt,
    subtitles.srt (nếu có), metadata.json.
    - Không tìm thấy video -> FileNotFoundError (endpoint map 404).
    - Video chưa completed -> ValueError (endpoint map 409).
    - ZIP đã có và còn mới hơn final.mp4 thì dùng lại (trừ khi force=True).
    """
    v = BATCH_MANAGER.get_video(video_id)
    if not v:
        raise FileNotFoundError(f"Không tìm thấy video '{video_id}'")
    if v.get("status") != "completed":
        raise ValueError(
            f"Video chưa hoàn tất (trạng thái hiện tại: {v.get('status')}) — "
            "chỉ tạo được gói đăng bài khi video đã xong"
        )

    v_dir = _video_dir(video_id)
    final_mp4 = v_dir / "final.mp4"
    if not final_mp4.exists():
        raise RuntimeError(f"Không tìm thấy file video hoàn chỉnh của '{video_id}'")

    zip_path = v_dir / "publish_pack.zip"
    if (not force and zip_path.exists()
            and zip_path.stat().st_mtime >= final_mp4.stat().st_mtime):
        return zip_path

    # Thumbnail: trích từ video; nếu lỗi thì thử clip cảnh đầu tiên.
    thumb_path = v_dir / "thumbnail.jpg"
    aspect = v.get("aspect_ratio") or "9:16"
    if not _extract_thumbnail(final_mp4, thumb_path, aspect):
        clips_dir = v_dir / "clips"
        first_clip = None
        if clips_dir.exists():
            clips = sorted(clips_dir.glob("*.mp4"))
            first_clip = clips[0] if clips else None
        if first_clip is None or not _extract_thumbnail(first_clip, thumb_path, aspect):
            raise RuntimeError(f"Không tạo được thumbnail cho video '{video_id}'")

    bundle = v.get("bundle") or {}
    title = v.get("title") or ""
    viral_title = bundle.get("title") or title
    caption = bundle.get("caption") or ""
    hashtags = bundle.get("hashtags") or (
        ["#fyp", "#xuhuong", "#viral"] + _topic_hashtags(title)
    )
    description = "\n\n".join(
        part for part in [viral_title, caption, " ".join(hashtags)] if part
    )

    duration = 0.0
    try:
        duration = round(get_media_duration(str(final_mp4)), 1)
    except Exception:
        pass
    srt_path = v_dir / "subtitles.srt"

    metadata = {
        "video_id": video_id,
        "title": title,
        "viral_title": viral_title,
        "duration_s": duration,
        "scenes": v.get("scenes_count"),
        "aspect_ratio": aspect,
        "visual_style": v.get("visual_style"),
        "voice": v.get("voice"),
        "source": v.get("factory_source"),
        "created_at": v.get("created_at"),
    }

    tmp_zip = v_dir / "publish_pack.zip.tmp"
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(final_mp4, arcname="final.mp4")
        zf.write(thumb_path, arcname="thumbnail.jpg")
        zf.writestr("title.txt", viral_title)
        zf.writestr("description.txt", description)
        if srt_path.exists():
            zf.write(srt_path, arcname="subtitles.srt")
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    tmp_zip.replace(zip_path)
    return zip_path


def get_thumbnail(video_id: str) -> Optional[Path]:
    """Lấy thumbnail.jpg của video (dùng cache sẵn, nếu chưa có thì trích
    từ final.mp4; chưa có video hoàn chỉnh thì trả None)."""
    v = BATCH_MANAGER.get_video(video_id)
    if not v:
        return None
    v_dir = _video_dir(video_id)
    thumb_path = v_dir / "thumbnail.jpg"
    if thumb_path.exists() and thumb_path.stat().st_size > 1000:
        return thumb_path
    final_mp4 = v_dir / "final.mp4"
    if final_mp4.exists():
        if _extract_thumbnail(final_mp4, thumb_path, v.get("aspect_ratio") or "9:16"):
            return thumb_path
    return None
