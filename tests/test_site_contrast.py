"""Text on the download page must be readable against what is behind it.

Reported 2026-09-27: "filter text is white one white". The palette is
dark-only -- no prefers-color-scheme anywhere on the page -- and the filter
input declared `background: #fff; color: inherit`, so it inherited the body's
near-white --text onto a white field. Typing produced invisible text.

It had been that way since the filter was added on 2026-09-22 but nobody could
see it, because the whole block was nested inside another rule and never
applied. Un-nesting it so the filter worked is what made this visible -- one
dead rule hiding another.

A string check ("does it say #fff") would not survive the next palette tweak,
so this computes WCAG contrast from the declared custom properties.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "web" / "template.html").read_text(encoding="utf-8")

MIN_CONTRAST = 4.5          # WCAG AA for body text


def root_vars():
    block = re.search(r":root \{(.*?)\}", CSS, re.S)
    assert block, "no :root block"
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block.group(1)))


def rule(selector):
    m = re.search(r"\n  " + re.escape(selector) + r" \{(.*?)\n  \}", CSS, re.S)
    assert m, f"no rule for {selector}"
    # Split on ';', not per line: `background: a; color: b;` on one line is
    # ordinary CSS, and a line-anchored regex silently sees only the first.
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    decls = {}
    for part in body.split(";"):
        if ":" not in part:
            continue
        prop, _, value = part.partition(":")
        prop = prop.strip()
        if re.fullmatch(r"[a-z-]+", prop):
            decls[prop] = value.strip()
    return decls


def resolve(value, variables, depth=0):
    """Follow var(--x, fallback) chains down to a literal colour."""
    value = value.split("/*")[0].strip()
    m = re.fullmatch(r"var\(--([\w-]+)(?:,\s*(.*))?\)", value)
    if m and depth < 5:
        name, fallback = m.group(1), m.group(2)
        if name in variables:
            return resolve(variables[name], variables, depth + 1)
        if fallback:
            return resolve(fallback, variables, depth + 1)
        return None
    return value


def rgb(colour):
    colour = colour.strip()
    if colour.startswith("#"):
        h = colour[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.fullmatch(r"rgba?\(([^)]+)\)", colour)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        return tuple(int(float(p)) for p in parts[:3])
    return None


def luminance(channels):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    lf, lb = luminance(fg), luminance(bg)
    hi, lo = max(lf, lb), min(lf, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_the_page_really_is_dark_only():
    """The premise of every check below: there is no light-mode palette."""
    assert "prefers-color-scheme" not in CSS, (
        "the page now has a light mode, so these single-palette contrast "
        "checks need to run per scheme")


def test_the_filter_input_declares_its_own_colours():
    decls = rule("#map-filter")
    assert decls.get("color", "").strip() != "inherit", (
        "`color: inherit` on a field with its own background is how the "
        "white-on-white bug happened")
    assert "background" in decls, "the input must state its background"


def test_filter_text_is_readable_on_the_filter_background():
    variables = root_vars()
    decls = rule("#map-filter")
    # An absent or `inherit` colour is not "no colour": the field shows the
    # body's text colour, which is what made it white on white. Resolve it the
    # way the browser does so the failure reports the real contrast.
    declared = decls.get("color", "inherit").strip()
    if declared in ("inherit", "unset", "initial"):
        declared = variables["text"]
    fg = rgb(resolve(declared, variables))
    bg = rgb(resolve(decls["background"], variables))
    assert fg and bg, f"could not resolve colours: {decls}"
    ratio = contrast(fg, bg)
    assert ratio >= MIN_CONTRAST, (
        f"filter text {fg} on {bg} is {ratio:.1f}:1, under {MIN_CONTRAST}:1")


def test_the_placeholder_is_dimmer_but_still_visible():
    variables = root_vars()
    bg = rgb(resolve(rule("#map-filter")["background"], variables))
    m = re.search(r"#map-filter::placeholder \{([^}]*)\}", CSS)
    assert m, "no placeholder colour, so it falls back to the UA's grey"
    colour = re.search(r"color:\s*([^;]+);", m.group(1))
    assert colour, "placeholder rule sets no colour"
    ratio = contrast(rgb(resolve(colour.group(1), variables)), bg)
    # Placeholders are allowed to be dimmer than body text, not invisible.
    assert ratio >= 3.0, f"placeholder contrast {ratio:.1f}:1 is too low"


@pytest.mark.parametrize("selector,fg_prop", [
    ("body", "color"),
    (".map-card-title", "color"),
])
def test_core_text_is_readable_on_the_page_background(selector, fg_prop):
    variables = root_vars()
    bg = rgb(resolve(variables["bg"], variables))
    decls = rule(selector) if selector != "body" else None
    fg_value = decls[fg_prop] if decls else variables["text"]
    ratio = contrast(rgb(resolve(fg_value, variables)), bg)
    assert ratio >= MIN_CONTRAST, f"{selector} is {ratio:.1f}:1 on the page background"
