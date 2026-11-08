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
    # Heuristic từ khóa, chưa hiểu ngữ nghĩa sâu: chỉ cảnh báo khi phát hiện
    # rõ ràng (đổi nhóm thời gian, đổi trong/ngoài nhà, hoặc đổi địa điểm).
    ordered = sorted(scenes, key=lambda s: s.get("index", 0))
    for prev, cur in zip(ordered, ordered[1:]):
        prev_text, cur_text = _scene_text(prev), _scene_text(cur)
        prev_idx, cur_idx = prev.get("index"), cur.get("index")
        reason = ""
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
