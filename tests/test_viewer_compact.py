"""The built index.html is its parts without comments and indentation
(tools/viewer_compact.py). tools/lint_viewer.mjs proves the JavaScript
token streams equal; this checks the rest: CSS and HTML are unchanged but
for comments and whitespace, the comments the gates read survive, and the
compactor only ever drops WHOLE comment lines."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_viewer  # noqa: E402
import viewer_compact  # noqa: E402

BUILT = (ROOT / "resources" / "viewer" / "index.html").read_text(encoding="utf-8")
SOURCE = build_viewer.joined()


def _segments(html: str) -> list[tuple[str, str]]:
    """[(kind, text)]: 'js' / 'css' / 'raw' inline blocks and the 'html'
    between them."""
    out, pos = [], 0
    for m in re.finditer(r"<(script|style)(\s[^>]*)?>(.*?)</\1>", html, re.S | re.I):
        out.append(("html", html[pos:m.start()] + m.group(0)[:m.group(0).index(">") + 1]))
        kind = m.group(1).lower()
        if kind == "script":
            kind = "js" if viewer_compact._script_is_js(m.group(2) or "") else "raw"
        else:
            kind = "css"
        out.append((kind, m.group(3)))
        pos = m.end() - len("</" + m.group(1) + ">")
    out.append(("html", html[pos:]))
    return out


def _norm(kind: str, text: str) -> str:
    if kind == "css":
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    elif kind == "html":
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    elif kind == "js":
        return ""     # tools/lint_viewer.mjs compares tokens
    return " ".join(text.split())


def test_built_file_is_smaller_and_current() -> None:
    # the UI tables' block (compressed, the same in both) aside: what the compaction strips
    tables = re.compile(r'<script type="application/json" id="sz-i18n">.*?</script>', re.S)
    assert len(tables.sub("", BUILT)) < len(tables.sub("", SOURCE)) * 0.8
    assert BUILT.encode("utf-8") == build_viewer.build()


def test_css_and_html_differ_only_in_comments_and_whitespace() -> None:
    a, b = _segments(SOURCE), _segments(BUILT)
    assert [k for k, _ in a] == [k for k, _ in b]
    for (kind, x), (_, y) in zip(a, b):
        assert _norm(kind, x) == _norm(kind, y), kind


def test_gate_markers_survive() -> None:
    # Read back out of shipped ZIMs by ops/cloud/rollout_viewer_patch.sh,
    # ops/host-tests/run-driver-gates.sh and the host's allzims scripts:
    # some are comment text (part 000 is not compacted for that reason).
    for marker in ("Kiwix iOS safe-area shim", "aspect instead", ".maplibregl-popup { z-index: 4; }",
                   "touch-action: pan-x;", "_szPopupGap", "_szChipSynth", "water-lowzoom",
                   "_SZ_LAKES", "_ranked.sort", "retryWikiGeoIndex", "_findResolveChipDef"):
        assert marker in BUILT, marker


def test_only_whole_comment_lines_go() -> None:
    src = "\n".join([
        "<script>",
        "  // a comment line",
        "  var a = 1; // trailing comment stays",
        "  var u = 'http://x//y';",
        "  /* one-line block */",
        "  /* a block",
        "     over lines */",
        "  var b = 2; /* trailing block */",
        "  /* not whole */ var c = 3;",
        "  // BEGIN shared",
        "    // kept: inside a BEGIN/END block",
        "  // END shared",
        "</script>",
        "  <!-- an html comment -->",
        "<p>text // not js</p>",
        "<style>",
        "  /* css comment */",
        "  a { color: red; } /* trailing */",
        "</style>",
    ])
    assert viewer_compact.compact(src).split("\n") == [
        "<script>",
        "var a = 1; // trailing comment stays",
        "var u = 'http://x//y';",
        "var b = 2; /* trailing block */",
        "/* not whole */ var c = 3;",
        "  // BEGIN shared",
        "    // kept: inside a BEGIN/END block",
        "  // END shared",
        "</script>",
        "<p>text // not js</p>",
        "<style>",
        "a { color: red; } /* trailing */",
        "</style>",
    ]


def test_json_and_external_scripts_are_left_alone() -> None:
    src = '<script type="application/json" id="x">\n  // {"a": 1}\n</script>\n<script src="a.js">\n</script>'
    assert viewer_compact.compact(src) == src


def test_only_the_blocks_named_to_keep_are_kept_verbatim() -> None:
    src = "\n".join([
        "<script>",
        "  // BEGIN shared",
        "    // kept: places.html has this block too",
        "  // END shared",
        "  // BEGIN mine",
        "    // dropped: only index.html has it",
        "    var m = 1;",
        "  // END mine",
        "</script>",
    ])
    assert viewer_compact.compact(src, {"shared"}).split("\n") == [
        "<script>",
        "  // BEGIN shared",
        "    // kept: places.html has this block too",
        "  // END shared",
        "// BEGIN mine",
        "var m = 1;",
        "// END mine",
        "</script>",
    ]


def test_the_blocks_places_html_shares_are_verbatim_in_the_built_viewer() -> None:
    import re
    sys.path.insert(0, str(ROOT / "tools"))
    import build_viewer
    built = (ROOT / "resources" / "viewer" / "index.html").read_text(encoding="utf-8")
    places = (ROOT / "resources" / "viewer" / "places.html").read_text(encoding="utf-8")
    shared = build_viewer.shared_blocks()
    assert {"search-shards", "chip-shards", "sz-i18n-runtime"} <= shared
    both = 0
    for name in shared:                  # (chip-rules is in places.html only)
        block = re.compile(r"//\s*BEGIN " + re.escape(name) + r"\b.*?//\s*END " + re.escape(name), re.S)
        mi = block.search(built)
        if mi:
            both += 1
            assert mi.group(0) in places, f"{name} differs between index.html and places.html"
    assert both >= 3
    # an index-only block is compacted: no comment lines inside it
    sl = re.search(r"// BEGIN sign-lines\n(.*?)// END sign-lines", built, re.S)
    assert sl and not re.search(r"^\s*//(?!\s*(BEGIN|END))", sl.group(1), re.M)
