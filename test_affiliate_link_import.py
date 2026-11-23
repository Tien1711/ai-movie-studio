"""Test affiliate link import + thời lượng cảnh biến thiên.

Phạm vi: feat/affiliate-link-import (2026-11-23).
- POST /api/affiliate/import-link: Shopee (slug/API/og tags), TikTok oEmbed,
  link rút gọn redirect, host lạ -> 422, lỗi mạng -> 502.
- Duration biến thiên: mốc clip API 5/6/8/10s theo độ dài thoại ước lượng
  (~11 ký tự/giây); thoại > 10s vẫn lấy clip 10s, video loop để đọc kịp.
- Product DNA lock vẫn nguyên văn mọi cảnh.
- Không gọi mạng thật ở bất kỳ test nào (mock http_get inject được).
"""
import pytest

import affiliate_engine


class _FakeResp:
    """Response tối giản cho mock http_get (đủ cho import_affiliate_product_link)."""

    def __init__(self, status_code=200, url="", text=""):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.content = text.encode("utf-8")

    def json(self):
        import json
        return json.loads(self.text)


SHOPEE_SLUG_URL = (
    "https://shopee.vn/Gia-do-dien-thoai-xoay-360-do-hop-kim-nhom-"
    "i.1057167833.28821306303"
)
TIKTOK_URL = "https://www.tiktok.com/@shopgiadung/video/7293985556338695429"
JS_SHELL_HTML = "<html><head><title>Shopee</title></head><body><div id='app'></div></body></html>"


def _get_shopee_slug_only(url, **kwargs):
    """Shopee chặn API (403) + trang là JS shell không og tags -> chỉ còn slug."""
    if "api/v4/item/get" in url:
        return _FakeResp(403, url, "Forbidden")
    return _FakeResp(200, url, JS_SHELL_HTML)


def _get_shopee_api_ok(url, **kwargs):
    """Shopee API trả 200 đầy đủ (đường này ngoài đời thường bị 403)."""
    if "api/v4/item/get" in url:
        import json
        data = {
            "data": {
                "name": "Giá đỡ điện thoại xoay 360 hợp kim nhôm",
                "price": 9900000000,          # 99.000₫ (đơn vị API = VNĐ x 100000)
                "price_max": 19900000000,     # 199.000₫
                "images": ["abc123def456"],
                "shop": {"name": "Shop Gia Dụng"},
                "description": "Giá đỡ xoay 360 độ, hợp kim nhôm cao cấp.",
            }
        }
        return _FakeResp(200, url, json.dumps(data))
    return _FakeResp(200, url, JS_SHELL_HTML)


def _get_shopee_og(url, **kwargs):
    """Trang có og tags (fallback khi API chặn)."""
    if "api/v4/item/get" in url:
        return _FakeResp(403, url, "Forbidden")
    html = (
        '<html><head>'
        '<meta property="og:title" content="Nồi chiên không dầu 5L">'
        '<meta property="og:image" content="https://cdn.example.com/img.jpg">'
        '<meta property="og:description" content="Nồi chiên giòn ngon">'
        '</head><body></body></html>'
    )
    return _FakeResp(200, url, html)


def _get_tiktok_oembed(url, **kwargs):
    """TikTok oEmbed trả tối giản: xác nhận video tồn tại, không có title/thumbnail."""
    if "oembed" in url:
        import json
        return _FakeResp(200, url, json.dumps({
            "type": "video",
            "embed_product_id": "7293985556338695429",
            "width": 325, "height": 580,
            "html": "<blockquote>...</blockquote>",
        }))
    return _FakeResp(200, url, JS_SHELL_HTML)


def _get_short_link(url, **kwargs):
    """Link rút gọn shope.ee -> redirect tới URL Shopee có slug (final url)."""
    if "shope.ee" in url:
        return _FakeResp(200, SHOPEE_SLUG_URL, JS_SHELL_HTML)
    if "api/v4/item/get" in url:
        return _FakeResp(403, url, "Forbidden")
    return _FakeResp(200, url, JS_SHELL_HTML)


def _get_boom(url, **kwargs):
    raise ConnectionError("mock network down")


# ── Shopee ─────────────────────────────────────────────────────────────────

def test_import_shopee_slug_fallback():
    """Shopee chặn API + không og tags -> lấy tên từ slug, fetched=True."""
    r = affiliate_engine.import_affiliate_product_link(SHOPEE_SLUG_URL, http_get=_get_shopee_slug_only)
    assert r["platform"] == "shopee"
    assert r["item_id"] == "28821306303" and r["shop_id"] == "1057167833"
    assert r["product_name"] == "Gia do dien thoai xoay 360 do hop kim nhom"
    assert r["source"] == "shopee_slug"
    assert r["fetched"] is True
    assert r["category"] == "gadgets"  # slug không dấu "Gia do" vẫn đoán được nhờ so khớp bỏ dấu
    assert "giá" in r["message"] or "Shopee" in r["message"]


def test_import_shopee_api_ok():
    """Shopee API 200 -> tên + giá + ảnh CDN + shop đầy đủ."""
    r = affiliate_engine.import_affiliate_product_link(SHOPEE_SLUG_URL, http_get=_get_shopee_api_ok)
    assert r["source"] == "shopee_api"
    assert r["product_name"] == "Giá đỡ điện thoại xoay 360 hợp kim nhôm"
    assert r["price_sale"] == "Chỉ 99.000₫"
    assert r["price_original"] == "199.000₫"
    assert r["image_url"] == "https://down-vn.img.susercontent.com/file/abc123def456"
    assert r["shop_name"] == "Shop Gia Dụng"
    assert r["fetched"] is True


def test_import_shopee_og_tags():
    """API chặn nhưng trang có og tags -> lấy tên + ảnh từ og tags."""
    url = "https://shopee.vn/Noi-chien-khong-dau-5L-i.111.222"
    r = affiliate_engine.import_affiliate_product_link(url, http_get=_get_shopee_og)
    assert r["source"] == "og_tags"
    assert r["product_name"] == "Nồi chiên không dầu 5L"
    assert r["image_url"] == "https://cdn.example.com/img.jpg"
    assert r["category"] == "home"
    assert r["fetched"] is True


def test_import_shopee_no_slug_no_data():
    """URL /product/<shopid>/<itemid> không có slug, API chặn, không og -> fetched=False, không kẹt."""
    r = affiliate_engine.import_affiliate_product_link(
        "https://shopee.vn/product/1057167833/28821306303", http_get=_get_shopee_slug_only)
    assert r["platform"] == "shopee"
    assert r["item_id"] == "28821306303"
    assert r["product_name"] == ""
    assert r["fetched"] is False
    assert "nhập tay" in r["message"]


# ── TikTok ─────────────────────────────────────────────────────────────────

def test_import_tiktok_oembed_confirms_video_only():
    """TikTok oEmbed chỉ xác nhận video tồn tại + video_id; tên vẫn cần nhập tay."""
    r = affiliate_engine.import_affiliate_product_link(TIKTOK_URL, http_get=_get_tiktok_oembed)
    assert r["platform"] == "tiktok"
    assert r["video_id"] == "7293985556338695429"
    assert r["author"] == "shopgiadung"
    assert r["source"] == "tiktok_oembed"
    assert r["product_name"] == ""
    assert r["fetched"] is False  # chưa có tên sản phẩm -> UI vẫn cho nhập tay
    assert "nhập" in r["message"]


# ── Link rút gọn / lỗi ─────────────────────────────────────────────────────

def test_import_short_link_redirect():
    """shope.ee redirect tới Shopee -> lấy tên từ slug của URL cuối."""
    r = affiliate_engine.import_affiliate_product_link(
        "https://shope.ee/abcXYZ", http_get=_get_short_link)
    assert r["platform"] == "shopee"
    assert r["url"] == SHOPEE_SLUG_URL
    assert r["product_name"] == "Gia do dien thoai xoay 360 do hop kim nhom"
    assert r["fetched"] is True


def test_import_unknown_host_422():
    with pytest.raises(ValueError, match="Chỉ hỗ trợ link Shopee"):
        affiliate_engine.import_affiliate_product_link(
            "https://example.com/san-pham", http_get=_get_shopee_slug_only)


def test_import_bad_url_422():
    with pytest.raises(ValueError):
        affiliate_engine.import_affiliate_product_link("", http_get=_get_shopee_slug_only)
    with pytest.raises(ValueError):
        affiliate_engine.import_affiliate_product_link("shopee.vn/abc", http_get=_get_shopee_slug_only)


def test_import_network_error_502():
    with pytest.raises(RuntimeError, match="Không tải được link"):
        affiliate_engine.import_affiliate_product_link(SHOPEE_SLUG_URL, http_get=_get_boom)


def test_guess_affiliate_category():
    assert affiliate_engine.guess_affiliate_category("Giá đỡ điện thoại xoay 360") == "gadgets"
    assert affiliate_engine.guess_affiliate_category("Nồi chiên không dầu 5L") == "home"
    assert affiliate_engine.guess_affiliate_category("Serum vitamin C dưỡng trắng") == "beauty"
    assert affiliate_engine.guess_affiliate_category("Khô bò xé sợi cay") == "food"
    assert affiliate_engine.guess_affiliate_category("Cái gì đó không rõ danh mục xyz") == ""


# ── Thời lượng biến thiên ──────────────────────────────────────────────────

def test_snap_clip_duration_boundaries():
    snap = affiliate_engine.snap_clip_duration
    est = affiliate_engine.estimate_voiceover_seconds
    assert snap(est("")) == 5            # không thoại -> mốc nhỏ nhất
    assert snap(est("Xem ngay!")) == 5    # 8 ký tự ~0.7s
    assert snap(est("x" * 55)) == 5       # đúng 5.0s
    assert snap(est("x" * 56)) == 6       # 5.09s -> mốc 6
    assert snap(est("x" * 60)) == 6       # 5.45s
    assert snap(est("x" * 80)) == 8       # 7.27s
    assert snap(est("x" * 100)) == 10     # 9.09s
    assert snap(est("x" * 150)) == 10     # 13.6s -> mốc tối đa 10 (video loop)
    assert snap(est("x" * 500)) == 10
    for d in (snap(est("x" * n)) for n in (10, 60, 80, 100, 150)):
        assert d in affiliate_engine.AFFILIATE_CLIP_DURATIONS


def test_normalize_duration_follows_voiceover_not_10():
    """Thoại ngắn -> cảnh ngắn, thoại dài -> cảnh dài; không còn mặc định 10s."""
    req = affiliate_engine.ProductAffiliateRequest(
        product_name="Giá đỡ điện thoại xoay 360 hợp kim nhôm",
        category="gadgets", framework="pas", shooting_style="hands",
        koc_persona="nam_bua_hai")
    scenes = [
        affiliate_engine.AffiliateScene(index=1, title="C1", role="hook", duration=10,
                                        prompt="Hook shot.", voiceover="Dừng lại!"),
        affiliate_engine.AffiliateScene(index=2, title="C2", role="demo", duration=10,
                                        prompt="Demo shot.", voiceover="x" * 80),
        affiliate_engine.AffiliateScene(index=3, title="C3", role="cta", duration=10,
                                        prompt="CTA shot.", voiceover="x" * 150),
    ]
    out, warnings = affiliate_engine._normalize_affiliate_script(scenes, req)
    assert [s.duration for s in out] == [5, 8, 10], "thoại 8/80/150 ký tự -> clip 5/8/10s"
    for s in out:
        assert affiliate_engine.PRODUCT_LOCK_MARKER in s.prompt
        assert "360-degree rotating phone stand" in s.prompt


def test_voiceover_longer_than_10s_loops_not_cut():
    """Thoại 40 từ (~13s) -> clip 10s + warning ghi rõ video loop, KHÔNG cắt thoại."""
    req = affiliate_engine.ProductAffiliateRequest(
        product_name="Giá đỡ điện thoại xoay 360 hợp kim nhôm",
        category="gadgets", framework="pas", shooting_style="hands",
        koc_persona="nam_bua_hai")
    scenes = [affiliate_engine.AffiliateScene(
        index=1, title="C1", role="demo", duration=10, prompt="Demo shot.",
        voiceover=" ".join(["từ"] * 40))]
    out, warnings = affiliate_engine._normalize_affiliate_script(scenes, req)
    assert out[0].duration == 10
    assert len(out[0].voiceover.split()) == 40, "thoại dài KHÔNG được cắt lặng lẽ"
    assert len(warnings) == 1 and "loop" in warnings[0]


def test_local_script_durations_match_voiceover_all_frameworks():
    """Script local mọi framework: duration == snap(est(voiceover)), lock nguyên văn."""
    for fw in ("pas", "hai_bua", "review_koc", "before_after", "aida"):
        req = affiliate_engine.ProductAffiliateRequest(
            product_name="Giá đỡ điện thoại xoay 360 hợp kim nhôm",
            category="gadgets", framework=fw, shooting_style="hands",
            koc_persona="nam_bua_hai")
        res = affiliate_engine.generate_affiliate_script_local(req)
        assert len(res.scenes) == 5
        for s in res.scenes:
            want = affiliate_engine.snap_clip_duration(
                affiliate_engine.estimate_voiceover_seconds(s.voiceover))
            assert s.duration == want, f"{fw} cảnh {s.index}: {s.duration} != {want}"
            assert affiliate_engine.PRODUCT_LOCK_MARKER in s.prompt


# ── Endpoint /api/affiliate/import-link ────────────────────────────────────

def test_endpoint_import_link_ok_422_502(monkeypatch):
    try:
        import app as app_module
    except ImportError as e:
        pytest.skip(f"không import được app (thiếu deps): {e}")
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as client:
        # 200: Shopee slug (mock tầng http của affiliate_engine).
        monkeypatch.setattr(
            affiliate_engine, "_default_link_http_get", _get_shopee_slug_only)
        r = client.post("/api/affiliate/import-link", json={"url": SHOPEE_SLUG_URL})
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["platform"] == "shopee"
        assert body["fetched"] is True
        assert "Gia do dien thoai" in body["product_name"]

        # 422: host lạ.
        r = client.post("/api/affiliate/import-link",
                        json={"url": "https://example.com/sp"})
        assert r.status_code == 422
        assert "Shopee" in r.json()["error"]

        # 422: thiếu URL.
        r = client.post("/api/affiliate/import-link", json={"url": ""})
        assert r.status_code == 422

        # 502: lỗi mạng.
        monkeypatch.setattr(affiliate_engine, "_default_link_http_get", _get_boom)
        r = client.post("/api/affiliate/import-link", json={"url": SHOPEE_SLUG_URL})
        assert r.status_code == 502
