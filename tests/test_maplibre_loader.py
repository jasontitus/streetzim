"""The viewer's MapLibre loader (part 000) and MapLibre 6's worker, in
headless Chromium (tests/maplibre_loader_page.mjs).

Each case loads the loader exactly as the viewer has it, then draws a
GeoJSON circle, which needs MapLibre's worker:

- a new map (maplibre-gl.js is MapLibre 6, maplibre-gl-legacy.js 5) gets 6,
  or 5 on an engine without WebGL2;
- an old map (maplibre-gl.js is 5, no legacy file) gets 5 either way;
- a maplibre-gl.js that throws falls back to the legacy file;
- a CSP that forbids eval still gets 6 (the probe is no eval);
- 6 draws in a sandboxed iframe and from file:, opaque origins where a
  module worker from a blob: URL cannot start (its worker is classic).

Needs the browser stack (NODE, CHROME_PATH, PLAYWRIGHT_CORE; BROWSER_LIBS on
the build host); skips without it."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NEED = ("NODE", "CHROME_PATH", "PLAYWRIGHT_CORE")
if not all(os.environ.get(k) and os.path.exists(os.environ[k]) for k in NEED):
    pytest.skip(f"the browser stack: set {', '.join(NEED)}", allow_module_level=True)

VENDOR = ROOT / "resources" / "vendor"
LOCK = json.loads((ROOT / "resources" / "viewer-assets.lock.json").read_text(encoding="utf-8"))
V6 = LOCK["maplibre-gl"]["version"]
V5 = LOCK["maplibre-gl-legacy"]["version"]

MAP = """<script>
var map = new maplibregl.Map({container: 'm', center: [0, 0], zoom: 2, attributionControl: false,
  style: {version: 8, sources: {p: {type: 'geojson', data: {type: 'FeatureCollection', features: [
    {type: 'Feature', properties: {}, geometry: {type: 'Point', coordinates: [0, 0]}}]}}},
    layers: [{id: 'c', type: 'circle', source: 'p', paint: {'circle-radius': 30, 'circle-color': '#c00'}}]}});
map.on('error', function(e) { window.__err = String(e && e.error && e.error.message || e); });
map.on('idle', function() {
  window.__done = {v: maplibregl.getVersion(), n: map.queryRenderedFeatures().length};
});
</script>"""


def loader() -> str:
    src = (ROOT / "resources/viewer/src/index/000-head.html").read_text(encoding="utf-8")
    m = re.search(r"<!-- BEGIN maplibre-loader[^>]*-->\n(.*?)<!-- END maplibre-loader -->", src, re.S)
    assert m, "part 000 has no maplibre-loader block"
    return m.group(1)


@pytest.fixture(scope="module")
def shown(tmp_path_factory: pytest.TempPathFactory) -> dict:
    site = tmp_path_factory.mktemp("loader")
    page = ('<!doctype html><html><head><meta charset="utf-8">' + loader()
            + '</head><body><div id="m" style="width:400px;height:300px"></div>' + MAP
            + "</body></html>")
    v6 = (VENDOR / "maplibre-gl" / "maplibre-gl.js").read_bytes()
    v5 = (VENDOR / "maplibre-gl-legacy" / "maplibre-gl-legacy.js").read_bytes()
    layouts = {
        "new": {"maplibre-gl.js": v6, "maplibre-gl-legacy.js": v5},
        "old": {"maplibre-gl.js": v5},
        "broken": {"maplibre-gl.js": b"throw new Error('broken');", "maplibre-gl-legacy.js": v5},
        "csp": {"maplibre-gl.js": v6, "maplibre-gl-legacy.js": v5},
    }
    for name, files in layouts.items():
        d = site / name
        d.mkdir()
        (d / "index.html").write_text(page, encoding="utf-8")
        for f, data in files.items():
            (d / f).write_bytes(data)
    (site / "frame.html").write_text(
        '<!doctype html><iframe sandbox="allow-scripts" src="new/index.html" '
        'style="width:420px;height:320px"></iframe>', encoding="utf-8")
    r = subprocess.run([os.environ["NODE"], str(ROOT / "tests" / "maplibre_loader_page.mjs"), str(site)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("case,version", [
    ("new", V6),
    ("new, no WebGL2", V5),
    ("old", V5),
    ("old, no WebGL2", V5),
    ("broken 6", V5),
    ("CSP without eval", V6),
    ("sandboxed iframe", V6),
    ("file:", V6),
])
def test_the_loader_picks_a_maplibre_that_draws(shown, case, version):
    got = shown[case]
    assert got.get("v") == version, got
    assert got.get("n", 0) >= 1, got   # the worker ran: the circle was laid out
