"""MapLibre 6 in the viewer (docs/viewer-supply-chain.md).

- tools/pin_viewer_assets.py turns MapLibre 6's ES module and module worker
  into one classic script (classic_maplibre): the exports become
  `maplibregl`, `import.meta.url` the script's URL, the worker a string
  started from a blob: URL; anything else left in either module stops it.
- The vendored script is what that conversion gives, it runs, and it sets
  the worker up.
- The script fonts: the viewer's SZ_SCRIPT_FONTS, the pin tool's
  SCRIPT_FONTS and the lock file name the same files and Unicode ranges,
  each font covers its ranges, and the PWA serves each file from the site.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import viewer_assets as va  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import pin_viewer_assets as pin  # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

MAIN = ("/** header */\nvar a=1;function b(){return import.meta.url}class C{static{this.x=1}}"
        "function W(e){return e}\nexport{b as getUrl,C as Thing,W as setWorkerUrl,a};\n"
        "//# sourceMappingURL=maplibre-gl.mjs.map")
WORKER = "self.worker={ok:1};var d=class{};export{d as default};\n//# sourceMappingURL=w.map"


def _run(js: str, tail: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "run.cjs"
        f.write_text(js + "\n" + tail, encoding="utf-8")
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@needs_node
def test_conversion_exports_url_and_worker():
    out = pin.classic_maplibre(MAIN, WORKER, "6.99.0")
    assert "export" not in out.split("var __maplibreApi")[0].split("*/", 1)[1]
    assert "import.meta" not in out
    got = json.loads(_run(
        "var seen=[];globalThis.document={currentScript:{src:'zim://x/maplibre-gl.js'}};",
        out.replace("__maplibreApi.setWorkerUrl(", "seen.push(1);__maplibreApi.setWorkerUrl(")
        + "\nconsole.log(JSON.stringify({keys:Object.keys(maplibregl).sort(),"
          "url:maplibregl.getUrl(),a:maplibregl.a,x:maplibregl.Thing.x,seen:seen.length}))"))
    assert got == {"keys": ["Thing", "a", "getUrl", "setWorkerUrl"],
                   "url": "zim://x/maplibre-gl.js", "a": 1, "x": 1, "seen": 1}


@needs_node
def test_conversion_hands_the_worker_over_as_a_blob():
    out = pin.classic_maplibre(MAIN.replace("function W(e){return e}",
                                            "function W(e){globalThis.__w=e}"), WORKER, "6.99.0")
    got = _run("", out + "\n(async()=>{const [u,frag]=globalThis.__w.split('#');"
                         "const r=await fetch(u);"
                         "console.log(frag+'|'+r.headers.get('content-type')+'|'+(await r.text()))})()")
    frag, kind, src = got.split("|", 2)
    # MapLibre starts a URL ending in .cjs as a classic worker (#8769).
    assert frag == "maplibre-gl-worker.cjs"
    assert kind == "text/javascript"
    assert src.startswith('"use strict";\nself.worker={ok:1};var d=class{};')
    assert "export" not in src and "sourceMappingURL" not in src


@needs_node
@pytest.mark.parametrize("main,worker,msg", [
    (MAIN.replace("export{", "export const z=1;export{"), WORKER, "classic script"),
    ("import x from './y.mjs';" + MAIN, WORKER, "classic script"),
    (MAIN.replace("import.meta.url", "import.meta.resolve('x')"), WORKER, "beyond import.meta.url"),
    (MAIN.replace(",W as setWorkerUrl", ""), WORKER, "no setWorkerUrl"),
    (MAIN.replace(",a}", ",a,a as getUrl}"), WORKER, "a name twice"),
    (MAIN.replace("export{", "await 1;export{"), WORKER, "classic script"),
    (MAIN.rsplit("export", 1)[0], WORKER, "no export statement"),
    (MAIN, WORKER.replace("export{d as default}", "export{d as default,d as other}"), "not only a default"),
    (MAIN, "import 'z';" + WORKER, "the worker does not parse"),
    (MAIN, WORKER.replace("var d", "var u=import.meta.url;var d"), "worker reads import.meta"),
    (MAIN.replace("var a=1", "var a=1,__maplibreScriptUrl=2"), WORKER, "names are taken"),
])
def test_conversion_refuses_what_it_does_not_handle(main, worker, msg):
    with pytest.raises(SystemExit, match=re.escape(msg)):
        pin.classic_maplibre(main, worker, "6.99.0")


def test_pin_maplibre_refuses_maplibre_5():
    with pytest.raises(SystemExit, match="MapLibre 6 or later"):
        pin.pin_maplibre("5.23.0")


@needs_node
def test_vendored_maplibre_is_the_conversion_of_the_pinned_tarball(tmp_path):
    """Rebuild maplibre-gl.js from the tarball the lock names, when it is in
    the npm cache this test keeps (no network otherwise): byte-identical."""
    lock = va.load_lock()
    entry = lock["maplibre-gl"]
    cache = ROOT / "viewer-assets" / "npm" / Path(entry["tarball"]).name
    if not cache.is_file():
        pytest.skip(f"{cache} not cached (fetch it to run this check offline)")
    tgz = cache.read_bytes()
    pin.check_npm_integrity(tgz, entry["integrity"])
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        def member(name: str) -> str:
            f = tar.extractfile(f"package/dist/{name}")
            assert f is not None
            return f.read().decode("utf-8")
        out = pin.classic_maplibre(member("maplibre-gl.mjs"), member("maplibre-gl-worker.mjs"),
                                   entry["version"])
    assert out.encode("utf-8") == (va.VENDOR / "maplibre-gl" / "maplibre-gl.js").read_bytes()


@needs_node
def test_vendored_maplibre_runs_and_starts_its_worker_from_a_blob():
    js = (va.VENDOR / "maplibre-gl" / "maplibre-gl.js").read_text(encoding="utf-8")
    version = va.load_lock()["maplibre-gl"]["version"]
    got = json.loads(_run("", js + "\nconsole.log(JSON.stringify({v:maplibregl.getVersion(),"
                               "w:maplibregl.getWorkerUrl(),m:typeof maplibregl.Map,"
                               "f:typeof maplibregl.Map.prototype.setFontFaces}))"))
    assert got["v"] == version and got["m"] == "function" and got["f"] == "function"
    assert got["w"].startswith("blob:") and got["w"].endswith("#maplibre-gl-worker.cjs")


def _viewer_script_fonts() -> dict[str, list[str]]:
    src = (ROOT / "resources/viewer/src/index/130-lowzoom-lakes.js").read_text(encoding="utf-8")
    m = re.search(r"var SZ_SCRIPT_FONTS = (\{.*?\n\});", src, re.S)
    assert m, "SZ_SCRIPT_FONTS not found in part 130"
    return json.loads(m.group(1))


def test_viewer_tool_and_lock_name_the_same_script_fonts():
    tool = {pin.script_font_file(s): r for s, r in pin.SCRIPT_FONTS.items()}
    assert _viewer_script_fonts() == tool
    lock = va.load_lock()[va.SCRIPT_FONTS]
    assert lock["ranges"] == tool
    assert set(lock["files"]) == set(tool) | {"OFL.txt"}
    va.vendored(va.SCRIPT_FONTS)


def test_each_script_font_covers_its_ranges():
    fontTools = pytest.importorskip("fontTools.ttLib")
    import unicodedata
    for fname, ranges in pin.SCRIPT_FONTS.items():
        path = va.VENDOR / va.SCRIPT_FONTS / pin.script_font_file(fname)
        cmap = fontTools.TTFont(path).getBestCmap()
        for r in ranges:
            lo, hi = (int(x, 16) for x in r[2:].split("-"))
            assigned = [c for c in range(lo, hi + 1) if unicodedata.category(chr(c)) != "Cn"]
            covered = sum(c in cmap for c in assigned)
            # Vedic Extensions: Noto Sans Devanagari lacks two rare marks.
            assert covered >= len(assigned) - 2, (fname, r, covered, len(assigned))


def test_the_pwa_serves_the_script_fonts_and_maplibre_5_from_the_site():
    sw = (ROOT / "web/drive/sw.js").read_text(encoding="utf-8")
    m = re.search(r"const SCRIPT_FONT = /(.+)/;", sw)
    assert m
    pattern = re.compile(m.group(1).replace("\\/", "/"))
    for fname in _viewer_script_fonts():
        assert pattern.match(fname), fname
    assert not pattern.match("tiles/1/2/3.pbf") and not pattern.match("NotoSansX-Regular.otf/x")
    assert "'maplibre-gl-legacy.js'" in sw
    sync = (ROOT / "scripts/sync-drive-viewer.sh").read_text(encoding="utf-8")
    assert "maplibre-gl-legacy/maplibre-gl-legacy.js" in sync
    assert "noto-script-fonts/NotoSans*-Regular.otf" in sync


# ---- the builder: what a ZIM carries ----------------------------------

from streetzim import tiles as tiles_mod, zim_writer  # noqa: E402


class FakeItem:
    def __init__(self, path, title, mimetype, content, is_front=False, compress=True, namespace=None):
        self.path, self.mimetype, self.content = path, mimetype, content


class FakeCreator:
    def __init__(self):
        self.items = {}

    def add_item(self, item):
        assert item.path not in self.items, item.path
        self.items[item.path] = item


def _viewer_entries(tmp_path, script_fonts):
    js = tmp_path / "maplibre-gl.js"; js.write_text("//")
    css = tmp_path / "maplibre-gl.css"; css.write_text("/**/")
    html = tmp_path / "index.html"; html.write_text("<html></html>")
    c = FakeCreator()
    zim_writer._add_viewer(c, FakeItem, maplibre_js_path=js, maplibre_css_path=css,
                           viewer_html_path=html, map_config={}, name="t",
                           script_fonts=script_fonts)
    return c.items


FONT_DIR = va.VENDOR / va.SCRIPT_FONTS
ALL_FONTS = {pin.script_font_file(s) for s in pin.SCRIPT_FONTS}


@pytest.mark.parametrize("chosen,want", [
    ({"NotoSansBengali-Regular.otf"}, {"NotoSansBengali-Regular.otf"}),
    (None, ALL_FONTS),           # tiles not scanned: every font
    (set(), set()),              # no label in these scripts: none, and no licence
])
def test_a_zim_carries_maplibre_5_and_the_fonts_its_labels_use(tmp_path, chosen, want):
    items = _viewer_entries(tmp_path, chosen)
    legacy = items[va.MAPLIBRE_LEGACY_ENTRY]
    assert legacy.mimetype == "application/javascript"
    assert Path(legacy.content).read_bytes() == (
        va.VENDOR / va.MAPLIBRE_LEGACY / va.MAPLIBRE_LEGACY_ENTRY).read_bytes()
    fonts = {p for p in items if p.endswith(".otf")}
    assert fonts == want
    for f in fonts:
        assert items[f].mimetype == "font/otf"
        assert Path(items[f].content).read_bytes() == (FONT_DIR / f).read_bytes()
    assert (va.SCRIPT_FONTS_LICENCE in items) == bool(want)
    # The viewer asks for every font by the name the ZIM stores it under.
    assert want <= set(_viewer_script_fonts())


def test_a_font_that_is_not_the_pinned_one_stops_the_build(tmp_path, monkeypatch):
    lock = va.load_lock()
    lock[va.SCRIPT_FONTS]["files"]["NotoSansTamil-Regular.otf"] = "0" * 64
    monkeypatch.setattr(va, "load_lock", lambda *a, **k: lock)
    with pytest.raises(SystemExit, match=re.escape("NotoSansTamil-Regular.otf: sha256")):
        _viewer_entries(tmp_path, None)


def _v(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _ld(field: int, data: bytes) -> bytes:
    return _v(field << 3 | 2) + _v(len(data)) + data


def mvt(*values: str, key: str = "name") -> bytes:
    """One layer, one feature per value, each tagged ``key=value``."""
    feats = b"".join(_ld(2, _ld(2, _v(0) + _v(i)) + _ld(4, _v(9) + _v(2) + _v(2)))
                     for i in range(len(values)))
    layer = (_ld(1, b"poi") + feats + _ld(3, key.encode())
             + b"".join(_ld(4, _ld(1, s.encode())) for s in values))
    return _ld(3, layer)


def test_the_script_fonts_follow_the_labels(monkeypatch):
    monkeypatch.delenv("STREETZIM_TILE_LANGUAGES", raising=False)
    t = {(14, 0, 0): mvt("गुवाहाटी", "Main Street"), (14, 1, 0): mvt("চট্টগ্রাম"),
         (14, 2, 0): mvt("ᱥᱟᱱᱛᱟᱲᱤ")}
    assert tiles_mod.script_fonts_in_tiles(t) == {
        "NotoSansDevanagari-Regular.otf", "NotoSansBengali-Regular.otf",
        "NotoSansOlChiki-Regular.otf"}
    assert tiles_mod.script_fonts_in_tiles({(14, 0, 0): mvt("Main Street", "شارع")}) == set()
    # A name the style does not show does not count; the build's own
    # language does (STREETZIM_TILE_LANGUAGES, glyph_fallback.label_keys).
    hi = {(14, 0, 0): mvt("गुवाहाटी", key="name:hi")}
    assert tiles_mod.script_fonts_in_tiles(hi) == set()
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "hi")
    assert tiles_mod.script_fonts_in_tiles(hi) == {"NotoSansDevanagari-Regular.otf"}
    # Streamed or too big to scan: every font.
    assert tiles_mod.script_fonts_in_tiles(None) is None
    monkeypatch.setattr(tiles_mod, "FALLBACK_SCAN_MAX_BYTES", 3)
    assert tiles_mod.script_fonts_in_tiles(t) is None
