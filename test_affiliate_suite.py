"""
test_affiliate_suite.py – Kiểm tra toàn diện module Tiếp Thị Liên Kết (Affiliate)
Bao gồm tính năng Kiểu quay (Shooting Styles):
1. Người mẫu không lộ mặt (faceless)
2. Chỉ quay tay (hands)
3. Chỉ quay sản phẩm (product)
4. KOC lộ mặt đầy đủ (koc_face)
"""

import asyncio
from fastapi.testclient import TestClient
from app import app, JOBS
from affiliate_engine import (
    AFFILIATE_CATEGORIES,
    AFFILIATE_FRAMEWORKS,
    AFFILIATE_SHOOTING_STYLES,
    ProductAffiliateRequest,
    generate_affiliate_script_local,
    build_commercial_prompt
)

client = TestClient(app)

def test_templates_catalog():
    """Kiểm tra danh mục sản phẩm, framework kịch bản và kiểu quay (shooting styles)."""
    resp = client.get("/api/affiliate/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    assert "frameworks" in data
    assert "koc_personas" in data
    assert "shooting_styles" in data

    # Kiểm tra đầy đủ 4 kiểu quay theo yêu cầu
    styles = data["shooting_styles"]
    assert "faceless" in styles, "Thiếu kiểu quay: Người mẫu không lộ mặt"
    assert "hands" in styles, "Thiếu kiểu quay: Chỉ quay tay"
    assert "product" in styles, "Thiếu kiểu quay: Chỉ quay sản phẩm"
    assert "koc_face" in styles, "Thiếu kiểu quay: KOC lộ mặt"

    assert "cổ trở xuống" in styles["faceless"]["desc"] or "phía sau" in styles["faceless"]["desc"]
    assert "Chỉ quay tay" in styles["hands"]["name"]
    assert "Không có người" in styles["product"]["desc"]
    print("✅ test_templates_catalog PASSED (Categories + Frameworks + KOC + 4 Shooting Styles)")


def test_shooting_styles_prompts():
    """Kiểm tra logic sinh prompt cho từng kiểu quay (Shooting Styles)."""
    # 1. Người mẫu không lộ mặt (faceless)
    p_faceless = build_commercial_prompt("hook", "Áo thun Oversize", "fashion", "vải cotton thoáng mát", shooting_style="faceless")
    assert "no face" in p_faceless.lower() or "neck down" in p_faceless.lower(), f"Lỗi prompt faceless: {p_faceless}"
    assert "chest down" in p_faceless.lower() or "neck down" in p_faceless.lower()

    # 2. Chỉ quay tay (hands)
    p_hands = build_commercial_prompt("demo", "Giá đỡ điện thoại", "gadgets", "xoay 360 độ", shooting_style="hands")
    assert "hands" in p_hands.lower(), f"Lỗi prompt hands: {p_hands}"
    assert "no face" in p_hands.lower() or "zero face" in p_hands.lower() or "hands only" in p_hands.lower()

    # 3. Chỉ quay sản phẩm (product)
    p_product = build_commercial_prompt("hook", "Nồi chiên không dầu", "home", "giảm 90% dầu mỡ", shooting_style="product")
    assert "no people" in p_product.lower() or "zero humans" in p_product.lower(), f"Lỗi prompt product: {p_product}"
    assert "no hands" in p_product.lower() or "turntable" in p_product.lower()

    # 4. KOC lộ mặt (koc_face)
    p_koc = build_commercial_prompt("hook", "Sạc dự phòng", "gadgets", "sạc nhanh 65W", koc_persona_id="nam_bua_hai", shooting_style="koc_face")
    assert "charismatic" in p_koc.lower() or "vietnamese" in p_koc.lower(), f"Lỗi prompt koc_face: {p_koc}"

    print("✅ test_shooting_styles_prompts PASSED (Faceless + Hands + Product + KOC Face)")


def test_local_script_generator_with_styles():
    """Kiểm tra sinh kịch bản Tiếp thị cục bộ áp dụng kiểu quay."""
    # Test sinh kịch bản chỉ quay sản phẩm (product only)
    req_prod = ProductAffiliateRequest(
        product_name="Đèn LED chống cận",
        category="gadgets",
        framework="pas",
        shooting_style="product",
        price_sale="199.000đ",
        aspect_ratio="9:16"
    )
    res_prod = generate_affiliate_script_local(req_prod)
    assert len(res_prod.scenes) == 5
    for s in res_prod.scenes:
        assert "no people" in s.prompt.lower() or "zero humans" in s.prompt.lower() or "product" in s.prompt.lower()

    # Test sinh kịch bản chỉ quay tay (hands only)
    req_hands = ProductAffiliateRequest(
        product_name="Son dưỡng môi Collagen",
        category="beauty",
        framework="review_koc",
        shooting_style="hands",
        price_sale="99.000đ",
        aspect_ratio="9:16"
    )
    res_hands = generate_affiliate_script_local(req_hands)
    assert len(res_hands.scenes) == 5
    for s in res_hands.scenes:
        assert "hands" in s.prompt.lower()

    print("✅ test_local_script_generator_with_styles PASSED")


def test_local_script_generator_hai_bua():
    """Kiểm tra sinh kịch bản Hài Bựa (⭐ Adam bựa)."""
    req = ProductAffiliateRequest(
        product_name="Nồi chiên không dầu Lock&Lock",
        category="home",
        framework="hai_bua",
        tone="hai_bua",
        shooting_style="koc_face",
        price_sale="899.000đ",
        promotion_deal="Freeship 0đ và tặng kẹp gắp inox",
        pain_points="Dầu mỡ bắn tung tóe, lau dọn bếp mệt đứt hơi",
        key_features="Dung tích 8L kính nhìn xuyên thấu, giảm 90% dầu mỡ",
        voice="vieneu:AdamBua",
        aspect_ratio="9:16"
    )
    res = generate_affiliate_script_local(req)
    assert len(res.scenes) == 5
    assert res.recommended_voice == "vieneu:AdamBua"
    assert res.aspect_ratio == "9:16"
    assert "Lock&Lock" in res.title or "Nồi chiên" in res.title
    roles = [s.role for s in res.scenes]
    assert roles == ["hook", "pain", "demo", "proof", "cta"]
    assert "giỏ hàng" in res.scenes[4].voiceover.lower()
    assert "góc trái" in res.scenes[4].voiceover.lower()
    print("✅ test_local_script_generator_hai_bua PASSED")


def test_api_generate_script_endpoint():
    """Kiểm tra endpoint /api/affiliate/generate-script qua HTTP với shooting_style."""
    payload = {
        "product_name": "Tai nghe Bluetooth chống ồn",
        "category": "gadgets",
        "framework": "pas",
        "shooting_style": "faceless",
        "voice": "vieneu:AdamBua",
        "aspect_ratio": "9:16"
    }
    resp = client.post("/api/affiliate/generate-script", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "title" in data
    assert "scenes" in data
    assert len(data["scenes"]) == 5
    # Cảnh 1 phải là faceless
    assert "faceless" in data["scenes"][0]["prompt"].lower() or "neck down" in data["scenes"][0]["prompt"].lower()
    print("✅ test_api_generate_script_endpoint PASSED (HTTP faceless shooting style)")


def test_api_render_affiliate_endpoint():
    """Kiểm tra endpoint /api/affiliate/render nạp job thành công."""
    scenes = [
        {"prompt": "Commercial hook shot, 9:16 vertical, 8k", "voiceover": "Xem ngay kẻo lỡ!", "duration": 4},
        {"prompt": "Product demo shot, 9:16 vertical, 8k", "voiceover": "Cực kỳ tiện lợi luôn nha cả nhà.", "duration": 5},
        {"prompt": "Call to action shot, 9:16 vertical, 8k", "voiceover": "Bấm ngay giỏ hàng góc trái nhé!", "duration": 4}
    ]
    payload = {
        "title": "Test Affiliate Video TikTok",
        "voice": "vieneu:AdamBua",
        "aspect_ratio": "9:16",
        "scenes": scenes
    }
    resp = client.post("/api/affiliate/render", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    job_id = data["job_id"]
    assert job_id in JOBS
    job = JOBS[job_id]
    assert job["req"].aspect_ratio == "9:16"
    assert len(job["req"].scenes) == 3
    print(f"✅ test_api_render_affiliate_endpoint PASSED (Job ID: {job_id})")


if __name__ == "__main__":
    test_templates_catalog()
    test_shooting_styles_prompts()
    test_local_script_generator_with_styles()
    test_local_script_generator_hai_bua()
    test_api_generate_script_endpoint()
    test_api_render_affiliate_endpoint()
    print("\n🎉 TẤT CẢ 6/6 TESTS AFFILIATE MARKETING (BAO GỒM KIỂU QUAY) ĐÃ PASS 100%!")
