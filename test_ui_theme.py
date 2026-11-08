"""Hoi quy Light theme: chu tren nen sang phai du tuong phan (WCAG).

Bug goc (2026-11-08, anh chup man hinh cua Tien): [data-theme="light"] chi
override accent + --bg*, KHONG override --text/--border -> chu #fafafa tren
nen #ffffff (tuong phan 1.0:1, "trang xoa"), vien tang hinh. Nut wizard cung
khong co rule goc nen hien nhu nut mac dinh cua trinh duyet.

Test thuan Python, khong can trinh duyet: parse custom properties tu CSS,
tinh ti le tuong phan WCAG, va assert cac rule wizard ton tai.
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
UI_CSS = (ROOT / "static" / "css" / "ui.css").read_text(encoding="utf-8")


def _custom_props(block: str) -> dict:
    """Trich --ten: gia-tri; trong mot block CSS (bo qua comment)."""
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


def _block(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{", css)
    assert m, f"khong tim thay selector {selector!r}"
    depth, i = 0, m.end() - 1
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[m.end():i]
        i += 1
    raise AssertionError(f"block {selector!r} khong dong ngoac")


def resolve_tokens():
    """Dark = app.css :root + ui.css :root (ui.css nap sau, thang khi trung).
    Light = dark + [data-theme="light"] override."""
    dark = _custom_props(_block(APP_CSS, ":root"))
    dark.update(_custom_props(_block(UI_CSS, ":root")))
    light = dict(dark)
    light.update(_custom_props(_block(UI_CSS, '[data-theme="light"]')))
    return dark, light


def _hex_to_rgb(value: str):
    value = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    assert m, f"gia tri mau khong phai hex 6 ky tu: {value!r}"
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(rgb) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    l1, l2 = _luminance(_hex_to_rgb(fg)), _luminance(_hex_to_rgb(bg))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _assert_theme(name, tokens):
    text, secondary, muted = tokens["--text"], tokens["--text-secondary"], tokens["--muted"]
    for bg_key in ("--bg0", "--bg1"):
        bg = tokens[bg_key]
        assert contrast(text, bg) >= 4.5, (
            f"{name}: --text {text} tren {bg_key} {bg} chi {contrast(text, bg):.2f}:1 (< 4.5)")
        assert contrast(secondary, bg) >= 3.0, (
            f"{name}: --text-secondary {secondary} tren {bg_key} {bg} chi {contrast(secondary, bg):.2f}:1 (< 3.0)")
        assert contrast(muted, bg) >= 3.0, (
            f"{name}: --muted {muted} tren {bg_key} {bg} chi {contrast(muted, bg):.2f}:1 (< 3.0)")


def test_light_theme_overrides_all_text_and_border_tokens():
    _, light = resolve_tokens()
    for key in ("--text", "--text-secondary", "--muted",
                "--border", "--border-light", "--border-hover"):
        assert key in light, f"[data-theme=light] thieu override {key}"
    # Border sang phai dung den (rgba(0,0,0,...)), khong phai trang.
    assert light["--border"].replace(" ", "").startswith("rgba(0,0,0"), (
        f"--border light phai la den mo, got {light['--border']!r}")
    assert light["--border-light"].replace(" ", "").startswith("rgba(0,0,0"), (
        f"--border-light light phai la den mo, got {light['--border-light']!r}")


def test_contrast_dark_theme():
    dark, _ = resolve_tokens()
    _assert_theme("dark", dark)


def test_contrast_light_theme():
    _, light = resolve_tokens()
    _assert_theme("light", light)


def test_wizard_step_buttons_have_base_style():
    # Rule goc .wizard-step-btn (khong phai :hover/:focus) phai ton tai va
    # dinh nghia nen + vien + chu -- neu khong nut hien nhu nut mac dinh.
    m = re.search(r"\.wizard-step-btn\s*\{([^}]*)\}", UI_CSS)
    assert m, "thieu rule goc .wizard-step-btn { ... }"
    body = m.group(1)
    for prop in ("background", "border", "color"):
        assert prop in body, f".wizard-step-btn thieu {prop}"
    assert ".wizard-step-btn.is-active" in UI_CSS, "thieu .wizard-step-btn.is-active"
    assert ".wizard-num" in UI_CSS, "thieu .wizard-num"


def test_wizard_steps_hidden_until_active():
    # Step an mac dinh, chi step active hien; step 2 giu rule grid
    # (specificity .wizard-step.is-active[data-step="2"] thang rule block).
    assert re.search(r"\.wizard-step\s*\{\s*display:\s*none", UI_CSS), (
        "thieu .wizard-step { display: none }")
    assert re.search(r"\.wizard-step\.is-active\s*\{\s*display:\s*block", UI_CSS), (
        "thieu .wizard-step.is-active { display: block }")
    m = re.search(
        r"\.wizard-step\.is-active\[data-step=\"2\"\]\s*\{([^}]*)\}", UI_CSS)
    assert m and "display: grid" in m.group(1), (
        "thieu rule grid cho .wizard-step.is-active[data-step=\"2\"]")
