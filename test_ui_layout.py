"""Regression tests for the desktop shell layout (ai-movie-studio).

Bug (2026-11-08, screenshot from Tien): the main content was squeezed into
the narrow 272px left column and the right side was empty. Root cause:
``<div id="sidebarOverlay" class="sidebar-overlay">`` sits DIRECTLY inside
``.shell`` (``display:grid; grid-template-columns: 272px 1fr``) between
``<aside class="sidebar">`` and ``<main class="main">``, and it had NO CSS
rule at all — so it became a normal grid item, stole row 2 / col 2 (the
cell meant for ``<main>``) and pushed ``<main>`` down to implicit row 3,
col 1 (the narrow 272px column).

The fix: ``.sidebar-overlay`` must be taken out of the grid flow
(``position: fixed``) and hidden by default (``display: none``), only
shown while the sidebar is open as a drawer (``body.sidebar-open``,
mobile <=1024px — the mechanism in ``static/js/ui.js``).

Pure-Python checks (no browser needed): parse ``static/index.html`` for
the ``.shell`` children and parse the CSS for the overlay / drawer /
wizard rules.

Set ``AMS_REPO_ROOT`` to point the tests at a different checkout
(used to prove the test FAILS on the pre-fix code).
"""

import os
import re
from html.parser import HTMLParser

import pytest

REPO_ROOT = os.environ.get("AMS_REPO_ROOT", os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")
CSS_FILES = [
    os.path.join(REPO_ROOT, "static", "css", "app.css"),
    os.path.join(REPO_ROOT, "static", "css", "ui.css"),
]


# --------------------------------------------------------------------------
# Minimal CSS parser: (selector, declarations dict, media query or None)
# --------------------------------------------------------------------------
def _strip_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _parse_rules(text, media=None):
    """Yield (selector, {prop: value}, media) for every rule block."""
    i, n = 0, len(text)
    while i < n:
        brace = text.find("{", i)
        if brace == -1:
            break
        selector = text[i:brace].strip()
        depth = 1
        j = brace + 1
        while j < n and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        body = text[brace + 1 : j - 1]
        if selector.startswith("@media"):
            yield from _parse_rules(body, media=selector)
        elif selector and not selector.startswith("@"):
            decls = {}
            for part in body.split(";"):
                if ":" in part:
                    prop, _, val = part.partition(":")
                    decls[prop.strip().lower()] = val.strip().lower()
            for sel in selector.split(","):
                yield (sel.strip(), decls, media)
        i = j


def load_css_rules():
    rules = []
    for path in CSS_FILES:
        with open(path, encoding="utf-8") as fh:
            rules.extend(_parse_rules(_strip_comments(fh.read())))
    return rules


def find_rules(rules, selector):
    return [(d, m) for (s, d, m) in rules if s == selector]


# --------------------------------------------------------------------------
# HTML: direct children of .shell
# --------------------------------------------------------------------------
class _ShellChildrenParser(HTMLParser):
    """Collect the direct children of the element with class 'shell'."""

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__()
        self.stack = []  # (tag, attrs dict, is_shell)
        self.shell_children = []
        self.shell_depth = None

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID_TAGS:
            return  # void elements never close: keep them off the stack
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        is_shell = "shell" in classes
        if self.shell_depth is not None and len(self.stack) == self.shell_depth:
            self.shell_children.append(
                (tag, attrs.get("id", ""), attrs.get("class", ""))
            )
        self.stack.append((tag, attrs, is_shell))
        if is_shell and self.shell_depth is None:
            self.shell_depth = len(self.stack)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            _, _, was_shell = self.stack.pop()
            if was_shell:
                self.shell_depth = None
        elif self.stack:
            self.stack.pop()


def shell_children():
    parser = _ShellChildrenParser()
    with open(INDEX_HTML, encoding="utf-8") as fh:
        parser.feed(fh.read())
    return parser.shell_children


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_shell_has_exactly_the_expected_direct_children():
    """The grid must only ever place topbar / sidebar / main.

    #sidebarOverlay is allowed to stay in the HTML as a direct child
    (JS hooks onto it), but the CSS tests below must take it out of the
    grid flow — otherwise it steals <main>'s cell.
    """
    children = shell_children()
    kinds = {(tag, cls.split()[0] if cls else "") for (tag, _id, cls) in children}
    assert ("header", "topbar") in kinds
    assert ("aside", "sidebar") in kinds
    assert ("main", "main") in kinds
    assert ("div", "sidebar-overlay") in kinds  # present in HTML, fixed by CSS


def test_sidebar_overlay_is_out_of_grid_flow_and_hidden_by_default():
    rules = load_css_rules()
    base = find_rules(rules, ".sidebar-overlay")
    assert base, ".sidebar-overlay has no CSS rule at all — it becomes a grid item"
    decls, _media = base[0]
    assert decls.get("position") in ("fixed", "absolute"), (
        ".sidebar-overlay must be position:fixed/absolute so it is not a "
        f"grid item (got position={decls.get('position')!r})"
    )
    assert decls.get("display") == "none", (
        ".sidebar-overlay must be display:none by default "
        f"(got display={decls.get('display')!r})"
    )
    assert "grid-column" not in decls and "grid-row" not in decls, (
        ".sidebar-overlay must not carry any grid placement"
    )


def test_sidebar_overlay_shows_only_when_drawer_is_open():
    rules = load_css_rules()
    open_rules = find_rules(rules, "body.sidebar-open .sidebar-overlay")
    assert open_rules, "no rule shows the overlay when body.sidebar-open"
    decls, _media = open_rules[0]
    assert decls.get("display") in ("block", "flex", "grid") or decls.get(
        "visibility"
    ) == "visible", (
        "body.sidebar-open .sidebar-overlay must make the overlay visible, "
        f"got {decls}"
    )


def test_mobile_drawer_rules_exist():
    """The JS (ui.js) toggles body.sidebar-open at <=1024px; CSS must
    answer it, otherwise the hamburger shows a dim overlay with no drawer.
    """
    rules = load_css_rules()
    drawer = [
        (d, m)
        for (s, d, m) in rules
        if s == ".sidebar" and m and "1024" in m
    ]
    assert drawer, "no .sidebar rule inside a max-width:1024px media query"
    assert drawer[0][0].get("position") == "fixed", (
        "mobile .sidebar must be position:fixed (drawer), "
        f"got {drawer[0][0].get('position')!r}"
    )
    assert drawer[0][0].get("display") == "flex", (
        "mobile .sidebar must restore display:flex (app.css hides it at "
        "<=768px), got "
        f"{drawer[0][0].get('display')!r}"
    )
    open_sidebar = find_rules(rules, "body.sidebar-open .sidebar")
    assert open_sidebar, "no body.sidebar-open .sidebar rule to open the drawer"
    assert "translatex(0)" in open_sidebar[0][0].get("transform", "").replace(
        " ", ""
    ), f"drawer open rule must reset the transform, got {open_sidebar[0][0]}"


def test_wizard_step_buttons_have_a_base_rule():
    """Regression for the unstyled default-browser-button look in the
    screenshot: .wizard-step-btn only had :hover/:focus-visible rules."""
    rules = load_css_rules()
    base = find_rules(rules, ".wizard-step-btn")
    assert base, "no base .wizard-step-btn rule"
    decls = base[0][0]
    assert "display" in decls and "padding" in decls, (
        f"base .wizard-step-btn rule must style the button, got {decls}"
    )
    assert find_rules(rules, ".wizard-step-btn.is-active"), (
        "no .wizard-step-btn.is-active rule"
    )
    assert find_rules(rules, ".wizard-step-btn .wizard-num"), (
        "no .wizard-step-btn .wizard-num rule"
    )


def test_wizard_steps_have_a_display_rule():
    """.wizard-step is display:none; every active step needs a display
    rule — the old code only had one for [data-step="2"], so steps 1 and
    3 showed nothing."""
    rules = load_css_rules()
    active = find_rules(rules, ".wizard-step.is-active")
    assert active, "no .wizard-step.is-active display rule"
    assert active[0][0].get("display") == "block", (
        f".wizard-step.is-active must be display:block, got {active[0][0]}"
    )
    step2 = [
        (d, m)
        for (s, d, m) in rules
        if s == '.wizard-step.is-active[data-step="2"]'
    ]
    assert step2 and step2[0][0].get("display") == "grid", (
        "the step-2 grid rule must be kept (it wins over the block rule "
        "by specificity)"
    )
