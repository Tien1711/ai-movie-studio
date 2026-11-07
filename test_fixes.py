"""
test_fixes.py — Tests cho các fix theo audit 2026-11-07 (refactor 2026-11-07).

Phạm vi refactor lần này CHỈ: shot_desk.py, affiliate_engine.py,
prompt_templates.py, .env.example (+ file test này).
Các bug ở file ngoài phạm vi (composer.py: dissolve/SRT, batch_engine.py
trước khi có helper, app.py: save_job/scenes) được test ở đây để ghi nhận
hiện trạng: test nào FAIL nghĩa là bug đó vẫn còn và cần sửa ở file tương
ứng trong đợt sau — KHÔNG được sửa file ngoài phạm vi để "cho test pass".

Chạy từ thư mục gốc repo:
    python -m pytest test_fixes.py -v
"""

import asyncio
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ffmpeg_available = shutil.which("ffmpeg") is not None or Path(FFMPEG).exists()


def _make_clip(path: str, duration: float = 2.0, color: str = "red", freq: int = 440):
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=320x240:d={duration}:r=24",
        "-f", "lavfi", "-i", f"sine=f={freq}:d={duration}:r=44100",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ── (a) Dissolve — composer.merge_clips_advanced ──────────────────────────
# Bug audit §1.1, file composer.py (NGOÀI phạm vi refactor lần này):
# 3 clip 2s + 2 dissolve 0.5s phải ra ~5.0s, hiện tại ra 6.0s.

@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg không có sẵn")
def test_a_dissolve_three_clips_2s_overlap_0_5s_is_5s(tmp_path):
    from composer import merge_clips_advanced, get_media_duration

    clips = []
    for i, (color, freq) in enumerate([("red", 300), ("blue", 450), ("green", 600)]):
        p = str(tmp_path / f"clip_{i}.mp4")
        _make_clip(p, duration=2.0, color=color, freq=freq)
        clips.append(p)

    out = str(tmp_path / "merged_dissolve.mp4")
    merge_clips_advanced(
        clips=clips,
        transitions=["dissolve", "dissolve"],
        sound_bridge_duration=0.5,
        output_path=out,
        quality="720p",
    )
    dur = get_media_duration(out)
    # 3×2s − 2×0.5s overlap = 5.0s
    assert dur == pytest.approx(5.0, abs=0.5), (
        f"dissolve bị bỏ qua: kỳ vọng ~5.0s, thực tế {dur:.2f}s "
        "(composer.py §1.1 chưa sửa — ngoài phạm vi refactor lần này)"
    )


# ── (b) Traversal reference_image — helper resolve trong batch_engine ─────
# Audit §2.3. Helper _resolve_safe_reference() do đợt refactor batch_engine
# (file ngoài phạm vi task này) bổ sung; test gọi đúng helper đó.

def _get_reference_resolver():
    import batch_engine

    for name in ("_resolve_safe_reference", "resolve_safe_reference_image",
                 "resolve_reference_image", "_resolve_reference"):
        fn = getattr(batch_engine, name, None)
        if callable(fn):
            return fn
    return None


def test_b_reference_image_traversal_blocked(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)  # helper resolve workspace theo cwd
    resolver = _get_reference_resolver()
    assert resolver is not None, (
        "batch_engine không có helper resolve an toàn cho reference_image "
        "(cần _resolve_safe_reference); nhánh join() cũ đọc được file ngoài workspace"
    )
    # (1) relative traversal
    assert resolver("../etc/passwd") is None
    assert resolver("../../etc/passwd") is None
    assert resolver("../../../etc/passwd") is None
    # (2) đường dẫn tuyệt đối ngoài workspace
    assert resolver("/etc/passwd") is None
    assert resolver("/tmp/secret.txt") is None
    # (3) traversal qua route shot-desk
    assert resolver("/api/shot-desk/projects/proj123/../../etc/passwd") is None
    assert resolver("/api/shot-desk/projects/proj123/../../../etc/passwd") is None
    assert resolver("/api/shot-desk/projects/../..//etc/passwd") is None
    # (4) traversal qua route uploads (basename)
    assert resolver("/api/uploads/../../etc/passwd") is None


def test_b_reference_image_valid_upload_resolves(monkeypatch, tmp_path):
    monkeypatch.chdir(REPO_ROOT)
    resolver = _get_reference_resolver()
    if resolver is None:
        pytest.skip("batch_engine chưa có helper resolve an toàn")
    uploads = REPO_ROOT / "workspace" / "_uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    probe = uploads / "test_fixes_probe.png"
    try:
        probe.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        resolved = resolver("/api/uploads/test_fixes_probe.png")
        assert resolved is not None, "file hợp lệ trong _uploads phải resolve được"
        assert Path(resolved).resolve() == probe.resolve()
    finally:
        probe.unlink(missing_ok=True)


# ── (c) save_job atomic + key scenes int/str — app.save_job ───────────────
# Audit §1.2 + §1.3, file app.py (NGOÀI phạm vi refactor lần này).

def test_c_save_job_atomic_and_scenes_int_str_keys():
    try:
        import app as app_module
    except ImportError as e:
        pytest.skip(f"không import được app (thiếu deps): {e}")

    job_id = "test_fixes_job"
    job_dir = REPO_ROOT / "workspace" / job_id
    try:
        job = app_module.new_job(job_id, 2, req=None)
        job["scenes"][1] = {"status": "done", "msg": "ok"}
        job["scenes"][2] = {"status": "running", "msg": "doing"}
        app_module.save_job(job)

        state_file = job_dir / "job_state.json"
        assert state_file.exists(), "save_job phải ghi job_state.json"
        # File phải là JSON hợp lệ (không cắt nửa / rỗng sau khi save).
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert data["id"] == job_id
        # Không còn file tmp sót lại sau save (ghi atomic tmp+rename).
        leftovers = [p for p in job_dir.iterdir()
                     if p.name.startswith("job_state.json.tmp") or p.suffix == ".tmp"]
        assert not leftovers, f"còn file tmp sót sau save_job: {leftovers}"

        # Sau save/load, key scenes phải truy cập được cả kiểu int và str.
        restored = app_module.get_or_restore_job(job_id)
        assert restored is not None
        assert restored["scenes"][1]["status"] == "done", (
            "key scenes int bị mất sau JSON restore (app.py §1.2 chưa sửa — "
            "ngoài phạm vi refactor lần này)"
        )
        assert restored["scenes"]["1"]["status"] == "done"
        assert restored["scenes"][2]["status"] == "running"
        assert restored["scenes"]["2"]["status"] == "running"
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        app_module.JOBS.pop(job_id, None)


# ── (d) SRT _fmt_srt_time — composer ──────────────────────────────────────
# Audit §1.13, file composer.py (NGOÀI phạm vi refactor lần này).

def test_d_srt_time_no_60_seconds():
    from composer import _fmt_srt_time

    # Task cho phép "00:01:59,999" (cắt) HOẶC bất kỳ kết quả đúng chuẩn nào
    # không ra 00:01:60,000. Bản fix hiện tại làm tròn đúng qua tổng ms:
    # 119.9996s -> 00:02:00,000 (cascade phút/giờ đúng).
    result = _fmt_srt_time(119.9996)
    assert ":60," not in result, f"SRT sai chuẩn (giây = 60): {result!r}"
    assert result in ("00:01:59,999", "00:02:00,000"), f"119.9996s ra {result!r}"
    assert ":60," not in _fmt_srt_time(59.9996)
    assert ":60," not in _fmt_srt_time(3599.9996)
    assert _fmt_srt_time(0.0) == "00:00:00,000"
    assert _fmt_srt_time(61.5) == "00:01:01,500"


# ── (e) Secret — không còn default key hardcode ────────────────────────────

def test_e_no_hardcoded_default_api_key(monkeypatch):
    """Gọi hàm với api_key=None và env trống phải raise lỗi cấu hình,
    không gọi mạng, không dùng key giả."""
    import shot_desk
    import affiliate_engine

    # Signature: default phải là None, không phải chuỗi key.
    assert inspect.signature(shot_desk.render_single_shot_desk).parameters["api_key"].default is None
    assert inspect.signature(affiliate_engine.generate_affiliate_script_ai).parameters["muse_api_key"].default is None

    monkeypatch.delenv("MUSE2API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MUSE2API_KEY"):
        asyncio.run(shot_desk.render_single_shot_desk(
            project_id="no_such_project", shot_index=1, api_key=None))
    # affiliate: thiếu key -> fallback local ngay, KHÔNG gọi HTTP.
    monkeypatch.setattr(affiliate_engine.httpx, "AsyncClient", _NoNetworkAsyncClient)
    req = affiliate_engine.ProductAffiliateRequest(
        product_name="Đèn ngủ", category="home", framework="pas")
    result = asyncio.run(affiliate_engine.generate_affiliate_script_ai(req, muse_api_key=None))
    assert len(result.scenes) >= 3, "thiếu key thì phải fallback local ra kịch bản"


class _NoNetworkAsyncClient:
    """Stub httpx.AsyncClient: nếu code dám gọi mạng thì test fail ngay."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("không được gọi HTTP khi thiếu MUSE2API_KEY")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_e_env_key_resolves_without_network(monkeypatch):
    import shot_desk

    monkeypatch.setenv("MUSE2API_KEY", "env-key-dùng-để-test")
    assert shot_desk._resolve_muse2api_key(None) == "env-key-dùng-để-test"
    assert shot_desk._resolve_muse2api_key("explicit-key") == "explicit-key"
    monkeypatch.delenv("MUSE2API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MUSE2API_KEY"):
        shot_desk._resolve_muse2api_key(None)
    with pytest.raises(RuntimeError, match="MUSE2API_KEY"):
        shot_desk._resolve_muse2api_key("   ")


# ── Các fix trong phạm vi refactor (phải pass) ─────────────────────────────

def test_translate_product_keeps_vietnamese_letters():
    from affiliate_engine import translate_product_to_en

    out = translate_product_to_en("Đèn ngủ")
    # Phần transliterate trong ngoặc KHÔNG được là "n ng" (kết quả lỗi cũ).
    assert "(n ng" not in out, f"'Đèn ngủ' bị strip dấu thành ký tự rác: {out!r}"
    assert "Den ngu" in out, f"transliterate 'Đèn ngủ' phải ra 'Den ngu': {out!r}"
    assert "Đèn ngủ" in out, "nên giữ tên gốc có dấu trong prompt"
    out2 = translate_product_to_en("Ghế massage toàn thân")
    assert "Ghe massage toan than" in out2


def test_build_commercial_prompt_uses_aspect_ratio():
    from affiliate_engine import build_commercial_prompt

    p_16x9 = build_commercial_prompt("hook", "Sạc dự phòng", "gadgets",
                                     "sạc nhanh 65W", aspect_ratio="16:9",
                                     shooting_style="faceless")
    assert "horizontal 16:9 framing" in p_16x9
    assert "vertical 9:16 framing" not in p_16x9
    p_9x16 = build_commercial_prompt("hook", "Sạc dự phòng", "gadgets",
                                     "sạc nhanh 65W", aspect_ratio="9:16",
                                     shooting_style="faceless")
    assert "vertical 9:16 framing" in p_9x16
    assert "horizontal 16:9 framing" not in p_9x16
    p_1x1 = build_commercial_prompt("hook", "Sạc dự phòng", "gadgets",
                                    "sạc nhanh 65W", aspect_ratio="1:1",
                                    shooting_style="product")
    assert "square 1:1 framing" in p_1x1


def test_affiliate_json_parse_tolerates_trailing_text(monkeypatch):
    """LLM trả thêm text sau JSON + timeout 60s (connect 5s)."""
    import affiliate_engine
    import httpx

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content":
                'Đây là kịch bản:\n{"title": "T", "scenes": ['
                '{"index": 1, "title": "A", "role": "hook", "duration": 4, '
                '"prompt": "p1", "voiceover": "v1", "badge": "b1"}, '
                '{"index": 2, "title": "B", "role": "pain", "duration": 5, '
                '"prompt": "p2", "voiceover": "v2", "badge": "b2"}, '
                '{"index": 3, "title": "C", "role": "demo", "duration": 6, '
                '"prompt": "p3", "voiceover": "v3", "badge": "b3"}]}'
                '\nCảm ơn bạn đã dùng dịch vụ!'}}]}

    class FakeClient:
        def __init__(self, timeout=None, **kwargs):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr(affiliate_engine.httpx, "AsyncClient", FakeClient)
    req = affiliate_engine.ProductAffiliateRequest(
        product_name="Đèn ngủ", category="home", framework="pas")
    result = asyncio.run(affiliate_engine.generate_affiliate_script_ai(
        req, muse_api_key="test-key-không-gọi-mạng"))
    assert len(result.scenes) == 3, "JSON + text thừa sau JSON phải parse được 3 scenes"
    assert result.scenes[0].title == "A"
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout), f"timeout phải là httpx.Timeout, nhận {timeout!r}"
    assert timeout.read == 60.0 and timeout.connect == 5.0, (
        f"timeout chat completion phải là 60s/connect 5s, nhận read={timeout.read}, connect={timeout.connect}"
    )


def test_affiliate_json_extract_balanced():
    from affiliate_engine import _extract_json_object

    obj = _extract_json_object('prefix {"a": {"b": 1}, "c": [1, 2]} trailing } text }')
    assert obj == {"a": {"b": 1}, "c": [1, 2]}
    assert _extract_json_object("không có json") is None
    assert _extract_json_object('{"x": 1} garbage {') == {"x": 1}


def test_local_script_respects_user_voice(capsys):
    from affiliate_engine import (
        ProductAffiliateRequest, generate_affiliate_script_local)

    req = ProductAffiliateRequest(
        product_name="Đèn ngủ", category="home", framework="hai_bua",
        tone="hai_bua", voice="vieneu:CustomVoice")
    result = generate_affiliate_script_local(req)
    assert result.recommended_voice == "vieneu:CustomVoice"
    assert all(s.voice == "vieneu:CustomVoice" for s in result.scenes)
    assert "Warning" in capsys.readouterr().out

    req2 = ProductAffiliateRequest(
        product_name="Đèn ngủ", category="home", framework="review_koc",
        voice="vieneu:CustomVoice")
    result2 = generate_affiliate_script_local(req2)
    assert result2.recommended_voice == "vieneu:CustomVoice"


def test_prompt_sheet_data_returns_deep_copy():
    from prompt_templates import (
        get_prompt_sheet_data, SAMPLE_SCRIPTS, CINEMATIC_DICTIONARY, FORMAT_RULES)

    data1 = get_prompt_sheet_data()
    assert data1["sample_scripts"] is not SAMPLE_SCRIPTS
    assert data1["cinematic_dictionary"] is not CINEMATIC_DICTIONARY
    assert data1["format_rules"] is not FORMAT_RULES
    # Mutate bản trả về -> global và lần gọi sau không đổi.
    if isinstance(data1["sample_scripts"], list) and data1["sample_scripts"]:
        data1["sample_scripts"][0] = "MUTATED"
        data1["sample_scripts"].append("MUTATED")
    if isinstance(data1["cinematic_dictionary"], dict):
        data1["cinematic_dictionary"]["MUTATED_KEY"] = "x"
    data2 = get_prompt_sheet_data()
    assert data2["sample_scripts"] == SAMPLE_SCRIPTS
    assert data2["cinematic_dictionary"] == CINEMATIC_DICTIONARY
    assert data2["format_rules"] == FORMAT_RULES


def test_load_project_json_safe_backups_and_non_dict(tmp_path):
    from shot_desk import load_project_json_safe, SHOT_DESK_DIR

    # NOTE: save_shot_desk_project() ghi về thư mục project theo id, nên test
    # phải đặt project.json hỏng ngay trong SHOT_DESK_DIR/<id> như thực tế.
    proj_dir = SHOT_DESK_DIR / "test_fixes_proj_x"
    proj_dir.mkdir(parents=True, exist_ok=True)
    try:
        # (1) JSON hỏng vì extra data ở cuối -> cắt, backup, ghi đè dict hợp lệ.
        p = proj_dir / "project.json"
        p.write_text('{"id": "test_fixes_proj_x", "title": "T", "shots": []} GARBAGE{{{', encoding="utf-8")
        data = load_project_json_safe(p)
        assert data is not None and isinstance(data, dict) and data["id"] == "test_fixes_proj_x"
        backups = list(proj_dir.glob("project.json.bak.*"))
        assert len(backups) == 1, f"phải backup 1 bản trước khi cắt, thấy {backups}"
        assert "GARBAGE" in backups[0].read_text(encoding="utf-8")
        assert json.loads(p.read_text(encoding="utf-8"))["id"] == "test_fixes_proj_x"

        # (2) JSON cắt xong không phải dict -> lỗi, file gốc giữ nguyên.
        p2 = tmp_path / "project2.json"
        original = '[1, 2, 3] GARBAGE'
        p2.write_text(original, encoding="utf-8")
        assert load_project_json_safe(p2) is None
        assert p2.read_text(encoding="utf-8") == original
        assert list(tmp_path.glob("project2.json.bak.*")), "vẫn phải backup trước khi thử cắt"

        # (3) JSON hỏng không cắt được -> lỗi, file gốc giữ nguyên.
        p3 = tmp_path / "project3.json"
        p3.write_text('{not json at all', encoding="utf-8")
        assert load_project_json_safe(p3) is None
        assert p3.read_text(encoding="utf-8") == '{not json at all'
    finally:
        shutil.rmtree(proj_dir, ignore_errors=True)


def test_import_zip_bad_duration_defaults_to_10(monkeypatch):
    from shot_desk import import_shot_desk_zip, SHOT_DESK_DIR

    manifest = {
        "title": "TestFixes",
        "aspect_ratio": "9:16",
        "characters": [],
        "shots": [{"index": 1, "title": "Shot 1", "duration": "abc",
                   "prompt": "test prompt"}],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project.json", json.dumps(manifest))
    chars_file = REPO_ROOT / "workspace" / "_characters.json"
    before = chars_file.read_bytes() if chars_file.exists() else None
    project = None
    try:
        project = import_shot_desk_zip(buf.getvalue(), "test.zip")
        assert project["shots"][0]["duration"] == 10, (
            "duration='abc' phải default 10 thay vì crash sau khi giải nén"
        )
        assert (SHOT_DESK_DIR / project["id"] / "project.json").exists()
    finally:
        if project is not None:
            shutil.rmtree(SHOT_DESK_DIR / project["id"], ignore_errors=True)
        if before is None:
            chars_file.unlink(missing_ok=True)
        else:
            chars_file.write_bytes(before)


def test_resolve_safe_bg_music_path(monkeypatch):
    from shot_desk import resolve_safe_bg_music_path, SHOT_DESK_DIR

    music_dir = REPO_ROOT / "workspace" / "_music"
    music_dir.mkdir(parents=True, exist_ok=True)
    good = music_dir / "test_fixes_bgm.mp3"
    good.write_bytes(b"\x00" * 100)
    try:
        assert resolve_safe_bg_music_path(str(good), "any_proj") == good.resolve()
        # Path tương đối trong workspace/_music.
        assert resolve_safe_bg_music_path("_music/test_fixes_bgm.mp3", "any_proj") == good.resolve()
        # File trong thư mục project cũng được.
        proj_dir = SHOT_DESK_DIR / "proj_bgm"
        proj_dir.mkdir(parents=True, exist_ok=True)
        try:
            inner = proj_dir / "bgm.mp3"
            inner.write_bytes(b"\x00" * 10)
            assert resolve_safe_bg_music_path(str(inner), "proj_bgm") == inner.resolve()
        finally:
            shutil.rmtree(proj_dir, ignore_errors=True)
        # Cấm tuyệt đối ngoài workspace và ../
        for bad in ("/etc/passwd", "/tmp/evil.mp3",
                    "../secret.mp3", "_music/../../etc/passwd",
                    "../../etc/passwd", "/etc/hostname"):
            with pytest.raises(ValueError):
                resolve_safe_bg_music_path(bad, "proj_bgm")
        assert resolve_safe_bg_music_path(None, "proj_bgm") is None
        assert resolve_safe_bg_music_path("", "proj_bgm") is None
    finally:
        good.unlink(missing_ok=True)


def test_env_example_has_no_usable_secret():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("MUSE2API_KEY=") or stripped.startswith("STUDIO_API_KEY="):
            value = stripped.split("=", 1)[1].strip()
            assert value == "", f"{stripped.split('=')[0]} phải để trống (placeholder), không ghi giá trị dùng được"
    assert "X-API-Key" in text
    assert "MUSE2API_KEY" in text and "STUDIO_API_KEY" in text
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore, ".gitignore phải có .env"
