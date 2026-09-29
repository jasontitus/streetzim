"""openZIM metadata flags: validation rules (streetzim/zim_metadata.py) and
what create_zim writes with and without them."""
from __future__ import annotations

import gzip
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

zm = pytest.importorskip("streetzim.zim_metadata")
pytest.importorskip("regex")


# ------------------------------------------------------------ validation


def test_lengths_count_graphemes_not_code_points():
    # 30 flags = 30 graphemes but 60 code points: allowed, as in zimscraperlib.
    assert zm.build_overrides(title="🇫🇷" * 30)["Title"] == "🇫🇷" * 30
    with pytest.raises(ValueError, match="Title is too long"):
        zm.build_overrides(title="x" * 31)
    with pytest.raises(ValueError, match="Description is too long"):
        zm.build_overrides(description="x" * 81)
    with pytest.raises(ValueError, match="LongDescription is too long"):
        zm.build_overrides(long_description="x" * 4001)


def test_cleaning_and_empty_values():
    assert zm.build_overrides(title="  Monaco\x07 ")["Title"] == "Monaco"
    with pytest.raises(ValueError, match="empty"):
        zm.build_overrides(title=" \t ")
    with pytest.raises(ValueError, match="empty"):
        zm.build_overrides(name="")


def test_whitespace_only_is_empty_like_zimscraperlib():
    with pytest.raises(ValueError, match="empty"):
        zm.build_overrides(title="\u00a0 \u2003")
    # zimscraperlib has no Description != LongDescription rule; neither do we.
    assert zm.build_overrides(description="Same", long_description="Same")


def test_only_given_values_are_returned():
    assert zm.build_overrides() == {}
    assert set(zm.build_overrides(creator="X", publisher="Y")) == {"Creator", "Publisher"}


def test_tags_deduplicated_and_merged_after_builder_tags():
    assert zm.parse_tags("a; b;a") == ["a", "b"]
    with pytest.raises(ValueError, match="empty tag"):
        zm.parse_tags("a;;b")
    ours = "maps;osm;offline;_pictures:yes;_ftindex:yes"
    assert zm.merge_tags(ours, ["osm", "extra"]) == ours + ";extra"
    assert zm.merge_tags(ours, None) == ours
    # A `_key:value` tag replaces ours with the same key (maps2zim's merge).
    assert zm.merge_tags(ours, ["_pictures:no", "x"]) == \
        "maps;osm;offline;_ftindex:yes;_pictures:no;x"


def _png(size, mode="RGB", fmt="PNG"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, size, (10, 20, 30)).save(buf, fmt)
    return buf.getvalue()


@pytest.mark.parametrize("size,fmt", [((300, 200), "JPEG"), ((16, 16), "PNG"),
                                       ((48, 48), "WEBP")])
def test_illustration_becomes_48x48_png(size, fmt):
    from PIL import Image
    out = zm.illustration_png(_png(size, fmt=fmt))
    img = Image.open(io.BytesIO(out))
    assert (img.format, img.size) == ("PNG", (48, 48))


@pytest.mark.parametrize("sep", ["\u00a0", "\u3000", "\u2003", "\u0085"])
def test_blank_unicode_tag_refused(sep):
    # zimscraperlib's cleanup strips ASCII whitespace only; build_overrides
    # refuses such a tag on both paths.
    with pytest.raises(ValueError, match="empty tag"):
        zm.build_overrides(tags=f"maps;{sep}")


SVG = (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="20" '
       b'height="10"><rect width="20" height="10" fill="red"/></svg>')


def test_illustration_svg_and_garbage(tmp_path):
    from streetzim import scraperlib
    if scraperlib.AVAILABLE:
        # zimscraperlib converts SVG (cairosvg), as maps2zim's illustration does.
        from PIL import Image
        img = Image.open(io.BytesIO(zm.illustration_png(SVG)))
        assert (img.format, img.size) == ("PNG", (48, 48))
    else:
        with pytest.raises(ValueError, match="SVG"):
            zm.illustration_png(SVG)
    with pytest.raises(ValueError, match="not a (readable|usable) image"):
        zm.illustration_png(b"not an image")
    p = tmp_path / "i.png"
    p.write_bytes(_png((64, 64)))
    assert zm.load_illustration(str(p)) == zm.load_illustration(f"file://{p}")


# ------------------------------------------------------------ create_zim


def _build(tmp_path, **kw):
    libzim = pytest.importorskip("libzim.reader")
    mvt = pytest.importorskip("mapbox_vector_tile")
    from streetzim.zim_writer import create_zim
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X", "wikidata": "Q1"}}]}]))
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    out = tmp_path / "t.zim"
    create_zim(
        out, tiles={(14, 8529, 5974): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Monaco"}, name="OSM - Monaco",
        bbox=(7.40, 43.72, 7.44, 43.76),
        search_features=[{"name": "Casino", "type": "poi", "subtype": "casino",
                          "lat": 43.739, "lon": 7.428}],
        xapian_mode="none", **kw)
    a = libzim.Archive(str(out))
    md = {k: a.get_metadata(k) for k in a.metadata_keys}
    md["map-config"] = json.loads(bytes(a.get_entry_by_path("map-config.json")
                                        .get_item().content))
    return md


def test_defaults_unchanged_and_license_lists_only_present_layers(tmp_path):
    md = _build(tmp_path)
    assert md["Title"] == b"OSM - Monaco"
    assert md["Name"] == b"osm_osm_-_monaco"          # the lineage production keeps
    assert md["Publisher"] == b"create_osm_zim"
    assert md["Creator"] == b"OpenStreetMap contributors"
    assert md["Scraper"] == b"streetzim/1.0"
    assert md["Tags"] == b"maps;osm;offline;_pictures:yes"   # --xapian none: no _ftindex
    assert "LongDescription" not in md
    lic = md["License"].decode()
    assert "ODbL" in lic and "NC" not in lic and "Copernicus" not in lic
    assert "Wikipedia" not in lic


def test_osm_wiki_tags_alone_do_not_claim_wikipedia(tmp_path):
    lic = _build(tmp_path, wiki_cross_refs={("X", 43.7, 7.4): {"wikipedia": "en:X"}}
                 )["License"].decode()
    assert "Wikipedia" not in lic and "Wikidata" not in lic


@pytest.mark.parametrize("stored", [0, 1])
def test_wikipedia_credited_only_when_articles_are_stored(tmp_path, monkeypatch, stored):
    # Bundling requested with Wikipedia links, but the source may yield
    # nothing (an unreachable API, the wrong offline ZIM): then neither
    # map-config nor License may claim Wikipedia content.
    import cloud.wiki_articles as wa

    def fake_bundle(titles, add, **kw):
        if stored:
            add("wiki-article/X", "X", "text/html", b"<p>X</p>")
        return {"bundled": stored, "bytes": 8 * stored, "failed": 1 - stored,
                "stored_titles": {"X"} if stored else set()}
    monkeypatch.setattr(wa, "bundle_wiki_articles", fake_bundle)
    md = _build(tmp_path, wiki_cross_refs={("X", 43.7, 7.4): {"wikipedia": "en:X"}},
                bundle_wiki_articles=True)
    assert ("Wikipedia" in md["License"].decode()) == bool(stored)
    assert md["map-config"].get("hasWikiArticles", False) == bool(stored)


def test_license_names_satellite_terrain_and_wiki_when_present(tmp_path):
    sat = tmp_path / "sat"
    ter = tmp_path / "ter"
    sat.mkdir()
    ter.mkdir()
    lic = _build(tmp_path, satellite_dir=str(sat), terrain_dir=str(ter),
                 wikidata_data={"Q1": {"label": "x"}})["License"].decode()
    assert "CC BY-NC-SA 4.0" in lic and "Copernicus" in lic
    assert "CC BY-SA 4.0 (Wikipedia)" in lic


def test_overrides_and_illustration_are_written(tmp_path):
    md = _build(tmp_path, metadata=zm.build_overrides(
        name="osm_en_monaco", title="Monaco", description="Offline Monaco",
        long_description="Streets and places of Monaco.", creator="OSM",
        publisher="openZIM", tags="openstreetmap;maps", scraper="streetzim v1"),
        illustration=zm.illustration_png(_png((96, 96))))
    assert md["Name"] == b"osm_en_monaco"
    assert md["Title"] == b"Monaco"
    assert md["Description"] == b"Offline Monaco"
    assert md["LongDescription"] == b"Streets and places of Monaco."
    assert (md["Creator"], md["Publisher"], md["Scraper"]) == (b"OSM", b"openZIM", b"streetzim v1")
    assert md["Tags"] == b"maps;osm;offline;_pictures:yes;openstreetmap"
    from PIL import Image
    ill = Image.open(io.BytesIO(md["Illustration_48x48@1"]))
    assert ill.size == (48, 48) and ill.getpixel((24, 24))[:3] == (10, 20, 30)
