"""
consistency.py — Preflight kiểm tra đồng nhất cho chế độ "Đồng nhất tối đa".

Quy tắc làm video dài của Tiến, đưa vào tool dưới dạng check xác minh được
bằng code (không bịa, không tự sửa thoại/prompt của user — chỉ báo + gợi ý):
  1. Character Block + Voice Profile dán NGUYÊN VĂN vào mọi prompt.
  2. Nối cảnh bằng Last Frame tuần tự (chain_mode=sequential).
  3. Đổi thời gian/địa điểm phải có shot cầu nối.
  4. Phim dài neo lại nhân vật định kỳ (lỗi nhỏ tích lũy theo chuỗi).

Module này chỉ chứa hàm thuần trên dict (MovieRequest.model_dump()),
không import app/batch_engine nên test được độc lập, không sợ circular import.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional

# Clip API video trả về dài 10s/cảnh — scene dài hơn sẽ bị loop/cắt.
API_CLIP_SECONDS = 10

# Neo lại nhân vật: cứ mỗi N scene trong chuỗi sequential thì dùng ảnh
# nhân vật/keyframe thay cho prev_last_frame để reset drift tích lũy.
CONSISTENCY_ANCHOR_EVERY = 8

# Nhóm thời gian trong ngày (VI + EN). Hai scene liên tiếp đổi nhóm
# (vd ngày -> đêm) mà không có shot cầu nối là dấu hiệu nhảy thời gian.
TIME_GROUPS: Dict[str, set] = {
    "morning": {"sáng", "buổi sáng", "bình minh", "dawn", "morning", "sunrise"},
    "noon": {"trưa", "buổi trưa", "noon", "midday"},
    "afternoon": {"chiều", "buổi chiều", "afternoon"},
    "evening": {"tối", "buổi tối", "hoàng hôn", "dusk", "evening", "sunset"},
    "night": {"đêm", "ban đêm", "khuya", "nửa đêm", "night", "midnight"},
}

# Từ khóa trong nhà / ngoài trời.
INDOOR_KEYWORDS = {"trong nhà", "indoor", "inside", "in house"}
OUTDOOR_KEYWORDS = {"ngoài trời", "outdoor", "outside", "ngoài sân"}

# Danh từ địa điểm thường gặp (VI + EN) — dùng để phát hiện đổi bối cảnh.
PLACE_KEYWORDS = {
    "rừng", "biển", "núi", "thành phố", "làng", "cung điện", "trường",
    "bệnh viện", "nhà", "quán", "chợ", "sông", "sa mạc", "tuyết",
    "bãi biển", "đồng lúa", "chùa", "phố", "văn phòng", "bếp",
    "phòng khách", "hang động", "thảo nguyên",
    "forest", "beach", "mountain", "city", "village", "palace", "school",
    "hospital", "house", "market", "river", "desert", "snow", "street",
    "office", "kitchen", "temple", "cave",
}


# ── Preflight cầu nối v2: trích thực thể thời gian/địa điểm ───────────────
# v1 chỉ khớp từ khóa (TIME_GROUPS/PLACE_KEYWORDS). v2 trích thực thể thật
# từ prompt rồi so sánh 2 cảnh kề nhau:
#   - thời gian: nhóm buổi trong ngày + độ lệch ngày ("hôm sau", "tuần sau"...
#     "sáng" -> "sáng hôm sau" là nhảy thời gian dù cùng buổi sáng);
#   - địa điểm: từ khóa địa điểm + danh từ riêng sau giới từ (tại/ở/trong...)
#     + cụm từ viết hoa nhiều từ (Hà Nội, Cổ Loa) — so bằng tập hợp đã
#     chuẩn hóa, và so khớp chuỗi tương đồng (difflib) để "rừng trúc" vs
#     "rừng tre" không bị tính là đổi bối cảnh.
# Heuristic từ khóa v1 được GIỮ làm fallback khi không trích được thực thể
# ở một trong hai cảnh (vd chỉ có "trong nhà"/"ngoài trời").

# Cụm chỉ độ lệch ngày (dài trước để "ngày hôm sau" thắng "hôm sau").
# Giá trị: số ngày lệch so với "hôm nay" của cảnh (âm = quá khứ).
DAY_OFFSET_MARKERS = (
    ("vài ngày sau", 3),
    ("ngày hôm sau", 1),
    ("ngày hôm trước", -1),
    ("hôm sau", 1),
    ("ngày sau", 1),
    ("tuần sau", 7),
    ("tháng sau", 30),
    ("năm sau", 365),
    ("hôm trước", -1),
    ("hôm qua", -1),
)

# Giới từ mở đầu cụm địa điểm (VI + EN).
_PLACE_PREPOSITIONS = (
    "tại", "ở", "trong", "ngoài", "trên", "dưới", "giữa", "về", "đến",
    "tới", "từ", "ra", "vào", "qua", "at", "in", "on",
)
# Chú ý: KHÔNG dùng lớp ký tự [A-ZÀ-Ỹ] để nhận diện chữ hoa — dải Unicode
# đó chứa cả chữ thường có dấu (ố, ồ, ộ...). Chữ hoa được kiểm bằng
# str.isupper() trên từng token (xem _leading_capital_run).
_PLACE_PREP_RE = re.compile(
    r"(?:^|[\s,;:.])"
    r"(?:" + "|".join(_PLACE_PREPOSITIONS) + r")\s+"
    r"([^\s,;:.]+(?:\s+[^\s,;:.]+)*)",
    re.IGNORECASE,
)

# Từ viết hoa không phải địa điểm (đại từ/xưng hô) — loại khỏi thực thể.
_NON_PLACE_CAPITALIZED = {
    "tôi", "anh", "chị", "em", "ông", "bà", "cô", "chú", "bác", "bạn",
    "mình", "ta", "chúng ta", "mọi người", "ai",
}


def _extract_time(text: str) -> Optional[Dict[str, Any]]:
    """Trích thực thể thời gian: {"group": nhóm buổi|None, "day": lệch ngày}.

    Trả None khi prompt không nhắc gì về thời gian (để fallback quyết)."""
    lowered = text.lower()
    group = _time_group(text)
    day = 0
    for phrase, offset in DAY_OFFSET_MARKERS:
        if phrase in lowered:
            day = offset
            break
    if group is None and day == 0:
        return None
    return {"group": group, "day": day}


def _time_label(t: Dict[str, Any]) -> str:
    label = t.get("group") or ""
    day = t.get("day") or 0
    if day > 0:
        label = (label + " hôm sau") if label else "hôm sau"
    elif day < 0:
        label = (label + " hôm trước") if label else "hôm trước"
    return label.strip() or "không rõ thời gian"


def _time_changed(t_prev: Optional[Dict[str, Any]],
                  t_cur: Optional[Dict[str, Any]]) -> Optional[str]:
    """So sánh 2 thực thể thời gian. Trả chuỗi mô tả thay đổi, hoặc None
    khi không kết luận được đổi (thiếu dữ kiện ở một bên / giống nhau)."""
    if not t_prev or not t_cur:
        return None
    g_prev, g_cur = t_prev.get("group"), t_cur.get("group")
    d_prev, d_cur = t_prev.get("day") or 0, t_cur.get("day") or 0
    if g_prev and g_cur:
        if g_prev != g_cur or d_prev != d_cur:
            return (f"thời gian đổi từ '{_time_label(t_prev)}' "
                    f"sang '{_time_label(t_cur)}'")
        return None
    if d_prev != d_cur:
        return (f"thời gian đổi từ '{_time_label(t_prev)}' "
                f"sang '{_time_label(t_cur)}'")
    return None


def _normalize_place_entity(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip().lower().strip(".,;:!?()\"'"))


def _leading_capital_run(phrase: str) -> str:
    """Chuỗi các từ đầu cụm mà từ nào cũng bắt đầu bằng chữ hoa thật
    (str.isupper) — vd 'Hà Nội buổi chiều' -> 'Hà Nội'."""
    run = []
    for w in phrase.split():
        if w and w[0].isupper():
            run.append(w)
        else:
            break
    return " ".join(run)


def _extract_places(text: str, exclude: frozenset = frozenset()) -> set:
    """Trích thực thể địa điểm: từ khóa địa điểm + danh từ riêng sau giới
    từ + cụm viết hoa nhiều từ không ở đầu câu. `exclude`: tên nhân vật
    (để 'Lan' không bị tính là địa điểm)."""
    found = set(_places(text))
    for m in _PLACE_PREP_RE.finditer(text):
        ent = _normalize_place_entity(_leading_capital_run(m.group(1)))
        if ent and ent not in exclude and ent not in _NON_PLACE_CAPITALIZED:
            found.add(ent)
    for sentence in re.split(r"[.!?\n]+", text):
        tokens = re.findall(r"[^\s,;:.!?()\"']+", sentence.strip())
        if len(tokens) < 2:
            continue
        # Bỏ từ đầu câu (viết hoa do ngữ pháp), quét cụm viết hoa còn lại.
        rest = tokens[1:]
        i = 0
        while i < len(rest):
            if rest[i] and rest[i][0].isupper():
                j = i
                while j < len(rest) and rest[j] and rest[j][0].isupper():
                    j += 1
                ent = _normalize_place_entity(" ".join(rest[i:j]))
                words = ent.split()
                if ent and ent not in exclude \
                        and ent not in _NON_PLACE_CAPITALIZED:
                    # Cụm 1 từ viết hoa giữa câu vẫn có thể là tên người —
                    # chỉ nhận khi có trong danh sách địa điểm hoặc cụm
                    # >= 2 từ (tên riêng địa danh thường nhiều từ: Hà Nội,
                    # Cổ Loa, Trúc Lâm).
                    if len(words) >= 2 or ent in PLACE_KEYWORDS:
                        found.add(ent)
                i = j
            else:
                i += 1
    return found


def _place_similarity(a: str, b: str) -> float:
    """Độ tương đồng chuỗi 2 thực thể địa điểm (0..1)."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def _places_changed(prev_places: set, cur_places: set) -> bool:
    """Hai tập thực thể địa điểm có khác nhau thật không? Giống nhau khi
    giao nhau, hoặc khi thực thể gần nhất đủ tương đồng (>= 0.5, vd
    'rừng trúc' vs 'rừng tre')."""
    if not prev_places or not cur_places:
        return False
    if prev_places & cur_places:
        return False
    best = max((_place_similarity(a, b) for a in prev_places for b in cur_places),
               default=0.0)
    return best < 0.5


def _character_names_to_exclude(req: Dict[str, Any]) -> frozenset:
    """Tên + alias nhân vật (lowercase) để loại khỏi thực thể địa điểm."""
    names = set()
    for ch in req.get("characters") or []:
        if isinstance(ch, dict):
            if ch.get("name"):
                names.add(str(ch["name"]).strip().lower())
            for al in ch.get("aliases") or []:
                if al:
                    names.add(str(al).strip().lower())
    for scene in req.get("scenes") or []:
        for seg in scene.get("voice_segments") or []:
            if isinstance(seg, dict) and seg.get("character"):
                names.add(str(seg["character"]).strip().lower())
    return frozenset(n for n in names if n)


def _check(level: str, code: str, message: str, suggestion: str = "",
           scene_index: Optional[int] = None) -> Dict[str, Any]:
    return {
        "level": level,  # "error" | "warning" | "info"
        "code": code,
        "scene_index": scene_index,
        "message": message,
        "suggestion": suggestion,
    }


def _scene_text(scene: Dict[str, Any]) -> str:
    parts = [str(scene.get("prompt") or ""), str(scene.get("voiceover") or "")]
    for seg in scene.get("voice_segments") or []:
        if isinstance(seg, dict):
            parts.append(str(seg.get("text") or ""))
    return " ".join(parts)


def _time_group(text: str) -> Optional[str]:
    lowered = text.lower()
    for group, keywords in TIME_GROUPS.items():
        if any(kw in lowered for kw in keywords):
            return group
    return None


def _places(text: str) -> set:
    lowered = text.lower()
    return {kw for kw in PLACE_KEYWORDS if kw in lowered}


def _indoor_outdoor(text: str) -> Optional[str]:
    lowered = text.lower()
    if any(kw in lowered for kw in INDOOR_KEYWORDS):
        return "indoor"
    if any(kw in lowered for kw in OUTDOOR_KEYWORDS):
        return "outdoor"
    return None


def _character_locked(req: Dict[str, Any]) -> bool:
    """Nhân vật đã khóa khi: có Character Block trong style_locks, hoặc có
    scene dùng ảnh tham chiếu, hoặc danh sách characters có block."""
    locks = req.get("style_locks") or {}
    if str(locks.get("character") or "").strip():
        return True
    for scene in req.get("scenes") or []:
        if str(scene.get("reference_image") or "").strip():
            return True
    for ch in req.get("characters") or []:
        if isinstance(ch, dict) and str(
                ch.get("character_block") or ch.get("block") or "").strip():
            return True
    return False


def _has_people(req: Dict[str, Any]) -> bool:
    """Phim có nhân vật/thoại cần khóa: có voiceover/voice_segments,
    hoặc có danh sách characters, hoặc đã điền character lock."""
    locks = req.get("style_locks") or {}
    if str(locks.get("character") or "").strip():
        return True
    if req.get("characters"):
        return True
    for scene in req.get("scenes") or []:
        if str(scene.get("voiceover") or "").strip():
            return True
        if scene.get("voice_segments"):
            return True
    return False


def consistency_anchor_reference(scene: Dict[str, Any]) -> Optional[str]:
    """Ảnh neo nhân vật cho mốc reset drift: keyframe của scene, nếu không
    có thì ảnh tham chiếu nhân vật. Trả None khi không có gì để neo."""
    return (scene.get("keyframe_path")
            or scene.get("character_reference") or None)


def run_consistency_checks(req: Dict[str, Any],
                           consistency_mode: str = "standard") -> Dict[str, Any]:
    """Chạy toàn bộ check đồng nhất trên req đã model_dump() thành dict.

    Trả {"checks": [...], "errors": int, "warnings": int, "passed": bool}.
    Mỗi check: {level, code, scene_index, message, suggestion}.
    Hàm này KHÔNG sửa req, KHÔNG tự sửa prompt/thoại của user.
    """
    checks: List[Dict[str, Any]] = []
    scenes = req.get("scenes") or []
    locks = req.get("style_locks") or {}
    default_voice = str(req.get("voice") or "")

    # ── 1. lock_present (error): có người/thoại mà chưa khóa nhân vật ──
    if _has_people(req):
        if _character_locked(req):
            checks.append(_check(
                "info", "lock_present",
                "Đã khóa nhân vật (Character Block / ảnh tham chiếu)."))
        else:
            checks.append(_check(
                "error", "lock_present",
                "Phim có thoại/nhân vật nhưng chưa khóa nhân vật: "
                "ô 'Neo ngoại hình nhân vật chính' đang trống và không "
                "cảnh nào có ảnh tham chiếu.",
                "Điền Character Block (mặt, tóc, trang phục, đạo cụ) vào ô "
                "'Neo ngoại hình nhân vật chính' trong Hồ sơ nhân vật, "
                "hoặc gắn ảnh tham chiếu cho từng cảnh."))

    # ── 2. voice_locked (warning): nhiều nhân vật chung 1 giọng ──
    for scene in scenes:
        segments = scene.get("voice_segments") or []
        if len(segments) < 2:
            continue
        idx = scene.get("index")
        chars = [str(s.get("character") or "").strip() or "?"
                 for s in segments if isinstance(s, dict)]
        voices = [str(s.get("voice") or "").strip()
                  for s in segments if isinstance(s, dict)]
        distinct_chars = {c for c in chars if c != "?"}
        if len(distinct_chars) >= 2:
            if any(not v or v == default_voice for v in voices):
                checks.append(_check(
                    "warning", "voice_locked",
                    f"Cảnh {idx}: {len(distinct_chars)} nhân vật "
                    f"({', '.join(sorted(distinct_chars))}) nhưng có người "
                    "đang dùng giọng mặc định chung thay vì giọng riêng.",
                    "Gán giọng riêng cho từng nhân vật trong Voice Profile "
                    "(vd Thỏ Trắng / Chim Sẻ / Cáo Nhỏ mỗi người một giọng), "
                    "không để 2 nhân vật chung 1 giọng.",
                    scene_index=idx))
            elif len(set(voices)) < len(distinct_chars):
                checks.append(_check(
                    "warning", "voice_locked",
                    f"Cảnh {idx}: 2 nhân vật trở lên đang chung 1 giọng.",
                    "Đổi giọng cho 1 trong các nhân vật để phân biệt "
                    "khi nghe.",
                    scene_index=idx))

    # ── 3. chain (info): trạng thái nối cảnh ──
    chain_mode = str(req.get("chain_mode") or "none")
    if (consistency_mode or "standard") == "max":
        checks.append(_check(
            "info", "chain",
            "Chế độ Đồng nhất tối đa: đã ép chain_mode='sequential' — "
            "cảnh sau kế thừa Last Frame của cảnh trước."))
    elif chain_mode == "sequential":
        checks.append(_check(
            "info", "chain",
            "Đang chạy nối tiếp tuần tự (Last Frame chaining)."))
    else:
        checks.append(_check(
            "info", "chain",
            "Đang chạy song song — các cảnh render độc lập, dễ trôi "
            "nhân vật/bối cảnh. Bật 'Đồng nhất tối đa' để ép nối tiếp."))

    # ── 4. bridge (warning): nhảy thời gian/địa điểm giữa 2 cảnh liên tiếp ──
    # v2: trích thực thể thời gian/địa điểm từ prompt 2 cảnh kề nhau rồi so
    # sánh (kèm so khớp chuỗi tương đồng cho địa điểm). Không trích được
    # thực thể thì GIỮ heuristic từ khóa v1 làm fallback.
    ordered = sorted(scenes, key=lambda s: s.get("index", 0))
    exclude_names = _character_names_to_exclude(req)
    for prev, cur in zip(ordered, ordered[1:]):
        prev_text, cur_text = _scene_text(prev), _scene_text(cur)
        prev_idx, cur_idx = prev.get("index"), cur.get("index")
        reason = ""
        # v2a. Thời gian: nhóm buổi + lệch ngày.
        time_reason = _time_changed(_extract_time(prev_text),
                                    _extract_time(cur_text))
        if time_reason:
            reason = time_reason
        else:
            # v2b. Địa điểm: thực thể trích được, so tập hợp + tương đồng chuỗi.
            prev_places_v2 = _extract_places(prev_text, exclude_names)
            cur_places_v2 = _extract_places(cur_text, exclude_names)
            if _places_changed(prev_places_v2, cur_places_v2):
                reason = (f"địa điểm đổi từ "
                          f"'{', '.join(sorted(prev_places_v2))}' "
                          f"sang '{', '.join(sorted(cur_places_v2))}'")
        if not reason:
            # Fallback v1 (heuristic từ khóa): chỉ chạy khi v2 không kết
            # luận được — vd đổi trong nhà ↔ ngoài trời không có tên địa điểm.
            if _time_group(prev_text) and _time_group(cur_text) \
                    and _time_group(prev_text) != _time_group(cur_text):
                reason = (f"thời gian đổi từ '{_time_group(prev_text)}' "
                          f"sang '{_time_group(cur_text)}'")
            elif _indoor_outdoor(prev_text) and _indoor_outdoor(cur_text) \
                    and _indoor_outdoor(prev_text) != _indoor_outdoor(cur_text):
                reason = "bối cảnh đổi trong nhà ↔ ngoài trời"
            else:
                prev_places, cur_places = _places(prev_text), _places(cur_text)
                if prev_places and cur_places and prev_places != cur_places:
                    reason = (f"địa điểm đổi từ '{', '.join(sorted(prev_places))}' "
                              f"sang '{', '.join(sorted(cur_places))}'")
        if reason:
            checks.append(_check(
                "warning", "bridge",
                f"Cảnh {prev_idx} → {cur_idx}: {reason} mà không có "
                "shot cầu nối.",
                f"Thêm 1 shot cầu nối trước cảnh {cur_idx}: "
                f"\"Wide establishing shot chuyển cảnh ({reason}), cùng "
                "nhân vật và ánh sáng với cảnh trước, camera di chuyển "
                "chậm, không thoại.\"",
                scene_index=cur_idx))

    # ── 5. duration (warning): scene dài hơn clip API 10s ──
    for scene in scenes:
        try:
            duration = int(scene.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if duration > API_CLIP_SECONDS:
            checks.append(_check(
                "warning", "duration",
                f"Cảnh {scene.get('index')} dài {duration}s, vượt clip "
                f"API {API_CLIP_SECONDS}s — clip sẽ bị loop/cắt khi ghép.",
                f"Tách cảnh {scene.get('index')} thành 2 cảnh ≤ "
                f"{API_CLIP_SECONDS}s, hoặc rút ngắn thoại/hành động.",
                scene_index=scene.get("index")))

    errors = sum(1 for c in checks if c["level"] == "error")
    warnings = sum(1 for c in checks if c["level"] == "warning")
    return {
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "passed": errors == 0,
    }
