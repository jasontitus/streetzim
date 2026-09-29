"""download_overture_data.py: `--release latest` resolution, the anonymous
HTTPS listing, per-release category columns and the release recorded in the
output parquet. No network: the bucket listing is faked."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import urllib.error

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "download_overture_data", _ROOT / "download_overture_data.py")
dl = importlib.util.module_from_spec(_SPEC)
sys.modules["download_overture_data"] = dl
_SPEC.loader.exec_module(dl)


def _fake_bucket(monkeypatch, releases: dict[str, list[str]]):
    """releases: {name: [themes with files]}; returns the listed prefixes."""
    calls = []

    def fake(prefix, *, delimiter=None, max_keys=None, **_kw):
        calls.append(prefix)
        if prefix == "release/":
            return [], [f"release/{r}/" for r in releases] + ["release/README/"]
        _, rel, theme_dir, _type = prefix.split("/", 3)
        theme = theme_dir.split("=", 1)[1]
        if theme in releases.get(rel, []):
            return [f"{prefix}part-00000.zstd.parquet"], []
        return [], []

    monkeypatch.setattr(dl, "_list_bucket", fake)
    return calls


def test_release_sort_key_orders_numerically():
    names = ["2026-09-23.1", "2026-08-19.0", "2026-09-23.10", "2026-09-23.2"]
    assert sorted(names, key=dl.release_sort_key, reverse=True) == [
        "2026-09-23.10", "2026-09-23.2", "2026-09-23.1", "2026-08-19.0"]
    with pytest.raises(ValueError):
        dl.release_sort_key("latest")


def test_latest_is_newest_release_with_every_theme(monkeypatch):
    _fake_bucket(monkeypatch, {
        "2026-08-19.0": ["addresses", "places"],
        "2026-09-23.0": ["addresses", "places"],
        "2026-09-23.1": ["addresses"],          # places not published (yet)
    })
    assert dl.resolve_release("latest", ["addresses"]) == "2026-09-23.1"
    assert dl.resolve_release("latest", ["places"]) == "2026-09-23.0"
    assert dl.resolve_release("latest", ["addresses", "places"]) == "2026-09-23.0"


def test_explicit_pin_needs_no_listing(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a pinned release must not list the bucket")
    monkeypatch.setattr(dl, "_list_bucket", boom)
    assert dl.resolve_release("2026-05-20.0", ["places"]) == "2026-05-20.0"


def test_unreachable_listing_says_how_to_pin(monkeypatch):
    def down(prefix, **_k):
        raise dl.ReleaseListingError("connection refused")
    monkeypatch.setattr(dl, "_list_bucket", down)
    with pytest.raises(SystemExit) as e:
        dl.resolve_release("latest", ["places"])
    assert "--release YYYY-MM-DD.N" in str(e.value)
    assert "OVERTURE_RELEASE" in str(e.value)


def test_no_release_has_theme(monkeypatch):
    _fake_bucket(monkeypatch, {"2026-09-23.0": ["addresses"]})
    with pytest.raises(SystemExit, match="has theme"):
        dl.resolve_release("latest", ["places"])


_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>b</Name>
<Prefix>{prefix}</Prefix>{body}<IsTruncated>{trunc}</IsTruncated>{token}</ListBucketResult>"""


def test_list_bucket_parses_and_follows_continuation(monkeypatch):
    pages = [
        _PAGE.format(prefix="release/x/", trunc="true",
                     body="<Contents><Key>release/x/a.parquet</Key></Contents>",
                     token="<NextContinuationToken>t1</NextContinuationToken>"),
        _PAGE.format(prefix="release/x/", trunc="false",
                     body="<Contents><Key>release/x/b.parquet</Key></Contents>"
                          "<CommonPrefixes><Prefix>release/x/y/</Prefix></CommonPrefixes>",
                     token=""),
    ]
    urls = []

    class Resp:
        def __init__(self, body):
            self.body = body.encode()

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout):
        urls.append(url)
        return Resp(pages[len(urls) - 1])

    monkeypatch.setattr(dl.urllib.request, "urlopen", fake_urlopen)
    keys, prefixes = dl._list_bucket("release/x/")
    assert keys == ["release/x/a.parquet", "release/x/b.parquet"]
    assert prefixes == ["release/x/y/"]
    assert urls[0].startswith(dl.OVERTURE_HTTPS + "/?")
    assert "continuation-token=t1" in urls[1]


def test_list_bucket_retries_then_raises(monkeypatch):
    n = []

    def fail(url, timeout):
        n.append(url)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(dl.urllib.request, "urlopen", fail)
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)
    with pytest.raises(dl.ReleaseListingError):
        dl._list_bucket("release/", retries=3)
    assert len(n) == 3


def test_theme_columns_follow_the_release():
    spec = dl.THEME_SPECS["places"]
    old = dl.theme_columns(spec, {"id", "categories", "taxonomy", "basic_category"})
    new = dl.theme_columns(spec, {"id", "taxonomy", "basic_category"})
    assert "categories" in old and "taxonomy" in old
    assert "categories" not in new and "taxonomy, basic_category" in new
    for cols in (old, new):
        assert cols.endswith("ST_AsText(geometry) AS wkt")
    assert dl.theme_columns(dl.THEME_SPECS["addresses"], set()) == \
        dl.THEME_SPECS["addresses"]["columns"]


def test_overture_sql_file_list_and_release_metadata():
    sql = dl.overture_sql("id", ["https://h/a.parquet", "https://h/b'.parquet"],
                          (7.40, 43.72, 7.44, 43.76), "o.parquet",
                          kv_metadata={"overture_release": "2026-09-23.1"})
    assert "read_parquet(['https://h/a.parquet', 'https://h/b''.parquet'], hive_partitioning=1)" in sql
    assert "KV_METADATA {overture_release: '2026-09-23.1'}" in sql


def test_download_latest_over_listed_files(monkeypatch, tmp_path):
    """End to end on local files standing in for the bucket: `latest`
    resolves, the category columns this release has are projected, and
    the output records the release."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
    except Exception as e:  # offline CI without the extensions cached
        pytest.skip(f"duckdb extensions unavailable: {e}")
    part = tmp_path / "release/2026-09-23.1/theme=places/type=place"
    part.mkdir(parents=True)
    con.execute(f"""COPY (SELECT 'p1' id, {{'primary': 'Cafe'}} "names", 0.9 confidence,
        ['+1'] phones, ['https://c.example'] websites, []::VARCHAR[] emails,
        []::VARCHAR[] socials, NULL brand, NULL addresses, NULL sources,
        {{'primary': 'coffee_shop', 'hierarchy': ['food_and_drink', 'coffee_shop'],
          'alternates': []::VARCHAR[]}} taxonomy, 'cafe' basic_category,
        ST_Point(7.42, 43.74) geometry,
        {{'xmin': 7.42, 'xmax': 7.42, 'ymin': 43.74, 'ymax': 43.74}} bbox)
        TO '{part}/part-00000.zstd.parquet' (FORMAT PARQUET)""")
    _fake_bucket(monkeypatch, {"2026-08-19.0": ["places"], "2026-09-23.1": ["places"]})
    monkeypatch.setattr(dl, "OVERTURE_HTTPS", str(tmp_path))
    out = tmp_path / "out.parquet"
    dl.download_overture("places", "7.40,43.72,7.44,43.76", "latest", str(out))
    from streetzim.overture import parquet_release
    assert parquet_release(str(out)) == "2026-09-23.1"
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()]
    assert "taxonomy" in cols and "categories" not in cols
