#!/usr/bin/env python3
"""
wikidata_cache.py - Extract Wikidata Q-IDs from OSM data and fetch summaries.

Builds a local JSON cache of Wikidata information (population, area, description,
Wikipedia extract, etc.) for all features in an OSM PBF or MBTiles file that
have a wikidata=Q* tag.

Usage:
    # Build cache from a PBF file
    python3 wikidata_cache.py --pbf district-of-columbia.osm.pbf

    # Build cache from an MBTiles file (extracts Q-IDs from vector tiles)
    python3 wikidata_cache.py --mbtiles tiles.mbtiles

    # Use existing cache, only fetch missing Q-IDs
    python3 wikidata_cache.py --pbf data.osm.pbf --cache wikidata_cache/

    # Just show stats for an existing cache
    python3 wikidata_cache.py --cache wikidata_cache/ --stats

The cache is stored as a directory of JSON files, one per Q-ID prefix bucket,
to allow incremental updates and efficient loading.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CACHE_DIR = SCRIPT_DIR / "wikidata_cache"

# Wikidata SPARQL endpoint
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Wikipedia REST API for extracts
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

# Properties we fetch from Wikidata
WIKIDATA_PROPERTIES = {
    "P1082": "population",
    "P2046": "area_km2",
    "P2044": "elevation_m",
    "P17": "country",
    "P36": "capital",
    "P421": "timezone",
    "P856": "website",
    "P31": "instance_of",
}

USER_AGENT = "StreetZIM/1.0 (https://github.com/user/streetzim; wikidata cache builder)"


def extract_qids_from_pbf(pbf_path):
    """Extract all wikidata Q-IDs from an OSM PBF file.

    Returns a dict mapping Q-ID -> list of {name, type, lat, lon} for each
    OSM feature that references it.
    """
    try:
        import osmium
    except ImportError:
        print("Error: osmium not installed. Install with: pip install osmium")
        print("  (or use --mbtiles mode instead)")
        sys.exit(1)

    print(f"  Scanning PBF for wikidata tags: {pbf_path}")

    qid_features = {}

    class WikidataHandler(osmium.SimpleHandler):
        def _process(self, obj, geom_type):
            wd = obj.tags.get("wikidata", "")
            if not wd or not wd.startswith("Q"):
                return

            name = obj.tags.get("name", "") or obj.tags.get("name:en", "")
            place = obj.tags.get("place", "")
            tourism = obj.tags.get("tourism", "")
            historic = obj.tags.get("historic", "")
            natural = obj.tags.get("natural", "")
            amenity = obj.tags.get("amenity", "")
            leisure = obj.tags.get("leisure", "")
            aeroway = obj.tags.get("aeroway", "")
            boundary = obj.tags.get("boundary", "")

            # Determine feature type
            if place:
                ftype = place
            elif boundary == "administrative":
                ftype = "admin"
            elif tourism:
                ftype = tourism
            elif historic:
                ftype = historic
            elif natural:
                ftype = natural
            elif amenity:
                ftype = amenity
            elif leisure:
                ftype = leisure
            elif aeroway:
                ftype = aeroway
            else:
                ftype = geom_type

            # Get location if available
            lat, lon = None, None
            if geom_type == "node":
                try:
                    lat = obj.location.lat
                    lon = obj.location.lon
                except osmium.InvalidLocationError:
                    pass

            feature = {"name": name, "type": ftype}
            if lat is not None:
                feature["lat"] = round(lat, 6)
                feature["lon"] = round(lon, 6)

            if wd not in qid_features:
                qid_features[wd] = feature
            elif not qid_features[wd].get("name") and name:
                qid_features[wd] = feature

        def node(self, n):
            self._process(n, "node")

        def way(self, w):
            self._process(w, "way")

        def relation(self, r):
            self._process(r, "relation")

    handler = WikidataHandler()
    # Only node locations are used (ways/relations don't extract lat/lon),
    # so skip the expensive in-memory node location index.
    handler.apply_file(str(pbf_path))

    print(f"    Found {len(qid_features)} unique Q-IDs")
    return qid_features


def extract_qids_from_mbtiles(mbtiles_path):
    """Extract wikidata Q-IDs by scanning vector tiles for features with known names,
    then matching against Wikidata by name + coordinates.

    Note: Standard OpenMapTiles vector tiles don't include wikidata tags directly.
    This is a fallback — PBF extraction is preferred.

    Returns a dict mapping Q-ID -> {name, type, lat, lon}.
    """
    print(f"  Note: MBTiles mode extracts feature names but not Q-IDs directly.")
    print(f"  For best results, use --pbf mode which reads wikidata tags from OSM.")
    print(f"  Scanning tiles for named features to look up in Wikidata...")

    import gzip
    import mapbox_vector_tile

    conn = sqlite3.connect(str(mbtiles_path))

    # Get z14 tiles (highest detail)
    rows = conn.execute(
        "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = 14"
    ).fetchall()
    conn.close()

    if not rows:
        print("    No z14 tiles found")
        return {}

    # Layers with important named features
    search_layers = {
        "place": "place",
        "poi": "poi",
        "park": "park",
        "mountain_peak": "peak",
        "aerodrome_label": "airport",
        "water_name": "water",
    }

    # Extract named features with coordinates
    features_by_name = {}
    for col, row, data in rows:
        tile_data = data
        if data[:2] == b"\x1f\x8b":
            try:
                tile_data = gzip.decompress(data)
            except Exception:
                continue
        try:
            decoded = mapbox_vector_tile.decode(tile_data, y_coord_down=True)
        except Exception:
            continue

        # TMS -> XYZ row conversion
        y = (1 << 14) - 1 - row
        for layer_name, feature_type in search_layers.items():
            layer = decoded.get(layer_name)
            if not layer:
                continue
            extent = layer.get("extent", 4096)
            for feature in layer.get("features", []):
                props = feature.get("properties", {})
                name = props.get("name:latin") or props.get("name", "")
                if not name or len(name) < 2:
                    continue
                place_class = props.get("class", "")
                # Only look up significant features
                if feature_type == "place" and place_class not in (
                    "continent", "country", "state", "province", "city", "town"
                ):
                    continue

                geom = feature.get("geometry", {})
                coords = geom.get("coordinates")
                if not coords:
                    continue
                geom_type = geom.get("type", "")
                try:
                    if geom_type == "Point":
                        px, py = coords[0], coords[1]
                    elif geom_type in ("Polygon", "MultiPolygon"):
                        ring = coords[0] if geom_type == "Polygon" else coords[0][0]
                        px = sum(c[0] for c in ring) / len(ring)
                        py = sum(c[1] for c in ring) / len(ring)
                    else:
                        continue
                except (IndexError, ZeroDivisionError):
                    continue

                import math
                n = 1 << 14
                lon = (col + px / extent) / n * 360.0 - 180.0
                lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n)))
                lat = math.degrees(lat_rad)

                key = (name, feature_type, place_class)
                if key not in features_by_name:
                    features_by_name[key] = {
                        "name": name,
                        "type": feature_type,
                        "subtype": place_class,
                        "lat": round(lat, 6),
                        "lon": round(lon, 6),
                    }

    print(f"    Found {len(features_by_name)} named features to look up")

    # Batch query Wikidata by name + coordinates
    qid_features = _lookup_qids_by_name(list(features_by_name.values()))
    return qid_features


def _lookup_qids_by_name(features, batch_size=50):
    """Look up Wikidata Q-IDs for features by name + coordinates using SPARQL.

    Returns dict mapping Q-ID -> feature dict.
    """
    qid_features = {}
    total = len(features)

    for i in range(0, total, batch_size):
        batch = features[i:i + batch_size]
        # Build SPARQL VALUES block
        values_parts = []
        for f in batch:
            name_escaped = f["name"].replace('"', '\\"')
            values_parts.append(f'("{name_escaped}"@en {f["lat"]} {f["lon"]})')

        if not values_parts:
            continue

        sparql = f"""
        SELECT ?item ?itemLabel ?name ?lat ?lon WHERE {{
          VALUES (?name ?lat ?lon) {{ {" ".join(values_parts)} }}
          ?item rdfs:label ?name .
          ?item wdt:P625 ?coord .
          BIND(geof:latitude(?coord) AS ?clat)
          BIND(geof:longitude(?coord) AS ?clon)
          FILTER(ABS(?clat - ?lat) < 0.1 && ABS(?clon - ?lon) < 0.1)
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}
        LIMIT {batch_size * 2}
        """

        try:
            results = _run_sparql(sparql)
            for r in results:
                qid = r["item"]["value"].rsplit("/", 1)[-1]
                name = r.get("name", {}).get("value", "")
                # Find matching feature
                for f in batch:
                    if f["name"] == name:
                        qid_features[qid] = f
                        break
        except Exception as e:
            print(f"    Warning: SPARQL lookup failed for batch {i}: {e}")

        if (i + batch_size) % 200 == 0:
            print(f"    Looked up {min(i + batch_size, total)}/{total} features...")
        time.sleep(0.5)  # Rate limit

    print(f"    Resolved {len(qid_features)} Q-IDs from name lookups")
    return qid_features


def _run_sparql(query, retries=3):
    """Execute a SPARQL query against the Wikidata endpoint."""
    url = WIKIDATA_SPARQL + "?" + urllib.parse.urlencode({
        "query": query,
        "format": "json",
    })
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/sparql-results+json",
    }

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("results", {}).get("bindings", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Rate limited
                wait = 2 ** (attempt + 2)
                print(f"    Rate limited, waiting {wait}s...")
                time.sleep(wait)
            elif e.code == 500 and attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
    return []


def fetch_wikidata_batch(qids, batch_size=40, cache_dir=None, save_interval=10000):
    """Fetch Wikidata properties for a list of Q-IDs using SPARQL.

    Returns a dict mapping Q-ID -> {label, description, population, area_km2, ...}.
    If cache_dir is set, saves incrementally every save_interval Q-IDs so
    progress is not lost if the process is killed.
    """
    results = {}
    total = len(qids)
    qid_list = list(qids)
    last_save = 0

    print(f"  Fetching Wikidata properties for {total} Q-IDs...")
    start_time = time.time()

    for i in range(0, total, batch_size):
        batch = qid_list[i:i + batch_size]
        values = " ".join(f"wd:{qid}" for qid in batch)

        sparql = f"""
        SELECT ?item ?itemLabel ?itemDescription
               ?pop ?area ?elev ?countryLabel ?capitalLabel
               ?timezoneLabel ?website ?instanceLabel
               ?sitelink
        WHERE {{
          VALUES ?item {{ {values} }}

          OPTIONAL {{ ?item wdt:P1082 ?pop . }}
          OPTIONAL {{ ?item wdt:P2046 ?area . }}
          OPTIONAL {{ ?item wdt:P2044 ?elev . }}
          OPTIONAL {{ ?item wdt:P17 ?country . }}
          OPTIONAL {{ ?item wdt:P36 ?capital . }}
          OPTIONAL {{ ?item wdt:P421 ?timezone . }}
          OPTIONAL {{ ?item wdt:P856 ?website . }}
          OPTIONAL {{ ?item wdt:P31 ?instance . }}
          OPTIONAL {{
            ?sitelink schema:about ?item ;
                      schema:isPartOf <https://en.wikipedia.org/> .
          }}

          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fr,de,es". }}
        }}
        """

        try:
            bindings = _run_sparql(sparql)
        except Exception as e:
            print(f"    Warning: SPARQL failed for batch {i}: {e}")
            time.sleep(2)
            continue

        # Process results — may have multiple rows per Q-ID (multiple instance_of, etc.)
        for row in bindings:
            qid = row["item"]["value"].rsplit("/", 1)[-1]
            if qid not in results:
                results[qid] = {
                    "qid": qid,
                    "label": _val(row, "itemLabel"),
                    "description": _val(row, "itemDescription"),
                }

            entry = results[qid]

            # Take the first non-empty value for each property
            pop = _val(row, "pop")
            if pop and "population" not in entry:
                try:
                    entry["population"] = int(float(pop))
                except (ValueError, TypeError):
                    pass

            area = _val(row, "area")
            if area and "area_km2" not in entry:
                try:
                    entry["area_km2"] = round(float(area), 2)
                except (ValueError, TypeError):
                    pass

            elev = _val(row, "elev")
            if elev and "elevation_m" not in entry:
                try:
                    entry["elevation_m"] = round(float(elev))
                except (ValueError, TypeError):
                    pass

            country = _val(row, "countryLabel")
            if country and "country" not in entry:
                entry["country"] = country

            capital = _val(row, "capitalLabel")
            if capital and "capital" not in entry:
                entry["capital"] = capital

            tz = _val(row, "timezoneLabel")
            if tz and "timezone" not in entry:
                entry["timezone"] = tz

            website = _val(row, "website")
            if website and "website" not in entry:
                entry["website"] = website

            instance = _val(row, "instanceLabel")
            if instance and "instance_of" not in entry:
                entry["instance_of"] = instance

            sitelink = _val(row, "sitelink")
            if sitelink and "wikipedia_url" not in entry:
                entry["wikipedia_url"] = sitelink
                # Extract article title for extract fetching
                title = sitelink.rsplit("/wiki/", 1)[-1] if "/wiki/" in sitelink else ""
                if title:
                    entry["wikipedia_title"] = urllib.parse.unquote(title)

        elapsed = time.time() - start_time
        done = min(i + batch_size, total)
        rate = done / elapsed if elapsed > 0 else 0
        remaining = (total - done) / rate if rate > 0 else 0
        print(f"\r    Fetched {done}/{total} ({rate:.0f}/s, ~{remaining:.0f}s left)...",
              end="", flush=True)

        # Incremental save to avoid losing hours of progress on crash/kill
        if cache_dir and done - last_save >= save_interval:
            save_cache(cache_dir, results)
            last_save = done

        # Rate limit: Wikidata SPARQL allows ~60 req/min for anonymous users
        time.sleep(1.0)

    print(f"\r    Fetched properties for {len(results)}/{total} Q-IDs in {time.time() - start_time:.0f}s")
    return results


def _val(row, key):
    """Extract a string value from a SPARQL result row."""
    v = row.get(key, {})
    if isinstance(v, dict):
        return v.get("value", "")
    return ""


def fetch_wikipedia_extracts(wikidata_entries, batch_size=20):
    """Fetch short Wikipedia extracts for entries that have wikipedia_title.

    Modifies entries in-place, adding an 'extract' field.
    """
    titles_to_fetch = []
    for qid, entry in wikidata_entries.items():
        title = entry.get("wikipedia_title")
        if title:
            titles_to_fetch.append((qid, title))

    if not titles_to_fetch:
        return

    total = len(titles_to_fetch)
    print(f"  Fetching Wikipedia extracts for {total} articles...")
    start_time = time.time()

    for i in range(0, total, batch_size):
        batch = titles_to_fetch[i:i + batch_size]
        titles = "|".join(t for _, t in batch)

        params = urllib.parse.urlencode({
            "action": "query",
            "titles": titles,
            "prop": "extracts",
            "exintro": "true",
            "explaintext": "true",
            "exsentences": "3",
            "format": "json",
            "formatversion": "2",
        })

        url = f"{WIKIPEDIA_API}?{params}"
        headers = {"User-Agent": USER_AGENT}

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            pages = data.get("query", {}).get("pages", [])
            # Build title -> extract map
            extract_map = {}
            for page in pages:
                title = page.get("title", "")
                extract = page.get("extract", "")
                if title and extract:
                    # Normalize title for matching
                    extract_map[title.replace(" ", "_")] = extract

            # Match back to Q-IDs
            for qid, title in batch:
                normalized = urllib.parse.unquote(title).replace(" ", "_")
                extract = extract_map.get(normalized, "")
                if not extract:
                    # Try with spaces
                    extract = extract_map.get(title.replace("_", " ").replace(" ", "_"), "")
                if extract:
                    # Truncate to ~500 chars for space efficiency
                    if len(extract) > 500:
                        # Cut at last sentence boundary before 500 chars
                        cut = extract[:500].rfind(". ")
                        if cut > 200:
                            extract = extract[:cut + 1]
                        else:
                            extract = extract[:500] + "..."
                    wikidata_entries[qid]["extract"] = extract

        except Exception as e:
            print(f"    Warning: Wikipedia API failed for batch {i}: {e}")

        done = min(i + batch_size, total)
        if done % 100 == 0 or done == total:
            print(f"\r    Fetched {done}/{total} extracts...", end="", flush=True)
        time.sleep(0.2)

    count = sum(1 for e in wikidata_entries.values() if "extract" in e)
    print(f"\r    Fetched {count} Wikipedia extracts in {time.time() - start_time:.0f}s")


def load_cache(cache_dir):
    """Load existing cache from directory. Returns dict of Q-ID -> data."""
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return {}

    entries = {}
    for json_file in sorted(cache_dir.glob("*.json")):
        if json_file.name == "manifest.json":
            continue
        try:
            with open(json_file) as f:
                bucket = json.load(f)
            for qid, data in bucket.items():
                entries[qid] = data
        except (json.JSONDecodeError, OSError) as e:
            print(f"    Warning: failed to read {json_file}: {e}")

    return entries


def save_cache(cache_dir, entries, qid_features=None):
    """Save cache entries to directory, bucketed by Q-ID prefix.

    Each bucket file contains entries for Q-IDs sharing the same numeric prefix
    (first 3 digits after 'Q'), keeping individual files small and updates incremental.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Bucket by prefix (Q1234 -> bucket "1", Q12345 -> bucket "12", etc.)
    buckets = defaultdict(dict)
    for qid, data in entries.items():
        # Use first 2 digits of the numeric part as bucket key
        num_part = qid[1:]  # strip 'Q'
        bucket_key = num_part[:2] if len(num_part) >= 2 else num_part
        # Merge OSM feature info if available
        if qid_features and qid in qid_features:
            osm = qid_features[qid]
            if osm.get("name") and not data.get("osm_name"):
                data["osm_name"] = osm["name"]
            if osm.get("type") and not data.get("osm_type"):
                data["osm_type"] = osm["type"]
            if osm.get("lat") and not data.get("lat"):
                data["lat"] = osm["lat"]
                data["lon"] = osm["lon"]
        buckets[bucket_key][qid] = data

    for bucket_key, bucket_entries in buckets.items():
        bucket_path = cache_dir / f"{bucket_key}.json"
        # Merge with existing bucket if present
        if bucket_path.exists():
            try:
                with open(bucket_path) as f:
                    existing = json.load(f)
                existing.update(bucket_entries)
                bucket_entries = existing
            except (json.JSONDecodeError, OSError):
                pass
        with open(bucket_path, "w") as f:
            json.dump(bucket_entries, f, separators=(",", ":"), ensure_ascii=False)

    # Write manifest
    manifest = {
        "total_entries": len(entries),
        "buckets": len(buckets),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(cache_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"    Saved {len(entries)} entries in {len(buckets)} buckets to {cache_dir}/")


def print_cache_stats(cache_dir):
    """Print statistics about an existing cache."""
    entries = load_cache(cache_dir)
    if not entries:
        print(f"  Cache is empty or not found: {cache_dir}")
        return

    total = len(entries)
    has_pop = sum(1 for e in entries.values() if "population" in e)
    has_area = sum(1 for e in entries.values() if "area_km2" in e)
    has_extract = sum(1 for e in entries.values() if "extract" in e)
    has_desc = sum(1 for e in entries.values() if "description" in e)
    has_country = sum(1 for e in entries.values() if "country" in e)
    has_wp_url = sum(1 for e in entries.values() if "wikipedia_url" in e)

    # Count by instance type
    types = defaultdict(int)
    for e in entries.values():
        itype = e.get("instance_of", "unknown")
        types[itype] += 1

    print(f"  Wikidata cache: {cache_dir}")
    print(f"    Total entries:     {total:,}")
    print(f"    Has description:   {has_desc:,} ({100*has_desc//total}%)")
    print(f"    Has population:    {has_pop:,} ({100*has_pop//total}%)")
    print(f"    Has area:          {has_area:,} ({100*has_area//total}%)")
    print(f"    Has extract:       {has_extract:,} ({100*has_extract//total}%)")
    print(f"    Has country:       {has_country:,} ({100*has_country//total}%)")
    print(f"    Has Wikipedia URL: {has_wp_url:,} ({100*has_wp_url//total}%)")
    print(f"    Top types:")
    for itype, count in sorted(types.items(), key=lambda x: -x[1])[:15]:
        print(f"      {itype}: {count:,}")


def build_cache(pbf_path=None, mbtiles_path=None, cache_dir=None, skip_extracts=False):
    """Main entry point: extract Q-IDs, fetch Wikidata, save cache.

    Returns the cache directory path.
    """
    cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)

    # Step 1: Extract Q-IDs from OSM data
    if pbf_path:
        qid_features = extract_qids_from_pbf(pbf_path)
    elif mbtiles_path:
        qid_features = extract_qids_from_mbtiles(mbtiles_path)
    else:
        print("Error: must specify --pbf or --mbtiles")
        return None

    if not qid_features:
        print("  No wikidata-tagged features found")
        return cache_dir

    # Step 2: Check what's already cached
    existing = load_cache(cache_dir)
    new_qids = [qid for qid in qid_features if qid not in existing]

    if not new_qids:
        print(f"  All {len(qid_features)} Q-IDs already cached")
        return cache_dir

    print(f"  {len(new_qids)} new Q-IDs to fetch ({len(existing)} already cached)")

    # Step 3: Fetch Wikidata properties (with incremental saves)
    new_entries = fetch_wikidata_batch(new_qids, cache_dir=cache_dir)

    # Step 4: Fetch Wikipedia extracts
    if not skip_extracts:
        fetch_wikipedia_extracts(new_entries)

    # Step 5: Merge and save
    all_entries = {**existing, **new_entries}
    save_cache(cache_dir, all_entries, qid_features)

    return cache_dir


def load_cache_for_zim(cache_dir):
    """Load cache and format it for embedding in a ZIM file.

    Returns a compact JSON string suitable for bundling.
    """
    entries = load_cache(cache_dir)
    if not entries:
        return None

    # Build compact format: only include non-empty fields
    compact = {}
    for qid, data in entries.items():
        c = {}
        if data.get("label"):
            c["l"] = data["label"]
        if data.get("description"):
            c["d"] = data["description"]
        if data.get("population"):
            c["p"] = data["population"]
        if data.get("area_km2"):
            c["a"] = data["area_km2"]
        if data.get("elevation_m"):
            c["e"] = data["elevation_m"]
        if data.get("country"):
            c["c"] = data["country"]
        if data.get("capital"):
            c["cap"] = data["capital"]
        if data.get("extract"):
            c["x"] = data["extract"]
        if data.get("instance_of"):
            c["i"] = data["instance_of"]
        if data.get("timezone"):
            c["tz"] = data["timezone"]
        if c:
            compact[qid] = c
    return compact


def main():
    parser = argparse.ArgumentParser(
        description="Build a Wikidata cache from OSM data for StreetZIM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build cache from PBF (recommended — reads wikidata tags directly)
  python3 wikidata_cache.py --pbf district-of-columbia.osm.pbf

  # Build from MBTiles (fallback — uses name/coordinate matching)
  python3 wikidata_cache.py --mbtiles tiles.mbtiles

  # Show cache statistics
  python3 wikidata_cache.py --stats

  # Custom cache directory
  python3 wikidata_cache.py --pbf data.osm.pbf --cache /path/to/cache/
""",
    )

    parser.add_argument("--pbf", help="OSM PBF file to extract Q-IDs from")
    parser.add_argument("--mbtiles", help="MBTiles file to extract features from")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE_DIR),
                        help=f"Cache directory (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--stats", action="store_true",
                        help="Show cache statistics and exit")
    parser.add_argument("--skip-extracts", action="store_true",
                        help="Skip fetching Wikipedia extracts (faster)")

    args = parser.parse_args()

    if args.stats:
        print_cache_stats(args.cache)
        return

    if not args.pbf and not args.mbtiles:
        print("Error: must specify --pbf or --mbtiles (or --stats to view cache)")
        parser.print_help()
        sys.exit(1)

    print("=== Building Wikidata Cache ===")
    cache_dir = build_cache(
        pbf_path=args.pbf,
        mbtiles_path=args.mbtiles,
        cache_dir=args.cache,
        skip_extracts=args.skip_extracts,
    )

    if cache_dir:
        print()
        print_cache_stats(cache_dir)


if __name__ == "__main__":
    main()
