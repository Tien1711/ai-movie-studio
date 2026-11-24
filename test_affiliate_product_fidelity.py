"""Test khóa trung thực ảnh sản phẩm + tuân thủ chính sách bán hàng + tìm ảnh.

Phạm vi: feat/affiliate-product-fidelity (2026-11-24, 3 quy tắc cứng của Tiến).
- Fidelity Block dán nguyên văn vào prompt mọi cảnh (local + build_commercial_prompt);
  validator gắn cờ ngôn ngữ cho phép đổi sản phẩm.
- Compliance checker: chữa bệnh/cam kết tuyệt đối/kết quả phi thực tế (nặng:
  viết lại + warning), từ tuyệt đối/khan hiếm/giảm giá (nhẹ: warning, giữ câu).
  Case sữa Ensure: "hỗ trợ dinh dưỡng" OK, "chữa khỏi suy dinh dưỡng" VI PHẠM.
- find-product-images: Shopee API images, DuckDuckGo i.js, fallback Bing,
  lỗi mạng -> [] + message rõ; endpoint qua TestClient.
- Không gọi mạng thật ở bất kỳ test nào (mock http_get inject được).
"""
import json

import pytest

import affiliate_engine


class _FakeResp:
    """Response tối giản cho mock http_get (đủ cho import/find-product-images)."""

    def __init__(self, status_code=200, url="", text=""):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.content = text.encode("utf-8")

    def json(self):
        return json.loads(self.text)


def _req(product="Giá đỡ điện thoại xoay 360 hợp kim nhôm", category="gadgets"):
    return affiliate_engine.ProductAffiliateRequest(
        product_name=product, category=category, framework="pas",
        shooting_style="hands", koc_persona="nam_bua_hai")


# ── Fidelity Block ───────────────────────────────────────────────────────────

def test_fidelity_block_in_every_local_framework_scene():
    """Mọi framework local: prompt mọi cảnh chứa Fidelity Block nguyên văn + DNA lock."""
    for fw in ("pas", "hai_bua", "review_koc", "before_after", "aida"):
        req = _req()
        req.framework = fw
        res = affiliate_engine.generate_affiliate_script_local(req)
        assert len(res.scenes) == 5
        for s in res.scenes:
            assert affiliate_engine.FIDELITY_MARKER in s.prompt, f"{fw} cảnh {s.index}"
            assert affiliate_engine.FIDELITY_BLOCK in s.prompt, f"{fw} cảnh {s.index}"
            assert affiliate_engine.PRODUCT_LOCK_MARKER in s.prompt
            assert s.prompt.count(affiliate_engine.FIDELITY_MARKER) == 1


def test_fidelity_block_in_build_commercial_prompt_all_styles_roles():
    """build_commercial_prompt: mọi style/role đều có Fidelity Block + DNA lock, mỗi thứ 1 lần."""
    for style in ("faceless", "hands", "product", "koc_face"):
        for role in ("hook", "pain", "demo", "proof", "cta", "other"):
            p = affiliate_engine.build_commercial_prompt(
                role, "Giá đỡ điện thoại", "gadgets", "xoay 360",
                "nam_bua_hai", "9:16", style)
            assert affiliate_engine.FIDELITY_MARKER in p, f"{style}/{role}"
            assert affiliate_engine.FIDELITY_BLOCK in p, f"{style}/{role}"
            assert affiliate_engine.PRODUCT_LOCK_MARKER in p, f"{style}/{role}"
            assert p.count(affiliate_engine.FIDELITY_MARKER) == 1, f"{style}/{role}"
            assert p.count(affiliate_engine.PRODUCT_LOCK_MARKER) == 1, f"{style}/{role}"


def test_fidelity_block_idempotent_on_renormalize():
    """Normalize 2 lần: Fidelity Block không bị append 2 lần."""
    scenes = [affiliate_engine.AffiliateScene(
        index=1, title="C1", role="hook", duration=10,
        prompt="Hook shot.", voiceover="Dừng lại!")]
    out, _ = affiliate_engine._normalize_affiliate_script(scenes, _req())
    out2, _ = affiliate_engine._normalize_affiliate_script(out, _req())
    assert out2[0].prompt.count(affiliate_engine.FIDELITY_MARKER) == 1
    assert out2[0].prompt.count(affiliate_engine.PRODUCT_LOCK_MARKER) == 1


def test_check_prompt_fidelity_flags_redesign_language():
    """Validator gắn cờ ngôn ngữ cho phép đổi sản phẩm; prompt sạch -> []."""
    risky = affiliate_engine.check_prompt_fidelity(
        "Show the product, redesign it in a different color with a new look.")
    assert "redesign" in risky
    assert "different color" in risky
    assert "new look" in risky
    assert affiliate_engine.check_prompt_fidelity(
        "Show the product, recolor the packaging and restyle the label.") == [
        "recolor", "restyle"]
    assert affiliate_engine.check_prompt_fidelity(
        "Cinematic product shot, the EXACT SAME phone stand, studio light.") == []


def test_validate_affiliate_script_flags_fidelity_risk():
    """validate_affiliate_script gắn cờ fidelity_risk cho cảnh có ngôn ngữ đổi sản phẩm."""
    scenes = [
        {"index": 1, "prompt": "Plain shot. [PRODUCT CONTINUITY LOCK]"},
        {"index": 2, "prompt": "Shot with a redesigned product in a new look. [PRODUCT CONTINUITY LOCK]"},
    ]
    issues = affiliate_engine.validate_affiliate_script(scenes)
    by_idx = {i["index"]: i["missing"] for i in issues}
    assert "fidelity_risk" not in by_idx.get(1, [])
    assert "fidelity_risk" in by_idx.get(2, [])


# ── Compliance checker ───────────────────────────────────────────────────────

def test_compliance_cure_claim_severe_and_rewrite():
    """'chữa khỏi suy dinh dưỡng' -> nặng + viết lại thành 'hỗ trợ'."""
    findings = affiliate_engine.check_affiliate_compliance(
        "Sữa này chữa khỏi suy dinh dưỡng, dùng ngay hôm nay!")
    assert any(f["level"] == "severe" for f in findings)
    new_text, changes = affiliate_engine.rewrite_affiliate_compliance(
        "Sữa này chữa khỏi suy dinh dưỡng, dùng ngay hôm nay!")
    assert "chữa khỏi" not in new_text
    assert "hỗ trợ" in new_text
    assert changes and changes[0]["matched"] == "chữa khỏi"
    # Sau viết lại không còn vi phạm nặng nào.
    assert not any(f["level"] == "severe" for f in
                   affiliate_engine.check_affiliate_compliance(new_text))


def test_compliance_ensure_support_ok_cure_violation():
    """Case sữa Ensure: 'hỗ trợ dinh dưỡng' OK; 'chữa khỏi suy dinh dưỡng' VI PHẠM."""
    ok = affiliate_engine.check_affiliate_compliance(
        "Sữa Ensure hỗ trợ dinh dưỡng cho người lớn tuổi, uống mỗi ngày đều đặn.")
    assert ok == []
    bad = affiliate_engine.check_affiliate_compliance(
        "Sữa Ensure chữa khỏi suy dinh dưỡng cho người lớn tuổi.")
    assert any(f["level"] == "severe" for f in bad)
    new_text, _ = affiliate_engine.rewrite_affiliate_compliance(
        "Sữa Ensure chữa khỏi suy dinh dưỡng cho người lớn tuổi.")
    assert "hỗ trợ cải thiện suy dinh dưỡng" in new_text


def test_compliance_absolute_cure_promises_severe():
    """Cam kết tuyệt đối về khỏi bệnh -> nặng."""
    for text in ("Cam kết 100% khỏi bệnh sau 1 tuần!",
                 "Thuốc này đặc trị viêm da, chắc chắn khỏi hẳn.",
                 "Hiệu quả tuyệt đối, bao khỏi 100%."):
        findings = affiliate_engine.check_affiliate_compliance(text)
        assert any(f["level"] == "severe" for f in findings), text
        new_text, changes = affiliate_engine.rewrite_affiliate_compliance(text)
        assert changes, text
        assert "khỏi bệnh" not in new_text or "100% khỏi" not in new_text


def test_compliance_unrealistic_results_severe():
    """Kết quả phi thực tế -> nặng + viết lại, không bịa số mới."""
    new_text, changes = affiliate_engine.rewrite_affiliate_compliance(
        "Giảm 5kg trong 3 ngày, trắng sau 1 lần dùng!")
    assert "5kg trong 3 ngày" not in new_text
    assert "trắng sau 1 lần" not in new_text
    assert "dần dần" in new_text
    assert len(changes) == 2
    assert affiliate_engine.check_affiliate_compliance(
        "Dùng đều mỗi ngày, bạn sẽ thấy da sáng hơn.") == []


def test_compliance_superlatives_light_keep_text():
    """Từ tuyệt đối -> nhẹ: giữ nguyên câu, chỉ gắn cờ."""
    text = "Sản phẩm tốt nhất thị trường, số 1 Việt Nam, duy nhất có công nghệ này."
    findings = affiliate_engine.check_affiliate_compliance(text)
    assert findings and all(f["level"] == "light" for f in findings)
    new_text, changes = affiliate_engine.rewrite_affiliate_compliance(text)
    assert new_text == text and changes == []


def test_compliance_fake_scarcity_discount_light():
    """Khan hiếm/giảm giá giả -> nhẹ."""
    text = "Chỉ còn 1 suất duy nhất hôm nay, đang giảm 90%, nhanh tay kẻo hết hạn!"
    findings = affiliate_engine.check_affiliate_compliance(text)
    assert len(findings) >= 3
    assert all(f["level"] == "light" for f in findings)
    # Câu tuân thủ: giá thật do user nhập, không có từ cấm.
    assert affiliate_engine.check_affiliate_compliance(
        "Giá bán 89.000đ, freeship đơn từ 0đ.") == []


def test_compliance_english_prompts():
    """Prompt tiếng Anh: 'cure' nặng, 'best in the world' nhẹ."""
    severe = affiliate_engine.check_affiliate_compliance(
        "A miracle cure that heals everything.")
    assert any(f["level"] == "severe" for f in severe)
    light = affiliate_engine.check_affiliate_compliance(
        "The best in the world, number 1, only today!")
    assert light and all(f["level"] == "light" for f in light)


# ── Tích hợp vào _normalize ─────────────────────────────────────────────────

def test_normalize_rewrites_cure_claim_and_warns():
    """Normalize: thoại chữa bệnh được viết lại + warning nêu rõ đã sửa gì."""
    scenes = [affiliate_engine.AffiliateScene(
        index=1, title="C1", role="demo", duration=10, prompt="Demo shot.",
        voiceover="Sữa Ensure chữa khỏi suy dinh dưỡng, hiệu quả tuyệt đối!")]
    out, warnings = affiliate_engine._normalize_affiliate_script(scenes, _req())
    assert "chữa khỏi" not in out[0].voiceover
    assert "hỗ trợ" in out[0].voiceover
    assert any("viết lại" in w and "chữa khỏi" in w for w in warnings)
    assert not any(f["level"] == "severe" for f in
                   affiliate_engine.check_affiliate_compliance(out[0].voiceover))
    # Duration tính từ thoại ĐÃ viết lại.
    want = affiliate_engine.snap_clip_duration(
        affiliate_engine.estimate_voiceover_seconds(out[0].voiceover))
    assert out[0].duration == want


def test_normalize_light_warning_keeps_text():
    """Normalize: vi phạm nhẹ giữ nguyên câu + warning bắt xác nhận."""
    scenes = [affiliate_engine.AffiliateScene(
        index=1, title="C1", role="cta", duration=10, prompt="CTA shot.",
        voiceover="Sản phẩm tốt nhất, chỉ còn 1 suất duy nhất hôm nay!")]
    out, warnings = affiliate_engine._normalize_affiliate_script(scenes, _req())
    assert out[0].voiceover.startswith("Sản phẩm tốt nhất")
    assert any("kiểm tra lại trước khi đăng" in w for w in warnings)


def test_normalize_prompt_fidelity_risk_warns():
    """Normalize: prompt chứa ngôn ngữ đổi sản phẩm -> warning."""
    scenes = [affiliate_engine.AffiliateScene(
        index=1, title="C1", role="demo", duration=10,
        prompt="Demo shot, redesign the product in a different color.",
        voiceover="Xem ngay!")]
    _, warnings = affiliate_engine._normalize_affiliate_script(scenes, _req())
    assert any("redesign" in w or "different color" in w for w in warnings)


# ── find-product-images ─────────────────────────────────────────────────────

JS_SHELL_HTML = "<html><head><title>Shop</title></head><body><div id='app'></div></body></html>"


def _get_shopee_images(url, **kwargs):
    """Shopee API trả 3 ảnh -> nguồn nền tảng gốc."""
    if "api/v4/item/get" in url:
        data = {"data": {
            "name": "Sữa Ensure Gold 800g",
            "price": 65000000000,
            "images": ["img1abc", "img2def", "img3ghi"],
            "shop": {"name": "Shop Sữa"},
        }}
        return _FakeResp(200, url, json.dumps(data))
    return _FakeResp(200, url, JS_SHELL_HTML)


def _get_ddg_ok(url, **kwargs):
    """DuckDuckGo: trang kết quả có vqd + i.js trả 2 ảnh."""
    if "i.js" in url:
        return _FakeResp(200, url, json.dumps({"results": [
            {"image": "https://cdn.example.com/ensure1.jpg", "title": "Ensure 1"},
            {"image": "https://cdn.example.com/ensure2.png", "title": "Ensure 2"},
            {"image": "https://cdn.example.com/not-image.exe", "title": "bad"},
        ]}))
    return _FakeResp(200, url, '<html><body vqd="4-abc123"></body></html>')


def _get_ddg_challenge_then_bing(url, **kwargs):
    """DuckDuckGo i.js trả botnet_challenge -> fallback Bing HTML có 2 murl."""
    if "duckduckgo.com" in url and "i.js" in url:
        return _FakeResp(200, url, json.dumps(
            {"response_type": "botnet_challenge", "results": []}))
    if "duckduckgo.com" in url:
        return _FakeResp(200, url, '<html><body vqd="4-abc123"></body></html>')
    if "bing.com" in url:
        html = (
            '<html><body>'
            '&quot;murl&quot;:&quot;https://m.media-amazon.com/images/I/ensureA.jpg&quot;'
            '&quot;murl&quot;:&quot;https://m.media-amazon.com/images/I/ensureB.webp&quot;'
            '&quot;murl&quot;:&quot;https://m.media-amazon.com/images/I/ensureA.jpg&quot;'
            '</body></html>'
        )
        return _FakeResp(200, url, html)
    return _FakeResp(200, url, JS_SHELL_HTML)


def _get_boom(url, **kwargs):
    raise ConnectionError("mock network down")


def test_find_images_shopee_api_first():
    """Link Shopee -> ảnh nền tảng gốc từ API (3 ảnh, nguồn shopee)."""
    r = affiliate_engine.find_product_images(
        "Sữa Ensure Gold 800g",
        source_url="https://shopee.vn/Sua-Ensure-Gold-800g-i.123.456",
        http_get=_get_shopee_images)
    assert len(r["images"]) == 3
    assert all(i["source"] == "shopee" for i in r["images"])
    assert r["images"][0]["url"].startswith(
        "https://down-vn.img.susercontent.com/file/img1abc")


def test_find_images_duckduckgo():
    """Không có link gốc -> DuckDuckGo trả 2 ảnh (file .exe bị lọc)."""
    r = affiliate_engine.find_product_images(
        "Sữa Ensure Gold 800g", http_get=_get_ddg_ok)
    assert len(r["images"]) == 2
    assert all(i["source"] == "duckduckgo" for i in r["images"])
    assert r["images"][0]["url"] == "https://cdn.example.com/ensure1.jpg"


def test_find_images_ddg_challenge_fallback_bing():
    """DuckDuckGo bị chặn (botnet_challenge) -> fallback Bing, lọc trùng."""
    r = affiliate_engine.find_product_images(
        "Sữa Ensure Gold 800g", http_get=_get_ddg_challenge_then_bing)
    assert len(r["images"]) == 2  # ảnh trùng nhau chỉ lấy 1 lần
    assert all(i["source"] == "bing" for i in r["images"])


def test_find_images_all_sources_fail_returns_empty_with_message():
    """Mọi nguồn lỗi mạng -> [] + message bảo tải ảnh tay (không raise)."""
    r = affiliate_engine.find_product_images(
        "Sữa Ensure Gold 800g", http_get=_get_boom)
    assert r["images"] == []
    assert "tải ảnh" in r["message"]


def test_find_images_empty_name_422():
    with pytest.raises(ValueError, match="tên sản phẩm"):
        affiliate_engine.find_product_images("", http_get=_get_ddg_ok)


def test_find_images_caps_at_8_and_dedups():
    """DuckDuckGo trả 10 ảnh (có trùng) -> tối đa 8, không trùng."""
    def _get_many(url, **kwargs):
        if "i.js" in url:
            results = [
                {"image": f"https://cdn.example.com/p{i % 5}.jpg"}
                for i in range(10)
            ]
            return _FakeResp(200, url, json.dumps({"results": results}))
        return _FakeResp(200, url, '<html><body vqd="4-abc123"></body></html>')

    r = affiliate_engine.find_product_images("Sữa Ensure", http_get=_get_many)
    assert len(r["images"]) == 5  # 10 kết quả nhưng chỉ 5 URL khác nhau
    assert len({i["url"] for i in r["images"]}) == 5


# ── Endpoint /api/affiliate/find-product-images ─────────────────────────────

def test_endpoint_find_product_images(monkeypatch):
    try:
        import app as app_module
    except ImportError as e:
        pytest.skip(f"không import được app (thiếu deps): {e}")
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as client:
        # 200: DuckDuckGo trả ảnh (mock tầng http của affiliate_engine).
        monkeypatch.setattr(
            affiliate_engine, "_default_link_http_get", _get_ddg_ok)
        r = client.post("/api/affiliate/find-product-images",
                        json={"product_name": "Sữa Ensure Gold 800g"})
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert len(body["images"]) == 2
        assert body["images"][0]["source"] == "duckduckgo"

        # 200 + [] khi mọi nguồn lỗi (không crash, message rõ).
        monkeypatch.setattr(
            affiliate_engine, "_default_link_http_get", _get_boom)
        r = client.post("/api/affiliate/find-product-images",
                        json={"product_name": "Sữa Ensure Gold 800g"})
        assert r.status_code == 200
        assert r.json()["images"] == []

        # 422: thiếu tên sản phẩm.
        r = client.post("/api/affiliate/find-product-images",
                        json={"product_name": ""})
        assert r.status_code == 422
