"""
test_perfect_g2_preflight_v2.py — Hồi quy NHÓM 2 / nhiệm vụ 3: preflight
cầu nối v2 (consistency.py).

- So sánh thực thể thời gian/địa điểm trích từ prompt 2 cảnh kề nhau:
  đổi bối cảnh rõ ràng -> cảnh báo bridge + gợi ý cầu nối; cùng bối cảnh
  -> không cảnh báo sai.
- Nhảy ngày ("sáng" -> "sáng hôm sau") vẫn cảnh báo dù cùng buổi.
- Địa danh riêng (Hà Nội -> Huế) cảnh báo; cùng địa danh thì không.
- Tương đồng chuỗi: "rừng trúc" vs "rừng tre" không tính là đổi bối cảnh.
- Fallback v1 còn hoạt động: đổi trong nhà <-> ngoài trời (không có tên
  địa điểm trích được) vẫn cảnh báo.

Chạy: python -m pytest test_perfect_g2_preflight_v2.py -v
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from consistency import run_consistency_checks

LOCK = "Lan: nữ hiệp áo trắng, tóc dài buộc cao."


def _scene(idx, prompt, voiceover="Lan nói."):
    return {"index": idx, "prompt": prompt, "voiceover": voiceover,
            "duration": 10, "voice_segments": []}


def _req(scenes):
    return {
        "title": "Test Bridge v2",
        "voice": "vi-VN-HoaiMyNeural",
        "style_locks": {"character": LOCK},
        "chain_mode": "sequential",
        "consistency_mode": "max",
        "characters": [{"id": "char-lan", "name": "Lan",
                        "aliases": [], "description": LOCK}],
        "scenes": scenes,
    }


def _bridge(res):
    return [c for c in res["checks"] if c["code"] == "bridge"]


# ── Đổi bối cảnh rõ ràng -> cảnh báo ────────────────────────────────────────

def test_v2_clear_place_and_time_change_warns():
    res = run_consistency_checks(_req([
        _scene(1, "Lan đứng trong rừng trúc buổi sáng, chim hót."),
        _scene(2, "Lan đứng trên bãi biển lúc hoàng hôn, sóng vỗ."),
    ]), consistency_mode="max")
    warns = _bridge(res)
    assert len(warns) == 1 and warns[0]["level"] == "warning"
    assert warns[0]["scene_index"] == 2
    assert "cầu nối" in warns[0]["suggestion"]
    assert "Wide establishing shot" in warns[0]["suggestion"]


def test_v2_time_only_change_warns():
    res = run_consistency_checks(_req([
        _scene(1, "Lan ở nhà buổi sáng, pha trà."),
        _scene(2, "Lan ở nhà buổi tối, thắp đèn."),
    ]), consistency_mode="max")
    warns = _bridge(res)
    assert len(warns) == 1
    assert "thời gian" in warns[0]["message"]


def test_v2_day_jump_same_time_of_day_warns():
    # Cùng buổi sáng nhưng sang hôm sau: v1 (chỉ nhóm buổi) bỏ lọt, v2 bắt được.
    res = run_consistency_checks(_req([
        _scene(1, "Lan hái rau trong vườn buổi sáng."),
        _scene(2, "Sáng hôm sau, Lan hái rau trong vườn."),
    ]), consistency_mode="max")
    warns = _bridge(res)
    assert len(warns) == 1
    assert "hôm sau" in warns[0]["message"]


def test_v2_proper_noun_place_change_warns():
    res = run_consistency_checks(_req([
        _scene(1, "Lan ở Hà Nội buổi chiều, uống cà phê."),
        _scene(2, "Lan ở Huế buổi chiều, uống cà phê."),
    ]), consistency_mode="max")
    warns = _bridge(res)
    assert len(warns) == 1
    assert "địa điểm" in warns[0]["message"]


# ── Cùng bối cảnh -> không cảnh báo sai ─────────────────────────────────────

def test_v2_same_place_and_time_no_warning():
    res = run_consistency_checks(_req([
        _scene(1, "Lan đứng trong rừng trúc buổi sáng, chim hót."),
        _scene(2, "Lan đi sâu vào rừng trúc, lá rơi buổi sáng."),
    ]), consistency_mode="max")
    assert _bridge(res) == []


def test_v2_similar_place_names_no_warning():
    # "rừng trúc" vs "rừng tre": tương đồng chuỗi cao -> cùng bối cảnh.
    res = run_consistency_checks(_req([
        _scene(1, "Lan đi trong rừng trúc buổi sáng."),
        _scene(2, "Lan nghỉ dưới rừng tre buổi sáng."),
    ]), consistency_mode="max")
    assert _bridge(res) == []


def test_v2_same_proper_noun_no_warning():
    res = run_consistency_checks(_req([
        _scene(1, "Lan ở Hà Nội buổi chiều, uống cà phê."),
        _scene(2, "Lan ở Hà Nội buổi chiều, ngắm hoàng hôn."),
    ]), consistency_mode="max")
    assert _bridge(res) == []


def test_v2_character_name_is_not_a_place():
    # "Lan" xuất hiện ở cả 2 cảnh nhưng là tên nhân vật, không phải địa điểm.
    res = run_consistency_checks(_req([
        _scene(1, "Lan mỉm cười trong vườn buổi sáng."),
        _scene(2, "Lan gật đầu trong vườn buổi sáng."),
    ]), consistency_mode="max")
    assert _bridge(res) == []


def test_v2_no_time_or_place_info_no_warning():
    res = run_consistency_checks(_req([
        _scene(1, "Lan mỉm cười."),
        _scene(2, "Lan gật đầu."),
    ]), consistency_mode="max")
    assert _bridge(res) == []


# ── Fallback v1 còn hoạt động ───────────────────────────────────────────────

def test_v1_fallback_indoor_outdoor_still_warns():
    # Không có tên địa điểm trích được, chỉ có trong nhà <-> ngoài trời:
    # v2 không kết luận được, fallback v1 phải bắt.
    res = run_consistency_checks(_req([
        _scene(1, "Lan ngồi trong nhà, đọc sách."),
        _scene(2, "Lan chạy ngoài trời, cười vang."),
    ]), consistency_mode="max")
    warns = _bridge(res)
    assert len(warns) == 1
    assert "trong nhà" in warns[0]["message"]


def test_v1_fallback_keyword_place_change_still_warns():
    res = run_consistency_checks(_req([
        _scene(1, "Lan đi trong rừng."),
        _scene(2, "Lan bơi ngoài biển."),
    ]), consistency_mode="max")
    assert len(_bridge(res)) == 1
