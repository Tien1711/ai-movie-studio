# -*- coding: utf-8 -*-
"""Kiểm thử chất lượng video chuẩn Reels/TikTok/YouTube Shorts (fix 2026-11-23).

Gốc lỗi (QC thật video bv_c4bd5f86 "Ăn Khế Trả Vàng — Chương 1", 72s, 9:16):
1. Phụ đề 1 cue/cảnh = toàn bộ voiceover → khối 7–9 dòng, che 40–50% khung hình.
2. Chim thần vẽ không nhất quán (lúc phượng hoàng vàng, lúc diệc xám).
3. Hook "Chỉ trong 60 giây" trong khi video dài 72s.
4. Ảnh AI tự bịa thêm người lạ (bóng phụ nữ áo trắng 0:33).
"""
import re

import pytest

from composer import create_srt
from story_engine import (
    MAGIC_BIRD_BLOCK,
    _HOOK_PREFIXES,
    _NO_EXTRA_SUBJECTS,
    _scene_prompt,
    adapt_chapter,
)


# ── Phụ trợ parse SRT ────────────────────────────────────────────────────────

def _parse_srt(path):
    """Trả về list (index, start_s, end_s, text) từ file .srt."""
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return []
    cues = []
    for block in raw.split("\n\n"):
        lines = block.strip().split("\n")
        idx = int(lines[0])
        start, end = lines[1].split(" --> ")
        cues.append((idx, _ts(start), _ts(end), "\n".join(lines[2:])))
    return cues


def _ts(ts):
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _norm(text):
    return " ".join(text.split())


_LONG_VOICEOVER = (
    "Ngày xưa, có hai anh em sống trong một làng quê yên bình bên dòng sông. "
    "Người em hiền lành được cha mẹ để lại một cây khế ngọt trĩu quả. "
    "Một hôm, chim thần bay đến ăn khế, bộ lông vàng rực rỡ sáng cả một góc trời. "
    "Chim thần cất tiếng nói sẽ chở người em ra đảo vàng để trả ơn, "
    "rồi sải cánh bay vút qua biển rộng mênh mông."
)


# ── 1. Phụ đề chia nhỏ ──────────────────────────────────────────────────────

def test_srt_long_voiceover_splits_into_short_cues(tmp_path):
    srt = tmp_path / "long.srt"
    create_srt(
        [{"voiceover": _LONG_VOICEOVER, "duration": 10.0}],
        str(srt), clip_durations=[10.0], timeline_offsets=[0.0],
    )
    cues = _parse_srt(srt)
    assert len(cues) >= 3, cues  # ~330 ký tự phải tách thành nhiều cue
    for idx, start, end, text in cues:
        lines = text.split("\n")
        assert len(lines) <= 2, f"cue {idx} có {len(lines)} dòng: {text!r}"
        for line in lines:
            assert len(line) <= 42, f"cue {idx} dòng dài {len(line)}: {line!r}"
        assert len(text.replace("\n", "")) <= 84
        assert end > start, f"cue {idx} rỗng"
    # Phủ kín cửa sổ speech [0, 10], đơn điệu tăng, không chồng lấn.
    assert cues[0][1] == pytest.approx(0.0)
    assert cues[-1][2] == pytest.approx(10.0)
    for (_, _, end_a, _), (_, start_b, _, _) in zip(cues, cues[1:]):
        assert end_a == pytest.approx(start_b), "cue chồng lấn hoặc hở khoảng"
    # Không mất chữ, không đổi chữ.
    joined = " ".join(t.replace("\n", " ") for _, _, _, t in cues)
    assert _norm(joined) == _norm(_LONG_VOICEOVER)
    # Thời gian chia tỉ lệ theo số ký tự (sai số làm tròn mili-giây).
    total = sum(len(t.replace("\n", "")) for _, _, _, t in cues)
    for _, start, end, text in cues:
        expect = 10.0 * len(text.replace("\n", "")) / total
        assert (end - start) == pytest.approx(expect, abs=0.01)


def test_srt_short_voiceover_stays_single_cue(tmp_path):
    srt = tmp_path / "short.srt"
    create_srt(
        [{"voiceover": "Cảnh một bắt đầu", "duration": 5.0}],
        str(srt), clip_durations=[5.0], timeline_offsets=[0.0],
    )
    cues = _parse_srt(srt)
    assert len(cues) == 1
    assert cues[0][1] == pytest.approx(0.0)
    assert cues[0][2] == pytest.approx(5.0)
    assert cues[0][3] == "Cảnh một bắt đầu"


def test_srt_multi_scene_offsets_preserved(tmp_path):
    srt = tmp_path / "multi.srt"
    entries = [
        {"voiceover": "Cảnh một bắt đầu", "duration": 5.0},
        {"voiceover": "Cảnh hai tiếp nối", "duration": 5.0},
        {"voiceover": "Cảnh ba kết thúc", "duration": 5.0},
    ]
    create_srt(entries, str(srt), clip_durations=[5.0, 5.0, 5.0],
               timeline_offsets=[0.0, 4.2, 8.4])
    cues = _parse_srt(srt)
    assert len(cues) == 3
    assert cues[0][1] == pytest.approx(0.0)
    assert cues[1][1] == pytest.approx(4.2)
    assert cues[2][1] == pytest.approx(8.4)
    assert cues[2][2] == pytest.approx(13.4)


def test_srt_hard_break_opens_new_cue(tmp_path):
    srt = tmp_path / "break.srt"
    create_srt(
        [{"voiceover": "Ngày xưa, ở một làng quê yên bình. | Chim thần bay về đậu trên cây khế.",
          "duration": 6.0}],
        str(srt), clip_durations=[6.0], timeline_offsets=[0.0],
    )
    cues = _parse_srt(srt)
    assert len(cues) == 2
    assert "làng quê" in cues[0][3] and "cây khế" not in cues[0][3]
    assert "cây khế" in cues[1][3] and "làng quê" not in cues[1][3]


def test_srt_empty_voiceover_skipped(tmp_path):
    srt = tmp_path / "empty.srt"
    create_srt([{"voiceover": "", "duration": 5.0}], str(srt),
               clip_durations=[5.0], timeline_offsets=[0.0])
    assert _parse_srt(srt) == []


# ── 2. Khóa chim thần ───────────────────────────────────────────────────────

_BIRD_CHAPTER = (
    "Ngày xưa, có hai anh em sống trong một làng quê yên bình. "
    "Người em hiền lành được cha mẹ để lại một cây khế ngọt. "
    "Một hôm, chim thần bay đến ăn khế, lông vàng rực rỡ. "
    "Chim thần nói sẽ chở người em ra đảo vàng để trả ơn. "
    "Người em leo lên lưng chim thần, bay qua biển rộng mênh mông. "
    "Chim thần đáp xuống đảo vàng, khắp nơi lấp lánh ánh vàng. "
    "Người em nhặt đầy một túi vàng rồi chim thần chở về nhà. "
    "Từ đó, hai anh em sống sung túc bên nhau mãi mãi."
)


def test_bird_block_locked_in_every_bird_scene():
    script = adapt_chapter(_BIRD_CHAPTER, "short", 5, "cinematic")
    bird_scenes = [sc for sc in script["scenes"] if "phoenix" in sc["prompt"]]
    assert len(bird_scenes) >= 3, "phải có ít nhất 3 cảnh có chim"
    for sc in bird_scenes:
        # Cùng một block nguyên văn ở mọi cảnh có chim.
        assert MAGIC_BIRD_BLOCK in sc["prompt"], sc["prompt"]
        # Không còn từ "bird" chung chung nào ngoài block.
        assert sc["prompt"].count("bird") == MAGIC_BIRD_BLOCK.count("bird")
    # Cảnh không có chim thì không dính block.
    no_bird = [sc for sc in script["scenes"] if "phoenix" not in sc["prompt"]]
    assert no_bird, "phải có cảnh không chim để đối chứng"
    for sc in no_bird:
        assert MAGIC_BIRD_BLOCK not in sc["prompt"]


@pytest.mark.parametrize("text", [
    "Chim thần bay vút lên trời cao, lông vàng rực rỡ.",
    "Phượng hoàng sải cánh bay qua biển rộng mênh mông.",
])
def test_scene_prompt_bird_mentions_use_block(text):
    prompt = _scene_prompt(text, "cinematic", "9:16")
    assert MAGIC_BIRD_BLOCK in prompt


def test_scene_prompt_plain_bird_keeps_bird_not_phoenix():
    # Audit #6 (2026-11-25): "chim la" khong co ngu canh chim than thi
    # GIU keyword "bird" chung — truoc day moi chim deu thanh phuong
    # hoang vang (vd chim se cung thanh phuong). Truong hop "Chim la..."
    # tung nam trong parametrize tren (PR #21) nhung do la luat qua rong
    # ma audit da chi ra; chuong An Khe that van dung "chim than" nen
    # test_bird_block_locked_in_every_bird_scene van xanh.
    prompt = _scene_prompt("Chim lạ đậu trên cành khế vàng trĩu quả.",
                           "cinematic", "9:16")
    assert MAGIC_BIRD_BLOCK not in prompt
    assert "bird" in prompt


def test_scene_prompt_without_bird_has_no_block():
    prompt = _scene_prompt("Người em ra đồng gặt lúa vàng.", "cinematic", "9:16")
    assert MAGIC_BIRD_BLOCK not in prompt
    assert "phoenix" not in prompt


# ── 3. Hook không tuyên bố sai thời lượng ────────────────────────────────────

def test_hook_prefixes_have_no_duration_claim():
    for prefix in _HOOK_PREFIXES:
        assert "60 giây" not in prefix
        assert not re.search(r"\d+\s*(giây|phút)", prefix), prefix


def test_short_mode_first_voiceover_has_no_seconds_claim():
    script = adapt_chapter(_BIRD_CHAPTER, "short", 5, "cinematic")
    first = script["scenes"][0]["voiceover"]
    assert "giây" not in first
    assert "?" in first or "Khoan đã" in first or "không thể rời mắt" in first


# ── 4. Ràng buộc chống bịa thêm người/vật ────────────────────────────────────

def test_scene_prompt_has_no_extra_subjects_guard():
    prompt = _scene_prompt("Chim thần bay về đậu trên cây khế.", "cinematic", "9:16")
    assert _NO_EXTRA_SUBJECTS in prompt
    # Diễn đạt an toàn: không cấm tuyệt đối "người" để khỏi xóa nhân vật hợp lệ.
    assert "no people" not in prompt


def test_adapt_scenes_all_carry_no_extra_subjects_guard():
    script = adapt_chapter(_BIRD_CHAPTER, "short", 5, "cinematic")
    for sc in script["scenes"]:
        assert _NO_EXTRA_SUBJECTS in sc["prompt"], sc["prompt"]


# ── 5. Từ khóa khớp theo từ, không khớp chuỗi con ───────────────────────────
# Gốc lỗi: "ma" bắt cả "may/mang/mãi" → prompt cảnh vô tội bị gắn "ghost"
# (video Ăn Khế ch.1 cảnh 4: prompt có "ghost" → ảnh AI vẽ bóng phụ nữ áo trắng).

def test_keywords_no_ghost_from_may_mang_words():
    from story_engine import _scene_keywords

    assert "ghost" not in _scene_keywords("Người em may mắn mang khế về nhà.")
    assert "ghost" not in _scene_keywords("Chim ăn mãi không hết, mãi vẫn còn.")
    assert "ghost" not in _scene_keywords("Người anh tham lam lấy hết nhà cửa.")


def test_keywords_ghost_only_for_standalone_ma():
    from story_engine import _scene_keywords

    assert "ghost" in _scene_keywords("Đêm khuya, con ma hiện ra trong rừng.")
    assert "ghost" in _scene_keywords("Ngôi nhà có ma, ai cũng sợ.")


def test_keywords_other_words_still_match():
    from story_engine import _scene_keywords

    kws = _scene_keywords("Chim thần bay qua biển, đậu trên khế vàng.")
    assert "bird" in kws
    assert "sea" in kws
    assert "starfruit tree" in kws
    assert "gold" in kws


def test_scene_prompt_no_ghost_for_innocent_scene():
    prompt = _scene_prompt(
        "Người em may mắn mang khế về, chim ăn mãi không hết.", "cinematic", "9:16"
    )
    assert "ghost" not in prompt
