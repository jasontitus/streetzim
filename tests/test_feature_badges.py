"""The download-page feature badges must match what the ZIM actually contains.

On 2026-09-27 both Light variants -- the two products whose entire selling
point is "no satellite imagery, roughly half the download" -- carried a
"Satellite" badge on streetzim.web.app. Their own streetzim-meta.json said
hasSatellite: false and neither ZIM held a single satellite/ entry; the badge
came from cloud/upload_validated.sh passing --satellite unconditionally on
every upload, which stamped streetzim_satellite=yes on the archive.org item.

The badge renderer was never wrong -- it faithfully showed the metadata it was
given, and its docstring promises "no false advertising". The lie was upstream.
These tests pin both halves: the stamp is derived from the ZIM, and the caller
does not go behind it.
"""
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


stamp_mod = _load("stamp_item_metadata", ROOT / "cloud" / "stamp_item_metadata.py")


# --- the stamp side -------------------------------------------------------

def test_every_badge_feature_has_a_meta_key():
    """A feature with no meta key could never be denied, only asserted."""
    assert set(stamp_mod.FEATURES) == set(stamp_mod.META_KEYS), (
        "FEATURES and META_KEYS disagree; a feature without a meta key falls "
        "back to whatever the item already said")


def test_meta_keys_are_the_ones_builds_actually_write():
    """Guards a rename in create_osm_zim.py from silently voiding the mapping."""
    src = (ROOT / "create_osm_zim.py").read_text(encoding="utf-8")
    for feature, key in stamp_mod.META_KEYS.items():
        assert f'"{key}"' in src, (
            f"{feature} maps to {key}, which create_osm_zim.py never writes")


def test_explicit_flags_only_ever_say_yes():
    """--satellite on the command line must not be able to write "no".

    Batch-stamping an old item means "I know it has routing"; it never means
    "I know it lacks satellite".
    """
    src = (ROOT / "cloud" / "stamp_item_metadata.py").read_text(encoding="utf-8")
    m = re.search(r"else:\s*\n\s*flags = \{f: \"yes\" for f in FEATURES", src)
    assert m, "the non---from-zim branch no longer hard-codes yes"


# --- the caller side ------------------------------------------------------

def test_upload_does_not_assert_features_it_has_not_checked():
    """The regression itself: unconditional --satellite on every upload."""
    sh = (ROOT / "cloud" / "upload_validated.sh").read_text(encoding="utf-8")
    # Follow backslash continuations: the flags that caused this bug sat on the
    # LINE AFTER the script name, so matching single lines would have missed
    # them -- which is exactly the shape a future regression would take.
    lines = sh.splitlines()
    call = []
    for i, line in enumerate(lines):
        if "stamp_item_metadata.py" not in line or line.lstrip().startswith("#"):
            continue
        cmd, j = line, i
        while cmd.rstrip().endswith("\\") and j + 1 < len(lines):
            j += 1
            cmd = cmd.rstrip()[:-1] + " " + lines[j]
        call.append(cmd)
    assert call, "upload_validated.sh no longer stamps at all"
    joined = " ".join(call)
    assert "--from-zim" in joined, "the stamp is not derived from the shipped ZIM"
    for flag in ("--satellite", "--terrain", "--wikidata", "--overture", "--routing"):
        assert flag not in joined, (
            f"upload_validated.sh asserts {flag} regardless of the ZIM's contents")


# --- the render side ------------------------------------------------------

@pytest.fixture(scope="module")
def render():
    os.environ.setdefault("STREETZIM_NO_NETWORK", "1")
    gen = _load("web_generate", ROOT / "web" / "generate.py")
    return gen.render_feature_badges


def test_a_no_hides_the_badge(render):
    html = render({"streetzim_routing": "yes", "streetzim_satellite": "no"})
    assert "badge-nav" in html
    assert "badge-satellite" not in html, '"no" must not render a badge'


def test_an_absent_field_hides_the_badge(render):
    html = render({"streetzim_routing": "yes"})
    assert "badge-satellite" not in html


def test_no_features_renders_no_empty_row(render):
    assert render({}) == ""
    assert render({"streetzim_satellite": "no"}) == ""


# --- end to end, on a real Light ZIM if one is on the host ----------------

LIGHT = sorted(ROOT.glob("osm-*-light-20??-??-??*.zim"))


@pytest.mark.skipif(not LIGHT, reason="no Light ZIM on this host")
def test_a_light_zim_yields_satellite_no_and_renders_no_badge(render):
    pytest.importorskip("libzim.reader")
    zim = LIGHT[-1]
    flags = stamp_mod.flags_from_zim(str(zim))
    assert flags["satellite"] == "no", f"{zim.name} should report no satellite"
    item_meta = {stamp_mod.FEATURES[f]: v for f, v in flags.items()}
    assert "badge-satellite" not in render(item_meta)
    # ...and the claim is true of the bytes, not just the metadata.
    from libzim.reader import Archive
    arc = Archive(str(zim))
    meta = json.loads(bytes(
        arc.get_entry_by_path("streetzim-meta.json").get_item().content))
    assert meta["hasSatellite"] is False
