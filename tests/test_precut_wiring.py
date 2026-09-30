"""create_osm_zim hands precut=True to the address, wiki-tag and routing
steps exactly when their input is its own cut to the box."""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import create_osm_zim as c  # noqa: E402

BOX = "7.40,43.72,7.44,43.76"


def _args(**kw):
    base = {"mbtiles": None, "area": None, "fast": False, "store": None, "pbf": None,
            "search_cache": None, "skip_address_extract": False,
            "overture_addresses": None, "overture_places": None, "no_admin_areas": False}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def seen(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(c, "extract_bbox_from_pbf",
                        lambda src, b, out: (calls.setdefault("cut", []).append((src, b)),
                                             Path(out).write_bytes(b""))[0])
    monkeypatch.setattr(c, "generate_tiles", lambda *a, **k: None)
    monkeypatch.setattr(c, "download_osm_extract",
                        lambda g, out: Path(out).write_bytes(b""))
    monkeypatch.setattr(c, "extract_addresses_pbf",
                        lambda p, o, bbox=None, precut=False: calls.__setitem__("addr", (p, precut)) or 0)
    monkeypatch.setattr(c, "extract_wiki_tags_pbf",
                        lambda p, bbox=None, precut=False: calls.__setitem__("wiki", (p, precut)) or {})
    monkeypatch.setattr(c, "append_admin_areas",
                        lambda p, feats, bbox=None, wiki_refs=None:
                        calls.__setitem__("admin", p) or 0)
    monkeypatch.setattr(c, "extract_routing_graph",
                        lambda p, d, bbox=None, precut=False: calls.__setitem__("rt", (p, bbox, precut)))
    return calls


def _run(args, tmp_path, *, bbox_str, geofabrik_path, pbf_path):
    cache = tmp_path / "cache.jsonl"
    cache.write_text(json.dumps({"lat": 43.74, "lon": 7.42, "name": "x"}) + "\n")
    args.search_cache = str(cache)
    _, work_pbf, cut = c._acquire_tiles(args=args, bbox_str=bbox_str, geofabrik_path=geofabrik_path,
                                        pbf_path=pbf_path, tmpdir=str(tmp_path), total_steps=6)
    c._build_search(args=args, bbox_str=bbox_str, mbtiles_path=None, pbf_path=pbf_path, tiles=None,
                    tmpdir=str(tmp_path), total_steps=6, use_streaming=False,
                    work_pbf=work_pbf, work_pbf_cut=cut)
    c._build_routing(args=args, bbox_str=bbox_str, include_routing=True, include_wikidata=False,
                     pbf_path=pbf_path, tmpdir=str(tmp_path), total_steps=6,
                     work_pbf=work_pbf, work_pbf_cut=cut)
    return work_pbf


def test_own_cut_is_not_cut_again(seen, tmp_path):
    w = _run(_args(pbf="in.pbf"), tmp_path, bbox_str=BOX, geofabrik_path=None, pbf_path="in.pbf")
    assert w == str(tmp_path / "area.osm.pbf")
    assert seen["addr"] == (w, True) and seen["wiki"] == (w, True)
    assert seen["rt"][0] == w and seen["rt"][2] is True and seen["rt"][1] is not None
    # Administrative areas read the extract before the cut, so an area the
    # cut clips still has its whole polygon.
    assert seen["admin"] == "in.pbf"


def test_preset_areas_own_extract_is_still_cut(seen, tmp_path):
    g = c.KNOWN_AREAS["monaco"]["geofabrik"]
    _run(_args(area="monaco"), tmp_path, bbox_str=c.KNOWN_AREAS["monaco"].get("bbox") or BOX,
         geofabrik_path=g, pbf_path=None)
    assert "cut" not in seen
    assert seen["addr"][1] is False and seen["wiki"][1] is False and seen["rt"][2] is False


def test_mbtiles_with_pbf_is_still_cut(seen, tmp_path):
    mb = tmp_path / "t.mbtiles"
    mb.write_bytes(b"x")
    _run(_args(mbtiles=str(mb), pbf="in.pbf"), tmp_path, bbox_str=BOX, geofabrik_path=None,
         pbf_path="in.pbf")
    assert seen["addr"] == ("in.pbf", False) and seen["rt"][2] is False


def test_precut_routing_still_joins_at_the_antimeridian(tmp_path):
    import shutil
    if not shutil.which("osmium"):
        pytest.skip("osmium CLI not on PATH")
    pytest.importorskip("osmium")
    from streetzim import area
    from streetzim.routing.build import extract_routing_graph
    from tests.test_antimeridian import _graph_counts
    opl = tmp_path / "road.opl"
    opl.write_text(
        "n1 v1 x179.99 y-16.8\nn2 v1 x180.0 y-16.8\nn3 v1 x-180.0 y-16.8\n"
        "n4 v1 x-179.99 y-16.8\nw10 v1 Thighway=primary Nn1,n2\n"
        "w11 v1 Thighway=primary Nn3,n4\n")
    g = extract_routing_graph(str(opl), str(tmp_path), precut=True,
                              bbox=list(area.normalize((179.9, -17.0, -179.9, -16.5))))
    assert _graph_counts(g) == (3, 4)


def test_preset_area_from_another_extract_is_cut_once(seen, tmp_path):
    # --area with an extract that is not the preset's own (the elif branch):
    # the build cuts it, so the three steps skip their own cut.
    w = _run(_args(area="monaco"), tmp_path, bbox_str=BOX,
             geofabrik_path="europe/france", pbf_path=None)
    assert seen["cut"] and w == str(tmp_path / "area.osm.pbf")
    assert seen["addr"] == (w, True) and seen["rt"][2] is True


@pytest.mark.parametrize("cut", [True, False])
def test_main_passes_the_cut_flag_to_both_steps(monkeypatch, tmp_path, cut):
    # main() itself, not just the helpers: what _acquire_tiles reports must
    # reach _build_search and _build_routing unchanged.
    class Stop(Exception):
        pass
    got = {}
    monkeypatch.setattr(c, "_openzim_options", lambda **k: (None, None, {}))
    monkeypatch.setattr(c, "_resolve_area", lambda **k: (
        BOX, None, "x", str(tmp_path / "x.zim"), "in.pbf"))
    monkeypatch.setattr(c, "_layer_options", lambda **k: (
        True, False, False, False, 0, "webp", 0, 0, 256, 12, 6, None))
    monkeypatch.setattr(c, "_acquire_tiles", lambda **k: ("t.mbtiles", "w.pbf", cut))
    monkeypatch.setattr(c, "_process_tiles", lambda **k: ([], {}, None, 0, False))

    def search(**k):
        got["search"] = k["work_pbf_cut"]
        return 0, None, None, None, None

    def routing(**k):
        got["routing"] = k["work_pbf_cut"]
        raise Stop
    monkeypatch.setattr(c, "_build_search", search)
    monkeypatch.setattr(c, "_build_wikidata", lambda **k: None)
    monkeypatch.setattr(c, "_build_routing", routing)
    with pytest.raises(Stop):
        c.main(["--bbox", BOX, "--pbf", "in.pbf"])
    assert got == {"search": cut, "routing": cut}


ADMIN = {"name": "Aalten", "type": "admin", "lat": 51.93, "lon": 6.59, "osm": "r9",
         "wikidata": "Q9", "wikipedia": "nl:Aalten"}


@pytest.mark.parametrize("salvage", [False, True])
def test_admin_wiki_tags_reach_the_lookup_without_a_second_read(monkeypatch, tmp_path,
                                                                 salvage):
    # A normal build takes the admin areas' tags from append_admin_areas,
    # which has them; the search JSONL (26.6 GB for Europe) is not read
    # again. A salvage build (--skip-address-extract) extracts nothing and
    # reads them from the JSONL, where the earlier build left them.
    from streetzim import admin_areas
    monkeypatch.setattr(c, "extract_addresses_pbf", lambda *a, **k: 0)
    monkeypatch.setattr(c, "extract_wiki_tags_pbf", lambda *a, **k: {})
    monkeypatch.setattr(c, "_sample_overture_themes_in_cache", lambda *a: None)

    def append(p, jsonl, bbox=None, wiki_refs=None):
        admin_areas.add_admin_wiki_refs(wiki_refs, [ADMIN])
        return 1
    monkeypatch.setattr(c, "append_admin_areas", append)
    scanned = []
    real = admin_areas.add_admin_wiki_refs

    def spy(refs, features):
        scanned.append(isinstance(features, str))
        return real(refs, features)
    monkeypatch.setattr(admin_areas, "add_admin_wiki_refs", spy)
    cache = tmp_path / "cache.jsonl"
    cache.write_text(json.dumps({"lat": 43.74, "lon": 7.42, "name": "x"}) + "\n"
                     + (json.dumps(dict(ADMIN, wikipedia="nl:Aalten (cache)")) + "\n"
                        if salvage else ""))
    args = _args(pbf="in.pbf", search_cache=str(cache), skip_address_extract=salvage)
    out = c._build_search(args=args, bbox_str=None, mbtiles_path=None, pbf_path="in.pbf",
                          tiles=None, tmpdir=str(tmp_path), total_steps=6,
                          use_streaming=False, work_pbf=None, work_pbf_cut=False)
    refs = out[-1]
    assert refs[("admin", "r9")]["wikipedia"] == ("nl:Aalten (cache)" if salvage else "nl:Aalten")
    assert scanned == [salvage]         # the JSONL is read only for a salvage build
