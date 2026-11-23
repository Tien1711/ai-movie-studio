"""story_engine.py — Truyện thành video (Story-to-Video)
------------------------------------------------------
Biến một truyện dài thành nhiều video ngắn (mỗi chương = 1 tập):
- Tách chương theo tiêu đề (Chương/Hồi/Phần + số) hoặc cắt theo độ dài.
- Nguồn truyện: kho tác phẩm có sẵn (bản kể lại ngắn, public domain),
  tìm trên web (Wikipedia/Wikisource tiếng Việt), nhập từ link,
  tải file lên (TXT/MD/DOCX/EPUB), dán văn bản.
- Xào nấu AI — KHÔNG dùng LLM (repo không có text LLM): chuyển thể
  deterministic theo 5 chế độ (ngắn 60s / ma mị / hồi hộp / triết lý /
  tóm tắt). Voiceover lấy từ câu văn gốc đã gọn lại, không bịa tình tiết.
- Tạo video: inject scenes đã build sẵn (voiceover + prompt) vào hàng
  đợi batch qua BATCH_MANAGER — worker có sẵn chạy nốt (TTS + ảnh +
  dựng). Không viết pipeline dựng video mới.
"""

from __future__ import annotations

import html as _html
import io
import json
import logging
import re
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from batch_video_engine import BATCH_MANAGER, BATCH_VISUAL_STYLES

logger = logging.getLogger("ai_movie_studio.story_engine")

# ── Hằng số ───────────────────────────────────────────────────────────────
STORY_LIBRARY_DIR = Path(__file__).resolve().parent / "story_library"
STORY_PROJECTS_DIR = Path(__file__).resolve().parent / "workspace" / "_story_projects"
MAX_STORY_CHARS = 500_000          # giới hạn text paste/import (ký tự)
MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # giới hạn file tải lên
MAX_URL_BYTES = 2 * 1024 * 1024      # giới hạn nội dung lấy từ link
DEFAULT_CHARS_PER_CHAPTER = 6000
MIN_CHAPTER_CHARS = 50             # chương ngắn hơn thì bỏ (rỗng/lạc)
RSS_TIMEOUT_S = 15

WIKIPEDIA_API = "https://vi.wikipedia.org/w/api.php"
WIKISOURCE_API = "https://vi.wikisource.org/w/api.php"

# 5 chế độ xào nấu (chuyển thể deterministic, không LLM).
ADAPT_MODES: Dict[str, Dict[str, str]] = {
    "short": {
        "name": "Kịch bản Video Ngắn 60s",
        "desc": "Mở đầu giật gân, beats ngắn, kết cliffhanger giữ người xem.",
    },
    "horror": {
        "name": "Phong cách ma mị kiểu Nguyễn Ngọc Ngạn",
        "desc": "Mở đầu u ám, nhịp kể chậm, kết lạnh sống lưng.",
    },
    "suspense": {
        "name": "Hồi hộp & Gay cấn",
        "desc": "Nhấn cao trào, đánh dấu cú lật, nhịp dồn dập.",
    },
    "podcast": {
        "name": "Triết lý & Chữa lành",
        "desc": "Kể chậm, kết chiêm nghiệm với thông điệp nhân văn.",
    },
    "summary": {
        "name": "Tóm tắt nhanh",
        "desc": "Rút gọn giữ diễn biến chính và thông điệp cốt lõi.",
    },
}

# Từ khóa Việt → Anh để dựng prompt ảnh (không LLM, chỉ tra cứu).
_VN_EN_KEYWORDS: Dict[str, str] = {
    "ma": "ghost", "quỷ": "demon", "đêm": "night", "rừng": "forest",
    "sông": "river", "biển": "sea", "núi": "mountain", "làng": "village",
    "vua": "king", "công chúa": "princess", "hoàng hậu": "queen",
    "cung điện": "palace", "chùa": "temple", "đền": "shrine",
    "trăng": "moon", "lửa": "fire", "mưa": "rain", "bão": "storm",
    "cây": "ancient tree", "nhà": "old house", "chợ": "market",
    "ruộng": "rice field", "cầu": "bridge", "giếng": "old well",
    "đàn": "musical instrument", "kiếm": "sword", "ngựa": "horse",
    "chim": "bird", "cá": "fish", "khế": "starfruit tree",
    "vàng": "gold", "thị": "persimmon fruit", "giếng": "well",
    "đồng": "countryside", "thành": "ancient citadel",
    "trẻ": "child", "già": "old man", "cô gái": "young woman",
    "chàng trai": "young man", "bà lão": "old woman",
    "sương": "mist", "mây": "clouds", "hoàng hôn": "sunset",
    "bình minh": "sunrise", "nước": "water", "đường": "road",
    "đá": "stone", "hang": "cave", "rồng": "dragon",
}

# Character Block chuẩn cho chim thần — dán NGUYÊN VĂN vào mọi prompt ảnh có chim
# (fix 2026-11-23: video Ăn Khế Trả Vàng ch.1 vẽ lúc phượng hoàng vàng, lúc diệc
# xám vì prompt chỉ có từ khóa "bird" chung chung).
MAGIC_BIRD_BLOCK = (
    "a majestic golden phoenix with radiant golden feathers, "
    "long flowing elegant tail feathers, warm divine glow"
)

# Ràng buộc chống ảnh AI tự bịa thêm người/vật lạ (best-effort: mô hình ảnh vẫn có
# thể bỏ qua). Diễn đạt mềm — chỉ cấm "bịa thêm", không cấm tuyệt đối "người" —
# để không xóa nhân vật hợp lệ như đám đông trong làng.
_NO_EXTRA_SUBJECTS = (
    "only the characters, animals and objects mentioned in the scene, "
    "no invented extra people or creatures"
)

_BIRD_WORDS = ("chim", "phượng")


def _clean_text(raw: str) -> str:
    """Gọn khoảng trắng, giải entity HTML, bỏ ký tự điều khiển."""
    if not raw:
        return ""
    text = _html.unescape(raw)
    text = "".join(ch for ch in text if ch.isprintable() or ch in "\n\t")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── 1. Tách chương ────────────────────────────────────────────────────────

_WORD_NUMS = (
    "mười một|mười hai|mười ba|mười bốn|mười lăm|mười sáu|mười bảy|"
    "mười tám|mười chín|hai mươi|một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười"
)
_CHAPTER_TITLE_RE = re.compile(
    r"^[ \t]*(Chương|Hồi|Phần|Chapter)[ \t]+"
    r"(?P<num>\d{1,3}|[IVXLCDM]{1,5}|" + _WORD_NUMS + r")"
    r"[ \t]*(?:[:.·\-—–][ \t]*(?P<name>.*?))?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _split_by_length(text: str, limit: int) -> List[str]:
    """Cắt text thành đoạn ~limit ký tự, ưu tiên ngắt ở đoạn văn, rồi câu."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for para in paras:
        if len(buf) + len(para) + 2 <= limit:
            buf = f"{buf}\n\n{para}".strip()
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(para) <= limit:
            buf = para
            continue
        # Đoạn quá dài: cắt ở cuối câu, cuối cùng mới cắt cứng.
        rest = para
        while len(rest) > limit:
            cut = rest.rfind(". ", 0, limit)
            if cut < limit // 2:
                cut = rest.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            else:
                cut = cut + 1
            chunks.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        buf = rest
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c) >= MIN_CHAPTER_CHARS or len(chunks) == 1]


def split_chapters(text: str, chars_per_chapter: int = DEFAULT_CHARS_PER_CHAPTER) -> List[Dict[str, Any]]:
    """Tách truyện thành danh sách chương {title, text, word_count}.

    - Có tiêu đề Chương/Hồi/Phần rõ ràng → tách theo tiêu đề.
    - Không có tiêu đề → cắt theo độ dài ký tự (mặc định 6000).
    - Chương nào quá dài vẫn bị cắt tiếp theo độ dài; chương rỗng bị bỏ.
    """
    if not text or not text.strip():
        return []
    limit = max(1000, min(20000, int(chars_per_chapter or DEFAULT_CHARS_PER_CHAPTER)))
    text = _clean_text(text)
    matches = list(_CHAPTER_TITLE_RE.finditer(text))
    raw_chapters: List[Dict[str, str]] = []
    if matches:
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            num = m.group("num")
            name = (m.group("name") or "").strip()
            kind = m.group(1)
            kind_vn = {"chapter": "Chương"}.get(kind.lower(), kind)
            title = f"{kind_vn} {num}" if not name else f"{kind_vn} {num}: {name}"
            # Tiêu đề lạc ở cuối truyện (không còn nội dung) thì bỏ.
            if body:
                raw_chapters.append({"title": title, "text": body})
        # Phần mở đầu trước tiêu đề đầu tiên (lời tựa) giữ làm chương riêng.
        head = text[: matches[0].start()].strip()
        if len(head) >= MIN_CHAPTER_CHARS:
            raw_chapters.insert(0, {"title": "Mở đầu", "text": head})
    else:
        raw_chapters = [{"title": f"Phần {i + 1}", "text": c}
                        for i, c in enumerate(_split_by_length(text, limit))]

    chapters: List[Dict[str, Any]] = []
    for ch in raw_chapters:
        parts = _split_by_length(ch["text"], limit) if len(ch["text"]) > limit else [ch["text"]]
        for j, part in enumerate(parts):
            title = ch["title"] if len(parts) == 1 else f"{ch['title']} ({j + 1})"
            if len(part.strip()) < MIN_CHAPTER_CHARS:
                continue
            chapters.append({
                "title": title,
                "text": part.strip(),
                "word_count": len(part.split()),
            })
    return chapters


# ── 2. Kho tác phẩm có sẵn ────────────────────────────────────────────────

def list_library() -> List[Dict[str, Any]]:
    """Danh sách truyện trong kho (metadata + word_count/chapter_count)."""
    stories: List[Dict[str, Any]] = []
    if not STORY_LIBRARY_DIR.exists():
        return stories
    for path in sorted(STORY_LIBRARY_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Không đọc được kho truyện %s: %s", path, e)
            continue
        chapters = data.get("chapters") or []
        stories.append({
            "id": data.get("id") or path.stem,
            "title": data.get("title", ""),
            "author": data.get("author", "Dân gian"),
            "category": data.get("category", ""),
            "style": data.get("style", "cinematic"),
            "summary": data.get("summary", ""),
            "word_count": sum(len((c.get("text") or "").split()) for c in chapters),
            "chapter_count": len(chapters),
        })
    return stories


def get_library_story(story_id: str) -> Optional[Dict[str, Any]]:
    """Toàn bộ 1 truyện trong kho (kèm chapters). story_id chỉ nhận [a-z0-9_-]."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", story_id or ""):
        return None
    path = STORY_LIBRARY_DIR / f"{story_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Không đọc được kho truyện %s: %s", path, e)
        return None
    chapters = data.get("chapters") or []
    data["word_count"] = sum(len((c.get("text") or "").split()) for c in chapters)
    data["chapter_count"] = len(chapters)
    return data


# ── 3. Tìm trên web (Wikipedia / Wikisource tiếng Việt, không key) ────────

def _default_http_get(url: str, **kwargs):
    """GET bằng httpx (hàm riêng để test có thể mock)."""
    return httpx.get(
        url,
        timeout=kwargs.pop("timeout", RSS_TIMEOUT_S),
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (AI Movie Studio Story-to-Video)"},
        **kwargs,
    )


def _wiki_search(api_url: str, query: str, http_get) -> List[Dict[str, Any]]:
    try:
        res = http_get(api_url, params={
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": 5, "format": "json",
        })
        items = (res.json().get("query") or {}).get("search") or []
    except Exception as e:
        logger.warning("Tìm trên %s lỗi: %s", api_url, e)
        return []
    out = []
    for item in items:
        snippet = _clean_text(re.sub(r"<[^>]+>", " ", item.get("snippet") or ""))
        out.append({"title": item.get("title", ""), "snippet": snippet[:200]})
    return out


def search_web(query: str, http_get: Optional[Callable] = None) -> Dict[str, Any]:
    """Tìm truyện: kho có sẵn trước, rồi Wikipedia + Wikisource tiếng Việt."""
    q = (query or "").strip()
    if not q:
        raise ValueError("Từ khóa tìm kiếm không được để trống")
    get = http_get or _default_http_get
    results: List[Dict[str, Any]] = []
    q_low = q.lower()
    for story in list_library():
        if q_low in story["title"].lower() or q_low in story.get("summary", "").lower():
            results.append({
                "title": story["title"], "source": "library",
                "story_id": story["id"], "snippet": story["summary"],
            })
    for source, api_url in (("wikipedia", WIKIPEDIA_API), ("wikisource", WIKISOURCE_API)):
        for item in _wiki_search(api_url, q, get):
            item["source"] = source
            results.append(item)
    return {"query": q, "results": results}


def fetch_web_text(title: str, source: str, http_get: Optional[Callable] = None) -> str:
    """Lấy toàn văn 1 bài từ kho / Wikipedia / Wikisource. Lỗi → raise rõ."""
    title = (title or "").strip()
    if not title:
        raise ValueError("Tiêu đề không được để trống")
    if source == "library":
        story = get_library_story(title) if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", title) else None
        if not story:
            # title ở đây có thể là tên truyện hiển thị → tìm theo tên
            for s in list_library():
                if s["title"] == title:
                    story = get_library_story(s["id"])
                    break
        if not story:
            raise ValueError(f"Không tìm thấy truyện '{title}' trong kho")
        parts = []
        for ch in story.get("chapters") or []:
            parts.append(f"{ch.get('title', '')}\n\n{ch.get('text', '')}")
        return "\n\n".join(parts).strip()
    api_url = WIKIPEDIA_API if source == "wikipedia" else WIKISOURCE_API if source == "wikisource" else None
    if not api_url:
        raise ValueError("Nguồn web chỉ nhận 'library', 'wikipedia' hoặc 'wikisource'")
    get = http_get or _default_http_get
    try:
        res = get(api_url, params={
            "action": "query", "prop": "extracts", "explaintext": "1",
            "titles": title, "format": "json",
        })
        pages = (res.json().get("query") or {}).get("pages") or {}
    except Exception as e:
        raise ValueError(f"Không lấy được bài '{title}' từ {source}: {e}")
    for page in pages.values():
        extract = (page.get("extract") or "").strip()
        if extract:
            return _clean_text(extract)
    raise ValueError(f"Không tìm thấy nội dung cho '{title}' trên {source}")


# ── 4. Nhập từ link ───────────────────────────────────────────────────────

def extract_text_from_url(url: str, http_get: Optional[Callable] = None) -> str:
    """Fetch 1 URL rồi trích văn bản chính (bỏ script/style/nav/quảng cáo).

    Giới hạn 2MB. Không dùng bs4 (không có trong requirements) — chỉ regex.
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("Link phải bắt đầu bằng http:// hoặc https://")
    get = http_get or _default_http_get
    try:
        res = get(url)
    except Exception as e:
        raise ValueError(f"Không mở được link: {e}")
    if getattr(res, "status_code", 0) != 200:
        raise ValueError(f"Link trả về lỗi HTTP {getattr(res, 'status_code', '?')}")
    html_text = getattr(res, "text", "") or ""
    if len(html_text.encode("utf-8", errors="ignore")) > MAX_URL_BYTES:
        raise ValueError("Trang web quá lớn (giới hạn 2MB) — hãy copy văn bản rồi dán trực tiếp")
    # Bỏ script/style/nav/header/footer/aside
    html_text = re.sub(r"<(script|style|nav|header|footer|aside|noscript)[^>]*>.*?</\1>",
                       " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    # Bỏ khối có class/id chứa nav/menu/quảng cáo/sidebar/footer/banner/popup/cookie
    html_text = re.sub(
        r"<(\w+)[^>]*(?:class|id)=[\"'][^\"']*(?:nav|menu|advert|sidebar|footer|banner|popup|cookie|comment)[^\"']*[\"'][^>]*>.*?</\1>",
        " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    # Ưu tiên <article> hoặc <main>, không thì toàn <body>
    for tag in ("article", "main"):
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html_text, flags=re.DOTALL | re.IGNORECASE)
        if m and len(m.group(1)) > 500:
            html_text = m.group(1)
            break
    else:
        m = re.search(r"<body[^>]*>(.*?)</body>", html_text, flags=re.DOTALL | re.IGNORECASE)
        if m:
            html_text = m.group(1)
    # Đổi thẻ xuống dòng/thẻ đoạn thành ngắt dòng rồi bỏ hết tag còn lại
    html_text = re.sub(r"<(br|/p|/div|/li|/h\d)[^>]*>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = _clean_text(text)
    if len(text) < 200:
        raise ValueError("Không trích được đủ nội dung từ link (trang có thể chặn bot hoặc chủ yếu là hình ảnh) — hãy copy văn bản rồi dán trực tiếp")
    return text


# ── 5. Tải file lên (TXT/MD/DOCX/EPUB — stdlib only, không dep mới) ───────

def _xml_local(tag: str) -> str:
    """Tên local của 1 tag XML (bỏ namespace)."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_docx(data: bytes) -> str:
    """DOCX = file ZIP: đọc word/document.xml, trích text trong <w:t>."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"Không đọc được file DOCX (file có thể hỏng): {e}")
    paras: List[str] = []
    for para_xml in re.split(r"</w:p>", xml):
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para_xml, flags=re.DOTALL)
        line = "".join(_html.unescape(t) for t in texts).strip()
        if line:
            paras.append(line)
    text = "\n\n".join(paras)
    if len(text) < MIN_CHAPTER_CHARS:
        raise ValueError("File DOCX không có nội dung văn bản (có thể chỉ chứa hình ảnh)")
    return _clean_text(text)


def _parse_epub(data: bytes) -> str:
    """EPUB = file ZIP: container.xml → OPF → đọc các file XHTML theo spine."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise ValueError(f"Không đọc được file EPUB (file có thể hỏng): {e}")
    with zf:
        try:
            container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
        except KeyError:
            raise ValueError("File EPUB thiếu META-INF/container.xml")
        m = re.search(r'full-path="([^"]+)"', container)
        if not m:
            raise ValueError("Không tìm thấy đường dẫn OPF trong EPUB")
        opf_path = m.group(1)
        opf_dir = str(Path(opf_path).parent).replace("\\", "/")
        if opf_dir == ".":
            opf_dir = ""
        try:
            opf = zf.read(opf_path).decode("utf-8", errors="replace")
        except KeyError:
            raise ValueError(f"Không đọc được file OPF '{opf_path}' trong EPUB")
        # manifest: id → href (bỏ namespace bằng regex)
        manifest: Dict[str, str] = {}
        for item_m in re.finditer(r'<item[^>]+>', opf):
            tag = item_m.group(0)
            id_m = re.search(r'id="([^"]+)"', tag)
            href_m = re.search(r'href="([^"]+)"', tag)
            type_m = re.search(r'media-type="([^"]+)"', tag)
            if id_m and href_m and (type_m or "").group(1) in (
                    "application/xhtml+xml", "text/html", ""):
                manifest[id_m.group(1)] = href_m.group(1)
        # spine: thứ tự đọc
        spine: List[str] = []
        spine_m = re.search(r"<spine[^>]*>(.*?)</spine>", opf, flags=re.DOTALL)
        if spine_m:
            for ref_m in re.finditer(r'idref="([^"]+)"', spine_m.group(1)):
                href = manifest.get(ref_m.group(1))
                if href:
                    spine.append(href)
        if not spine:
            spine = list(manifest.values())
        parts: List[str] = []
        for href in spine:
            href = href.split("#")[0]
            full = f"{opf_dir}/{href}" if opf_dir else href
            try:
                xhtml = zf.read(full).decode("utf-8", errors="replace")
            except KeyError:
                continue
            xhtml = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", xhtml,
                           flags=re.DOTALL | re.IGNORECASE)
            xhtml = re.sub(r"<(br|/p|/div|/li|/h\d)[^>]*>", "\n", xhtml, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", xhtml)
            text = _clean_text(text)
            if text:
                parts.append(text)
    text = "\n\n".join(parts)
    if len(text) < MIN_CHAPTER_CHARS:
        raise ValueError("File EPUB không có nội dung văn bản (có thể chỉ chứa hình ảnh)")
    return text


def parse_text_bytes(data: bytes, filename: str) -> str:
    """Trích văn bản từ file tải lên. PDF trả lỗi rõ (chưa hỗ trợ)."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".txt", ".md"):
        return _clean_text(data.decode("utf-8", errors="replace"))
    if suffix == ".docx":
        return _parse_docx(data)
    if suffix == ".epub":
        return _parse_epub(data)
    if suffix == ".pdf":
        raise ValueError(
            "PDF chưa được hỗ trợ — hãy chuyển sang DOCX hoặc TXT trước rồi tải lên "
            "(mở PDF bằng Word/Google Docs rồi lưu thành .docx, hoặc copy văn bản rồi dán trực tiếp).")
    raise ValueError(
        f"Định dạng '{suffix or 'không rõ'}' chưa được hỗ trợ — chỉ nhận TXT, MD, DOCX, EPUB.")


# ── 6. Xào nấu AI (deterministic, không LLM) ──────────────────────────────

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-Ỹ0-9\"“\(\[])")
_DRAMATIC_WORDS = ("bỗng", "chết", "ma", "quỷ", "kinh hoàng", "bí mật", "máu",
                   "giết", "trả thù", "không ngờ", "hóa ra", "sợ", "rùng mình",
                   "oan", "lừa", "phản bội", "cứu", "thoát", "nguy hiểm")
_TWIST_WORDS = ("bỗng nhiên", "bất ngờ", "hóa ra", "không ngờ", "đột nhiên",
                "nào ngờ", "ai ngờ", "ngờ đâu", "bỗng dưng")
_HOOK_PREFIXES = (
    "Bạn có dám nghe hết câu chuyện này không? ",
    "Câu chuyện này sẽ khiến bạn không thể rời mắt. ",
    "Khoan đã — đừng lướt qua, vì câu chuyện này có một cái kết không ai ngờ. ",
)


def _split_sentences(text: str) -> List[str]:
    """Chia văn bản thành câu (giữ nguyên câu gốc, không bịa thêm)."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    parts = _SENT_SPLIT_RE.split(text)
    out = [p.strip() for p in parts if p.strip()]
    # Câu quá dài (>220 ký tự): tách thêm ở dấu phẩy để nhịp kể thoáng.
    final: List[str] = []
    for s in out:
        if len(s) > 220 and "," in s:
            bits = [b.strip() for b in s.split(",") if b.strip()]
            acc = ""
            for b in bits:
                if len(acc) + len(b) + 2 > 220 and acc:
                    final.append(acc)
                    acc = b
                else:
                    acc = f"{acc}, {b}" if acc else b
            if acc:
                final.append(acc)
        else:
            final.append(s)
    return final


def _dramatic_score(sentence: str) -> int:
    s = sentence.lower()
    score = 0
    if "!" in sentence:
        score += 3
    if "?" in sentence:
        score += 2
    for w in _DRAMATIC_WORDS:
        if w in s:
            score += 2
    if 20 <= len(sentence) <= 130:
        score += 1
    return score


def _scene_keywords(text: str, limit: int = 4) -> List[str]:
    """Lấy tối đa `limit` từ khóa Anh từ văn bản cảnh (theo thứ tự xuất hiện).

    Khớp theo TỪ (word boundary), không khớp chuỗi con — fix 2026-11-23: trước đây
    `"ma" in text` bắt cả "may/mắn/mang/mãi" nên cảnh vô tội cũng bị gắn keyword
    "ghost" vào prompt ảnh (video Ăn Khế Trả Vàng ch.1: prompt cảnh 4 có "ghost" →
    ảnh AI vẽ bóng phụ nữ áo trắng mờ lúc 0:33).
    """
    low = text.lower()
    found: List[str] = []
    for vn, en in _VN_EN_KEYWORDS.items():
        if re.search(r"\b" + re.escape(vn) + r"\b", low) and en not in found:
            found.append(en)
        if len(found) >= limit:
            break
    return found


def _scene_prompt(scene_text: str, visual_style: str, aspect_ratio: str,
                  mood: str = "") -> str:
    """Prompt ảnh cho 1 cảnh: từ khóa nội dung + hậu tố phong cách."""
    info = BATCH_VISUAL_STYLES.get(visual_style, BATCH_VISUAL_STYLES["cinematic"])
    suffix = info["prompt_suffix"]
    ratio = ("9:16 vertical smartphone format" if aspect_ratio == "9:16"
             else "16:9 widescreen format" if aspect_ratio == "16:9"
             else "1:1 square format")
    kws = _scene_keywords(scene_text)
    low = scene_text.lower()
    if any(w in low for w in _BIRD_WORDS):
        # Khóa chim thần: thay "bird" chung chung bằng Character Block nguyên văn,
        # giống hệt mọi cảnh có chim để ảnh AI không đổi loài.
        kws = [MAGIC_BIRD_BLOCK if k == "bird" else k for k in kws]
        if MAGIC_BIRD_BLOCK not in kws:
            kws.append(MAGIC_BIRD_BLOCK)
    subject = ", ".join(kws) if kws else "Vietnamese folk tale scene"
    mood_part = f", {mood}" if mood else ""
    return (f"Cinematic scene, {subject}, {_NO_EXTRA_SUBJECTS}, "
            f"Vietnamese folk tale atmosphere{mood_part}, {ratio}, {suffix}")


def _group_sentences(sentences: List[str], scenes_count: int) -> List[List[str]]:
    """Chia câu thành nhóm cảnh (đều nhau; ít câu hơn số cảnh thì giảm cảnh)."""
    n = max(1, min(int(scenes_count), len(sentences)))
    groups: List[List[str]] = [[] for _ in range(n)]
    base, extra = divmod(len(sentences), n)
    idx = 0
    for g in range(n):
        take = base + (1 if g < extra else 0)
        groups[g] = sentences[idx:idx + take]
        idx += take
    return [g for g in groups if g]


def _trim_voiceover(text: str, max_chars: int = 420) -> str:
    """Gọn lời thoại: cắt ở cuối câu trong giới hạn, không cắt giữa câu."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text.rfind(". ", 0, max_chars)
    if cut < max_chars // 2:
        cut = text.rfind(" ", 0, max_chars)
    return text[:cut].strip() if cut > 0 else text[:max_chars].strip()


def adapt_chapter(chapter_text: str, mode: str, scenes_count: int,
                  visual_style: str, extra_req: str = "",
                  aspect_ratio: str = "9:16") -> Dict[str, Any]:
    """Chuyển thể 1 chương thành kịch bản {scenes: [{voiceover, prompt, duration, ...}]}.

    - Voiceover = câu văn gốc đã gọn lại, không bịa thêm tình tiết.
    - `extra_req` (nếu có) chỉ được dùng làm tiền tố lời dẫn cảnh đầu.
    - mode lạ → ValueError; chương rỗng → ValueError.
    """
    if mode not in ADAPT_MODES:
        raise ValueError(
            f"Chế độ xào nấu '{mode}' không hợp lệ. Cho phép: {sorted(ADAPT_MODES)}")
    if visual_style not in BATCH_VISUAL_STYLES:
        raise ValueError(f"Phong cách '{visual_style}' không hợp lệ")
    if aspect_ratio not in ("9:16", "16:9", "1:1"):
        raise ValueError(f"Tỷ lệ khung hình '{aspect_ratio}' không hợp lệ")
    sentences = _split_sentences(chapter_text)
    if not sentences:
        raise ValueError("Chương không có nội dung để chuyển thể")

    if mode == "summary":
        # Rút gọn: giữ câu đầu mỗi đoạn + câu kịch tính nhất mỗi nhóm.
        paras = [p.strip() for p in re.split(r"\n\s*\n", chapter_text) if p.strip()]
        picked: List[str] = []
        for p in paras:
            p_sents = _split_sentences(p)
            if p_sents:
                picked.append(p_sents[0])
        if len(picked) < 2:
            picked = sentences
        # Thêm câu kịch tính nhất nếu chưa có
        best = max(sentences, key=_dramatic_score)
        if best not in picked:
            picked.append(best)
        sentences = picked

    groups = _group_sentences(sentences, scenes_count)
    n = len(groups)
    moods = {
        "horror": "dark eerie night, thick fog, cold pale moonlight, haunted atmosphere",
        "suspense": "tense dramatic atmosphere, high stakes, cinematic tension",
        "podcast": "warm soft light, peaceful contemplative mood",
    }
    mood = moods.get(mode, "")
    scenes: List[Dict[str, Any]] = []
    for i, group in enumerate(groups):
        voiceover = _trim_voiceover(" ".join(group))
        role = "hook" if i == 0 else ("ending" if i == n - 1
                                       else "climax" if i == n - 2 and n > 2 else "buildup")
        scenes.append({
            "index": i + 1,
            "role": role,
            "prompt": _scene_prompt(" ".join(group), visual_style, aspect_ratio, mood),
            "voiceover": voiceover,
            "duration": max(5, min(10, round(len(voiceover) / 11) or 6)),
        })

    # ── Biến thể theo chế độ (chỉ khung dẫn/kết, không bịa tình tiết) ──
    if mode == "short" and scenes:
        hook_idx = max(range(len(sentences)), key=lambda k: _dramatic_score(sentences[k]))
        dramatic = sentences[hook_idx]
        pick = _HOOK_PREFIXES[sum(ord(c) for c in chapter_text) % len(_HOOK_PREFIXES)]
        scenes[0]["voiceover"] = _trim_voiceover(pick + dramatic)
        scenes[-1]["voiceover"] = _trim_voiceover(
            scenes[-1]["voiceover"] + " Nhưng đó mới chỉ là bắt đầu… phần sau còn ly kỳ hơn.")
    elif mode == "horror" and scenes:
        first = scenes[0]["voiceover"]
        first = first[0].lower() + first[1:] if first else first
        scenes[0]["voiceover"] = _trim_voiceover("Đêm ấy, " + first)
        for sc in scenes:
            sc["voiceover"] = sc["voiceover"].replace(". ", "… ")
        scenes[-1]["voiceover"] = _trim_voiceover(
            scenes[-1]["voiceover"] + " Câu chuyện khép lại trong bóng đêm… hay vẫn còn tiếp diễn?")
    elif mode == "suspense" and scenes:
        twist_at = None
        for i, sc in enumerate(scenes):
            if any(w in sc["voiceover"].lower() for w in _TWIST_WORDS):
                twist_at = i
                break
        if twist_at is None:
            twist_at = max(0, n - 2)
        scenes[twist_at]["voiceover"] = _trim_voiceover(
            "Và rồi, điều không ai ngờ tới đã xảy ra: " + scenes[twist_at]["voiceover"])
    elif mode == "podcast" and scenes:
        moral = None
        for s in reversed(sentences):
            low = s.lower()
            if any(k in low for k in ("bài học", "rút ra", "hãy nhớ", "đừng", "phải biết", "mới hiểu")):
                moral = s
                break
        closing = (f"Bài học đọng lại sau câu chuyện này: {moral}"
                   if moral and moral not in scenes[-1]["voiceover"]
                   else "Câu chuyện khép lại, nhưng điều nó để lại thì còn ở mãi với mỗi chúng ta.")
        scenes[-1]["voiceover"] = _trim_voiceover(scenes[-1]["voiceover"] + " " + closing)

    if extra_req and extra_req.strip():
        scenes[0]["voiceover"] = _trim_voiceover(
            extra_req.strip() + " " + scenes[0]["voiceover"])

    return {"mode": mode, "scenes": scenes, "scene_count": len(scenes)}


# ── 7. Project (lưu JSON trong workspace/_story_projects) ────────────────

def _project_path(project_id: str) -> Path:
    if not re.fullmatch(r"st_[a-z0-9]{8,16}", project_id or ""):
        raise ValueError(f"ID project '{project_id}' không hợp lệ")
    STORY_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return STORY_PROJECTS_DIR / f"{project_id}.json"


def create_project(title: str, author: str, text: str, source: str,
                   style: str = "cinematic",
                   chars_per_chapter: int = DEFAULT_CHARS_PER_CHAPTER) -> Dict[str, Any]:
    """Tạo project từ toàn văn: validate độ dài, tách chương, lưu JSON."""
    title = (title or "").strip()
    if not title:
        raise ValueError("Tên truyện không được để trống")
    if len(title) > 200:
        raise ValueError("Tên truyện tối đa 200 ký tự")
    if not text or not text.strip():
        raise ValueError("Nội dung truyện trống — chưa có gì để tách chương")
    if len(text) > MAX_STORY_CHARS:
        raise ValueError(
            f"Nội dung vượt giới hạn {MAX_STORY_CHARS:,} ký tự "
            f"(đang có {len(text):,} ký tự) — hãy chia nhỏ truyện rồi nhập từng phần")
    chapters = split_chapters(text, chars_per_chapter)
    if not chapters:
        raise ValueError("Không tách được chương nào từ nội dung đã nhập")
    project = {
        "id": "st_" + uuid.uuid4().hex[:12],
        "title": title,
        "author": (author or "Không rõ").strip() or "Không rõ",
        "source": source,
        "style": style if style in BATCH_VISUAL_STYLES else "cinematic",
        "chars_per_chapter": chars_per_chapter,
        "created_at": time.time(),
        "word_count": sum(c["word_count"] for c in chapters),
        "chapter_count": len(chapters),
        "chapters": chapters,
        "scripts": {},
    }
    save_project(project)
    return project


def save_project(project: Dict[str, Any]) -> Dict[str, Any]:
    path = _project_path(project["id"])
    path.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    return project


def load_project(project_id: str) -> Optional[Dict[str, Any]]:
    try:
        path = _project_path(project_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Không đọc được project %s: %s", project_id, e)
        return None


def list_projects() -> List[Dict[str, Any]]:
    """Danh sách project (metadata, mới nhất trước)."""
    out: List[Dict[str, Any]] = []
    if not STORY_PROJECTS_DIR.exists():
        return out
    for path in STORY_PROJECTS_DIR.glob("st_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({k: data.get(k) for k in (
            "id", "title", "author", "source", "style", "created_at",
            "word_count", "chapter_count")})
    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out


def create_project_from_library(story_id: str) -> Dict[str, Any]:
    """Tạo project từ 1 truyện trong kho (giữ nguyên chương đã chia sẵn)."""
    story = get_library_story(story_id)
    if not story:
        raise ValueError(f"Không tìm thấy truyện '{story_id}' trong kho")
    project = {
        "id": "st_" + uuid.uuid4().hex[:12],
        "title": story["title"],
        "author": story.get("author", "Dân gian"),
        "source": "library",
        "style": story.get("style", "cinematic"),
        "chars_per_chapter": DEFAULT_CHARS_PER_CHAPTER,
        "created_at": time.time(),
        "word_count": story.get("word_count", 0),
        "chapter_count": len(story.get("chapters") or []),
        "chapters": [
            {"title": c.get("title", ""), "text": c.get("text", ""),
             "word_count": len((c.get("text") or "").split())}
            for c in story.get("chapters") or []
        ],
        "scripts": {},
    }
    save_project(project)
    return project


# ── 8. Tạo video từ chương (inject scenes vào hàng đợi batch) ─────────────

def create_story_videos(project_id: str, chapter_indexes: List[int],
                        visual_style: str, voice: str, aspect_ratio: str,
                        scenes_count: int, mode: str = "short",
                        extra_req: str = "") -> List[Dict[str, Any]]:
    """Tạo 1 video cho mỗi chương đã chọn.

    Dùng kịch bản đã adapt lưu trong project nếu khớp tham số, không thì
    adapt mới (mode mặc định `short`). Scenes inject sẵn vào queue item
    (voiceover + prompt) nên worker có sẵn chạy nốt, không sinh lại script.
    """
    project = load_project(project_id)
    if not project:
        raise FileNotFoundError(f"Không tìm thấy truyện '{project_id}'")
    if visual_style not in BATCH_VISUAL_STYLES:
        raise ValueError(f"Phong cách '{visual_style}' không hợp lệ")
    if mode not in ADAPT_MODES:
        raise ValueError(f"Chế độ xào nấu '{mode}' không hợp lệ")
    chapters = project.get("chapters") or []
    if not chapter_indexes:
        raise ValueError("Chưa chọn chương nào để tạo video")
    prompts: List[str] = []
    scripts: List[Dict[str, Any]] = []
    changed = False
    for ci in chapter_indexes:
        if not isinstance(ci, int) or ci < 0 or ci >= len(chapters):
            raise ValueError(f"Chỉ số chương {ci} không hợp lệ (truyện có {len(chapters)} chương)")
        ch = chapters[ci]
        saved = (project.get("scripts") or {}).get(str(ci))
        if (saved and saved.get("mode") == mode
                and saved.get("scenes_count") == scenes_count
                and saved.get("visual_style") == visual_style
                and saved.get("scenes")):
            script = saved
        else:
            script = adapt_chapter(ch["text"], mode, scenes_count, visual_style,
                                   extra_req=extra_req, aspect_ratio=aspect_ratio)
            script = {**script, "scenes_count": scenes_count, "visual_style": visual_style}
            project.setdefault("scripts", {})[str(ci)] = script
            changed = True
        title = f"{project['title']} — {ch['title']}"
        prompts.append(title)
        scripts.append({
            "title": title,
            "viral_title": title,
            "caption": f"Truyện: {project['title']} — {ch['title']}. "
                       f"Nghe truyện mỗi ngày, đăng ký kênh để không bỏ lỡ chương sau.",
            "hashtags": ["#truyen", "#kechuyen", "#truyenngan", "#xuhuong"],
            "scenes": script["scenes"],
        })
    if changed:
        save_project(project)
    created = BATCH_MANAGER.add_batch(
        prompts=prompts,
        aspect_ratio=aspect_ratio,
        scenes_count=scenes_count,
        visual_style=visual_style,
        voice=voice,
        auto_run=True,
        scripts=scripts,
        extra={"story_source": "story", "story_project_id": project_id},
    )
    return created
