#!/usr/bin/env python3
"""Download Overture Maps Foundation data for a bbox via DuckDB.

Used by `create_osm_zim.py --overture-addresses <parquet-path>` to fill in
address gaps in OSM (e.g. the 1029-block residential stretch of Ramona
Street in Palo Alto that OSM is missing as of 2026-03-10 planet PBF).

Reads `release/<release>/` of the public overturemaps-us-west-2 bucket and
filters to bbox with the predicate pushed down into parquet readers, so
only relevant row groups get fetched. Caches per-release per-bbox so
reruns are instant.

Transport: by default the theme's files are listed with an anonymous S3
ListObjectsV2 request over HTTPS and DuckDB reads those https:// URLs. That
is the same endpoint DuckDB's s3:// (vhost style) talks to, and works where
s3:// does not (a proxy that injects its own credentials makes s3:// fail
with "Invalid Access Key"). When Overture's STAC catalog lists the same
files, only those whose bbox meets the requested bbox are read.
`--transport s3` keeps the old s3:// glob over every file.

Release: `--release latest` (the default) is the release Overture's STAC
catalog (https://stac.overturemaps.org/catalog.json) marks "latest",
walking back through its `prev` links to the newest one that is complete
for every requested theme: its S3 file count per theme/type equals the
STAC collection's item count, so a release still being uploaded is never
picked. Only when STAC is down does it fall back to the S3 listing, and
then only to releases dated more than 2 days ago. The resolved name is
printed (stderr for --print-release) and written into the output parquet's
key-value metadata (`overture_release`); create_osm_zim.py records it in
the ZIM's overture-sources.json. Pin a release (`--release 2026-09-23.1`)
for a reproducible rebuild; `--print-release` resolves and prints the name
only, which wrappers use to fix the release once per run.

Usage:
  python3 download_overture_data.py addresses \\
      --bbox=-122.6,37.2,-121.7,37.9 \\
      --release latest \\
      --out overture_cache/sv-addresses.parquet
  python3 download_overture_data.py addresses places --print-release

Supported themes: `addresses`, `places`. `transportation` is out of
scope — see docs/overture-matching.md for the integration plan.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

# Run as a script from the repository root, which is then on sys.path.
from streetzim.overture import KV_RELEASE, parquet_release

OVERTURE_BUCKET = "overturemaps-us-west-2"
OVERTURE_S3_BUCKET = f"s3://{OVERTURE_BUCKET}"
# Virtual-hosted endpoint: what DuckDB's s3:// with s3_url_style='vhost'
# and s3_region='us-west-2' requests; public, no credentials needed.
OVERTURE_HTTPS = f"https://{OVERTURE_BUCKET}.s3.us-west-2.amazonaws.com"
DEFAULT_RELEASE = "latest"
RELEASE_CALENDAR = "https://docs.overturemaps.org/release-calendar/"
STAC_ROOT = "https://stac.overturemaps.org"
# Without STAC, a release counts as complete only once it is this old.
S3_FALLBACK_MIN_AGE_DAYS = 2
# DuckDB's reads of the parquet files: retry transient HTTP failures.
DUCKDB_HTTP_SETTINGS = ("SET http_retries=8; SET http_retry_backoff=2; "
                        "SET http_retry_wait_ms=500;")
_RELEASE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(\d+)$")

# Each theme needs its own S3 path suffix + column projection.
# Adding a theme requires auditing its schema against
# create_osm_zim.py's merge_* consumers — don't add blindly.
THEME_SPECS = {
    "addresses": {
        "s3_glob": "theme=addresses/type=address/*",
        "type": "address",
        "columns": (
            "id, number, street, postcode, unit, country, "
            "address_levels, sources, "
            "ST_AsText(geometry) AS wkt"
        ),
    },
    "places": {
        "s3_glob": "theme=places/type=place/*",
        "type": "place",
        # Struct-typed fields (names / categories / brand / addresses /
        # sources) come across as DuckDB STRUCT / LIST values which
        # fetch_record_batch materializes as Python dicts / lists in
        # pyarrow-backed rows. No extraction needed here — the merge
        # step unpacks them.
        "columns": (
            "id, names, confidence, phones, websites, "
            "emails, socials, brand, addresses, sources, "
            "ST_AsText(geometry) AS wkt"
        ),
        # Category columns, each projected only when the release has it.
        # `categories` was removed in 2026-09-23.0; `taxonomy` and
        # `basic_category` replace it (2026-08-19.0 carries all three).
        # streetzim.overture.overture_category reads whichever is there.
        "optional_columns": ("categories", "taxonomy", "basic_category"),
    },
}
SUPPORTED_THEMES = set(THEME_SPECS.keys())


class ReleaseListingError(RuntimeError):
    """The bucket listing needed to resolve `latest` (or to find a
    theme's files over HTTPS) failed."""


class StacUnavailable(RuntimeError):
    """The STAC catalog could not be read (outage, timeout, bad JSON)."""


class StacMissing(RuntimeError):
    """The STAC catalog has no such document (HTTP 404)."""


def _get(url: str, *, timeout: float = 60, retries: int = 3) -> bytes:
    """GET with retries on timeouts, connection errors and 5xx. Any other
    HTTP error (403, 404, ...) is raised at once: retrying cannot fix it."""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries - 1:
                raise
        except (urllib.error.URLError, OSError):
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _theme_prefix(release: str, theme: str) -> str:
    return f"release/{release}/{THEME_SPECS[theme]['s3_glob'].rstrip('*')}"


def _list_bucket(prefix: str, *, delimiter: str | None = None,
                 max_keys: int | None = None, timeout: float = 60,
                 retries: int = 3) -> tuple[list[str], list[str]]:
    """Anonymous S3 ListObjectsV2 over HTTPS: (keys, common prefixes).
    Follows continuation tokens unless `max_keys` caps the listing."""
    keys: list[str] = []
    prefixes: list[str] = []
    token = None
    while True:
        q = {"list-type": "2", "prefix": prefix}
        if delimiter:
            q["delimiter"] = delimiter
        if max_keys:
            q["max-keys"] = str(max_keys)
        if token:
            q["continuation-token"] = token
        url = f"{OVERTURE_HTTPS}/?{urllib.parse.urlencode(q)}"
        try:
            body = _get(url, timeout=timeout, retries=retries)
        except (urllib.error.URLError, OSError) as e:
            raise ReleaseListingError(f"{url}: {e}") from e
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as e:
            raise ReleaseListingError(f"{url}: unparseable listing ({e})") from e
        ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
        keys += [k.text or "" for k in root.iter(f"{ns}Key")]
        prefixes += [p.text or "" for p in root.iter(f"{ns}Prefix")
                     if p.text != prefix]
        token = root.findtext(f"{ns}NextContinuationToken")
        if max_keys or not token or root.findtext(f"{ns}IsTruncated") != "true":
            return keys, prefixes


def release_sort_key(name: str) -> tuple[str, int]:
    """2026-09-23.1 sorts after 2026-09-23.0, and .10 after .9."""
    m = _RELEASE_RE.match(name)
    if not m:
        raise ValueError(f"not an Overture release name: {name!r}")
    return m.group(1), int(m.group(2))


def list_releases() -> list[str]:
    """Release names under release/, newest first."""
    _, prefixes = _list_bucket("release/", delimiter="/")
    names = [p[len("release/"):].rstrip("/") for p in prefixes]
    return sorted((n for n in names if _RELEASE_RE.match(n)),
                  key=release_sort_key, reverse=True)


def theme_files(release: str, theme: str) -> list[str]:
    """The theme's parquet object keys in `release`."""
    keys, _ = _list_bucket(_theme_prefix(release, theme))
    return [k for k in keys if k.endswith(".parquet")]


def has_theme(release: str, theme: str) -> bool:
    """True if `release` has any object under the theme's prefix (one key
    listed, so a probe costs one small request)."""
    return bool(_list_bucket(_theme_prefix(release, theme), max_keys=1)[0])


def _pin_hint(detail: str) -> str:
    return (f"{detail}\n  Pin a release instead: --release YYYY-MM-DD.N "
            f"(OVERTURE_RELEASE=… for the ops wrappers). Current names: "
            f"{RELEASE_CALENDAR} or `aws s3 ls --no-sign-request "
            f"{OVERTURE_S3_BUCKET}/release/`.")


def stac_json(url: str) -> dict:
    try:
        body = _get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise StacMissing(url) from e
        raise StacUnavailable(f"{url}: {e}") from e
    except (urllib.error.URLError, OSError) as e:
        raise StacUnavailable(f"{url}: {e}") from e
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise StacUnavailable(f"{url}: not JSON ({e})") from e
    if not isinstance(doc, dict):
        raise StacUnavailable(f"{url}: not a STAC object")
    return doc


def _links(doc: dict, rel: str) -> list[dict]:
    return [ln for ln in doc.get("links") or [] if ln.get("rel") == rel and ln.get("href")]


def stac_collection(release: str, theme: str) -> dict:
    """The STAC collection of the theme's type in `release`
    (StacMissing when the release has no such collection)."""
    return stac_json(f"{STAC_ROOT}/{release}/{theme}/{THEME_SPECS[theme]['type']}/collection.json")


def release_complete(release: str, theme: str) -> str | None:
    """None if `release` is complete for `theme`: STAC has the collection
    and S3 holds exactly as many parquet files as it has items. Otherwise
    why not. StacUnavailable / ReleaseListingError propagate."""
    try:
        coll = stac_collection(release, theme)
    except StacMissing:
        return "no STAC collection"
    n_stac = len(_links(coll, "item"))
    n_s3 = len(theme_files(release, theme))
    if n_stac == 0 or n_s3 != n_stac:
        return f"{n_s3} files on S3, STAC lists {n_stac}"
    return None


def _resolve_via_stac(themes) -> str:
    try:
        root = stac_json(f"{STAC_ROOT}/catalog.json")
    except StacMissing as e:
        raise StacUnavailable(str(e)) from e
    latest = root.get("latest")
    if not isinstance(latest, str) or not latest:
        raise StacUnavailable(f"{STAC_ROOT}/catalog.json has no 'latest'")
    url = next((ln["href"] for ln in _links(root, "child") if ln.get("latest")),
               f"{STAC_ROOT}/{latest}/catalog.json")
    seen: set[str] = set()
    skipped: list[str] = []
    while url and url not in seen:
        seen.add(url)
        try:
            cat = stac_json(url)
        except StacMissing as e:
            raise StacUnavailable(str(e)) from e
        name = str(cat.get("id") or "")
        if not _RELEASE_RE.match(name):
            raise StacUnavailable(f"{url}: unexpected release id {name!r}")
        why = [f"{t}: {w}" for t in themes if (w := release_complete(name, t))]
        if not why:
            return name
        skipped.append(f"{name} ({'; '.join(why)})")
        print(f"  Overture release {name} is incomplete ({'; '.join(why)}); trying the previous one",
              file=sys.stderr, flush=True)
        prev = _links(cat, "prev")
        url = prev[0]["href"] if prev else ""
    raise SystemExit(_pin_hint(
        "Could not resolve --release latest: no release in the STAC catalog is complete "
        f"for {', '.join(themes)} (checked {', '.join(skipped) or 'none'})."))


def _release_date(name: str) -> datetime.date:
    return datetime.date.fromisoformat(release_sort_key(name)[0])


def _resolve_via_s3(themes, today: datetime.date) -> str:
    names = list_releases()
    for name in names:
        age = (today - _release_date(name)).days
        if age <= S3_FALLBACK_MIN_AGE_DAYS:
            print(f"  skipping {name}: {age} day(s) old, may still be uploading "
                  "(and STAC cannot confirm it)", file=sys.stderr, flush=True)
            continue
        if all(has_theme(name, t) for t in themes):
            return name
    raise SystemExit(_pin_hint(
        f"Could not resolve --release latest: STAC is unavailable and no release under "
        f"{OVERTURE_HTTPS}/release/ older than {S3_FALLBACK_MIN_AGE_DAYS} days has "
        f"theme(s) {', '.join(themes)} (found: {', '.join(names) or 'none'})."))


def resolve_release(release: str, themes, *, today: datetime.date | None = None) -> str:
    """`latest` → the newest complete release with every theme in
    `themes` (see the module docstring); anything else is returned as
    given (an explicit pin), with no network access."""
    if release != "latest":
        return release
    try:
        try:
            return _resolve_via_stac(themes)
        except StacUnavailable as e:
            print(f"  STAC catalog unavailable ({e}); falling back to the S3 listing, "
                  f"releases older than {S3_FALLBACK_MIN_AGE_DAYS} days only",
                  file=sys.stderr, flush=True)
            return _resolve_via_s3(themes, today or datetime.date.today())
    except ReleaseListingError as e:
        raise SystemExit(_pin_hint(
            f"Could not resolve --release latest: listing {OVERTURE_HTTPS}/release/ failed ({e}).")) from e


def _boxes(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    """The bbox as plain boxes: two across the antimeridian (streetzim/area.py)."""
    minlon, _minlat, maxlon, _maxlat = bbox
    if minlon > maxlon or maxlon > 180:
        from streetzim import area
        return list(area.sides(bbox))
    return [bbox]


def _meets(a, b) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def stac_files(release: str, theme: str, bbox) -> tuple[list[str], int] | None:
    """(URLs of the theme's files whose STAC item bbox meets `bbox`, number
    of items), or None when STAC cannot say. A row inside the bbox lies
    inside its file's bbox, so skipping the other files loses nothing."""
    try:
        coll = stac_collection(release, theme)
        hrefs = [ln["href"] for ln in _links(coll, "item")]
        with ThreadPoolExecutor(max_workers=16) as pool:
            items = list(pool.map(stac_json, hrefs))
    except (StacUnavailable, StacMissing):
        return None
    boxes = _boxes(bbox)
    out = []
    for it in items:
        href = ((it.get("assets") or {}).get("aws") or {}).get("href")
        if not href:
            return None
        bb = it.get("bbox")
        if not bb or any(_meets(bb, b) for b in boxes):
            out.append(href)
    return out, len(items)


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def overture_sql(columns: str, source, bbox: tuple[float, float, float, float],
                 out_path: str, kv_metadata: dict[str, str] | None = None) -> str:
    """The COPY statement for the bbox.

    `source` is one path/glob, or a list of files (the HTTPS transport
    lists them, since DuckDB cannot glob plain https:// URLs), read with
    union_by_name.
    `kv_metadata` goes into the parquet footer (the release it was cut
    from).

    The bbox filter exploits Overture's per-row `bbox` struct, which DuckDB
    can push into the parquet predicate and cut >99% of IO. A bbox across
    the antimeridian (minlon > maxlon, streetzim/area.py) is one SELECT per
    side joined by UNION ALL, each with a plain box filter: an OR of the two
    boxes is not pushed down (about 10x slower)."""
    boxes = _boxes(bbox)
    # A file list may mix schemas (a column added mid-release): union by
    # name, so a column missing from some files reads as NULL there.
    src = (f"'{source}', hive_partitioning=1" if isinstance(source, str)
           else "[" + ", ".join(_sql_str(s) for s in source) + "], "
                "hive_partitioning=1, union_by_name=true")
    selects = "\n      UNION ALL\n".join(f"""      SELECT {columns}
      FROM read_parquet({src})
      WHERE bbox.xmin >= {w} AND bbox.xmax <= {e}
        AND bbox.ymin >= {s} AND bbox.ymax <= {n}""" for w, s, e, n in boxes)
    kv = ""
    if kv_metadata:
        kv = ", KV_METADATA {" + ", ".join(
            f"{k}: {_sql_str(v)}" for k, v in kv_metadata.items()) + "}"
    return f"""
    COPY (
{selects}
    ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD{kv});
    """


def theme_columns(spec: dict, available) -> str:
    """The projection for one release: the fixed columns plus whichever
    optional columns (places categories) this release has."""
    extra = [c for c in spec.get("optional_columns", ()) if c in available]
    if not extra:
        return spec["columns"]
    head, _, wkt = spec["columns"].rpartition(", ")
    return f"{head}, {', '.join(extra)}, {wkt}"


def download_overture(theme: str, bbox: str, release: str, out_path: str,
                      transport: str = "https") -> str:
    """Fetch the given Overture theme for the bbox into a local parquet.

    `release` may be `latest`; the resolved name is printed and stored in
    the parquet's key-value metadata. Returns the output path. Skips the
    download if `out_path` already exists and is non-empty — callers
    guarantee uniqueness via release + bbox hashing in the filename, so
    "exists" implies "up to date".
    """
    if theme not in SUPPORTED_THEMES:
        raise ValueError(f"Unsupported theme: {theme!r}. Try one of {sorted(SUPPORTED_THEMES)}")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        rec = parquet_release(out_path)
        print(f"  Cached: {out_path} ({os.path.getsize(out_path) / 1024 / 1024:.1f} MB"
              f"{', release ' + rec if rec else ''})")
        return out_path

    try:
        import duckdb  # local import — only required when Overture is used
    except ImportError:
        sys.exit("duckdb not installed. Run `pip install duckdb` inside venv312.")

    resolved = resolve_release(release, [theme])
    if resolved != release:
        print(f"  Overture release: {release} -> {resolved}", flush=True)

    minlon, minlat, maxlon, maxlat = [float(x) for x in bbox.split(",")]

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    con = duckdb.connect()
    # httpfs + spatial are bundled extensions; first INSTALL auto-
    # downloads into ~/.duckdb/extensions, subsequent runs are a no-op.
    con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
    con.execute(DUCKDB_HTTP_SETTINGS)

    spec = THEME_SPECS[theme]
    box = (minlon, minlat, maxlon, maxlat)
    if transport == "s3":
        con.execute("SET s3_region='us-west-2'; SET s3_url_style='vhost';")
        source = f"{OVERTURE_S3_BUCKET}/release/{resolved}/{spec['s3_glob']}"
        probe = f"'{source}'"
    else:
        try:
            keys = theme_files(resolved, theme)
        except ReleaseListingError as e:
            raise SystemExit(f"Could not list Overture {theme} files for release "
                             f"{resolved}: {e}") from e
        if not keys:
            raise SystemExit(_pin_hint(f"Overture release {resolved!r} has no {theme} "
                                       f"files under {OVERTURE_HTTPS}/{_theme_prefix(resolved, theme)}."))
        source = [f"{OVERTURE_HTTPS}/{k}" for k in keys]
        # Read only the files whose STAC bbox meets ours, when STAC lists
        # exactly the files S3 has; otherwise all of them.
        picked = stac_files(resolved, theme, box)
        if picked and picked[1] == len(source) and set(picked[0]) <= set(source):
            print(f"  STAC: {len(picked[0])} of {len(source)} {theme} files meet the bbox",
                  flush=True)
            # Keep one file when none meets it, for the schema of an empty result.
            source = picked[0] or source[:1]
        else:
            print(f"  STAC file list unavailable or different from S3: reading all "
                  f"{len(source)} {theme} files", flush=True)
        probe = "[" + ", ".join(_sql_str(u) for u in source) + "], union_by_name=true"
    columns = spec["columns"]
    if spec.get("optional_columns"):
        available = {r[0] for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet({probe})").fetchall()}
        columns = theme_columns(spec, available)
    sql = overture_sql(columns, source, box, out_path,
                       kv_metadata={KV_RELEASE: resolved})
    print(f"  Downloading Overture {theme} for bbox={bbox} (release {resolved}, {transport})...")
    con.execute(sql)
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_path}')").fetchone()[0]
    print(f"    → {out_path} ({size_mb:.1f} MB, {rows} rows, release {resolved})")
    return out_path


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("theme", nargs="+", choices=sorted(SUPPORTED_THEMES),
                   help="theme to download (several only with --print-release)")
    p.add_argument("--bbox", help="minlon,minlat,maxlon,maxlat")
    p.add_argument("--release", default=DEFAULT_RELEASE,
                   help="Overture release, e.g. 2026-09-23.1, or 'latest' (default): "
                        "the newest release that has the theme(s)")
    p.add_argument("--out", help="Output parquet path")
    p.add_argument("--transport", choices=("https", "s3"), default="https",
                   help="https (default): list the files anonymously and read them "
                        "over HTTPS; s3: DuckDB's s3:// glob")
    p.add_argument("--print-release", action="store_true",
                   help="print the release --release resolves to (for every theme "
                        "given) and exit without downloading")
    args = p.parse_args()
    if args.print_release:
        print(resolve_release(args.release, args.theme))
        return
    if len(args.theme) != 1 or not args.bbox or not args.out:
        p.error("a download takes exactly one theme, --bbox and --out")
    download_overture(args.theme[0], args.bbox, args.release, args.out, args.transport)


if __name__ == "__main__":
    main()
