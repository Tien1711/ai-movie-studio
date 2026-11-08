"""Test affiliate continuity — chain sequential + Product DNA lock + 10s chuẩn.

Phạm vi: fix affiliate 2026-11-07 (sau merge PR #1, nhánh feature/affiliate-continuity-10s).
- API video trả đúng 1 clip 10s/cảnh -> mọi cảnh affiliate chuẩn 10s.
- Render nối tiếp (sequential chain) + Product DNA lock nguyên văn mọi prompt.
- Không gọi API video thật ở bất kỳ test nào.
"""
import asyncio
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent

PRODUCT = "Giá đỡ điện thoại xoay 360 hợp kim nhôm"


def _req(**over):
    import affiliate_engine
    kw = dict(product_name=PRODUCT, category="gadgets", framework="pas",
              shooting_style="hands", koc_persona="nam_bua_hai")
    kw.update(over)
    return affiliate_engine.ProductAffiliateRequest(**kw)


# ── (a) Script local: mọi framework đều 10s + có lock ─────────────────────

def test_a_local_script_all_frameworks_duration_10_and_locked():
    import affiliate_engine
    for fw in ("pas", "hai_bua", "review_koc", "before_after", "aida"):
        res = affiliate_engine.generate_affiliate_script_local(_req(framework=fw))
        assert len(res.scenes) == 5, f"framework {fw} phải có 5 cảnh"
        for s in res.scenes:
            assert s.duration == 10, (
                f"framework {fw} cảnh {s.index}: duration={s.duration}, "
                "affiliate chuẩn 10s = clip API trả về")
            assert affiliate_engine.PRODUCT_LOCK_MARKER in s.prompt, (
                f"framework {fw} cảnh {s.index}: prompt thiếu Product DNA lock")
            assert "360-degree rotating phone stand" in s.prompt, (
                f"framework {fw} cảnh {s.index}: prompt thiếu tên sản phẩm EN")
        assert affiliate_engine.validate_affiliate_script(res.scenes, PRODUCT) == [], (
            f"framework {fw}: validate phải sạch sau khi sinh local")
        assert any("10 giây" in n for n in res.marketing_notes), (
            f"framework {fw}: marketing_notes phải ghi rõ chuẩn 10s")


def test_a_build_commercial_prompt_lock_all_styles_roles():
    import affiliate_engine
    for style in ("faceless", "hands", "product", "koc_face"):
        for role in ("hook", "pain", "demo", "proof", "cta", "weird_role"):
            p = affiliate_engine.build_commercial_prompt(
                role, PRODUCT, "gadgets", "Thiết kế kim loại cao cấp",
                "nam_bua_hai", "9:16", style)
            assert affiliate_engine.PRODUCT_LOCK_MARKER in p, (
                f"style={style} role={role}: build_commercial_prompt thiếu lock")
            assert p.count(affiliate_engine.PRODUCT_LOCK_MARKER) == 1, (
                f"style={style} role={role}: lock bị lặp {p.count(affiliate_engine.PRODUCT_LOCK_MARKER)} lần")


def test_a_product_dna_lock_content_per_style():
    import affiliate_engine
    lock_hands = affiliate_engine.build_product_dna_lock(
        PRODUCT, "gadgets", "Thiết kế kim loại cao cấp", "nam_bua_hai", "hands")
    assert "360-degree rotating phone stand" in lock_hands
    assert "oak wood desk" in lock_hands
    assert "no text" not in lock_hands.lower()  # từ cấm không nằm trong lock
    lock_product = affiliate_engine.build_product_dna_lock(
        PRODUCT, "gadgets", "", "nam_bua_hai", "product")
    assert "turntable" in lock_product
    lock_face = affiliate_engine.build_product_dna_lock(
        PRODUCT, "gadgets", "", "nam_bua_hai", "koc_face")
    assert "25-year-old Vietnamese male tech reviewer" in lock_face
    lock_faceless = affiliate_engine.build_product_dna_lock(
        PRODUCT, "gadgets", "", "nu_tre_trung", "faceless")
    assert "neck down" in lock_faceless


# ── (b) Hậu xử lý AI: thiếu lock thì append, ép 10s, thoại dài chỉ warning ─

def test_b_normalize_appends_lock_forces_10_warns_long_voiceover():
    import affiliate_engine
    req = _req()
    scenes = [
        affiliate_engine.AffiliateScene(
            index=1, title="C1", role="hook", duration=4,
            prompt="A plain hook shot of the product, no lock here.",
            voiceover="Ngắn gọn vừa đủ mười giây."),
        affiliate_engine.AffiliateScene(
            index=2, title="C2", role="demo", duration=6,
            prompt="Demo shot.",
            voiceover=" ".join(["từ"] * 40)),  # 40 từ > 30: phải warning, KHÔNG cắt
    ]
    out, warnings = affiliate_engine._normalize_affiliate_script(scenes, req)
    assert [s.duration for s in out] == [10, 10]
    for s in out:
        assert affiliate_engine.PRODUCT_LOCK_MARKER in s.prompt
        assert "360-degree rotating phone stand" in s.prompt
    assert len(warnings) == 1 and "Cảnh 2" in warnings[0]
    assert len(out[1].voiceover.split()) == 40, "thoại dài KHÔNG được cắt lặng lẽ"
    # Chạy lại lần 2 (idempotent): lock không bị append 2 lần.
    out2, _ = affiliate_engine._normalize_affiliate_script(out, req)
    for s in out2:
        assert s.prompt.count(affiliate_engine.PRODUCT_LOCK_MARKER) == 1


class _NoNetworkAsyncClient:
    """Stub httpx.AsyncClient: nếu code dám gọi mạng thì test fail ngay."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("không được gọi HTTP khi thiếu MUSE2API_KEY")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_b_ai_fallback_no_key_returns_normalized_script(monkeypatch):
    import affiliate_engine
    monkeypatch.delenv("MUSE2API_KEY", raising=False)
    monkeypatch.setattr(affiliate_engine.httpx, "AsyncClient", _NoNetworkAsyncClient)
    res = asyncio.run(affiliate_engine.generate_affiliate_script_ai(_req(), muse_api_key=None))
    assert len(res.scenes) == 5
    assert all(s.duration == 10 for s in res.scenes)
    assert all(affiliate_engine.PRODUCT_LOCK_MARKER in s.prompt for s in res.scenes)


# ── (c) Render: chain sequential + ép 10s (TestClient, không gọi video thật) ─

def test_c_affiliate_render_uses_sequential_chain_and_10s(monkeypatch):
    try:
        import app as app_module
    except ImportError as e:
        pytest.skip(f"không import được app (thiếu deps): {e}")
    from fastapi.testclient import TestClient

    # Tắt auth + tắt Muse key: render vẫn tạo job, pipeline lỗi rõ ràng
    # vì thiếu key (không crash, không gọi mạng).
    monkeypatch.delenv("STUDIO_API_KEY", raising=False)
    monkeypatch.delenv("MUSE2API_KEY", raising=False)
    monkeypatch.delenv("MUSE_API_KEY", raising=False)
    monkeypatch.setattr(app_module, "MUSE_API_KEY", None)

    job_id = None
    try:
        with TestClient(app_module.app) as client:
            # Script CŨ gửi duration 5/6 -> backend phải ép về 10.
            resp = client.post("/api/affiliate/render", json={
                "title": "Test Affiliate Chain",
                "voice": "vieneu:AdamBua",
                "aspect_ratio": "9:16",
                "scenes": [
                    {"prompt": "Hook shot", "voiceover": "Dừng lại 3 giây!",
                     "duration": 5, "voice": "vieneu:AdamBua"},
                    {"prompt": "Demo shot", "voiceover": "Xem nè!",
                     "duration": 6, "voice": "vieneu:AdamBua"},
                ],
            })
            assert resp.status_code == 200, f"render phải 200, nhận {resp.status_code}: {resp.text[:300]}"
            job_id = resp.json()["job_id"]
            got = client.get(f"/api/jobs/{job_id}")
            assert got.status_code == 200
            job = got.json()
            req = job["req"]
            assert req["chain_mode"] == "sequential", (
                f"affiliate phải render sequential, nhận chain_mode={req['chain_mode']!r}")
            durations = [s["duration"] for s in req["scenes"]]
            assert durations == [10, 10], (
                f"render phải ép mọi cảnh về 10s, nhận {durations}")
    finally:
        if job_id:
            shutil.rmtree(REPO_ROOT / "workspace" / job_id, ignore_errors=True)
            app_module.JOBS.pop(job_id, None)


# ── (d) validate_affiliate_script phát hiện thiếu lock ─────────────────────

def test_d_validate_detects_missing_lock_and_name():
    import affiliate_engine
    ok = affiliate_engine.AffiliateScene(
        index=1, title="C1", role="hook", duration=10,
        prompt=f"Hook shot. {affiliate_engine.build_product_dna_lock(PRODUCT, 'gadgets', '', 'nam_bua_hai', 'hands')}",
        voiceover="ok")
    bad = affiliate_engine.AffiliateScene(
        index=2, title="C2", role="demo", duration=10,
        prompt="Demo shot with no lock and no product name at all.",
        voiceover="ok")
    wrong_product = affiliate_engine.AffiliateScene(
        index=3, title="C3", role="cta", duration=10,
        prompt=f"CTA shot. {affiliate_engine.build_product_dna_lock(PRODUCT, 'gadgets', '', 'nam_bua_hai', 'hands')}",
        voiceover="ok")
    issues = affiliate_engine.validate_affiliate_script([ok, bad, wrong_product], PRODUCT)
    assert [i["index"] for i in issues] == [2], f"chỉ cảnh 2 thiếu lock, nhận {issues}"
    assert "product_lock" in issues[0]["missing"]
    # Prompt có lock của sản phẩm A nhưng validate cho sản phẩm B -> thiếu tên.
    issues_b = affiliate_engine.validate_affiliate_script(
        [wrong_product], "Tai nghe Bluetooth chống ồn")
    assert issues_b and "product_name" in issues_b[0]["missing"]
    # Dict cũng validate được (dạng payload render gửi lên).
    assert affiliate_engine.validate_affiliate_script(
        [{"index": 1, "prompt": "nothing here"}], PRODUCT)[0]["missing"] == [
        "product_lock", "product_name"]
