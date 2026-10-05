"""MBTiles selection must preserve cached facts without successful name lookups."""
import argparse
import gzip
import json
import sqlite3

import mapbox_vector_tile
import pytest

from cloud.wikimedia_http import TransientError
import wikidata_cache as wc


def _feature(properties, geometry=None):
    return {
        "properties": properties,
        "geometry": geometry or {"type": "Point", "coordinates": [1024, 2048]},
    }


def _mbtiles(path, rows, *, compressed=False, extent=4096):
    """Write real MVT data; supplied coordinates and tile rows use XYZ."""
    tiles = {}
    for zoom, x, y, layers in rows:
        data = mapbox_vector_tile.encode(
            [{"name": name, "features": features} for name, features in layers.items()],
            default_options={"y_coord_down": True, "extents": extent})
        tiles[(zoom, x, y)] = gzip.compress(data, mtime=0) if compressed else data
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                     "tile_row INTEGER, tile_data BLOB)")
        conn.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", [
            (z, x, (1 << z) - 1 - y, data) for (z, x, y), data in tiles.items()])
        conn.commit()
    finally:
        conn.close()
    return tiles


@pytest.mark.parametrize("zoom", [13, 14])
@pytest.mark.parametrize("compressed", [False, True])
def test_cached_tile_tags_survive_failed_name_lookup_through_writer(
        tmp_path, monkeypatch, zoom, compressed):
    import create_osm_zim as builder
    from streetzim.zim_writer import _add_wikidata

    mbtiles = tmp_path / "places.mbtiles"
    tiles = _mbtiles(mbtiles, [(zoom, 0, 0, {
        "place": [
            _feature({"name": "Tagged village", "class": "village", "wikidata": "Q110"}),
            _feature({"name": "Untagged city", "class": "city"}),
        ],
        "poi": [_feature({"wikidata": "Q111"})],  # no name
        "custom_layer": [_feature(
            {"name": "Tagged line", "wikidata": "Q112"},
            {"type": "LineString", "coordinates": [[100, 100], [500, 500]]})],
    })], compressed=compressed)
    cached = {qid: {"label": label, "extract": f"{label} cached fact"}
              for qid, label in (("Q110", "Village"), ("Q111", "Unnamed place"),
                                 ("Q112", "Custom line"), ("Q999", "Other region"))}
    cache_dir = tmp_path / "cache"
    wc.save_cache(cache_dir, cached)
    queries = []

    def unavailable(query, **kwargs):
        queries.append(query)
        raise TransientError("fixture rate limit exhausted", stop=True)

    monkeypatch.setattr(wc, "_run_sparql", unavailable)
    compact, selection = builder._build_wikidata(
        args=argparse.Namespace(pbf=None, wikidata_no_extracts=True),
        include_wikidata=True, mbtiles_path=mbtiles, pbf_path=None,
        total_steps=9, wikidata_cache_dir=cache_dir, work_pbf=None)
    assert set(compact) == {"Q110", "Q111", "Q112"} == selection
    assert len(queries) == 1 and "Untagged city" in queries[0]
    assert "Tagged village" not in queries[0] and "Tagged line" not in queries[0]

    class Creator:
        def __init__(self):
            self.items = {}

        def add_item(self, item):
            self.items[item[0]] = json.loads(item[3])

    creator = Creator()
    written = _add_wikidata(
        creator, lambda *args: args, tiles=tiles, mbtiles_path=None,
        bbox=(-180, 85, -179, 85.1), wikidata_data=compact, max_zoom=zoom)
    assert set(written) == {"Q110", "Q111", "Q112"}
    assert creator.items["wikidata/manifest.json"] == {"total": 3, "chunks": {"11": 3}}
    assert creator.items["wikidata/11.json"] == {
        qid: {"l": cached[qid]["label"], "x": cached[qid]["extract"]}
        for qid in ("Q110", "Q111", "Q112")}
    assert wc.load_cache(cache_dir) == cached


def test_malformed_direct_ids_are_not_selected_or_requested(tmp_path, monkeypatch):
    mbtiles = tmp_path / "malformed.mbtiles"
    invalid = ["Q0", "Q01", "Q1;Q2", "Q١", "Q10000000000", 42]
    _mbtiles(mbtiles, [(14, 0, 0, {"custom_layer": [
        _feature({"wikidata": qid}) for qid in ["Q110", *invalid]
    ]})])

    def forbidden(*args, **kwargs):
        pytest.fail("direct or malformed tile tags triggered name lookup")

    monkeypatch.setattr(wc, "_run_sparql", forbidden)
    assert set(wc.extract_qids_from_mbtiles(mbtiles)) == {"Q110"}


def test_direct_tags_take_priority_over_name_matches(tmp_path, monkeypatch):
    mbtiles = tmp_path / "preferred.mbtiles"
    _mbtiles(mbtiles, [(14, 0, 0, {"place": [
        _feature({"name": "Tagged village", "class": "village", "wikidata": "Q110"}),
        _feature({"name": "Ambiguous city", "class": "city"}),
        _feature({"name": "Another city", "class": "city"}),
    ]})])
    queries = []

    def names(query, **kwargs):
        queries.append(query)
        return [{"item": {"value": f"http://www.wikidata.org/entity/{qid}"},
                 "name": {"value": name}}
                for qid, name in (("Q110", "Ambiguous city"), ("Q113", "Another city"))]

    monkeypatch.setattr(wc, "_run_sparql", names)
    selected = wc.extract_qids_from_mbtiles(mbtiles)
    assert set(selected) == {"Q110", "Q113"}
    assert selected["Q110"]["name"] == "Tagged village"
    assert selected["Q113"]["name"] == "Another city"
    assert len(queries) == 1 and "Tagged village" not in queries[0]


def test_lower_zoom_uses_its_own_tms_rows_and_layer_extent(tmp_path, monkeypatch):
    mbtiles = tmp_path / "z13.mbtiles"
    point = {"type": "Point", "coordinates": [256, 512]}
    _mbtiles(mbtiles, [(13, 4096, 4095, {"place": [
        _feature({"name": "Tagged village", "class": "village", "wikidata": "Q110"}, point),
        _feature({"name": "Untagged city", "class": "city"}, point),
    ]})], extent=1024)
    looked_up = []

    def names(features):
        looked_up.extend(features)
        return {"Q113": features[0]}

    monkeypatch.setattr(wc, "_lookup_qids_by_name", names)
    selected = wc.extract_qids_from_mbtiles(mbtiles)
    assert len(looked_up) == 1 and looked_up[0]["name"] == "Untagged city"
    # The point is 1/4 of a z13 tile east of Greenwich and 1/2 tile north
    # of the equator. Hardcoded reference values catch a z14 divisor,
    # reversed TMS row, default4096 extent or flipped pixel y-coordinate.
    for feature in (selected["Q110"], selected["Q113"], looked_up[0]):
        assert feature["lon"] == pytest.approx(0.010986, abs=1e-6)
        assert feature["lat"] == pytest.approx(0.021973, abs=1e-6)


@pytest.mark.parametrize("zooms,expected", [
    ([12, 13, 15], "Q13"), ([13, 14, 15], "Q14"), ([15, 16], "Q15"), ([], None)])
def test_selection_uses_available_detail_zoom(tmp_path, monkeypatch, zooms, expected):
    mbtiles = tmp_path / "zooms.mbtiles"
    _mbtiles(mbtiles, [(z, 0, 0, {"custom_layer": [_feature({"wikidata": f"Q{z}"})]})
                      for z in zooms])

    def forbidden(*args, **kwargs):
        pytest.fail("direct tile IDs triggered name lookup")

    monkeypatch.setattr(wc, "_run_sparql", forbidden)
    assert set(wc.extract_qids_from_mbtiles(mbtiles)) == ({expected} if expected else set())


@pytest.mark.parametrize("give_cache", [True, False])
def test_tile_qids_past_the_extracts_edge_come_from_the_cache(tmp_path, give_cache):
    """Tiles are whole and reach past the extract's edge, so they carry
    Q-IDs the extract's own selection lacks. Those are read from the cache
    at the ZIM step, as when the whole cache was loaded (D.C.'s shipped ZIM:
    27 of its 1,825 entries); Q-IDs in no tile still stay out."""
    from streetzim.zim_writer import _add_wikidata
    tiles = _mbtiles(tmp_path / "t.mbtiles", [(14, 0, 0, {
        "place": [_feature({"name": "Inside", "class": "village", "wikidata": "Q110"}),
                  _feature({"name": "Over the edge", "class": "town", "wikidata": "Q120"}),
                  _feature({"name": "Never cached", "class": "town", "wikidata": "Q130"})],
    })])
    cache_dir = tmp_path / "cache"
    wc.save_cache(cache_dir, {q: {"label": q} for q in ("Q110", "Q120", "Q999")})
    selected = wc.load_cache_for_zim(cache_dir, qids={"Q110"})

    class Creator:
        def add_item(self, item):
            pass

    written = _add_wikidata(
        Creator(), lambda *args: args, tiles=tiles, mbtiles_path=None,
        bbox=(-180, 85, -179, 85.1), wikidata_data=selected, max_zoom=14,
        wikidata_cache=cache_dir if give_cache else None)
    assert set(written) == ({"Q110", "Q120"} if give_cache else {"Q110"})


@pytest.mark.parametrize("given", [None, "/caches/wd"])
def test_the_build_hands_the_zim_writer_its_wikidata_cache(monkeypatch, given):
    import create_osm_zim as builder

    class Args:
        wikidata_cache = given

        def __getattr__(self, name):          # every other option: off
            return 0

    seen = {}
    monkeypatch.setattr(builder, "create_zim", lambda *a, **k: seen.update(k))
    common = {
        "address_count": 0, "args": Args(), "bbox_str": None, "fonts": None,
        "map_config": {}, "maplibre_css": None, "maplibre_js": None, "mbtiles_path": None,
        "name": "x", "output_path": "x.zim", "overture_sources": None,
        "overture_themes": None, "routing_graph_path": None, "satellite_dir": None,
        "satellite_format": "webp", "satellite_max_zoom": None, "search_features": None,
        "terrain_dir": None, "terrain_max_zoom": None, "tile_metadata": {}, "tiles": {},
        "tmpdir": ".", "total_tile_count": 0, "use_streaming": False,
        "wiki_cross_refs": None, "zim_illustration": None, "zim_metadata": {}}
    builder._write_zim(wikidata_data={"Q1": {}}, **common)
    assert str(seen["wikidata_cache"]) == str(given or wc.DEFAULT_CACHE_DIR)
    builder._write_zim(wikidata_data=None, **common)
    assert seen["wikidata_cache"] is None


def _edge_case(tmp_path, monkeypatch, places, cached, selection):
    """_add_wikidata over one tile of `places` with the extract's
    `selection` loaded from a cache of `cached`; also the qids each extra
    cache read asked for, and the chunk files written."""
    from streetzim.zim_writer import _add_wikidata
    tiles = _mbtiles(tmp_path / "t.mbtiles", [(14, 0, 0, {"place": [
        _feature({"name": q, "class": "town", "wikidata": q}) for q in places]})])
    cache_dir = tmp_path / "cache"
    wc.save_cache(cache_dir, {q: {"label": f"label {q}"} for q in cached})
    selected = wc.load_cache_for_zim(cache_dir, qids=selection)
    reads, real = [], wc.load_cache_for_zim

    def recording(path, *, qids=None):
        reads.append(qids)
        return real(path, qids=qids)

    monkeypatch.setattr(wc, "load_cache_for_zim", recording)
    items = {}

    class Creator:
        def add_item(self, item):
            items[item[0]] = item[3]

    written = _add_wikidata(Creator(), lambda *args: args, tiles=tiles, mbtiles_path=None,
                            bbox=(-180, 85, -179, 85.1), wikidata_data=selected, max_zoom=14,
                            wikidata_cache=cache_dir, wikidata_selection=selection)
    return written, reads, items


def test_no_extra_cache_read_without_qids_past_the_edge(tmp_path, monkeypatch):
    """A malformed tag and the extract's own uncached Q-ID are not past the
    edge: China's PBF has both (5 and 159), and would pay a needless pass."""
    written, reads, _ = _edge_case(
        tmp_path, monkeypatch, places=["Q110", "Q111", "Q112;Q113"],
        cached=["Q110", "Q999"], selection={"Q110", "Q111"})
    assert set(written) == {"Q110"} and reads == []


def test_the_extra_read_asks_only_for_the_tiles_qids_in_cache_order(tmp_path, monkeypatch):
    written, reads, items = _edge_case(
        tmp_path, monkeypatch, places=["Q110", "Q115"],
        cached=["Q115", "Q999", "Q110"], selection={"Q110"})
    assert set(written) == {"Q110", "Q115"}
    assert reads == [{"Q110", "Q115"}]               # never the whole cache
    order = list(wc.load_cache_for_zim(tmp_path / "cache", qids={"Q110", "Q115"}))
    assert list(json.loads(items["wikidata/11.json"])) == order
