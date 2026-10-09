"""--clip-poly and the pre-upload terrain gate (ops/cloud/check_terrain_coverage.py).

Moved from tests/test_clip.py: core tests must not name ops files
(tools/check_boundary.py). A clipped ZIM has no terrain past the clip zoom
outside the outline, by design, and the gate must not count that as missing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("shapely")
from streetzim import clip  # noqa: E402

# The same outline as tests/test_clip.py: a triangle around the Nile delta,
# with a square hole near its middle.
POLY = """delta
1
   29.0   30.0
   32.5   30.0
   30.8   31.6
   29.0   30.0
END
!2
   30.5   30.4
   31.0   30.4
   31.0   30.8
   30.5   30.8
   30.5   30.4
END
END
"""
BBOX = (28.0, 29.0, 34.0, 32.5)


@pytest.fixture
def poly_file(tmp_path):
    p = tmp_path / "delta.poly"
    p.write_text(POLY)
    return p


def test_the_terrain_gate_skips_what_the_clip_dropped(poly_file):
    # cloud/check_terrain_coverage.py counts a missing terrain tile over land
    # as a failure; a clipped ZIM has none past the clip zoom outside the
    # outline, by design.
    import importlib.util
    import mercantile
    spec = importlib.util.spec_from_file_location("ctc", ROOT / "ops" / "cloud" / "check_terrain_coverage.py")
    ctc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctc)
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0)

    class Arc:
        def __init__(self, mc):
            self.mc = mc

        def get_entry_by_path(self, p):
            assert p == "map-config.json"
            if self.mc is None:
                raise KeyError(p)
            data = json.dumps(self.mc).encode()
            return type("E", (), {"get_item": lambda s: type("I", (), {"content": data})()})()
    inside, outside = mercantile.tile(29.9, 30.3, 11), mercantile.tile(33.5, 31.5, 11)
    assert ctc.clip_filter(Arc(None)) is None
    assert ctc.clip_filter(Arc({"minZoom": 0})) is None
    for mc in ({"clipMinZoom": 10, "clipArea": c.area_geojson()},
               {"clipMinZoom": 10, "clipMask": c.mask_geojson(BBOX)}):        # early trial builds
        z, keeps = ctc.clip_filter(Arc(mc))
        assert z == 10
        assert keeps(inside.x, inside.y, 11) and not keeps(outside.x, outside.y, 11)


def test_the_terrain_gate_counts_the_clip_zoom_and_the_outline_edge():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ctc", ROOT / "ops" / "cloud" / "check_terrain_coverage.py")
    ctc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctc)
    gone = (10, lambda x, y, z: False)              # the clip kept nothing past z10
    assert not ctc.clip_skips(gone, 0, 0, 10)       # the clip zoom is always in the ZIM
    assert ctc.clip_skips(gone, 0, 0, 11)
    assert not ctc.clip_skips(None, 0, 0, 11)
    # A tile that meets clipArea only within CLIP_EDGE_DEG of its edge is not
    # counted: the simplified area may run past the outline the writer used.
    import mercantile
    t = mercantile.tile(30.0, 30.0, 12)
    w, s_, e, n = ctc.tile_bounds(t.x, t.y, 12)
    area = {"type": "Polygon", "coordinates": [[[e - 0.005, s_], [e + 1, s_], [e + 1, n], [e - 0.005, n], [e - 0.005, s_]]]}

    class Arc:
        def get_entry_by_path(self, p):
            data = json.dumps({"clipMinZoom": 10, "clipArea": area}).encode()
            return type("E", (), {"get_item": lambda s: type("I", (), {"content": data})()})()
    clipf = ctc.clip_filter(Arc())
    assert ctc.clip_skips(clipf, t.x, t.y, 12)
    assert not ctc.clip_skips(clipf, t.x + 1, t.y, 12)
