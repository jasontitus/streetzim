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
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Literal, overload

from cloud.wikimedia_http import Pacer, TransientError, get_json, polite_pacer, user_agent
from streetzim.paths import cache_root

SCRIPT_DIR = Path(__file__).parent.resolve()
# $STREETZIM_CACHE_DIR, else this checkout, else a user cache dir when installed.
DEFAULT_CACHE_DIR = cache_root() / "wikidata_cache"

# Wikidata SPARQL endpoint
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
# Match the single-item policy used by cloud.wikidata_titles. A semicolon
# list from OSM is ambiguous and would otherwise poison the whole query.
_QID_RE = re.compile(r"Q[1-9][0-9]{0,9}")


def _validated_qids(qids):
    valid, invalid = [], []
    for qid in qids:
        if isinstance(qid, str) and _QID_RE.fullmatch(qid):
            valid.append(qid)
        else:
            invalid.append(qid)
    if invalid:
        examples = ", ".join(repr(qid)[:80] for qid in invalid[:3])
        print(f"    Warning: ignoring {len(invalid)} malformed Wikidata IDs "
              f"({examples}); not requested or cached")
    return valid

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

# Wikimedia's User-Agent policy wants a way to reach the operator: the
# project's issue tracker, plus STREETZIM_WIKI_CONTACT when set.
USER_AGENT = user_agent("wikidata-cache")


def _qid_cache_path(pbf_path, cache_dir):
    """Return path for cached Q-ID extraction results, keyed by PBF file identity."""
    pbf = Path(pbf_path)
    stat = pbf.stat()
    # Key by filename + size + mtime — avoids hashing multi-GB files
    key = f"{pbf.name}_{stat.st_size}_{int(stat.st_mtime)}"
    return Path(cache_dir) / f"qids_{key}.json"


def _load_cached_qids(pbf_path, cache_dir):
    """Load previously extracted Q-IDs if the PBF hasn't changed."""
    cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
    cache_file = _qid_cache_path(pbf_path, cache_dir)
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                data = json.load(f)
            print(f"  Using cached Q-ID extraction ({len(data)} Q-IDs from {cache_file.name})")
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_cached_qids(pbf_path, cache_dir, qid_features):
    """Save extracted Q-IDs for future reuse."""
    cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = _qid_cache_path(pbf_path, cache_dir)
    with open(cache_file, "w") as f:
        json.dump(qid_features, f, separators=(",", ":"), ensure_ascii=False)
    print(f"    Saved Q-ID extraction to {cache_file.name}")


def extract_qids_from_pbf(pbf_path, cache_dir=None):
    """Extract all wikidata Q-IDs from an OSM PBF file.

    Returns a dict mapping Q-ID -> list of {name, type, lat, lon} for each
    OSM feature that references it.

    Results are cached in cache_dir keyed by PBF filename+size+mtime,
    so repeated builds with the same PBF skip the scan entirely.
    """
    # Check for cached extraction first
    cached = _load_cached_qids(pbf_path, cache_dir)
    if cached is not None:
        return cached

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
            if lat is not None and lon is not None:  # set together above
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
    _save_cached_qids(pbf_path, cache_dir, qid_features)
    return qid_features


def extract_qids_from_mbtiles(mbtiles_path):
    """Read tile Wikidata tags, with name/coordinate lookup for untagged features.

    Direct IDs are independent of feature class, name, geometry and API
    availability. Use the deepest available zoom up to 14 (or the lowest
    available higher zoom), so lower-detail MBTiles retain cached facts too.
    Returns Q-ID -> {name, type, optional lat/lon}.
    """
    import gzip
    import math
    import mapbox_vector_tile
    from contextlib import closing

    search_layers = {
        "place": "place", "poi": "poi", "park": "park",
        "mountain_peak": "peak", "aerodrome_label": "airport", "water_name": "water",
    }
    qid_features, features_by_name = {}, {}
    with closing(sqlite3.connect(str(mbtiles_path))) as conn:
        zoom = conn.execute(
            "SELECT MAX(zoom_level) FROM tiles WHERE zoom_level <= 14"
        ).fetchone()[0]
        if zoom is None:
            zoom = conn.execute("SELECT MIN(zoom_level) FROM tiles").fetchone()[0]
        if zoom is None:
            print("    No vector tiles found")
            return {}
        print(f"  Scanning z{zoom} tiles for Wikidata tags and untagged named features...")
        # Iterating the cursor bounds compressed-tile memory to one row.
        rows = conn.execute(
            "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = ?",
            (zoom,))
        for col, row, data in rows:
            try:
                tile_data = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
                decoded = mapbox_vector_tile.decode(tile_data, y_coord_down=True)
            except Exception:
                continue

            n = 1 << zoom
            y = n - 1 - row  # TMS -> XYZ
            for layer_name, layer in decoded.items():
                extent = layer.get("extent", 4096)
                feature_type = search_layers.get(layer_name, layer_name)
                for feature in layer.get("features", []):
                    props = feature.get("properties") or {}
                    name = props.get("name:latin") or props.get("name", "")
                    qid = props.get("wikidata")
                    direct = isinstance(qid, str) and _QID_RE.fullmatch(qid)
                    info = {"name": name, "type": feature_type}
                    geom = feature.get("geometry") or {}
                    coords = geom.get("coordinates")
                    point = None
                    if coords:
                        try:
                            if geom.get("type") == "Point":
                                point = coords[0], coords[1]
                            elif geom.get("type") in ("Polygon", "MultiPolygon"):
                                ring = coords[0] if geom["type"] == "Polygon" else coords[0][0]
                                point = (sum(c[0] for c in ring) / len(ring),
                                         sum(c[1] for c in ring) / len(ring))
                        except (IndexError, ZeroDivisionError, TypeError):
                            pass
                    if point is not None:
                        px, py = point
                        info["lon"] = round((col + px / extent) / n * 360.0 - 180.0, 6)
                        info["lat"] = round(math.degrees(math.atan(math.sinh(
                            math.pi * (1 - 2 * (y + py / extent) / n)))), 6)
                    if direct:
                        old = qid_features.get(qid)
                        if old is None or (not old.get("name") and name):
                            qid_features[qid] = info
                        continue
                    # Retain the existing name-lookup fallback for tiles that
                    # do not carry IDs. Its heuristics must not filter tags.
                    if layer_name not in search_layers or not name or len(name) < 2:
                        continue
                    place_class = props.get("class", "")
                    if feature_type == "place" and place_class not in (
                            "continent", "country", "state", "province", "city", "town"):
                        continue
                    if point is None:
                        continue
                    key = (name, feature_type, place_class)
                    features_by_name.setdefault(key, {**info, "subtype": place_class})

    print(f"    Found {len(qid_features)} tagged Q-IDs and "
          f"{len(features_by_name)} named features to look up")
    if features_by_name:
        for qid, info in _lookup_qids_by_name(list(features_by_name.values())).items():
            qid_features.setdefault(qid, info)
    return qid_features


def _lookup_qids_by_name(features, batch_size=50):
    """Look up Wikidata Q-IDs for features by name + coordinates using SPARQL.

    Returns dict mapping Q-ID -> feature dict.
    """
    qid_features = {}
    total = len(features)

    # 0.5 s from each response to the next query; 429s widen it.
    pacer = Pacer(0.5)
    for i in range(0, total, batch_size):
        batch = features[i:i + batch_size]
        # Build SPARQL VALUES block
        values_parts = []
        for f in batch:
            name_escaped = (
                f["name"]
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", " ")
                .replace("\n", " ")
            )
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
            results = _run_sparql(sparql, pacer=pacer)
            for r in results:
                qid = r["item"]["value"].rsplit("/", 1)[-1]
                name = r.get("name", {}).get("value", "")
                # Find matching feature
                for f in batch:
                    if f["name"] == name:
                        qid_features[qid] = f
                        break
        except TransientError as e:
            print(f"    Warning: SPARQL lookup failed for batch {i}: {e}")
            if e.stop:
                print("    Warning: not looking up the rest (the endpoint will not answer)")
                break
        except Exception as e:
            print(f"    Warning: SPARQL lookup failed for batch {i}: {e}")

        if (i + batch_size) % 200 == 0:
            print(f"    Looked up {min(i + batch_size, total)}/{total} features...")

    print(f"    Resolved {len(qid_features)} Q-IDs from name lookups")
    return qid_features


def _run_sparql(query, retries=3, pacer=None):
    """Execute a SPARQL query against the Wikidata endpoint.

    429/5xx/network errors are retried honouring Retry-After
    (cloud/wikimedia_http.py), paced by `pacer` when given; raises when
    they outlive the retries, and the callers skip that batch (nothing is
    cached for it), or stop when the error says to.
    """
    url = WIKIDATA_SPARQL + "?" + urllib.parse.urlencode({
        "query": query,
        "format": "json",
    })
    data = get_json(url, user_agent=USER_AGENT, retries=retries, timeout=60,
                    accept="application/sparql-results+json", pacer=pacer)
    return (data or {}).get("results", {}).get("bindings", [])


def fetch_wikidata_batch(qids, batch_size=40, cache_dir=None, save_interval=10000):
    """Fetch Wikidata properties for a list of Q-IDs using SPARQL.

    Returns a dict mapping Q-ID -> {label, description, population, area_km2, ...}.
    If cache_dir is set, saves incrementally every save_interval Q-IDs so
    progress is not lost if the process is killed.
    """
    results = {}
    qid_list = _validated_qids(qids)
    total = len(qid_list)
    last_save = 0

    print(f"  Fetching Wikidata properties for {total} Q-IDs...")
    start_time = time.time()
    # 1 s from each response to the next query (the query service allows
    # about 60 a minute to an anonymous client); 429s widen it.
    pacer = Pacer(1.0)

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
            bindings = _run_sparql(sparql, pacer=pacer)
        except TransientError as e:
            print(f"    Warning: SPARQL failed for batch {i}: {e}")
            if e.stop:
                print("    Warning: not fetching the rest (the endpoint will not answer)")
                break
            continue
        except Exception as e:
            print(f"    Warning: SPARQL failed for batch {i}: {e}")
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

    print(f"\r    Fetched properties for {len(results)}/{total} Q-IDs in {time.time() - start_time:.0f}s")
    return results


def _val(row, key):
    """Extract a string value from a SPARQL result row."""
    v = row.get(key, {})
    if isinstance(v, dict):
        return v.get("value", "")
    return ""


# An entry the API answered for but gave no extract (no such page, an
# empty lead) carries NO_EXTRACT: True and is not asked again. An entry
# with a wikipedia_title and neither field was never answered (a rate
# limit, 5xx, a skipped or stopped batch) and is asked on the next build.
NO_EXTRACT = "no_extract"


def extract_pending(entry):
    """True when `entry` has an article whose extract was never answered."""
    return bool(entry.get("wikipedia_title")) and not entry.get("extract") \
        and not entry.get(NO_EXTRACT)


def fetch_wikipedia_extracts(wikidata_entries, batch_size=20, pacer=None):
    """Fetch short Wikipedia extracts for entries that have wikipedia_title.

    Modifies entries in-place, adding an 'extract' field, or NO_EXTRACT
    when the API answered without one. Entries that already have either
    are skipped. Returns the number of entries still pending (never
    answered; the next build asks again).

    Requests go through cloud/wikimedia_http.get_json, paced by `pacer`
    (default polite_pacer(): serial, at most STREETZIM_WIKI_MAX_PER_MIN a
    minute): 429/5xx are retried honouring Retry-After; a batch the API
    still cannot answer, or answers with another 4xx, is skipped; a
    refused client (401/403), a Retry-After beyond the retry cap or a
    spent wait budget stops the rest.
    """
    titles_to_fetch = []
    for qid, entry in wikidata_entries.items():
        if extract_pending(entry):
            titles_to_fetch.append((qid, entry["wikipedia_title"]))

    if not titles_to_fetch:
        return 0

    total = len(titles_to_fetch)
    print(f"  Fetching Wikipedia extracts for {total} articles...")
    start_time = time.time()
    if pacer is None:
        pacer = polite_pacer()

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

        try:
            try:
                data = get_json(url, user_agent=USER_AGENT, pacer=pacer, timeout=30)
            except urllib.error.HTTPError as e:
                # 401/403: this client is refused, so every batch would be.
                # Any other 4xx is about this batch: skip it (left pending).
                raise TransientError(f"HTTP {e.code}", e.code,
                                     stop=e.code in (401, 403)) from e
            if not isinstance(data, dict) or not isinstance(data.get("query"), dict):
                raise TransientError("body has no query (an error or unexpected body)")

            pages = data["query"].get("pages", [])
            # A continued answer may hold the rest of the extracts in a
            # later page: only a complete answer says "no extract".
            complete = "continue" not in data
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
                elif complete:
                    wikidata_entries[qid][NO_EXTRACT] = True

        except TransientError as e:
            print(f"    Warning: Wikipedia API failed for batch {i}: {e}")
            if e.stop:
                print(f"    Warning: not fetching the remaining {total - i - len(batch)} "
                      "extracts (the API will not answer)")
                break
        except Exception as e:
            print(f"    Warning: Wikipedia API failed for batch {i}: {e}")

        done = min(i + batch_size, total)
        if done % 100 == 0 or done == total:
            print(f"\r    Fetched {done}/{total} extracts...", end="", flush=True)

    count = sum(1 for e in wikidata_entries.values() if "extract" in e)
    pending = sum(1 for e in wikidata_entries.values() if extract_pending(e))
    print(f"\r    Fetched {count} Wikipedia extracts in {time.time() - start_time:.0f}s"
          + (f"; {pending} not answered, asked again next build" if pending else ""))
    return pending


def _cache_buckets(cache_dir, *, skip=(), strict=False):
    """Yield one decoded cache bucket at a time, never the whole cache."""
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return

    for json_file in sorted(cache_dir.glob("*.json")):
        if json_file.name in skip:
            continue
        if json_file.name == "manifest.json":
            continue
        if json_file.name.startswith("qids_"):
            continue
        try:
            with open(json_file) as f:
                bucket = json.load(f)
            yield json_file.name, bucket
            del bucket
        except (json.JSONDecodeError, OSError) as e:
            if strict:
                raise
            what = ("it is not valid JSON (cut short?): deleting it has its Q-IDs "
                    "fetched again by the builds that use them"
                    if isinstance(e, json.JSONDecodeError) else
                    "check its owner and mode; it may be another user's valid "
                    "bucket, so do not delete it for this")
            print(f"    Warning: cannot read Wikidata cache bucket {json_file} ({e}); "
                  f"its entries are left out here and the file is not changed; {what}. "
                  "A build that needs to add to this bucket stops rather than "
                  "replace it.")


def load_cache(cache_dir, *, qids=None):
    """Load Q-ID -> data, optionally restricted to this extract's Q-IDs.

    An empty selection loads nothing; None keeps the full-cache API.
    Filtering happens bucket by bucket, before unrelated entries accumulate.
    """
    if qids is not None:
        qids = set(qids)
        if not qids:
            return {}
    entries = {}
    for _name, bucket in _cache_buckets(cache_dir):
        entries.update((qid, data) for qid, data in bucket.items()
                       if qids is None or qid in qids)
        del bucket
    return entries


def save_cache(cache_dir, entries, qid_features=None):
    """Save cache entries to directory, bucketed by Q-ID prefix.

    Each bucket file contains entries for Q-IDs sharing the same numeric prefix
    (first 2 digits after 'Q'), keeping individual files small and updates incremental.
    Regional updates merge under the cache write lock and publish atomically:
    an unreadable shared bucket must never be replaced by a partial selection.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    from streetzim.download import file_lock
    _prepare_cache_lock(cache_dir)
    with file_lock(cache_dir / "manifest.json"):
        _remove_dead_stages(cache_dir)
        _save_cache_locked(cache_dir, entries, qid_features)


# _write_cache_json's and _prepare_cache_lock's staging names.
_STAGE_RE = re.compile(r"\.(?:[0-9]{1,2}|manifest)\.json(?:\.lock)?\.[0-9a-f]{32}\.tmp")
# A lock's own staging file is made before the lock exists, so another
# writer may be using a young one; older than this, its writer is gone.
_LOCK_STAGE_MAX_AGE_S = 3600


def _stages(cache_dir):
    """Staging files and folders left in the cache by writers that died
    (killed mid-publication; a normal exit removes its own)."""
    for path in Path(cache_dir).glob(".*.tmp"):
        if not _STAGE_RE.fullmatch(path.name):
            continue                      # only names this module makes
        if path.name.startswith(".manifest.json.lock."):
            try:
                if time.time() - path.lstat().st_mtime < _LOCK_STAGE_MAX_AGE_S:
                    continue
            except FileNotFoundError:
                continue
        yield path


def _remove_dead_stages(cache_dir):
    """Remove _stages(). Call with the cache's write lock held: cache files
    are only staged under it, so none of these is a live writer's."""
    removed = 0
    for path in _stages(cache_dir):
        try:
            if path.is_dir() and not path.is_symlink():
                for inner in path.iterdir():
                    inner.unlink()
                path.rmdir()
            else:
                path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"    Warning: cannot remove {path}, left by a stopped cache writer "
                  f"({e}); its owner can delete it.")
    if removed:
        print(f"    Removed {removed} staging file(s) left in the Wikidata cache by "
              "stopped builds")


def clean_dead_stages(cache_dir):
    """At the start of a build: if stopped writers left staging files, take
    the write lock and remove them. A cache this build cannot lock (read-
    only) is left as it is, with a warning."""
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir() or not any(_stages(cache_dir)):
        return
    from streetzim.download import file_lock
    try:
        _prepare_cache_lock(cache_dir)
        # Never wait for this: a writer holding the lock cleans up itself
        # when it saves, and a leftover this user cannot remove must not
        # make every later build queue behind other writers.
        with file_lock(cache_dir / "manifest.json", block=False) as locked:
            if locked:
                _remove_dead_stages(cache_dir)
    except OSError as e:
        print(f"    Warning: staging files left by stopped builds are in {cache_dir}, "
              f"and this build cannot take the cache's lock to remove them ({e})")


def _preserve_cache_permissions(staging, previous):
    from streetzim.cache_permissions import preserve_cache_permissions
    preserve_cache_permissions(staging, previous)


def _discard_cache_stage(staging):
    try:
        staging.unlink(missing_ok=True)
    except OSError:
        # Leave an unremovable private stage; never mask the primary failure.
        pass


def _prepare_cache_lock(cache_dir):
    """Publish a stable lock inode with the existing shared cache's access.

    Fully set its group/mode before linking it into place, so a second UID
    cannot race creation and open a temporarily inaccessible lock. Existing
    lock inodes must never be replaced: other writers may already hold them.
    """
    manifest = cache_dir / "manifest.json"
    lock = cache_dir / "manifest.json.lock"
    if lock.exists():
        return
    staging = lock.with_name(f".{lock.name}.{uuid.uuid4().hex}.tmp")
    stream = staging.open("x")
    try:
        with stream:
            pass
        _preserve_cache_permissions(staging, manifest)
        try:
            os.link(staging, lock)
        except FileExistsError:
            pass  # Another writer published first; use its stable inode.
    finally:
        _discard_cache_stage(staging)


def _write_cache_json(path, value):
    """Atomic JSON publication while the shared cache's write lock is held."""
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    directory = None
    if path.exists():
        # An empty stage opened by another user before chmod stays readable
        # through that FD afterwards. Keep existing-file replacements behind
        # a private directory; clear inherited ACLs before creating any file.
        directory = staging
        directory.mkdir(mode=0o700)  # Exclusive: never clean up a collision.
        staging = directory / "contents"
        try:
            from streetzim.cache_permissions import prepare_private_stage_directory
            prepare_private_stage_directory(directory)
            stream = staging.open("x", encoding="utf-8")
        except BaseException:
            _discard_cache_stage_directory(directory)
            raise
    else:
        # A new cache file keeps its directory's normal inheritance/umask.
        # Do not clean up a colliding path when exclusive creation fails.
        stream = staging.open("x", encoding="utf-8")
    try:
        # Exclusive creation uses the normal cache-file permissions/umask.
        # Set an existing file's access rules before writing any content:
        # a private bucket must not become readable through its staging file.
        with stream as f:
            _preserve_cache_permissions(staging, path)
            json.dump(value, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(staging, path)
    finally:
        _discard_cache_stage(staging)
        if directory is not None:
            _discard_cache_stage_directory(directory)


def _discard_cache_stage_directory(directory):
    try:
        directory.rmdir()
    except OSError:
        pass  # As with stage-file cleanup, preserve the primary error.


def _save_cache_locked(cache_dir, entries, qid_features):

    # Bucket by prefix (Q1 -> bucket "1", Q12345 -> bucket "12", etc.)
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

    bucket_counts = {}
    for bucket_key, bucket_entries in buckets.items():
        bucket_path = cache_dir / f"{bucket_key}.json"
        # Merge with existing bucket if present
        # New entries take priority over existing (overwrites stubs with enriched data)
        if bucket_path.exists():
            try:
                with open(bucket_path) as f:
                    existing = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                # Replacing it with only this build's entries would lose every
                # other region's, so stop; say so before the raw error.
                print(f"    ERROR: cannot read {bucket_path} ({e}), which this build "
                      "must add entries to; replacing it would drop the entries of "
                      "every other region in it, so the build stops here. Fix or "
                      "remove the file (see the warning above), or use a cache of "
                      "its own (--wikidata-cache).", flush=True)
                raise
            # Merge: start with existing, then overlay new entries on top.
            # Prefer enriched entries to stubs. Read/decode failures propagate
            # before publication; unrelated regions are only in this bucket.
            for qid, new_data in bucket_entries.items():
                old_data = existing.get(qid)
                if old_data and old_data.get("label") and not new_data.get("label"):
                    continue
                existing[qid] = new_data
            bucket_entries = existing
        _write_cache_json(bucket_path, bucket_entries)
        bucket_counts[bucket_path.name] = len(bucket_entries)

    # A regional update still describes the whole shared cache. Count the
    # untouched buckets one at a time, without retaining their entries. Only
    # the manifest's totals use this count (nothing reads them to build), so
    # an unreadable bucket this build does not use is a warning, left out of
    # the totals and never rewritten: its entries are not lost.
    for name, bucket in _cache_buckets(cache_dir, skip=bucket_counts):
        bucket_counts[name] = len(bucket)
        del bucket
    manifest = {
        "total_entries": sum(bucket_counts.values()),
        "buckets": len(bucket_counts),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_cache_json(cache_dir / "manifest.json", manifest)

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
    print("    Top types:")
    for itype, count in sorted(types.items(), key=lambda x: -x[1])[:15]:
        print(f"      {itype}: {count:,}")


@overload
def build_cache(pbf_path=None, mbtiles_path=None, cache_dir=None, skip_extracts=False,
                *, return_qids: Literal[False] = False) -> Path | None: ...


@overload
def build_cache(pbf_path=None, mbtiles_path=None, cache_dir=None, skip_extracts=False,
                *, return_qids: Literal[True]) -> tuple[Path | None, set[str]]: ...


def build_cache(pbf_path=None, mbtiles_path=None, cache_dir=None, skip_extracts=False,
                *, return_qids: bool = False) -> Path | None | tuple[Path | None, set[str]]:
    """Main entry point: extract Q-IDs, fetch Wikidata, save cache.

    Returns the cache directory path, or (path, selected Q-IDs) when
    return_qids=True. The selection is this call's validated extraction,
    including an empty set, and is never inferred from a shared manifest.
    """
    cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
    clean_dead_stages(cache_dir)

    # Step 1: Extract Q-IDs from OSM data (cached by PBF identity)
    if pbf_path:
        qid_features = extract_qids_from_pbf(pbf_path, cache_dir=cache_dir)
    elif mbtiles_path:
        qid_features = extract_qids_from_mbtiles(mbtiles_path)
    else:
        print("Error: must specify --pbf or --mbtiles")
        return (None, set()) if return_qids else None

    # Validate even when extraction came from an older disk cache, and avoid
    # mutating the cached/external feature map supplied by the extractor.
    qid_features = {qid: qid_features[qid] for qid in _validated_qids(qid_features)}
    result = (cache_dir, set(qid_features)) if return_qids else cache_dir

    if not qid_features:
        print("  No wikidata-tagged features found")
        return result

    # Step 2: Check what's already cached
    existing = load_cache(cache_dir, qids=qid_features)
    new_qids = [qid for qid in qid_features if qid not in existing]
    # Cached entries whose extract was never answered (a rate limit, a
    # stopped run): asked again, since a transient failure is never a miss.
    retry = {} if skip_extracts else {
        qid: e for qid, e in existing.items()
        if qid in qid_features and extract_pending(e)}

    if not new_qids and not retry:
        print(f"  All {len(qid_features)} Q-IDs already cached")
        return result

    new_entries = {}
    if new_qids:
        print(f"  {len(new_qids)} new Q-IDs to fetch ({len(existing)} already cached)")
        # Step 3: Fetch Wikidata properties (with incremental saves)
        new_entries = fetch_wikidata_batch(new_qids, cache_dir=cache_dir)
    if retry:
        print(f"  {len(retry)} cached entries have no extract yet; asking again")

    # Step 4: Fetch Wikipedia extracts (new entries and the retries)
    if not skip_extracts:
        fetch_wikipedia_extracts({**retry, **new_entries})

    # Step 5: Merge and save
    all_entries = {**existing, **new_entries}
    save_cache(cache_dir, all_entries, qid_features)

    return result


def load_cache_for_zim(cache_dir, *, qids=None):
    """Load cache and format it for embedding in a ZIM file.

    Returns a compact Q-ID -> fields dict suitable for bundling. Pass the
    extract's Q-IDs to avoid loading every region in a shared cache.
    """
    entries = load_cache(cache_dir, qids=qids)
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
