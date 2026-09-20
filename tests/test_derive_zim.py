"""Tests for cloud/zimfmt.py + cloud/derive_zim.py.

The fixture is written by python-libzim's Creator (an independent writer),
derived with our raw-format tool, and read back with python-libzim (an
independent reader). That closes the loop on the format: if zimfmt misreads a
dirent or miswrites a cluster table, libzim notices.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

libzim = pytest.importorskip("libzim")
from libzim.reader import Archive  # noqa: E402
from libzim.writer import Creator, Hint, Item, StringProvider  # noqa: E402

from cloud import zimfmt  # noqa: E402
from cloud.derive_zim import Recipe, derive, tile_sort_key  # noqa: E402
from cloud.zim_inventory import inventory  # noqa: E402


class _Item(Item):
    def __init__(self, path, mime, data, compress=True, title=""):
        super().__init__()
        self._path, self._mime, self._data, self._compress, self._title = path, mime, data, compress, title

    def get_path(self): return self._path
    def get_title(self): return self._title
    def get_mimetype(self): return self._mime
    def get_contentprovider(self): return StringProvider(self._data)
    def get_hints(self): return {Hint.COMPRESS: self._compress}


def _tile_bytes(z, x, y, kind):
    return (f"{kind}-{z}-{x}-{y}-" + "payload" * (3 + z)).encode()


def build_fixture(path: Path) -> dict:
    """A miniature streetzim ZIM: viewer + config, tiles z0-14 (a handful per
    zoom, one raw satellite cluster, terrain), search data and wiki."""
    paths = {}
    cfg = {"name": "Fixture", "center": [8.5, 47.4], "zoom": 11, "minZoom": 0, "maxZoom": 14,
           "hasSatellite": True, "satelliteMaxZoom": 14, "satelliteFormat": "avif",
           "satelliteTileSize": 256, "hasTerrain": True, "terrainMaxZoom": 12, "hasRouting": True}
    with Creator(str(path)).config_indexing(True, "eng").config_clustersize(64 * 1024) as c:
        c.set_mainpath("index.html")
        c.add_metadata("Title", "Fixture")
        c.add_metadata("Name", "fixture")
        c.add_metadata("Language", "eng")
        c.add_item(_Item("index.html", "text/html", "<html><title>Map</title>hello map</html>", title="Map"))
        c.add_item(_Item("map-config.json", "application/json", json.dumps(cfg)))
        c.add_item(_Item("streetzim-meta.json", "application/json",
                         json.dumps({"name": "Fixture", "hasAddresses": True,
                                     "counts": {"total": 100, "addresses": 22,
                                                "byType": {"poi": 60, "addr": 22, "street": 18}}})))
        for z in range(0, 15):
            n = min(2 ** z, 4)
            for x in range(n):
                for y in range(n):
                    for kind, ext, mime, comp in (("tiles", "pbf", "application/x-protobuf", True),
                                                  ("satellite", "avif", "image/avif", False),
                                                  ("terrain", "png", "image/png", False)):
                        if kind == "terrain" and z > 12:
                            continue
                        p = f"{kind}/{z}/{x}/{y}.{ext}"
                        data = _tile_bytes(z, x, y, kind)
                        paths[p] = data
                        c.add_item(_Item(p, mime, data.decode(), comp))
        # search-data: a character-split prefix with tiered leaves (c/p/s/a),
        # plus legacy mixed leaves, plus the manifest the viewer resolves against
        chunks = {}
        for i in range(20):
            p = f"search-data/ch{i:02d}.json"
            recs = [{"n": f"place {i}", "t": "poi"}, {"n": f"Road {i} 5", "t": "addr"}]
            paths[p] = json.dumps(recs).encode()
            chunks[f"ch{i:02d}"] = len(recs)
            c.add_item(_Item(p, "application/json", paths[p].decode()))
        for tier, recs in (("c", [{"n": "Zurich", "t": "place"}]),
                           ("p", [{"n": "Zurich Cafe", "t": "poi"}]),
                           ("s", [{"n": "Zurichstrasse", "t": "street"}]),
                           ("a", [{"n": "Zurichstrasse 1", "t": "addr"}, {"n": "Zurichstrasse 2", "t": "addr"}])):
            p = f"search-data/zu~r~{tier}.json"
            paths[p] = json.dumps(recs).encode()
            chunks[f"zu~r~{tier}"] = len(recs)
            c.add_item(_Item(p, "application/json", paths[p].decode()))
        manifest = {"total": 100, "chunks": chunks, "sub_chunks": {"zu": list(k for k in chunks if k.startswith("zu"))},
                    "char_split": {"zu": ["r"]}}
        paths["search-data/manifest.json"] = json.dumps(manifest).encode()
        c.add_item(_Item("search-data/manifest.json", "application/json", paths["search-data/manifest.json"].decode()))
        for i in range(5):
            p = f"wiki-article/Article_{i}"
            paths[p] = f"<html><title>Article {i}</title>Zurich text {i}</html>".encode()
            c.add_item(_Item(p, "text/html", paths[p].decode(), title=f"Article {i}"))
        c.add_item(_Item("wiki-geo-index.json", "application/json", json.dumps({"Article_0": [8.5, 47.4]})))
        c.add_item(_Item("routing-data/graph-cell-00001.bin", "application/octet-stream", "R" * 5000, False))
        c.add_redirection("home", "Home", "index.html", {})
        c.add_redirection("sat-alias", "Sat", "satellite/3/1/1.avif", {})
        # redirect chains in both sort orders relative to their target
        c.add_redirection("a-alias", "A", "b-alias", {})
        c.add_redirection("b-alias", "B", "index.html", {})
        c.add_redirection("z-alias", "Z", "y-alias", {})
        c.add_redirection("y-alias", "Y", "index.html", {})
        c.add_redirection("sat-chain", "SC", "sat-alias", {})
    return paths


@pytest.fixture(scope="module")
def fixture_zim(tmp_path_factory):
    p = tmp_path_factory.mktemp("zim") / "fixture.zim"
    paths = build_fixture(p)
    return p, paths


def test_raw_reader_matches_libzim(fixture_zim):
    p, paths = fixture_zim
    r = zimfmt.ZimReader(str(p))
    a = Archive(str(p))
    assert r.header.entry_count == a.all_entry_count
    for path, data in paths.items():
        assert r.get(path) == data
    assert r.get("Title", "M") == b"Fixture"
    assert zimfmt.verify_checksum(str(p))


def test_inventory_components(fixture_zim):
    p, _ = fixture_zim
    inv = inventory(str(p), by_zoom=True)
    comps = {row["component"]: row for row in inv["rows"]}
    assert comps["tiles/14"]["entries"] == 16
    assert comps["satellite/14"]["entries"] == 16
    assert "terrain/13" not in comps
    assert comps["search-data"]["entries"] == 24   # 20 legacy + 3 tiered + manifest
    assert comps["search-data/addresses"]["entries"] == 1   # the tier-a leaf
    flags = dict(inv["savings"])
    assert "--no-satellite" in flags and "--max-tile-zoom 13" in flags
    assert flags["--no-satellite"] == comps_total(inv, "satellite/")
    assert sum(row["on_disk"] for row in inv["rows"]) <= inv["size"]


def comps_total(inv, prefix):
    return sum(row["on_disk"] for row in inv["rows"] if row["component"].startswith(prefix))


def test_inventory_exact_splits_addresses(fixture_zim):
    p, _ = fixture_zim
    approx = {r["component"]: r for r in inventory(str(p))["rows"]}
    exact = {r["component"]: r for r in inventory(str(p), exact=True, exact_level=3)["rows"]}
    # legacy leaves' address records are split out only in exact mode
    assert exact["search-data/addresses"]["uncompressed"] > approx["search-data/addresses"]["uncompressed"]
    # on-disk totals are conserved: every cluster's bytes land somewhere
    for inv in (approx, exact):
        assert abs(sum(r["on_disk"] for r in inv.values()) - sum(r["on_disk"] for r in approx.values())) < 64
    flags = dict(inventory(str(p), exact=True, exact_level=3)["savings"])
    assert "--strip-addresses" in flags


def test_plan_estimate(fixture_zim):
    p, _ = fixture_zim
    res = derive(str(p), "", Recipe(satellite_max_zoom=-1, level=3), dry_run=True, verbose=False, estimate=True)
    est = res["estimate"]
    assert 0 < est["projected"] < Path(p).stat().st_size
    assert est["reencoded_out"] > 0


def _libzim_paths(a: Archive) -> set[str]:
    return {a._get_entry_by_id(i).path for i in range(a.entry_count)}


def test_light_derive(fixture_zim, tmp_path):
    src, paths = fixture_zim
    dst = tmp_path / "light.zim"
    rec = Recipe(satellite_max_zoom=-1, max_tile_zoom=13, title="Fixture (Light)", name="fixture_light")
    res = derive(str(src), str(dst), rec, verbose=False)
    a = Archive(str(dst))
    assert a.check()
    assert zimfmt.verify_checksum(str(dst))
    assert a.uuid != Archive(str(src)).uuid
    got = _libzim_paths(a)
    assert not any(p.startswith("satellite/") or p.startswith("tiles/14/") for p in got)
    assert "sat-alias" not in got, "redirect to a dropped target must be dropped too"
    assert "sat-chain" not in got, "chain ending at a dropped target must be dropped too"
    assert {"home", "a-alias", "b-alias", "y-alias", "z-alias"} <= got, "redirect chains survive"
    assert a.get_entry_by_path("a-alias").get_redirect_entry().path == "b-alias"
    for path, data in paths.items():
        if path.startswith(("satellite/", "tiles/14/")):
            assert not a.has_entry_by_path(path)
        else:
            assert bytes(a.get_entry_by_path(path).get_item().content) == data
    cfg = json.loads(bytes(a.get_entry_by_path("map-config.json").get_item().content))
    assert cfg["hasSatellite"] is False and "satelliteMaxZoom" not in cfg
    assert cfg["maxZoom"] == 13 and cfg["hasTerrain"] is True
    assert a.get_metadata("Title") == b"Fixture (Light)"
    assert a.get_metadata("Name") == b"fixture_light"
    assert b"application/x-protobuf=" in a.get_metadata("Counter")
    assert a.main_entry.get_redirect_entry().path == "index.html"
    assert a.has_fulltext_index and a.has_title_index
    assert res["entries_kept"] == a.all_entry_count - 1  # + regenerated listing
    from libzim.suggestion import SuggestionSearcher
    assert SuggestionSearcher(a).suggest("Article").getEstimatedMatches() >= 5


def test_regroup_preserves_everything(fixture_zim, tmp_path):
    src, paths = fixture_zim
    dst = tmp_path / "regroup.zim"
    rec = Recipe(regroup_tiles=True, tile_order="hilbert", cluster_target=4096, level=3)
    derive(str(src), str(dst), rec, verbose=False)
    a = Archive(str(dst))
    assert a.check()
    for path, data in paths.items():
        assert bytes(a.get_entry_by_path(path).get_item().content) == data
    # zoom-major: no cluster holds two zooms of the same component
    r = zimfmt.ZimReader(str(dst))
    per_cluster = {}
    for _, d in r.dirents():
        if d.namespace == "C" and not d.is_redirect and d.path.split("/")[0] in ("tiles", "satellite", "terrain"):
            per_cluster.setdefault(d.cluster, set()).add(tuple(d.path.split("/")[:2]))
    assert all(len(s) == 1 for s in per_cluster.values()), per_cluster
    # satellite stays raw, tiles compressed
    for _, d in r.dirents():
        if d.path.startswith("satellite/"):
            assert not r.cluster_info(d.cluster).compressed
        if d.path.startswith("tiles/"):
            assert r.cluster_info(d.cluster).compressed


def test_no_routing_no_wiki(fixture_zim, tmp_path):
    src, _ = fixture_zim
    dst = tmp_path / "nr.zim"
    derive(str(src), str(dst), Recipe(no_routing=True, no_wiki=True), verbose=False)
    a = Archive(str(dst))
    assert a.check()
    got = _libzim_paths(a)
    assert not any(p.startswith(("routing-data/", "wiki-article/")) for p in got)
    cfg = json.loads(bytes(a.get_entry_by_path("map-config.json").get_item().content))
    assert cfg["hasRouting"] is False
    assert json.loads(bytes(a.get_entry_by_path("wiki-geo-index.json").get_item().content)) == {}


def test_strip_addresses(fixture_zim, tmp_path):
    src, paths = fixture_zim
    dst = tmp_path / "noaddr.zim"
    derive(str(src), str(dst), Recipe(strip_addresses=True, level=3), verbose=False)
    a = Archive(str(dst))
    assert a.check()
    def _json(p): return json.loads(bytes(a.get_entry_by_path(p).get_item().content))
    # tier-a leaf kept as an empty list (manifest lookup still hits), others intact
    assert _json("search-data/zu~r~a.json") == []
    assert _json("search-data/zu~r~s.json") == [{"n": "Zurichstrasse", "t": "street"}]
    # legacy mixed leaves lose only addr records
    for i in range(20):
        assert _json(f"search-data/ch{i:02d}.json") == [{"n": f"place {i}", "t": "poi"}]
    man = _json("search-data/manifest.json")
    assert man["chunks"]["zu~r~a"] == 0 and man["chunks"]["ch00"] == 1 and man["chunks"]["zu~r~c"] == 1
    assert man["total"] == 100 - 22 and man["addresses_stripped"] is True
    assert man["sub_chunks"] == {"zu": ["zu~r~c", "zu~r~p", "zu~r~s", "zu~r~a"]}   # untouched
    meta = _json("streetzim-meta.json")
    assert meta["hasAddresses"] is False and meta["counts"]["addresses"] == 0
    assert "addr" not in meta["counts"]["byType"] and meta["counts"]["total"] == 78
    cfg = _json("map-config.json")
    assert cfg["hasAddresses"] is False
    # nothing outside search-data changed
    for path, data in paths.items():
        if not path.startswith("search-data/"):
            assert bytes(a.get_entry_by_path(path).get_item().content) == data
    assert a.all_entry_count == Archive(str(src)).all_entry_count


def test_refuses_to_overwrite_source(fixture_zim, tmp_path):
    src, _ = fixture_zim
    import shutil
    copy = tmp_path / "copy.zim"
    shutil.copy(src, copy)
    with pytest.raises(SystemExit):
        derive(str(copy), str(copy), Recipe(satellite_max_zoom=-1), verbose=False)
    assert Archive(str(copy)).check()


def test_cluster_extents_exclude_dirent_table(fixture_zim, tmp_path):
    """ZimWriter lays out clusters | dirents | tables; the last cluster must
    end where the dirents start, or its bytes (and any copy of it) include
    the whole dirent table."""
    src, _ = fixture_zim
    dst = tmp_path / "d.zim"
    derive(str(src), str(dst), Recipe(satellite_max_zoom=-1, level=3), verbose=False)
    r = zimfmt.ZimReader(str(dst))
    last = r.header.cluster_count - 1
    ci = r.cluster_info(last)
    assert ci.offset + ci.size <= min(r.url_ptrs)
    offs, body = r.cluster_offsets(last)
    assert offs[-1] == len(body)          # payload ends exactly at the last blob
    total = sum(r.cluster_info(c).size for c in range(r.header.cluster_count))
    assert total < min(r.url_ptrs)


def test_dry_run_writes_nothing(fixture_zim, tmp_path):
    src, _ = fixture_zim
    dst = tmp_path / "never.zim"
    res = derive(str(src), str(dst), Recipe(satellite_max_zoom=-1), dry_run=True, verbose=False)
    assert not dst.exists()
    assert res["plan"]["drop"] + res["plan"]["reencode"] >= 1


def test_hilbert_key_is_a_bijection():
    keys = {tile_sort_key("hilbert", 4, x, y) for x in range(16) for y in range(16)}
    assert keys == set(range(256))
