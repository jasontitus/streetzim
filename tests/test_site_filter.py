"""The download page's filter box must actually hide cards.

Reported broken on 2026-09-27: "Website filter zim is broken. Doesn't filter."
The JavaScript was correct -- it set `card.hidden = true` on every non-match --
but `.maps [hidden] { display: none !important }`, the rule that makes that
visible over `.map-card { display: flex }`, had been written INSIDE
`.maps-tier-header:first-child { ... }`. Browsers read that as CSS nesting, so
it resolved to `.maps-tier-header:first-child .maps [hidden]` and matched
nothing. Four more selectors went with it, so the search input was unstyled too.

A DOM assertion would have passed: `hidden` really was set. These tests check
the stylesheet's shape, which is where the defect lived; the behavioural half
is cloud/site_filter_gate.mjs, run here when a browser is available.
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "web" / "template.html"
PAGE = ROOT / "web" / "index.html"


def _generator():
    spec = importlib.util.spec_from_file_location("web_generate", ROOT / "web" / "generate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["web_generate"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_hidden_override_is_a_top_level_rule():
    css = TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"^  \.maps \[hidden\] \{[^}]*display:\s*none", css, re.M), (
        "`.maps [hidden]` is absent or indented like a nested rule")


def test_nothing_is_nested_inside_the_first_child_header_rule():
    css = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(r"\n  \.maps-tier-header:first-child \{(.*?)\n  \}", css, re.S)
    assert block, "the :first-child header rule is gone"
    nested = re.findall(r"^\s+[.#][\w-]+.*\{", block.group(1), re.M)
    assert not nested, f"selectors nested inside the header rule: {nested}"


def test_the_filter_selectors_all_survive_at_top_level():
    css = TEMPLATE.read_text(encoding="utf-8")
    for sel in (".maps-filter", "#map-filter", "#map-filter:focus",
                ".maps-filter-count", ".maps [hidden]"):
        assert re.search(r"^  " + re.escape(sel) + r"[ ,{:]", css, re.M), (
            f"{sel} is not a top-level rule")


def test_the_generator_refuses_to_write_a_page_that_cannot_filter():
    """The guard, not just the current file: prove it rejects the shipped bug."""
    gen = _generator()
    good = PAGE.read_text(encoding="utf-8")
    gen.check_filter_css(good)                       # must not raise
    broken = good.replace(
        "  .maps [hidden] { display: none !important; }",
        "  /* .maps [hidden] removed */")
    with pytest.raises(ValueError):
        gen.check_filter_css(broken)


def test_the_guard_does_not_fire_on_a_page_without_a_filter():
    _generator().check_filter_css("<html><body>no maps here</body></html>")


# --- the behavioural half -------------------------------------------------

CHROME = os.environ.get(
    "CHROME_PATH",
    "/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome")
NODE = ROOT / ".browser-libs" / "node-v20.18.1-linux-x64" / "bin" / "node"


@pytest.mark.skipif(not (Path(CHROME).exists() and NODE.exists() and PAGE.exists()),
                    reason="no browser/node/page on this host")
def test_filtering_the_real_page_in_a_browser_hides_cards():
    env = dict(os.environ)
    env["CHROME_PATH"] = CHROME
    env["LD_LIBRARY_PATH"] = (
        f"{ROOT}/.browser-libs/ex/usr/lib/x86_64-linux-gnu:"
        + env.get("LD_LIBRARY_PATH", ""))
    r = subprocess.run([str(NODE), "cloud/site_filter_gate.mjs", str(PAGE), "france"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
