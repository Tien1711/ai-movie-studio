# -*- coding: utf-8 -*-
"""
test_audit_2026_11_25_behaviour.py — Behaviour tests for audit #3 (2026-11-25).

Compliance checker gaps, verified live at main 4d8a563 (2026-11-25):
  check_affiliate_compliance() returns [] for all six violating samples
  below, although each one breaks the hard affiliate rule #1 (no false
  cure claims, no absolute guarantees, no unrealistic results, no fake
  discounts/scarcity — TikTok Shop / Shopee).

These tests are RED at 4d8a563 by design: they prove the bug before the
fix and prove the fix after it. They must NOT be weakened to pass by
editing the samples — fix the checker patterns instead.

Scope: only the checker + rewrite round-trip (audit #3). SSRF (#1/#2),
job lifecycle (#4–#6), TTS/Shot Desk (#7–#10) and MEDIUM items get their
own behaviour tests with their fixes.
"""
import affiliate_engine


def _levels(text):
    return [f["level"] for f in affiliate_engine.check_affiliate_compliance(text)]


def _has(text, level):
    return level in _levels(text)


# ── RED at 4d8a563: checker misses these violations ─────────────────────────

def test_cure_claim_khoi_hoan_toan():
    # "khỏi hoàn toàn sau 7 ngày" — tuyên bố khỏi bệnh + mốc thời gian.
    assert _has("Uống sữa này khỏi hoàn toàn sau 7 ngày", "severe")


def test_cure_claim_tri_dut_diem():
    # "trị dứt điểm mụn" — tuyên bố trị dứt điểm, không pattern nào khớp.
    assert _has("Kem này trị dứt điểm mụn chỉ sau 1 tuần", "severe")


def test_absolute_guarantee_cam_ket_hieu_qua_100():
    # "cam kết hiệu quả 100%" — cam kết tuyệt đối; checker hiện trả [].
    assert _has("Sản phẩm cam kết hiệu quả 100%", "severe")


def test_unrealistic_result_giam_can_tuan():
    # "giảm 5kg trong 1 tuần" — kết quả phi thực tế; pattern hiện tại chỉ
    # khớp đơn vị "ngày" (giảm 5kg trong 7 ngày thì bắt được).
    assert _has("Giảm 5kg trong 1 tuần nhờ sữa này", "severe")


def test_fake_discount_sale_50():
    # "sale 50%" — mức giảm giá; pattern light hiện tại đòi chữ "giảm".
    # (Câu test cố tình không kèm từ vi phạm khác để khỏi bắt nhầm.)
    assert _has("Sale 50% toàn cửa hàng", "light")


def test_fake_promo_mua_1_tang_1():
    # "mua 1 tặng 1" — khuyến mãi bundle; không pattern nào khớp.
    # (Câu test cố tình không kèm từ vi phạm khác để khỏi bắt nhầm.)
    assert _has("Mua 1 tặng 1 áp dụng cho mọi đơn", "light")


def test_rewrite_must_not_leave_absolute_guarantee():
    # Audit #3, điểm nguy hiểm nhất: rewrite("cam kết hiệu quả 100%")
    # hiện cho ra "cam kết hiệu quả" — vẫn là cam kết tuyệt đối — mà
    # checker chấm là sạch. Sau khi vá, hoặc rewrite phải bỏ luôn cụm
    # "cam kết hiệu quả", hoặc checker phải bắt cụm đó.
    out, _changes = affiliate_engine.rewrite_affiliate_compliance(
        "Sữa này cam kết hiệu quả 100%"
    )
    assert "cam kết hiệu quả" not in out.lower()
    assert affiliate_engine.check_affiliate_compliance(out) == []


# ── GREEN at 4d8a563: guardrails — must keep passing after the fix ─────────

def test_still_catches_chua_khoi():
    assert _has("chữa khỏi suy dinh dưỡng", "severe")


def test_still_catches_giam_can_ngay():
    assert _has("Giảm 5kg trong 7 ngày", "severe")


def test_still_catches_absolute_superlative():
    assert _has("tốt nhất thị trường", "light")


def test_clean_support_claim_stays_clean():
    # Câu tuân thủ mẫu cho Ensure: chỉ "hỗ trợ", không chữa/khỏi/cam kết.
    assert affiliate_engine.check_affiliate_compliance(
        "Ensure Gold hỗ trợ dinh dưỡng cho người lớn tuổi"
    ) == []


def test_rewrite_safety_net_still_strips_leftover_100():
    # Lưới an toàn PR #26: "hỗ trợ ... 100%" còn sót sau viết lại phải mất "100%".
    out, _changes = affiliate_engine.rewrite_affiliate_compliance(
        "Sữa hỗ trợ sức khỏe 100% cho cả nhà"
    )
    assert "100%" not in out


# ── Audit #3 — pattern mới + re-check sau rewrite (fix nhóm B) ──────────────

def test_checker_new_severe_patterns():
    # "thần dược", "trắng da sau N ngày", "tăng N kg trong N tháng".
    assert _has("Thuốc thần dược trị bách bệnh", "severe")
    assert _has("Trắng da sau 7 ngày dùng kem này", "severe")
    assert _has("Tăng 3kg trong 1 tháng nhờ sữa", "severe")
    assert _has("Hết mụn sau 7 ngày dùng serum", "severe")


def test_checker_new_light_patterns():
    assert _has("Only 3 left, nhanh tay lên!", "light")
    assert _has("Limited time offer, mua ngay!", "light")
    assert _has("The best milk in the world!", "light")


def test_rewrite_cap_toc_scoped():
    # "giảm cân cấp tốc" -> nặng + viết lại giữ động từ; "giao hàng cấp tốc"
    # (vận chuyển, không phải kết quả) -> không bị bắt oan.
    assert _has("Giảm cân cấp tốc với sữa này", "severe")
    out, _ = affiliate_engine.rewrite_affiliate_compliance(
        "Giảm cân cấp tốc với sữa này")
    assert "cấp tốc" not in out.lower() and "giảm cân dần dần" in out.lower()
    assert affiliate_engine.check_affiliate_compliance(
        "Giao hàng cấp tốc trong 2 giờ") == []


def test_rewrite_no_leftover_100_percent_prefix():
    # "100%" đứng TRƯỚC cụm hỗ trợ: "100% cam kết hiệu quả" -> sau viết lại
    # không được còn trơ "100% hỗ trợ hiệu quả".
    out, _ = affiliate_engine.rewrite_affiliate_compliance(
        "100% cam kết hiệu quả, dùng ngay!")
    assert "100%" not in out
    assert affiliate_engine.check_affiliate_compliance(out) == []


def test_rewrite_recheck_warns_when_violation_remains(monkeypatch):
    # Cơ chế fail-closed: nếu sau viết lại vẫn còn vi phạm nặng (vd pattern
    # mới thêm vào checker nhưng rewrite chưa xử lý hết), rewrite phải ghi
    # cảnh báo vào changes thay vì im lặng cho qua. Giả lập bằng monkeypatch
    # checker để ép một vi phạm còn sót sau viết lại.
    real_check = affiliate_engine.check_affiliate_compliance

    def _fake_check(text):
        found = real_check(text)
        if "ZZZMARK" in text:
            found = found + [{"level": "severe", "category": "vi phạm giả lập",
                              "matched": "ZZZMARK", "message": "vi phạm giả lập"}]
        return found

    monkeypatch.setattr(affiliate_engine, "check_affiliate_compliance", _fake_check)
    out, changes = affiliate_engine.rewrite_affiliate_compliance(
        "Sữa ZZZMARK này tốt.")
    assert any(c.get("remaining_violation") for c in changes)
    # Chỉ cảnh báo, không tự ý xóa câu — để người dùng sửa tay.
    assert "ZZZMARK" in out


def test_normalize_warns_remaining_violation_separately():
    # _normalize: entry remaining_violation thành warning [CẢNH BÁO] riêng,
    # không ghi nhầm thành "đã viết lại ... -> ''".
    import affiliate_engine as ae
    real_rewrite = ae.rewrite_affiliate_compliance

    def _fake_rewrite(text):
        out, changes = real_rewrite(text)
        if "ZZZMARK" in text:
            changes = changes + [{"matched": "ZZZMARK", "replacement": "",
                                  "category": "vi phạm giả lập",
                                  "remaining_violation": True}]
        return out, changes

    ae.rewrite_affiliate_compliance = _fake_rewrite
    try:
        scenes = [ae.AffiliateScene(
            index=1, title="C1", role="demo", duration=10, prompt="Demo shot.",
            voiceover="Sữa ZZZMARK này tốt.")]
        _, warnings = ae._normalize_affiliate_script(
            scenes, ae.ProductAffiliateRequest(
                product_name="Sữa test", category="health", framework="pas"))
    finally:
        ae.rewrite_affiliate_compliance = real_rewrite
    assert any("[CẢNH BÁO]" in w and "ZZZMARK" in w for w in warnings)
    assert not any("ZZZMARK' → ''" in w for w in warnings)


# ── Audit #15 — DNA/Fidelity kiểm nội dung khối thật (fix nhóm B) ───────────

def _req_for_fidelity():
    return affiliate_engine.ProductAffiliateRequest(
        product_name="Giá đỡ điện thoại xoay 360 hợp kim nhôm",
        category="gadgets", framework="pas",
        shooting_style="hands", koc_persona="nam_bua_hai")


def test_fidelity_block_complete_verbatim():
    # Khối chuẩn nguyên văn -> đủ, không thiếu câu nào.
    assert affiliate_engine.fidelity_block_complete(
        affiliate_engine.FIDELITY_BLOCK)
    assert affiliate_engine.fidelity_block_missing_sentences(
        affiliate_engine.FIDELITY_BLOCK) == []


def test_fidelity_partial_block_missing_sentences():
    # Chỉ có marker + 1 câu phủ định -> thiếu các câu bắt buộc còn lại.
    partial = ("[PRODUCT FIDELITY LOCK] Do NOT redraw the product.")
    missing = affiliate_engine.fidelity_block_missing_sentences(partial)
    assert "100% identical" in missing
    assert "never invent or imagine what is inside" in missing
    assert not affiliate_engine.fidelity_block_complete(partial)


def test_fidelity_partial_block_gets_full_block_appended():
    # Prompt chỉ có block cắt ngắn -> _normalize append lại khối chuẩn đầy đủ.
    scenes = [affiliate_engine.AffiliateScene(
        index=1, title="C1", role="hook", duration=10,
        prompt="Hook shot. [PRODUCT FIDELITY LOCK] Do NOT redraw the product.",
        voiceover="Dừng lại!")]
    out, _ = affiliate_engine._normalize_affiliate_script(scenes, _req_for_fidelity())
    assert affiliate_engine.FIDELITY_BLOCK in out[0].prompt
    assert affiliate_engine.fidelity_block_complete(out[0].prompt)


def test_fidelity_paraphrased_block_not_flagged_but_completed():
    # Block viết lại ("Do NOT redesign, recolor or restyle...") KHÔNG bị gắn
    # cờ fidelity_risk oan — nhưng vì thiếu câu then chốt của khối chuẩn nên
    # _normalize vẫn append khối chuẩn đầy đủ (fail-closed).
    paraphrase = ("Do NOT redesign, recolor or restyle the product in any way. "
                  "Do NOT change its shape or size.")
    assert affiliate_engine.check_prompt_fidelity(paraphrase) == []
    scenes = [affiliate_engine.AffiliateScene(
        index=1, title="C1", role="hook", duration=10,
        prompt=f"Hook shot. {paraphrase}", voiceover="Dừng lại!")]
    out, warnings = affiliate_engine._normalize_affiliate_script(
        scenes, _req_for_fidelity())
    assert affiliate_engine.FIDELITY_BLOCK in out[0].prompt
    # Không có warning "đổi sản phẩm" nào cho khối phủ định viết lại.
    assert not any("đổi sản phẩm" in w for w in warnings)


def test_fidelity_negated_variants_not_flagged():
    # Các biến thể phủ định do not / don't / never + từ cấm -> sạch.
    for text in ("Never redesign the product.",
                 "Don't recolor the packaging.",
                 "Do not restyle the label in any way."):
        assert affiliate_engine.check_prompt_fidelity(text) == [], text


def test_fidelity_positive_risk_still_flagged():
    # Cụm khẳng định vẫn bị gắn cờ; trong câu hỗn hợp, chỉ phần khẳng định
    # bị gắn cờ, phần phủ định thì không.
    assert "redesign" in affiliate_engine.check_prompt_fidelity(
        "Show the product, redesign it in a different color.")
    mixed = affiliate_engine.check_prompt_fidelity(
        "Do NOT redesign the product, but give it a new look.")
    assert "redesign" not in mixed
    assert "new look" in mixed


def test_fidelity_full_block_not_duplicated():
    # Prompt đã có khối chuẩn nguyên văn -> _normalize không append lần 2.
    scenes = [affiliate_engine.AffiliateScene(
        index=1, title="C1", role="hook", duration=10,
        prompt=f"Hook shot. {affiliate_engine.FIDELITY_BLOCK}",
        voiceover="Dừng lại!")]
    out, _ = affiliate_engine._normalize_affiliate_script(scenes, _req_for_fidelity())
    assert out[0].prompt.count(affiliate_engine.FIDELITY_MARKER) == 1
