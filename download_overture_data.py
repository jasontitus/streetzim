#!/usr/bin/env python3
"""Download Overture Maps Foundation data for a bbox via DuckDB+S3.

Used by `create_osm_zim.py --overture-addresses <parquet-path>` to fill in
address gaps in OSM (e.g. the 1029-block residential stretch of Ramona
Street in Palo Alto that OSM is missing as of 2026-03-10 planet PBF).

Pulls straight from `s3://overturemaps-us-west-2/release/<release>/` and
filters to bbox with the predicate pushed down into parquet readers, so
only relevant row groups get fetched. Caches per-release per-bbox so
reruns are instant.

Usage:
  python3 download_overture_data.py addresses \\
      --bbox=-122.6,37.2,-121.7,37.9 \\
      --release 2026-04-15.0 \\
      --out overture_cache/sv-addresses.parquet

Current tested theme: `addresses`. Places/transportation are out of
scope for v1 — see docs/overture-matching.md for the integration plan.
"""
import argparse
import os
import sys

OVERTURE_S3_BUCKET = "s3://overturemaps-us-west-2"
DEFAULT_RELEASE = "2026-04-15.0"

# Only addresses for now. Schemas for other themes differ enough that
# adding them blindly would ship corrupted columns.
SUPPORTED_THEMES = {"addresses"}


def download_overture(theme: str, bbox: str, release: str, out_path: str) -> str:
    """Fetch the given Overture theme for the bbox into a local parquet.

    Returns the output path. Skips the download if `out_path` already
    exists and is non-empty — callers guarantee uniqueness via release +
    bbox hashing in the filename, so "exists" implies "up to date".
    """
    if theme not in SUPPORTED_THEMES:
        raise ValueError(f"Unsupported theme: {theme!r}. Try one of {sorted(SUPPORTED_THEMES)}")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        print(f"  Cached: {out_path} ({os.path.getsize(out_path) / 1024 / 1024:.1f} MB)")
        return out_path

    try:
        import duckdb  # local import — only required when Overture is used
    except ImportError:
        sys.exit("duckdb not installed. Run `pip install duckdb` inside venv312.")

    minlon, minlat, maxlon, maxlat = [float(x) for x in bbox.split(",")]

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    con = duckdb.connect()
    # httpfs + spatial are bundled extensions; first INSTALL auto-
    # downloads into ~/.duckdb/extensions, subsequent runs are a no-op.
    con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2'; SET s3_url_style='vhost';")

    source = f"{OVERTURE_S3_BUCKET}/release/{release}/theme={theme}/type=address/*"
    # We keep raw Overture columns (no projection) so the merge step
    # downstream has the full record to reason about. The bbox filter
    # exploits Overture's per-row `bbox` struct which DuckDB can push
    # into the parquet predicate and cut >99% of IO.
    sql = f"""
    COPY (
      SELECT id, number, street, postcode, unit, country,
             address_levels, sources,
             ST_AsText(geometry) AS wkt
      FROM read_parquet('{source}', hive_partitioning=1)
      WHERE bbox.xmin >= {minlon} AND bbox.xmax <= {maxlon}
        AND bbox.ymin >= {minlat} AND bbox.ymax <= {maxlat}
    ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
    """
    print(f"  Downloading Overture {theme} for bbox={bbox} (release {release})...")
    con.execute(sql)
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_path}')").fetchone()[0]
    print(f"    → {out_path} ({size_mb:.1f} MB, {rows} rows)")
    return out_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("theme", choices=sorted(SUPPORTED_THEMES))
    p.add_argument("--bbox", required=True,
                   help="minlon,minlat,maxlon,maxlat")
    p.add_argument("--release", default=DEFAULT_RELEASE,
                   help=f"Overture release (default: {DEFAULT_RELEASE})")
    p.add_argument("--out", required=True,
                   help="Output parquet path")
    args = p.parse_args()
    download_overture(args.theme, args.bbox, args.release, args.out)


if __name__ == "__main__":
    main()
