"""PBF address extraction, Overture address/place merges and Wikipedia /
Wikidata tag extraction (moved verbatim from create_osm_zim.py, which
re-exports these names)."""
import json
import os
import shutil
import subprocess
import tempfile

from streetzim import area
# The builder's flushing, phase-timing print (see streetzim/common.py).
from streetzim.common import (
    print,
)


def extract_addresses_pbf(pbf_path, output_path, bbox=None):
    """Extract addr:housenumber + addr:street features from OSM PBF.

    Appends address entries to the given JSONL output path in the same
    schema used by the rest of the search index (name/type/lat/lon).
    These feed the routing UI's typeahead so users can search by address.

    Returns count of address entries written.
    """
    print("  Extracting address features from OSM data...")
    if not shutil.which("osmium"):
        print("    Skipping: osmium CLI not found on PATH")
        return 0
    source_pbf = str(pbf_path)
    tmp = tempfile.mkdtemp(prefix="streetzim_addr_")
    try:
        if bbox:
            bbox_pbf = os.path.join(tmp, "region.osm.pbf")
            subprocess.run([
                "osmium", "extract",
                *area.osmium_extract_args(bbox, tmp),
                source_pbf, "-o", bbox_pbf, "--overwrite",
            ], check=True)
            source_pbf = bbox_pbf

        # osmium tags-filter keeps any element with addr:housenumber.
        # Covers address nodes and building ways/relations tagged directly.
        addr_pbf = os.path.join(tmp, "addresses.osm.pbf")
        subprocess.run([
            "osmium", "tags-filter", source_pbf,
            "addr:housenumber",
            "-o", addr_pbf, "--overwrite",
        ], check=True)

        addr_geojson = os.path.join(tmp, "addresses.geojsonseq")
        # Points and areas only. osmium export writes a closed way twice —
        # once as a LineString (the way) and once as a MultiPolygon (the
        # area) — so taking linestrings too would double-count every
        # addressed building. Areas cover closed building ways and
        # multipolygon relations alike.
        subprocess.run([
            "osmium", "export", addr_pbf,
            "-f", "geojsonseq",
            "--geometry-types=point,polygon",
            "-o", addr_geojson, "--overwrite",
        ], check=True)

        count = 0
        n_point = n_area = n_no_street = n_dup = 0
        # Duplicates: the same address is often tagged on a point AND on the
        # building around it, or on two points a few metres apart. Two checks:
        # - a ~11 m grid (1e-4 deg) searched including its 8 neighbours, so a
        #   pair straddling a cell boundary still matches — rounding alone let
        #   784 same-name Hawaii point pairs 5-15 m apart through;
        # - an area is skipped when a point with the same address already lies
        #   inside its bounding box. osmium export writes points before areas,
        #   so the point (usually the entrance or the business) is the one kept.
        #
        # Both indexes hold 64-bit hashes, not (display, cell) tuples: a tuple
        # keeps its display string alive, measured at ~290 B per address on
        # Iran — ~38 GB at a Europe-scale ~130 M OSM addresses. A set of ints
        # is ~60 B per entry. Collisions at 64 bits are ~1e-3 expected at 150 M
        # entries, and a collision only drops one duplicate-looking row.
        seen_cells = set()   # hash((display, cy, cx)) for every kept address
        point_cells = set()  # the same, for kept POINTS only
        point_names = set()  # hash(display) for kept POINTS; most buildings share
                             # no address with any point, so this skips their scan
        # Cell indices are offset to stay positive: CPython hashes -1 and -2
        # identically, so cells -1/-2 (a ~22 m strip south of the equator or
        # west of Greenwich) aliased and stretched dedup by one cell there.
        _CELL0 = 1 << 21
        with open(addr_geojson, encoding="utf-8") as fin, \
             open(output_path, "a", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    feat = json.loads(line)
                except Exception:
                    continue
                props = feat.get("properties") or {}
                num = (props.get("addr:housenumber") or "").strip()
                street = (props.get("addr:street") or "").strip()
                city = (props.get("addr:city") or "").strip()
                if not num or not street:
                    n_no_street += 1
                    continue  # skip orphan addresses that can't be typed

                geom = feat.get("geometry") or {}
                gtype = geom.get("type")
                coords = geom.get("coordinates")
                if gtype == "Point" and coords:
                    lon, lat = coords[0], coords[1]
                    is_area = False
                elif gtype in ("Polygon", "MultiPolygon") and coords:
                    # osmium export emits every area — including a plain closed
                    # building way — as a MultiPolygon. Only "Polygon" used to
                    # be accepted, so every address tagged on a building was
                    # dropped: Hawaii kept 3,937 of ~18,900, Iran 24,231 of
                    # ~207,900, Turkey 134,930 of ~472,900. Use the first
                    # polygon's outer ring, without its repeated closing vertex.
                    ring = coords[0] if gtype == "Polygon" else (coords[0][0] if coords[0] else [])
                    if len(ring) > 1 and ring[0] == ring[-1]:
                        ring = ring[:-1]
                    if not ring:
                        continue
                    lon = sum(c[0] for c in ring) / len(ring)
                    lat = sum(c[1] for c in ring) / len(ring)
                    is_area = True
                    area_bbox = (min(c[1] for c in ring), max(c[1] for c in ring),
                                 min(c[0] for c in ring), max(c[0] for c in ring))
                else:
                    continue

                display = f"{num} {street}"
                if city:
                    display = f"{display}, {city}"
                cy, cx = int(round(lat * 1e4)) + _CELL0, int(round(lon * 1e4)) + _CELL0
                if any(hash((display, cy + dy, cx + dx)) in seen_cells
                       for dy in (-1, 0, 1) for dx in (-1, 0, 1)):
                    n_dup += 1
                    continue
                if is_area:
                    # Skip the area when a same-address point lies within its
                    # bounding box padded ~20 m (an entrance just outside the
                    # footprint). Scan the grid cells the box covers; a huge area
                    # (campus, park) would mean thousands of lookups, so above
                    # ~50x50 cells fall back to the centroid neighbourhood above.
                    pad = 2e-4
                    s_, n_, w_, e_ = area_bbox
                    y0, y1 = int(round((s_ - pad) * 1e4)) + _CELL0, int(round((n_ + pad) * 1e4)) + _CELL0
                    x0, x1 = int(round((w_ - pad) * 1e4)) + _CELL0, int(round((e_ + pad) * 1e4)) + _CELL0
                    if hash(display) in point_names and (y1 - y0 + 1) * (x1 - x0 + 1) <= 2500 and any(
                            hash((display, yy, xx)) in point_cells
                            for yy in range(y0, y1 + 1) for xx in range(x0, x1 + 1)):
                        n_dup += 1
                        continue
                    n_area += 1
                cell_key = hash((display, cy, cx))   # one int, shared by both sets
                if not is_area:
                    point_cells.add(cell_key)
                    point_names.add(hash(display))
                    n_point += 1
                seen_cells.add(cell_key)
                entry = {
                    "name": display,
                    "type": "addr",
                    "subtype": "",
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                }
                fout.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))
                fout.write("\n")
                count += 1
                if count % 100000 == 0:
                    print(f"\r    Wrote {count} addresses...", end="", flush=True)
        print(f"\r    Wrote {count} address entries "
              f"({n_point} on points, {n_area} on buildings/areas; "
              f"skipped {n_no_street} without addr:street, {n_dup} duplicates)",
              flush=True)
        return count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# US street-suffix normalization map. The pass-2 matcher needs "1029 Ramona
# St" and "1029 Ramona Street" to hash to the same key. This dict is
# intentionally small and US-focused — full libpostal coverage would be
# overkill for v1 and pulls in a 2 GB libpostal dataset. For non-US
# regions the worst outcome is an extra Overture row slipping in
# alongside an equivalent OSM row, which degrades gracefully (dup at
# same coordinate) and can be tightened later per docs/overture-matching.md.
_STREET_ABBREV: dict[str, str] = {
    "st": "street", "str": "street",
    "ave": "avenue", "av": "avenue",
    "blvd": "boulevard", "bl": "boulevard",
    "rd": "road",
    "dr": "drive",
    "ln": "lane",
    "ct": "court",
    "pl": "place",
    "hwy": "highway",
    "pkwy": "parkway",
    "cir": "circle",
    "ter": "terrace",
    "ctr": "center",
    "sq": "square",
    "mt": "mount", "ft": "fort",
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
}

def _normalize_street(name):
    """Lowercase, strip punctuation, expand common US suffix abbreviations.

    Idempotent — running it twice is the same as running it once.
    """
    if not name:
        return ""
    import re as _re
    # Replace punctuation with spaces; strip accents via NFKD+combining.
    import unicodedata as _ud
    folded = "".join(
        c for c in _ud.normalize("NFKD", name.lower()) if not _ud.combining(c)
    )
    tokens: list[str] = _re.findall(r"[a-z0-9]+", folded)
    return " ".join(_STREET_ABBREV.get(t, t) for t in tokens)


def _sql_string_literal(value):
    """Return a single-quoted SQL string body for DuckDB path literals."""
    return str(value).replace("'", "''")


def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    r = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    a = min(1.0, max(0.0, a))
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _sample_overture_themes_in_cache(search_features, *,
                                     sample_per_slice: int = 5000):
    """Sample a search-cache jsonl for ``"source":"overture"`` markers
    and infer themes (subset of ``["addresses","places"]``).

    Used by the ``--skip-address-extract`` salvage path: the original
    Overture merge ran on a prior build, dataset names live in the
    Overture parquet metadata (not retained in the salvage cache), but
    we can still detect *that* overture data is present in the cache,
    pick the right theme labels, and emit a stub credits JSON. Without
    this the static link in index.html → overture-sources.json stays
    broken and zimcheck rejects the ZIM.

    Reads three slices (head/middle/tail, ~``sample_per_slice`` lines
    each) since overture features tend to cluster at one end of the
    cache depending on the merge order.
    """
    if not isinstance(search_features, str) or not os.path.isfile(search_features):
        return []
    themes: set[str] = set()
    try:
        size = os.path.getsize(search_features)
        # Head, middle, tail. ~16 MB per slice is plenty.
        slice_offsets = [0,
                         max(0, size // 2 - 8 * 1024 * 1024),
                         max(0, size - 16 * 1024 * 1024)]
        with open(search_features, "rb") as fh:
            for off in slice_offsets:
                if off > 0:
                    fh.seek(off)
                    fh.readline()  # discard partial line
                read = 0
                while read < sample_per_slice:
                    line = fh.readline()
                    if not line:
                        break
                    read += 1
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("source") != "overture":
                        continue
                    if rec.get("type") == "poi":
                        themes.add("places")
                    if (rec.get("addr") or rec.get("housenumber")
                            or rec.get("street")):
                        themes.add("addresses")
                    if "places" in themes and "addresses" in themes:
                        return ["addresses", "places"]
    except Exception:
        pass
    return sorted(themes)


def _load_url_cache(path):
    """Load the url_validation_cache.json entries map produced by
    cloud/validate_overture_urls.py. Returns {} on missing/invalid so
    callers can treat absence-of-evidence the same as "URL unknown".
    """
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _url_dead_statuses():
    """Optional narrowing of what counts as a dead website.

    ``STREETZIM_URL_DEAD_STATUSES`` (comma-separated cache ``status``
    values, e.g. ``404,410,dns``) restricts drop/scrub to those statuses.
    Unset/empty keeps the historical rule: any ``alive: false`` entry is
    dead. Motivation: 403 / 429 / 5xx / timeouts in the liveness cache
    are dominated by bot-blocking CDNs, not closed businesses (the
    2026-05-10 crawl marked 87k 403s and 35k 429s dead — Starbucks, BevMo,
    AAA all fell out of the California ZIM)."""
    raw = os.environ.get("STREETZIM_URL_DEAD_STATUSES", "").strip()
    if not raw:
        return None
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _is_url_dead(url, cache):
    """True iff the cache has an explicit alive=False for `url` (and, when
    STREETZIM_URL_DEAD_STATUSES is set, its status is in that set).
    Unknown URLs (never crawled) are treated as alive — we never drop
    on absence of evidence."""
    if not url or not isinstance(url, str):
        return False
    e = cache.get(url.strip())
    if not e:
        return False
    if e.get("alive") is not False:
        return False
    dead = _url_dead_statuses()
    if dead is None:
        return True
    return str(e.get("status", "")).lower() in dead


def merge_overture_addresses(overture_parquet, search_jsonl_path, bbox=None):
    """Append Overture-sourced address records to the search-feed JSONL.

    Two-pass conflation per docs/overture-matching.md:
      Pass 1 (deterministic) — skip any Overture row whose `sources[]`
        points to an OSM element ID we already extracted. The OSM
        record carries the same information and already participates in
        the routing graph, so keeping OSM as the authority is correct.
      Pass 2 (fuzzy) — for rows with no OSM provenance, match on rounded
        coord (~1m grid) with tie-break by matching (number, normalized
        street). The 1m coord grid catches OpenAddresses points that
        got mapped onto OSM-derived positions; normalized-street match
        collapses "RAMONA ST" / "Ramona Street" / "Ramona St." variants.

    Writes new Overture records into the same JSONL in the existing
    schema with `subtype="overture"` so downstream code and mcpzim can
    spot the provenance. Returns the count of rows added.
    """
    import duckdb  # local import — only needed when the flag is set
    print(f"  Merging Overture addresses from {overture_parquet}...")

    # ------------------------------------------------------------------
    # Build the OSM-side index from the existing JSONL. We scan only
    # `type == "addr"` entries because non-address records (cities,
    # POIs, ways) have fundamentally different identity.
    # ------------------------------------------------------------------
    # All three indexes are built as flat 8-byte arrays and sorted once, then
    # probed with numpy searchsorted. They used to be a set of float tuples,
    # a set of string tuples and a dict of coordinate lists — measured at
    # ~500 B per OSM address, which at a Europe-scale ~130 M OSM addresses is
    # ~65 GB before the rest of the build. Now ~40 B per address once built,
    # with a transient peak while each array is sorted. Keys are Python
    # hashes of the same tuples the old code compared, so matching is
    # unchanged except for genuine 64-bit collisions (~1e-3 expected at 150 M
    # keys, each costing at most one skipped Overture row). Rounded
    # coordinates are offset by 1000 before hashing: CPython hashes -1.0 and
    # -2.0 identically, which would otherwise alias those rounded values.
    import numpy as _np
    from array import array as _array
    _coord_keys = _array("q")           # hash((lat_e5, lon_e5))
    # attr_key was (number, normalized_street) but that collides across
    # cities — "1029 Ramona Street" in Ramona, CA (OSM) and "1029
    # RAMONA ST" in Palo Alto (Overture) hash to the same key, so the
    # Palo Alto Overture row gets dropped as a "spatial dup" 600 km
    # from any OSM neighbour. Include city to make the key
    # city-scoped; addresses with the same number+street in different
    # cities now both land. Cities are normalised the same way streets
    # are (lowercased, accent-stripped, single-spaced) so "PALO ALTO"
    # / "Palo Alto" / "palo  alto" all collapse to one value.
    _attr_keys = _array("q")            # hash((number, norm_street, norm_city))
    # Missing Overture address_levels should still dedupe against a
    # nearby OSM address with the same number+street, but only within a
    # short distance so we do not reintroduce cross-city collisions.
    _near_keys = _array("q")            # hash((number, norm_street)), one per point
    _near_lat = _array("d")
    _near_lon = _array("d")
    osm_count = 0
    with open(search_jsonl_path, encoding="utf-8") as f:
        for line in f:
            if '"type":"addr"' not in line:
                # Fast path: ~98% of lines in the world feed aren't
                # addresses. Skipping the json.loads here saves minutes.
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "addr":
                continue
            lat = rec.get("lat"); lon = rec.get("lon")
            if lat is None or lon is None:
                continue
            # ~1 m grid — rounds to 5 decimal places in degrees.
            _coord_keys.append(hash((round(lat, 5) + 1000.0, round(lon, 5) + 1000.0)))
            name = rec.get("name") or ""
            # Existing OSM records serialize as "<num> <street>, <city>".
            # Split on the first space + comma to recover number/street/city.
            num = ""
            street = name
            city = ""
            if " " in name:
                num, _, rest = name.partition(" ")
                if "," in rest:
                    street, _, city = rest.partition(",")
                else:
                    street = rest
            else:
                if "," in name:
                    street, _, city = name.partition(",")
            street = street.strip()
            city = city.strip()
            if num and street:
                attr2 = (num.strip(), _normalize_street(street))
                _attr_keys.append(hash((*attr2, _normalize_street(city))))
                _near_keys.append(hash(attr2))
                _near_lat.append(lat)
                _near_lon.append(lon)
            osm_count += 1
    print(f"    Indexed {osm_count} existing OSM address records")
    # Build one index at a time and free its source buffer before the next,
    # so only one sort's temporary copy exists at once.
    osm_coord_index = _np.unique(_np.frombuffer(_coord_keys, dtype=_np.int64))
    del _coord_keys
    osm_attr_index = _np.unique(_np.frombuffer(_attr_keys, dtype=_np.int64))
    del _attr_keys
    _order = _np.argsort(_np.frombuffer(_near_keys, dtype=_np.int64), kind="stable")
    near_keys = _np.frombuffer(_near_keys, dtype=_np.int64)[_order]
    del _near_keys
    near_lat = _np.frombuffer(_near_lat, dtype=_np.float64)[_order]
    del _near_lat
    near_lon = _np.frombuffer(_near_lon, dtype=_np.float64)[_order]
    del _near_lon, _order
    # Exact lower bound for the 100 m test: great-circle distance is never
    # less than R * |dlat| (radians), because the haversine term is at least
    # sin^2(dlat/2). Points beyond that bound cannot pass, so skipping them
    # cannot change a result; the survivors get the unchanged exact check.
    _NEAR_DLAT_DEG = (100.0 / 6371000.0) * (180.0 / 3.141592653589793) * (1 + 1e-9)


    # ------------------------------------------------------------------
    # Stream Overture rows via DuckDB Arrow batches. Materializing the
    # whole parquet OOMs a Mac for continent-scale bboxes; batch of
    # 2048 keeps working-set bounded.
    # ------------------------------------------------------------------
    con = duckdb.connect()
    # The parquet is already bbox-filtered by download_overture_data.py;
    # a second WHERE here would need a `bbox` struct the downloader doesn't
    # project. Instead, filter row-side in Python if the caller passes a
    # bbox — handles the edge case of reusing a larger-region parquet.
    parquet_sql = _sql_string_literal(overture_parquet)
    sql = f"""
      SELECT number, street, postcode,
             address_levels, sources,
             ST_X(ST_GeomFromText(wkt)) AS lon,
             ST_Y(ST_GeomFromText(wkt)) AS lat
      FROM read_parquet('{parquet_sql}')
    """
    con.execute("INSTALL spatial; LOAD spatial;")
    reader = con.execute(sql).fetch_record_batch(2048)

    if bbox is not None:
        bbox_minlon, bbox_minlat, bbox_maxlon, bbox_maxlat = bbox
    else:
        bbox_minlon = bbox_minlat = -1e9
        bbox_maxlon = bbox_maxlat = 1e9

    pass1_skipped = 0      # dropped via OSM-source link
    pass2_skipped = 0      # dropped via coord/attr match
    added = 0              # net new records appended
    orphan_skipped = 0     # missing number or street
    # Distinct upstream datasets observed across rows that survived to
    # output. Drives overture-sources.json + the viewer's attribution
    # panel — OpenAddresses / LINZ NZ / Asiaq / NYC Open Data / etc.
    source_datasets = set()
    with open(search_jsonl_path, "a", encoding="utf-8") as fout:
        for batch in reader:
            # Classify each row, then run all three index lookups for the
            # batch in one vectorised call. A per-row searchsorted made the
            # merge ~34% slower on Washington DC; at a Europe-scale ~140 M
            # Overture rows that would be hours. Rows are still decided and
            # written in their original order, so output is unchanged.
            cand = []          # (row, num, street_raw, lat, lon, city, attr_key)
            for row in batch.to_pylist():
                num = (row.get("number") or "").strip()
                street_raw = (row.get("street") or "").strip()
                if not num or not street_raw:
                    orphan_skipped += 1
                    continue
                lat = row.get("lat"); lon = row.get("lon")
                if lat is None or lon is None:
                    orphan_skipped += 1
                    continue
                if not (bbox_minlat <= lat <= bbox_maxlat and
                        area.contains_lon((bbox_minlon, 0, bbox_maxlon, 0), lon)):
                    continue

                # Pass 1: Overture-to-OSM provenance link. Today we
                # don't keep a set of imported OSM address node IDs —
                # extract_addresses_pbf doesn't expose them — so this
                # branch is a no-op for the address theme in v1. Left
                # in place so when we wire in the OSM-ID capture it
                # picks up automatically (docs/overture-matching.md §1).
                has_osm_source = False
                for src in (row.get("sources") or []):
                    if (src or {}).get("dataset") == "OpenStreetMap":
                        has_osm_source = True
                        break
                if has_osm_source:
                    pass1_skipped += 1
                    continue

                # Reconstruct a city label from address_levels when we
                # have it. For US addresses Overture writes
                # [{'value':'CA'},{'value':'PALO ALTO'}] — state first,
                # then city. For non-US layouts the last non-empty
                # value is usually the city, which is our best-effort
                # fallback. Computed before the dedup test because
                # attr_key now includes a normalised city.
                city = ""
                levels = row.get("address_levels") or []
                if len(levels) >= 2 and levels[-1]:
                    city = (levels[-1].get("value") or "").title()

                # Pass 2 keys: attr_key scopes by normalised city so two
                # addresses with the same number+street in different
                # cities both land — fixes the cross-city collision that
                # dropped "1029 Ramona St, Palo Alto" because OSM had
                # "1029 Ramona Street" in Ramona (city), 600 km away.
                attr_key = (
                    num,
                    _normalize_street(street_raw),
                    _normalize_street(city),
                )
                cand.append((row, num, street_raw, lat, lon, city, attr_key))
            if not cand:
                continue

            k_coord = _np.fromiter((hash((round(c[3], 5) + 1000.0, round(c[4], 5) + 1000.0)) for c in cand),
                                   dtype=_np.int64, count=len(cand))
            k_attr = _np.fromiter((hash(c[6]) for c in cand), dtype=_np.int64, count=len(cand))
            k_near = _np.fromiter((hash(c[6][:2]) for c in cand), dtype=_np.int64, count=len(cand))

            def _member(arr, keys):
                if arr.size == 0:
                    return _np.zeros(keys.size, dtype=bool)
                i = _np.searchsorted(arr, keys)
                i_clip = _np.minimum(i, arr.size - 1)
                return (i < arr.size) & (arr[i_clip] == keys)

            dup = _member(osm_coord_index, k_coord) | _member(osm_attr_index, k_attr)
            near_lo = _np.searchsorted(near_keys, k_near, side="left")
            near_hi = _np.searchsorted(near_keys, k_near, side="right")

            for j, (row, num, street_raw, lat, lon, city, _attr_key) in enumerate(cand):
                nearby_attr_dup = False
                if not dup[j]:
                    lo_j, hi_j = int(near_lo[j]), int(near_hi[j])
                    if hi_j > lo_j:
                        # Hot keys ("1 Hauptstraße" across a continent) can hold
                        # thousands of points; filter by latitude first with the
                        # exact lower bound, then run the unchanged haversine on
                        # plain Python floats for the few that remain.
                        seg_lat = near_lat[lo_j:hi_j]
                        close = _np.nonzero(_np.abs(seg_lat - lat) <= _NEAR_DLAT_DEG)[0]
                        if close.size:
                            c_lat = seg_lat[close].tolist()
                            c_lon = near_lon[lo_j:hi_j][close].tolist()
                            nearby_attr_dup = any(
                                _haversine_m(lat, lon, o_lat, o_lon) <= 100.0
                                for o_lat, o_lon in zip(c_lat, c_lon))
                if dup[j] or nearby_attr_dup:
                    pass2_skipped += 1
                    continue
                # Street is frequently uppercased in US OpenAddresses
                # feeds — title-case it so it renders nicely in search.
                street_display = street_raw.title() if street_raw.isupper() else street_raw
                display = f"{num} {street_display}"
                if city:
                    display = f"{display}, {city}"
                entry = {
                    "name": display,
                    "type": "addr",
                    "subtype": "overture",   # provenance marker
                    "lat": round(float(lat), 6),
                    "lon": round(float(lon), 6),
                }
                fout.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))
                fout.write("\n")
                added += 1
                for src in (row.get("sources") or []):
                    ds = (src or {}).get("dataset")
                    if ds:
                        source_datasets.add(ds)

    total_overture = pass1_skipped + pass2_skipped + added + orphan_skipped
    print(f"    Overture: {total_overture} rows scanned, "
          f"{pass1_skipped} skipped (OSM source link), "
          f"{pass2_skipped} skipped (spatial dup), "
          f"{orphan_skipped} orphan (missing num/street), "
          f"{added} added, "
          f"{len(source_datasets)} distinct upstream datasets")
    return {"added": added, "datasets": sorted(source_datasets)}


# Subtypes too generic to keep when Overture has a category for the POI.
_OVERTURE_REFINABLE_SUBTYPES = frozenset({
    "", "tourism", "amenity", "shop", "attraction", "leisure", "car",
    "historic", "landuse",
})


def _overture_may_refine(rec):
    """True if Overture's category may replace ``rec``'s subtype: the
    subtype is a generic bucket, or it is tilemaker's subclass for a POI
    whose class was one (``osm_key``, see search_record in
    streetzim/search_extract.py). The second case keeps tilemaker +
    Overture builds as they were when such a record's subtype was the raw
    key itself: an amenity=restaurant still becomes italian_restaurant.
    OpenFreeMap records never carry ``osm_key``."""
    return ((rec.get("subtype") or "") in _OVERTURE_REFINABLE_SUBTYPES
            or rec.get("osm_key") in _OVERTURE_REFINABLE_SUBTYPES)


def merge_overture_places(overture_parquet, search_jsonl_path, bbox=None,
                          url_cache=None, url_cache_policy="drop-record"):
    """Enrich OSM POIs with Overture places' websites / phones / socials /
    categories / brand — and emit new POI records for places OSM doesn't
    know about.

    Two passes, mirroring merge_overture_addresses:

      Pass 1 (enrich): for each Overture row, look up an OSM POI in the
        search feed by rounded coord + normalized name. If found, add
        the Overture fields to that record in place. This is the main
        win — OSM's `subtype` is noisy (museums bucketed under `tourism`,
        hotels under `amenity`); Overture's `categories.primary` gives
        a clean label we can drive chips + popups off.
      Pass 2 (add-new): Overture rows with no OSM match become fresh
        `type: "poi"` records tagged `subtype` = Overture primary
        category and `source: "overture"`.

    Per-record extensions (kept terse to bound chunk sizes):
      cat        — Overture primary category ("museum", "hotel", …)
      w          — first website URL
      p          — first phone
      soc        — first 3 social URLs (array)
      brand      — brand primary name (string, often empty)
      wd         — brand wikidata Q-ID when present
      source     — "overture" if the record was freshly added by this pass

    Returns {"enriched": N, "added": M, "datasets": [...], "size_bytes": {...}}
    so the caller can log the size impact without re-stat'ing the jsonl.
    """
    import duckdb
    print(f"  Merging Overture places from {overture_parquet}...")

    size_before = os.path.getsize(search_jsonl_path)

    # Streaming refactor (was: load ALL records into memory). For
    # europe-scale the JSONL post-addresses-merge has ~120M lines;
    # the in-memory records[] peaked at >24 GB and OOM-killed the
    # process during continent rebuilds. New flow:
    #
    #   Pass A — scan JSONL once to collect POI keys (only POI type
    #     records contribute keys; bounded by POI count, ~10M for
    #     europe → ~1 GB, vs 24 GB for ALL records).
    #   Pass B — stream Overture parquet; for each row decide
    #     enrich-existing or new-POI, store decisions in two small
    #     in-memory tables keyed by (round(lat,4), round(lon,4),
    #     normalized_name).
    #   Pass C — re-stream JSONL → tmp file applying enrichments;
    #     append new-POI additions at end.
    #
    # No intermediate full-feature materialization. Memory bounded by
    # POI count + Overture row count, not total feature count.
    poi_keys = set()  # POI keys seen in source JSONL
    with open(search_jsonl_path, encoding="utf-8") as f:
        for line in f:
            # Fast pre-filter: skip lines that aren't POIs without
            # parsing JSON. Saves minutes on continent-scale where
            # most of the feed is addresses + streets, not POIs.
            if '"type":"poi"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "poi":
                continue
            lat = rec.get("lat"); lon = rec.get("lon")
            nm = rec.get("name") or ""
            if lat is None or lon is None or not nm:
                continue
            poi_keys.add((round(lat, 4), round(lon, 4),
                          _normalize_street(nm)))
    print(f"    Indexed {len(poi_keys)} OSM POI keys (streaming, "
          f"bounded memory)")

    # Stream Overture places from parquet via arrow batches.
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    parquet_sql = _sql_string_literal(overture_parquet)
    sql = f"""
      SELECT names, categories, phones, websites, socials, brand, sources,
             ST_X(ST_GeomFromText(wkt)) AS lon,
             ST_Y(ST_GeomFromText(wkt)) AS lat
      FROM read_parquet('{parquet_sql}')
    """
    reader = con.execute(sql).fetch_record_batch(2048)

    if bbox is not None:
        bbox_minlon, bbox_minlat, bbox_maxlon, bbox_maxlat = bbox
    else:
        bbox_minlon = bbox_minlat = -1e9
        bbox_maxlon = bbox_maxlat = 1e9

    enriched = 0
    added = 0
    unnamed = 0
    dead_dropped = 0       # add-new rows skipped under drop-record policy
    dead_scrubbed = 0      # add-new rows kept after stripping dead `ws`
    enrich_ws_scrubbed = 0 # enrich rows that lost a dead `ws` from extras
    source_datasets = set()
    # Pass B accumulators (bounded by POI count + Overture row count,
    # not total feature count). enrichments stays under ~1-2 GB even
    # for europe; additions is the hot growth path.
    enrichments = {}   # key → extra-dict (applied to first matching POI)
    additions_path = search_jsonl_path + ".overture_additions"
    additions_count = 0
    with open(additions_path, "w", encoding="utf-8") as add_fh:
        for batch in reader:
            for row in batch.to_pylist():
                lat = row.get("lat"); lon = row.get("lon")
                if lat is None or lon is None:
                    continue
                if not (bbox_minlat <= lat <= bbox_maxlat and
                        area.contains_lon((bbox_minlon, 0, bbox_maxlon, 0), lon)):
                    continue

                names = row.get("names") or {}
                name = (names.get("primary") or "").strip()
                if not name:
                    unnamed += 1
                    continue

                cats = row.get("categories") or {}
                primary = (cats.get("primary") or "").strip()

                phones = row.get("phones") or []
                websites = row.get("websites") or []
                socials = row.get("socials") or []
                brand = row.get("brand") or {}
                brand_names = (brand or {}).get("names") or {}
                brand_primary = (brand_names.get("primary") or "").strip() if brand_names else ""
                brand_wd = (brand or {}).get("wikidata") or None

                # Surface-area extensions. Keep empty fields out of the
                # record so downstream JSON stays tight.
                #
                # Website is `ws`, not `w`. `w` is reserved for the Wikipedia
                # tag (e.g. "en:HP_Garage") that OSM POIs carry in the same
                # record — mcpzim reads `rec["w"]` into `Place.wiki` and then
                # calls `articleByTitle` on it. Putting a URL in that slot
                # corrupts Wikipedia lookups across every downstream tool
                # (`nearby_stories`, `near_places(has_wiki=true)`, etc.). See
                # commit "Rename Overture website field to ws (fix w collision)".
                extra = {}
                if primary: extra["cat"] = primary
                if websites: extra["ws"] = websites[0]
                if phones: extra["p"] = phones[0]
                if socials: extra["soc"] = socials[:3]
                if brand_primary: extra["brand"] = brand_primary
                if brand_wd: extra["wd"] = brand_wd

                ws_dead = bool(
                    url_cache and extra.get("ws")
                    and _is_url_dead(extra["ws"], url_cache)
                )

                key = (round(lat, 4), round(lon, 4), _normalize_street(name))
                if key in poi_keys:
                    # Pass 1 enrich: queue the extra-dict by key. First
                    # Overture row wins per key (matches old "first match
                    # wins" semantics); duplicates are rare at this
                    # precision.
                    if ws_dead:
                        # Don't propagate a dead URL onto an OSM POI.
                        # The OSM record stays; just strip the bad link.
                        extra.pop("ws", None)
                        enrich_ws_scrubbed += 1
                    if key not in enrichments:
                        enrichments[key] = extra
                        enriched += 1
                else:
                    # Pass 2 add-new: skip uncategorized noise; stream
                    # additions to a sidecar file so we don't hold
                    # millions of dicts in memory.
                    if not primary:
                        continue
                    if ws_dead:
                        if url_cache_policy == "drop-record":
                            dead_dropped += 1
                            continue
                        # scrub-only: keep the record sans dead link
                        extra.pop("ws", None)
                        dead_scrubbed += 1
                    rec = {
                        "name": name,
                        "type": "poi",
                        "subtype": primary,
                        "lat": round(float(lat), 6),
                        "lon": round(float(lon), 6),
                        "source": "overture",
                        **extra,
                    }
                    add_fh.write(json.dumps(rec, separators=(",", ":"),
                                            ensure_ascii=False))
                    add_fh.write("\n")
                    additions_count += 1
                    added += 1

                for src in (row.get("sources") or []):
                    ds = (src or {}).get("dataset")
                    if ds:
                        source_datasets.add(ds)

    print(f"    Pass B done: {enriched} enrichments queued, "
          f"{added} additions staged at {additions_path}")

    # Pass C: stream-rewrite the JSONL applying enrichments inline,
    # then append the additions sidecar.
    tmp_path = search_jsonl_path + ".overture_tmp"
    applied = set()  # keys already enriched ("first match wins")
    with open(search_jsonl_path, encoding="utf-8") as fin, \
         open(tmp_path, "w", encoding="utf-8") as out:
        for line in fin:
            if '"type":"poi"' not in line:
                # Fast path: not a POI, copy verbatim.
                out.write(line)
                continue
            try:
                rec = json.loads(line.rstrip("\n"))
            except Exception:
                out.write(line)
                continue
            if rec.get("type") != "poi":
                out.write(line)
                continue
            lat = rec.get("lat"); lon = rec.get("lon")
            nm = rec.get("name") or ""
            if lat is None or lon is None or not nm:
                out.write(line)
                continue
            key = (round(lat, 4), round(lon, 4), _normalize_street(nm))
            extra = enrichments.get(key)
            if not extra or key in applied:
                out.write(line)
                continue
            applied.add(key)
            for k, v in extra.items():
                if k not in rec:
                    rec[k] = v
            if extra.get("cat") and _overture_may_refine(rec):
                rec["subtype"] = extra["cat"]
                # Refined: no longer a generic bucket, so a second merge
                # leaves it alone, as it did when the subtype was the key.
                rec.pop("osm_key", None)
            out.write(json.dumps(rec, separators=(",", ":"),
                                 ensure_ascii=False))
            out.write("\n")
        # Append the additions sidecar (already JSONL formatted).
        if additions_count:
            with open(additions_path, encoding="utf-8") as add_fh:
                shutil.copyfileobj(add_fh, out, length=8 * 1024 * 1024)
    os.replace(tmp_path, search_jsonl_path)
    try:
        os.unlink(additions_path)
    except OSError:
        pass

    size_after = os.path.getsize(search_jsonl_path)
    delta_mb = (size_after - size_before) / (1024 * 1024)
    print(f"    Overture places: {enriched} enriched, {added} added, "
          f"{unnamed} unnamed skipped, "
          f"{len(source_datasets)} upstream datasets; "
          f"jsonl {size_before/1024/1024:.1f} MB → "
          f"{size_after/1024/1024:.1f} MB (+{delta_mb:.1f} MB)")
    if url_cache:
        print(f"    URL filter: {dead_dropped} add-new dropped (dead site), "
              f"{dead_scrubbed} add-new scrubbed (kept), "
              f"{enrich_ws_scrubbed} OSM enrich extras lost dead `ws` "
              f"(policy={url_cache_policy})", flush=True)
    return {
        "enriched": enriched,
        "added": added,
        "dead_dropped": dead_dropped,
        "dead_scrubbed": dead_scrubbed,
        "enrich_ws_scrubbed": enrich_ws_scrubbed,
        "datasets": sorted(source_datasets),
        "size_bytes": {"before": size_before, "after": size_after},
    }


def extract_wiki_tags_pbf(pbf_path, bbox=None):
    """Extract {wikipedia, wikidata} tags per OSM object with a name.

    Used to enrich the search index so offline agents can cross-link
    POI records to the Wikipedia ZIM (per the mcpzim contract doc).
    Returns a dict keyed by (normalized_name, quantized_lat, quantized_lon):
        { ("lincoln memorial", 3889018, -770358): {
              "wikipedia": "en:Lincoln_Memorial",
              "wikidata":  "Q162458",
          },
          ... }
    Coord quantization is round(lat*1e4) / round(lon*1e4) ≈ 11 m grid,
    which tolerates MVT-vs-PBF rounding without colliding unrelated POIs.
    """
    print("  Extracting wiki cross-ref tags from OSM data...")
    source_pbf = str(pbf_path)
    tmp = tempfile.mkdtemp(prefix="streetzim_wiki_")
    try:
        if bbox:
            bbox_pbf = os.path.join(tmp, "region.osm.pbf")
            subprocess.run([
                "osmium", "extract",
                *area.osmium_extract_args(bbox, tmp),
                source_pbf, "-o", bbox_pbf, "--overwrite",
            ], check=True)
            source_pbf = bbox_pbf

        # osmium-tags-filter: anything with wikipedia OR wikidata tag.
        wiki_pbf = os.path.join(tmp, "wiki.osm.pbf")
        subprocess.run([
            "osmium", "tags-filter", source_pbf,
            "wikipedia", "wikidata",
            "-o", wiki_pbf, "--overwrite",
        ], check=True)

        wiki_geojson = os.path.join(tmp, "wiki.geojsonseq")
        subprocess.run([
            "osmium", "export", wiki_pbf,
            "-f", "geojsonseq",
            "-o", wiki_geojson, "--overwrite",
        ], check=True)

        lookup = {}
        count = 0
        with open(wiki_geojson, encoding="utf-8") as fin:
            for line in fin:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    feat = json.loads(line)
                except Exception:
                    continue
                props = feat.get("properties") or {}
                name = (props.get("name") or props.get("name:latin") or "").strip()
                if not name:
                    continue
                wikipedia = (props.get("wikipedia") or "").strip()
                wikidata = (props.get("wikidata") or "").strip()
                if not wikipedia and not wikidata:
                    continue

                geom = feat.get("geometry") or {}
                gtype = geom.get("type")
                coords = geom.get("coordinates")
                if gtype == "Point" and coords:
                    lon, lat = coords[0], coords[1]
                elif gtype == "Polygon" and coords and coords[0]:
                    ring = coords[0]
                    lon = sum(c[0] for c in ring) / len(ring)
                    lat = sum(c[1] for c in ring) / len(ring)
                elif gtype == "LineString" and coords:
                    mid = coords[len(coords) // 2]
                    lon, lat = mid[0], mid[1]
                else:
                    continue

                key = (name.lower(), int(round(lat * 1e4)), int(round(lon * 1e4)))
                entry = {}
                if wikipedia:
                    entry["wikipedia"] = wikipedia
                if wikidata:
                    entry["wikidata"] = wikidata
                # If we already have an entry for this coord+name, prefer the one
                # with more fields (covers the case where a node and a way share
                # the same name but only one has both tags).
                existing = lookup.get(key)
                if existing is None or len(entry) > len(existing):
                    lookup[key] = entry
                    count += 1
                if count % 50000 == 0 and count:
                    print(f"\r    Indexed {count} wiki cross-refs...", end="", flush=True)
        print(f"\r    Indexed {len(lookup)} wiki cross-refs (from {count} raw)")
        return lookup
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
