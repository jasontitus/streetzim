"""Fallback glyphs (streetzim/glyph_fallback.py): Noto Sans glyphs for the
scripts Open Sans lacks are merged into the Open Sans ranges, only for the
scripts the map's labels use, leaving every other range byte-identical."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import glyph_fallback as gf, tiles, viewer_assets as va, zim_writer  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import pin_viewer_assets as pin  # noqa: E402


def _v(n: int) -> bytes:
    return gf._enc_varint(n)


def _ld(field: int, data: bytes) -> bytes:
    return _v(field << 3 | 2) + _v(len(data)) + data


def glyph(cp: int, tag: bytes = b"") -> bytes:
    # id, bitmap, width, height, left, top, advance
    return _ld(3, _v(1 << 3) + _v(cp) + _ld(2, b"sdf" + tag) + b"\x18\x05\x20\x05\x28\x02\x30\x10\x38\x0a")


def glyph_pbf(name: str, rng: str, cps, tag: bytes = b"") -> bytes:
    stack = _ld(1, name.encode()) + _ld(2, rng.encode()) + b"".join(glyph(c, tag) for c in cps)
    return _ld(1, stack)


ARABIC = [(0x600, 0x6FF)]


def test_merge_adds_only_missing_glyphs_in_the_blocks():
    primary = glyph_pbf("Open Sans Regular", "1536-1791", [0x60C], b"os")
    fallback = glyph_pbf("Noto Sans Regular", "1536-1791", [0x60C, 0x627, 0x644], b"noto")
    out = gf.merge_range(primary, fallback, [(0x620, 0x63F)])
    assert gf.glyph_ids(out) == {0x60C, 0x627}          # 0x644 is outside the block
    assert out[2:].startswith(primary[2:])               # Open Sans message kept as is
    assert b"sdfos" in out and out.count(b"sdfnoto") == 1  # Open Sans wins for 0x60C
    assert gf.is_glyph_pbf(out)


def test_merge_without_additions_returns_the_primary_unchanged():
    primary = glyph_pbf("Open Sans Regular", "0-255", [0x41, 0x42])
    fallback = glyph_pbf("Noto Sans Regular", "0-255", [0x41])
    assert gf.merge_range(primary, fallback, [(0, 255)]) is primary


def test_merge_into_an_absent_range_makes_a_fontstack():
    fallback = glyph_pbf("Noto Sans Regular", "1536-1791", [0x627])
    out = gf.merge_range(None, fallback, ARABIC, name="Open Sans Regular", range_key="1536-1791")
    assert gf.glyph_ids(out) == {0x627}
    assert b"Open Sans Regular" in out and b"1536-1791" in out


def test_html_error_page_is_not_a_glyph_range():
    assert not gf.is_glyph_pbf(b"<!DOCTYPE html>\n<html>")
    assert not gf.is_glyph_pbf(b"")
    assert gf.is_glyph_pbf(glyph_pbf("Open Sans Regular", "1536-1791", []))


def test_blocks_and_ranges():
    blocks = gf.parse_blocks(["0590-05FF", "FB1D-FB4F", "FE70-FEFF"])
    assert gf.ranges_for(blocks) == ["1280-1535", "64256-64511", "65024-65279"]
    with pytest.raises(ValueError):
        gf.parse_blocks(["06FF-0600"])


def mvt(*values: str, key: str = "name", layer_extra: bytes = b"") -> bytes:
    """One layer, one feature per value, each tagged ``key=value``."""
    feats = b"".join(_ld(2, _ld(2, _v(0) + _v(i)) + _ld(4, _v(9) + _v(2) + _v(2)))
                     for i in range(len(values)))
    layer = (_ld(1, b"poi") + layer_extra + feats + _ld(3, key.encode())
             + b"".join(_ld(4, _ld(1, s.encode())) for s in values))
    return _ld(3, layer)


SCRIPTS = {"Arabic": ARABIC, "Hebrew": [(0x590, 0x5FF)], "Thai": [(0xE00, 0xE7F)]}


def test_scripts_in_tiles_reads_string_values():
    tiles_ = [gzip.compress(mvt("Main Street", "Café")),
              mvt("شارع الملك", "Nguyễn Huệ"),
              gzip.compress(mvt("ถนน"))]
    assert gf.scripts_in_tiles(tiles_, SCRIPTS) == {"Arabic", "Thai"}


def test_script_bytes_outside_string_values_do_not_count():
    # Hebrew bytes inside a feature (geometry), not a string value.
    feature = _ld(2, _ld(4, "שלום".encode()))
    assert gf.scripts_in_tiles([mvt("Main Street", layer_extra=feature)], SCRIPTS) == set()


def test_names_the_style_does_not_display_do_not_count():
    # An OpenMapTiles-schema tile (OpenFreeMap, --mbtiles) carries name:ar,
    # name:he, ... on every feature; the style shows name:latin/name_int/name.
    tile = gzip.compress(mvt("موناكو", key="name:ar"))
    assert gf.scripts_in_tiles([tile, mvt("Monaco")], SCRIPTS) == set()
    assert gf.scripts_in_tiles([mvt("موناكو", key="name_int")], SCRIPTS) == {"Arabic"}


def _js_value(src: str, i: int) -> str:
    """The JS array or string literal starting at src[i] (quotes of either
    kind, escapes, nesting and line breaks allowed); AssertionError for
    anything else, e.g. a variable, which this test cannot see through."""
    while src[i].isspace():
        i += 1
    start, depth, quote = i, 0, None
    assert src[i] in "[\"'", f"text-field is not a literal: {src[i:i + 60]!r}"
    while True:
        c = src[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
                if depth == 0:
                    return src[start:i + 1]
        elif c in "\"'":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def text_field_keys(src: str) -> set[str]:
    """Every property a text-field reads, as a style layout key
    ("text-field": ...) or a setLayoutProperty(id, 'text-field', ...) call."""
    import re
    keys: set[str] = set()
    found = 0
    for m in re.finditer(r"""(["']?)text-field\1\s*[:,]""", src):
        found += 1
        expr = _js_value(src, m.end())
        gets = re.findall(r"""\[\s*(["'])get\1\s*,\s*(["'])(.*?)\2\s*[\],]""", expr, re.S)
        assert len(gets) == len(re.findall(r"""(["'])get\1""", expr)), \
            f"a get with a computed key: {expr!r}"
        keys |= {g[2] for g in gets}
        keys |= set(re.findall(r"\{([^{}]+)\}", expr)) if expr[0] in "\"'" else set()
    assert found, "no text-field found"
    return keys


def test_text_field_parser_sees_through_quotes_and_line_breaks():
    src = ("'text-field': ['get','ref'],\n"
           '"text-field": ["concat",\n  ["get", "name"],\n  " ",\n  [\'get\', "ele"]],\n'
           "map.setLayoutProperty('x', 'text-field', '{housenumber}');\n")
    assert text_field_keys(src) == {"ref", "name", "ele", "housenumber"}
    with pytest.raises(AssertionError):
        text_field_keys('"text-field": labelExpr,')
    with pytest.raises(AssertionError):
        text_field_keys('"text-field": ["get", key],')


def test_label_keys_are_what_the_style_displays():
    src = (ROOT / "resources" / "viewer" / "index.html").read_text(encoding="utf-8")
    keys = text_field_keys(src)
    assert keys and keys <= gf.LABEL_KEYS, keys - gf.LABEL_KEYS


def test_corrupt_tiles_are_skipped_and_counted():
    stats: dict = {}
    corrupt_gzip = gzip.compress(mvt("شارع"))[:-12]
    bad_mvt = mvt("شارع")[:-3]  # truncated
    assert gf.scripts_in_tiles([corrupt_gzip, bad_mvt, mvt("רחוב")], SCRIPTS, stats) == {"Hebrew"}
    assert stats["unreadable"] == 2


def test_scan_stops_once_every_script_is_found():
    seen = []

    def gen():
        for t in (mvt("شارع"), mvt("רחוב"), mvt("ถนน"), b"never read"):
            seen.append(t)
            yield t
    assert gf.scripts_in_tiles(gen(), SCRIPTS) == set(SCRIPTS)
    assert len(seen) == 3


# ---- the build: generate_sdf_font_glyphs with a fallback --------------------

STACKS = {"OpenSansRegular": "Open Sans Regular", "OpenSansBold": "Open Sans Bold"}
FB_STACKS = {"NotoSansRegular": "Noto Sans Regular"}
FB_RANGES = ["1280-1535", "1536-1791"]


def served(url: str) -> bytes:
    """What the fake servers return for a URL."""
    rng = url.rsplit("/", 1)[1][:-4]
    start = int(rng.split("-")[0])
    if "Noto" in url:
        return glyph_pbf("Noto Sans Regular", rng, range(start, start + 256, 7), b"noto")
    return glyph_pbf("Open Sans", rng, [start + 1], b"os")


def make_lock() -> dict:
    fonts_base, fb_base = "https://fonts.example", "https://fb.example/fonts"
    ranges = {s: {r: hashlib.sha256(served(f"{fonts_base}/{n}/{r}.pbf")).hexdigest()
                  for r in pin.RANGES} for s, n in STACKS.items()}
    fb_ranges = {s: {r: hashlib.sha256(served(f"{fb_base}/Noto/{r}.pbf")).hexdigest()
                     for r in FB_RANGES} for s in FB_STACKS}
    licence = ROOT / "resources" / "vendor" / "noto-sans" / "OFL.txt"
    return {"fonts": {
        "base_url": fonts_base, "fontstacks": STACKS, "ranges": ranges,
        "fallback": {
            "base_url": fb_base, "fontstacks": FB_STACKS, "ranges": fb_ranges,
            "for": {"OpenSansRegular": "NotoSansRegular", "OpenSansBold": "NotoSansRegular"},
            "scripts": {"Arabic": ["0600-06FF"], "Hebrew": ["0590-05FF"]},
            "licence_file": "resources/vendor/noto-sans/OFL.txt",
            "licence_sha256": hashlib.sha256(licence.read_bytes()).hexdigest(),
        }}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(va, "BAKED", tmp_path / "baked")
    monkeypatch.setattr(va.time, "sleep", lambda s: None)


def _serve(monkeypatch, fail=None):
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        if fail and fail in req.full_url:
            raise OSError("unreachable")
        return _Resp(served(req.full_url))
    monkeypatch.setattr(va.urllib.request, "urlopen", urlopen)
    return calls


def test_fallback_scripts_are_merged_and_nothing_else_changes(monkeypatch):
    _serve(monkeypatch)
    lock = make_lock()
    plain = tiles.generate_sdf_font_glyphs(lock, scripts=set())
    fonts = tiles.generate_sdf_font_glyphs(lock, scripts={"Arabic"})
    changed = {k for k in fonts if plain.get(k) != fonts[k]}
    assert changed == {("OpenSansRegular", "1536-1791"), ("OpenSansBold", "1536-1791"),
                       ("NotoSans", "OFL.txt")}
    ids = gf.glyph_ids(fonts[("OpenSansBold", "1536-1791")])
    assert 1537 in ids and 1536 in ids and not any(0x590 <= i <= 0x5FF for i in ids)
    assert fonts[("NotoSans", "OFL.txt")].startswith(b"Copyright")
    assert not any(k[0].startswith("Noto") and k[1].endswith(".pbf") for k in fonts)


def test_no_fallback_scripts_fetches_no_fallback_ranges(monkeypatch):
    calls = _serve(monkeypatch)
    fonts = tiles.generate_sdf_font_glyphs(make_lock(), scripts=set())
    assert not any("fb.example" in u for u in calls)
    assert ("NotoSans", "OFL.txt") not in fonts


def test_unreachable_fallback_stops_the_build_unless_waived(monkeypatch, capsys):
    _serve(monkeypatch, fail="fb.example")
    with pytest.raises(SystemExit, match="failed to download"):
        tiles.generate_sdf_font_glyphs(make_lock(), scripts=None)
    monkeypatch.setenv("STREETZIM_ALLOW_FONT_ERRORS", "1")
    capsys.readouterr()
    fonts = tiles.generate_sdf_font_glyphs(make_lock(), scripts={"Arabic"})
    assert gf.glyph_ids(fonts[("OpenSansRegular", "1536-1791")]) == {1537}  # unmerged
    out = capsys.readouterr().out
    # the loss is named: which style, which range, which script
    assert "WARNING: OpenSansBold lost glyphs in 1536-1791 for Arabic" in out
    assert "WARNING: OpenSansRegular lost glyphs in 1536-1791 for Arabic" in out
    assert "Hebrew" not in out.split("WARNING", 1)[1]
    assert "1280-1535" not in out  # Arabic's blocks do not reach that range


def test_pinning_uses_the_tools_settings_unless_told_to_keep_the_lock(tmp_path, monkeypatch, capsys):
    lock = va.load_lock()
    lock["fonts"]["fallback"]["scripts"] = {"Arabic": ["0600-06FF"]}
    lock["fonts"]["fallback"]["base_url"] = "https://old.example/fonts"
    path = tmp_path / "lock.json"
    pin.write_lock(lock, path)
    monkeypatch.setattr(pin, "read_lock_or_empty", lambda: va.load_lock(path))
    monkeypatch.setattr(pin, "write_lock", lambda lk: path.write_text(json.dumps(lk)))
    monkeypatch.setattr(pin, "_pin_ranges", lambda base, stacks, ranges, old: (
        {s: dict.fromkeys(ranges) for s in stacks}, 0, 0))
    pin.pin_fonts()
    fb = va.load_lock(path)["fonts"]["fallback"]
    assert fb["scripts"] == pin.DEFAULT_FALLBACK["scripts"]
    assert fb["base_url"] == pin.DEFAULT_FALLBACK["base_url"]
    assert "7168-7423" in fb["ranges"]["NotoSansRegular"]      # Mtavruli
    lock["fonts"]["fallback"]["scripts"] = {"Arabic": ["0600-06FF"]}
    path.write_text(json.dumps(lock))
    pin.pin_fonts(keep_lock=True)
    kept = va.load_lock(path)
    fb = kept["fonts"]["fallback"]
    assert fb["scripts"] == {"Arabic": ["0600-06FF"]}
    assert list(fb["ranges"]["NotoSansRegular"]) == ["1536-1791"]
    assert kept["fonts"]["kept_lock_settings"] is True
    assert pin.settings_drift(kept) == [
        'fonts.fallback.base_url: "https://old.example/fonts" -> '
        + json.dumps(pin.DEFAULT_FALLBACK["base_url"]),
        'fonts.fallback.scripts: {"Arabic": ["0600-06FF"]} -> '
        + json.dumps(pin.DEFAULT_FALLBACK["scripts"], ensure_ascii=False)]
    # a plain --fonts announces what it resets and drops the marker
    capsys.readouterr()
    pin.pin_fonts()
    out = capsys.readouterr().out
    assert "setting reset to the tool's default: fonts.fallback.scripts: " in out
    assert "kept_lock_settings" not in va.load_lock(path)["fonts"]


def test_check_fails_on_settings_edited_in_the_lock(monkeypatch):
    assert pin.settings_drift(va.load_lock()) == []   # the repository's lock
    lock = va.load_lock()
    lock["fonts"]["fallback"]["scripts"]["Arabic"] = ["0600-06FF"]
    monkeypatch.setattr(va, "load_lock", lambda path=None: json.loads(json.dumps(lock)))
    with pytest.raises(SystemExit, match=r"fonts\.fallback\.scripts"):
        pin.check()
    lock["fonts"]["kept_lock_settings"] = True       # pinned with --keep-lock: accepted,
    with pytest.raises(SystemExit, match="fallback fontstack"):
        pin.check()   # so the next check is reached (the ranges no longer match)


def test_tampered_fallback_is_fatal_even_when_waived(monkeypatch):
    lock = make_lock()
    lock["fonts"]["fallback"]["ranges"]["NotoSansRegular"]["1536-1791"] = "0" * 64
    _serve(monkeypatch)
    monkeypatch.setenv("STREETZIM_ALLOW_FONT_ERRORS", "1")
    with pytest.raises(SystemExit, match="do not match their pinned"):
        tiles.generate_sdf_font_glyphs(lock, scripts={"Arabic"})


def test_scan_is_skipped_for_large_or_streamed_builds(monkeypatch):
    lock = make_lock()
    assert tiles.fallback_scripts_in_tiles(None, lock) is None
    small = {(14, 0, 0): mvt("شارع")}
    assert tiles.fallback_scripts_in_tiles(small, lock) == {"Arabic"}
    monkeypatch.setattr(tiles, "FALLBACK_SCAN_MAX_BYTES", 3)
    assert tiles.fallback_scripts_in_tiles(small, lock) is None


def test_repository_lock_pins_the_fallback():
    lock = va.load_lock()
    fb = va.font_fallback(lock)
    assert fb is not None
    assert set(fb.for_stack) == set(lock["fonts"]["fontstacks"])
    assert {"Arabic", "Hebrew"} <= set(fb.scripts)
    assert "/" + pin.FALLBACK_COMMIT + "/" in lock["fonts"]["fallback"]["base_url"]
    assert all(fr.sha256 for fr in fb.ranges)
    assert va.fallback_licence(lock).startswith(b"Copyright")


def test_licence_entry_is_plain_text():
    class Item:
        def __init__(self, path, title, mimetype, content, **kw):
            self.path, self.mimetype = path, mimetype

    class Creator:
        def __init__(self):
            self.items = {}

        def add_item(self, item):
            self.items[item.path] = item
    c = Creator()
    zim_writer._add_font_glyphs(c, Item, fonts={("NotoSans", "OFL.txt"): b"x",
                                                ("OpenSansRegular", "0-255"): b"y"})
    assert c.items["fonts/NotoSans/OFL.txt"].mimetype == "text/plain"
    assert c.items["fonts/OpenSansRegular/0-255.pbf"].mimetype == "application/x-protobuf"
