"""download_overture_data.py: `--release latest` resolution (STAC catalog
first, S3 listing on a STAC outage), the anonymous HTTPS listing, STAC file
pruning, per-release category columns and the release recorded in the
output parquet. No network: every GET goes to a fake with fixtures of the
catalog, collection and item JSON and of the bucket listing."""
from __future__ import annotations

import datetime
import importlib.util
import io
import json
import pathlib
import sys
import urllib.error
import urllib.parse

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "download_overture_data", _ROOT / "download_overture_data.py")
dl = importlib.util.module_from_spec(_SPEC)
sys.modules["download_overture_data"] = dl
_SPEC.loader.exec_module(dl)

TYPES = {"addresses": "address", "places": "place"}
TODAY = datetime.date(2026, 9, 29)


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(b""))


class Net:
    """Stands in for download_overture_data._get.

    bucket: {release: {theme: number of parquet files on S3}}
    stac:   {release: {theme: number of STAC items}}, releases oldest first;
            the newest is "latest". None = STAC is down."""

    def __init__(self, bucket, stac, *, item_bboxes=None, base=None):
        self.bucket, self.calls = bucket, []
        self.base = base or dl.OVERTURE_HTTPS
        self.docs: dict[str, object] = {}
        self.down = stac is None
        if stac is None:
            return
        S = dl.STAC_ROOT
        names = list(stac)
        self.docs[f"{S}/catalog.json"] = {
            "type": "Catalog", "latest": names[-1],
            "links": [{"rel": "child", "href": f"{S}/{n}/catalog.json",
                       **({"latest": True} if n == names[-1] else {})} for n in names]}
        for i, n in enumerate(names):
            links = [{"rel": "child", "href": f"{S}/{n}/{t}/catalog.json"} for t in stac[n]]
            if i:
                links.append({"rel": "prev", "href": f"{S}/{names[i - 1]}/catalog.json"})
            self.docs[f"{S}/{n}/catalog.json"] = {"type": "Catalog", "id": n, "links": links}
            for t, count in stac[n].items():
                items = [f"{S}/{n}/{t}/{TYPES[t]}/{j:05d}/{j:05d}.json" for j in range(count)]
                self.docs[f"{S}/{n}/{t}/{TYPES[t]}/collection.json"] = {
                    "type": "Collection", "id": TYPES[t],
                    "links": [{"rel": "root", "href": f"{S}/catalog.json"}]
                    + [{"rel": "item", "href": h} for h in items]}
                for j, h in enumerate(items):
                    bb = (item_bboxes or {}).get(j, [-180, -90, 180, 90])
                    self.docs[h] = {"type": "Feature", "bbox": bb, "assets": {"aws": {
                        "href": f"{self.base}/{self._prefix(n, t)}part-{j:05d}.zstd.parquet"}}}

    @staticmethod
    def _prefix(release, theme):
        return f"release/{release}/theme={theme}/type={TYPES[theme]}/"

    def listing(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        prefix = q["prefix"]
        keys, prefixes = [], []
        if prefix == "release/" and q.get("delimiter") == "/":
            prefixes = [f"release/{r}/" for r in self.bucket] + ["release/README/"]
        else:
            for r, themes in self.bucket.items():
                for t, n in themes.items():
                    if prefix == self._prefix(r, t):
                        # _SUCCESS sorts first: a probe must not need a .parquet
                        keys = [f"{prefix}_SUCCESS"] + [
                            f"{prefix}part-{j:05d}.zstd.parquet" for j in range(n)]
        if q.get("max-keys"):
            keys = keys[:int(q["max-keys"])]
        body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
        body += "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
        return (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/'
                f'2006-03-01/"><Prefix>{prefix}</Prefix>{body}<IsTruncated>false</IsTruncated>'
                f'</ListBucketResult>').encode()

    def __call__(self, url, *, timeout=60, retries=3):
        self.calls.append(url)
        if url.startswith(dl.OVERTURE_HTTPS + "/?"):
            return self.listing(url)
        if url.startswith(dl.STAC_ROOT) and self.down:
            raise urllib.error.URLError("stac down")
        if url not in self.docs:
            raise _http_error(url, 404)
        return json.dumps(self.docs[url]).encode()


def _net(monkeypatch, bucket, stac, **kw):
    net = Net(bucket, stac, **kw)
    monkeypatch.setattr(dl, "_get", net)
    return net


FULL = {"addresses": 64, "places": 16}


def test_release_sort_key_orders_numerically():
    names = ["2026-09-23.1", "2026-08-19.0", "2026-09-23.10", "2026-09-23.2"]
    assert sorted(names, key=dl.release_sort_key, reverse=True) == [
        "2026-09-23.10", "2026-09-23.2", "2026-09-23.1", "2026-08-19.0"]
    with pytest.raises(ValueError):
        dl.release_sort_key("latest")


def test_latest_comes_from_stac(monkeypatch):
    net = _net(monkeypatch, {r: dict(FULL) for r in ("2026-08-19.0", "2026-09-23.1")},
               {"2026-08-19.0": FULL, "2026-09-23.1": FULL})
    assert dl.resolve_release("latest", ["addresses", "places"]) == "2026-09-23.1"
    # No root listing of release/: STAC names the release.
    assert not any("delimiter" in u for u in net.calls)


def test_partly_uploaded_release_is_skipped(monkeypatch, capsys):
    # 2026-09-23.1's places are still uploading: 7 of STAC's 16 files.
    _net(monkeypatch, {"2026-09-23.0": dict(FULL), "2026-09-23.1": {"addresses": 64, "places": 7}},
         {"2026-09-23.0": FULL, "2026-09-23.1": FULL})
    assert dl.resolve_release("latest", ["places"]) == "2026-09-23.0"
    assert dl.resolve_release("latest", ["addresses"]) == "2026-09-23.1"
    out = capsys.readouterr()
    assert "2026-09-23.1 is incomplete (places: 7 files on S3, STAC lists 16)" in out.err
    assert out.out == ""


def test_release_without_the_theme_walks_back(monkeypatch):
    _net(monkeypatch, {"2026-08-19.0": dict(FULL), "2026-09-23.1": {"addresses": 64}},
         {"2026-08-19.0": FULL, "2026-09-23.1": {"addresses": 64}})
    assert dl.resolve_release("latest", ["addresses", "places"]) == "2026-08-19.0"


def test_more_s3_files_than_stac_items_is_incomplete(monkeypatch):
    _net(monkeypatch, {"2026-08-19.0": dict(FULL), "2026-09-23.1": {"addresses": 64, "places": 17}},
         {"2026-08-19.0": FULL, "2026-09-23.1": FULL})
    assert dl.resolve_release("latest", ["places"]) == "2026-08-19.0"


def test_no_complete_release(monkeypatch):
    _net(monkeypatch, {"2026-09-23.1": {"addresses": 64}}, {"2026-09-23.1": {"addresses": 64}})
    with pytest.raises(SystemExit, match="no release in the STAC catalog is complete"):
        dl.resolve_release("latest", ["places"])


@pytest.mark.parametrize("today,want", [(TODAY, "2026-09-23.1"),
                                        (datetime.date(2026, 9, 25), "2026-08-19.0")])
def test_stac_outage_falls_back_to_old_enough_s3_releases(monkeypatch, capsys, today, want):
    _net(monkeypatch, {r: dict(FULL) for r in ("2026-08-19.0", "2026-09-23.0", "2026-09-23.1")},
         None)
    assert dl.resolve_release("latest", ["places"], today=today) == want
    assert "STAC catalog unavailable" in capsys.readouterr().err


def test_stac_root_404_counts_as_outage(monkeypatch):
    net = _net(monkeypatch, {"2026-08-19.0": dict(FULL)}, {"2026-08-19.0": FULL})
    del net.docs[f"{dl.STAC_ROOT}/catalog.json"]
    assert dl.resolve_release("latest", ["places"], today=TODAY) == "2026-08-19.0"
    assert any("delimiter" in u for u in net.calls)


def test_explicit_pin_needs_no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a pinned release must not touch the network")
    monkeypatch.setattr(dl, "_get", boom)
    assert dl.resolve_release("2026-05-20.0", ["places"]) == "2026-05-20.0"


def test_unreachable_listing_says_how_to_pin(monkeypatch):
    def down(url, **_k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(dl, "_get", down)
    with pytest.raises(SystemExit) as e:
        dl.resolve_release("latest", ["places"], today=TODAY)
    assert "--release YYYY-MM-DD.N" in str(e.value)
    assert "OVERTURE_RELEASE" in str(e.value)


def test_print_release_writes_only_the_name_to_stdout(monkeypatch, capsys):
    _net(monkeypatch, {"2026-09-23.0": dict(FULL), "2026-09-23.1": {"places": 3}},
         {"2026-09-23.0": FULL, "2026-09-23.1": FULL})
    monkeypatch.setattr(sys, "argv", ["d", "places", "--print-release"])
    dl.main()
    assert capsys.readouterr().out == "2026-09-23.0\n"


def test_theme_files_and_probe_ignore_non_parquet_keys(monkeypatch):
    _net(monkeypatch, {"r": {"places": 2}, "e": {"places": 0}}, None)
    assert dl.theme_files("r", "places") == [
        "release/r/theme=places/type=place/part-00000.zstd.parquet",
        "release/r/theme=places/type=place/part-00001.zstd.parquet"]
    assert dl.has_theme("r", "places")          # first key is _SUCCESS
    assert not dl.has_theme("x", "places")


class _Resp:
    def __init__(self, body):
        self.body = body.encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("err,retried", [
    (lambda u: _http_error(u, 503), True),
    (lambda u: TimeoutError("timed out"), True),
    (lambda u: urllib.error.URLError("reset"), True),
    (lambda u: _http_error(u, 403), False),
    (lambda u: _http_error(u, 404), False),
])
def test_get_retries_only_transient_errors(monkeypatch, err, retried):
    n = []

    def urlopen(url, timeout):
        n.append(url)
        if len(n) == 1:
            raise err(url)
        return _Resp("ok")

    monkeypatch.setattr(dl.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)
    if retried:
        assert dl._get("https://x/") == b"ok" and len(n) == 2
    else:
        with pytest.raises(urllib.error.HTTPError):
            dl._get("https://x/")
        assert len(n) == 1


def test_get_gives_up_after_retries(monkeypatch):
    n = []

    def urlopen(url, timeout):
        n.append(url)
        raise _http_error(url, 500)

    monkeypatch.setattr(dl.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        dl._get("https://x/", retries=3)
    assert len(n) == 3


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

    def fake_urlopen(url, timeout):
        urls.append(url)
        return _Resp(pages[len(urls) - 1])

    monkeypatch.setattr(dl.urllib.request, "urlopen", fake_urlopen)
    keys, prefixes = dl._list_bucket("release/x/")
    assert keys == ["release/x/a.parquet", "release/x/b.parquet"]
    assert prefixes == ["release/x/y/"]
    assert urls[0].startswith(dl.OVERTURE_HTTPS + "/?")
    assert "continuation-token=t1" in urls[1]


def test_list_bucket_forbidden_is_a_listing_error(monkeypatch):
    def urlopen(url, timeout):
        raise _http_error(url, 403)
    monkeypatch.setattr(dl.urllib.request, "urlopen", urlopen)
    with pytest.raises(dl.ReleaseListingError):
        dl._list_bucket("release/")


def test_stac_files_keep_files_meeting_the_bbox(monkeypatch):
    # item 0: Europe; item 1: Alaska east of the antimeridian; item 2: no bbox.
    _net(monkeypatch, {}, {"r1": {"places": 4}}, item_bboxes={
        0: [5.0, 40.0, 10.0, 50.0], 1: [-180.0, 51.0, -130.0, 72.0], 2: None,
        3: [100.0, -40.0, 150.0, -10.0]})
    pre = f"{dl.OVERTURE_HTTPS}/release/r1/theme=places/type=place/"
    monaco = (7.40, 43.72, 7.44, 43.76)
    files, n = dl.stac_files("r1", "places", monaco)
    assert n == 4 and files == [pre + "part-00000.zstd.parquet", pre + "part-00002.zstd.parquet"]
    files, _ = dl.stac_files("r1", "places", (172.0, 51.0, -130.0, 72.0))   # across 180°
    assert files == [pre + "part-00001.zstd.parquet", pre + "part-00002.zstd.parquet"]
    assert dl.stac_files("r0", "places", monaco) is None                   # no collection


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
    assert ("read_parquet(['https://h/a.parquet', 'https://h/b''.parquet'], "
            "hive_partitioning=1, union_by_name=true)") in sql
    assert "KV_METADATA {overture_release: '2026-09-23.1'}" in sql


def _place_file(con, path, pid, lon, lat, cat_cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT '{pid}' id, {{'primary': '{pid}'}} "names", 0.9 confidence,
        ['+1'] phones, ['https://c.example'] websites, []::VARCHAR[] emails,
        []::VARCHAR[] socials, NULL brand, NULL addresses, NULL sources, {cat_cols}
        ST_Point({lon}, {lat}) geometry,
        {{'xmin': {lon}, 'xmax': {lon}, 'ymin': {lat}, 'ymax': {lat}}} bbox)
        TO '{path}' (FORMAT PARQUET)""")


def test_download_latest_reads_stac_picked_files_with_mixed_schemas(monkeypatch, tmp_path):
    """End to end on local files standing in for the bucket: `latest`
    resolves through STAC, only the files whose STAC bbox meets Monaco are
    read, a file lacking a column reads it as NULL (union_by_name), and the
    output records the release."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
    except Exception as e:  # offline CI without the extensions cached
        pytest.skip(f"duckdb extensions unavailable: {e}")
    rel = "2026-09-23.1"
    part = tmp_path / f"release/{rel}/theme=places/type=place"
    tax = ("{'primary': 'coffee_shop', 'hierarchy': ['food_and_drink', 'coffee_shop'], "
           "'alternates': []::VARCHAR[]} taxonomy, 'cafe' basic_category,")
    _place_file(con, part / "part-00000.zstd.parquet", "a", 7.42, 43.74, tax)
    _place_file(con, part / "part-00001.zstd.parquet", "b", 7.43, 43.75,
                "{'primary': 'museum', 'alternate': []::VARCHAR[]} categories,")
    # In the bbox, but STAC says the file is elsewhere: must not be read.
    _place_file(con, part / "part-00002.zstd.parquet", "c", 7.41, 43.73, tax)
    monkeypatch.setattr(dl, "OVERTURE_HTTPS", str(tmp_path))
    _net(monkeypatch, {"2026-08-19.0": {"places": 3}, rel: {"places": 3}},
         {"2026-08-19.0": {"places": 3}, rel: {"places": 3}},
         item_bboxes={0: [7.0, 43.0, 8.0, 44.0], 1: [7.0, 43.0, 8.0, 44.0],
                      2: [100.0, 0.0, 101.0, 1.0]}, base=str(tmp_path))
    out = tmp_path / "out.parquet"
    dl.download_overture("places", "7.40,43.72,7.44,43.76", "latest", str(out))
    from streetzim.overture import parquet_release
    assert parquet_release(str(out)) == rel
    rows = con.execute(f"""SELECT id, categories.primary, taxonomy.primary
                           FROM '{out}' ORDER BY id""").fetchall()
    assert rows == [("a", None, "coffee_shop"), ("b", "museum", None)]
