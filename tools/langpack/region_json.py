#!/usr/bin/env python3
"""The gates' region file (tools/langpack/regions/<region>.json), from a base.

    region_json.py BASE.zim [-o OUT.json]

run_gates.sh needs, per region, a city whose label the UI gate clicks, the
searches it types and two points it routes between. A hand-written file in
tools/langpack/regions/ wins (ship_packs.sh looks there first); this
writes one for any other base, from the base alone:

- city: a place record (category-index/place.json; a base without one,
  or none of whose places qualifies: the base's search records of cities,
  towns and villages, classed as the map's style draws them; a base without
  those either: the geo-index's rows, "place" rows as cities) with a Q-ID that the
  base's own wiki-geo-index lists (so the base, not only a pack, offers its
  article), inside map-config's clipArea when the base has one. First a
  city or town within 100 km of map-config's center (the registry's main
  city), then any city or town, then any place; a city before a town, then
  the nearest to the center. zoom 7; the UI gate
  tries other zooms when the label is hidden there.
- search.native: the city's native-script name ("nn") when the record has
  one, else its name; search.english: its name. Both expect the name the
  result list shows (the record's "n"), which the UI gate also finds in a
  row's data-name (a map in another language shows the place under that
  language's name: Warschau).
- route: from the city to the nearest other place record 1.5-8 km away
  (a suburb or village: on the road graph), else to a point 2 km north.
- named_qids: the city and the next such place.
- base_lang: map-config "language", else en.

Exit 1 when the base has no place with a Q-ID in its geo-index (a base
without Wikipedia articles: the UI gate's article check cannot pass).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack.merge_zim import blob_of  # noqa: E402
from tools.langpack.zimfmt import ZimReader  # noqa: E402

CLASS_RANK = {"city": 0, "town": 1}
SEARCH_CLASSES = ("city", "town", "village")
PLACE_RECORD = re.compile(rb'"t":\s*"place",\s*"s":\s*"(?:city|town|village)"')
NEAR_KM = 100.0          # the main city's surroundings come first


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def inside(geom: object, lat: float, lon: float) -> bool:
    """Is the point inside a GeoJSON Polygon / MultiPolygon (even-odd rule,
    holes included)? Anything else counts as everywhere."""
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return True
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    hit = False
    for poly in polys:
        for ring in poly:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
                if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                    hit = not hit
    return hit


def blob(z: ZimReader, path: str) -> bytes | None:
    return blob_of(z, (ord("C"), path.encode()))


def search_places(z: ZimReader, geo_q: set) -> list[dict]:
    """The base's search records (search-data/) of cities, towns and villages
    whose Q-ID the geo-index lists, one per Q-ID (a city's before a town's
    before a village's, else the first in path order). A big map's search files
    hold millions of streets and POIs: only the place records are parsed,
    found in the raw JSON by their leading keys (the builder writes "n",
    "t", "s" first)."""
    prefix = b"search-data/"
    items: dict[int, list[int]] = {}   # cluster -> its blobs, in path order
    i = z.lower_bound(ord("C"), prefix)
    while i < z.header.entry_count:
        d = z.dirent_at(i)
        if d.ns != ord("C") or not d.path.startswith(prefix):
            break
        if not d.is_redirect and d.cluster is not None and d.blob is not None:
            items.setdefault(d.cluster, []).append(d.blob)
        i += 1
    dec = json.JSONDecoder()
    seen: dict[str, dict] = {}
    for cluster, blobs in items.items():
        data = z.cluster_blobs(cluster)
        for b in blobs:
            text = data[b]
            for m in PLACE_RECORD.finditer(text):
                start = text.rfind(b'{"n"', 0, m.start())
                end = text.find(b'{"n"', m.end())
                if start < 0:
                    continue
                try:
                    r, _ = dec.raw_decode(text[start:end if end >= 0 else len(text)].decode("utf-8"))
                except ValueError:
                    continue
                if (isinstance(r, dict) and r.get("t") == "place" and r.get("s") in SEARCH_CLASSES
                        and r.get("q") in geo_q and r.get("n")
                        and isinstance(r.get("a"), (int, float)) and isinstance(r.get("o"), (int, float))):
                    k = r["q"]
                    if k not in seen or SEARCH_CLASSES.index(r["s"]) < SEARCH_CLASSES.index(seen[k]["s"]):
                        seen[k] = r
    return list(seen.values())


def region_file(base: str) -> dict:
    z = ZimReader(base)
    cfg = json.loads(blob(z, "map-config.json") or b"{}")
    places = json.loads(blob(z, "category-index/place.json") or b"[]")
    geo = json.loads(blob(z, "wiki-geo-index.json") or b"{}")
    geo_q = {r[3] for r in geo.values() if isinstance(r, list) and len(r) >= 4 and isinstance(r[3], str)}
    c = cfg.get("center") or []
    center = (float(c[1]), float(c[0])) if len(c) == 2 else None
    if center is None:
        b = cfg.get("bounds") or [0, 0, 0, 0]
        center = ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2)
    cands = [p for p in places if isinstance(p, dict) and p.get("q") in geo_q
             and isinstance(p.get("a"), (int, float)) and isinstance(p.get("o"), (int, float)) and p.get("n")]
    if not cands:
        # no category file, or none of its places qualifies: the search records'
        # places, by the class the style draws
        found = search_places(z, geo_q)
        if found:
            places, cands = found, list(found)
    if not cands:
        # nor search records: the geo-index's own rows, places first
        cands = [{"n": t.replace("_", " "), "a": r[0], "o": r[1], "q": r[3], "s": "city" if r[2] == "place" else r[2]}
                 for t, r in sorted(geo.items()) if isinstance(r, list) and len(r) >= 4 and isinstance(r[3], str)
                 and isinstance(r[0], (int, float)) and isinstance(r[1], (int, float))]
    # --clip-poly bases: a place inside the region's outline, when there is one
    within = [p for p in cands if inside(cfg.get("clipArea"), p["a"], p["o"])]
    cands = within or cands
    if not cands:
        raise SystemExit("region_json: no place with a Q-ID in the base's wiki-geo-index")
    def tier(p: dict) -> tuple:
        d = km(center, (p["a"], p["o"]))
        big = p.get("s") in CLASS_RANK
        return (0 if big and d <= NEAR_KM else 1 if big else 2, CLASS_RANK.get(str(p.get("s")), 9), d, p["n"])
    cands.sort(key=tier)
    city = cands[0]
    cpt = (city["a"], city["o"])
    second = next((p for p in cands[1:] if p["q"] != city["q"]), None)
    near = sorted((p for p in places if isinstance(p, dict) and p is not city
                   and isinstance(p.get("a"), (int, float)) and isinstance(p.get("o"), (int, float))
                   and 1.5 <= km(cpt, (p["a"], p["o"])) <= 8.0),
                  key=lambda p: km(cpt, (p["a"], p["o"])))
    dst = (near[0]["a"], near[0]["o"]) if near else (cpt[0] + 0.018, cpt[1])
    native = city.get("nn") or city["n"]
    return {
        "base_lang": cfg.get("language") or "en",
        "city": {"qid": city["q"], "lon": round(city["o"], 5), "lat": round(city["a"], 5), "zoom": 7},
        "search": {"native": {"q": native, "expect": city["n"]},
                   "english": {"q": city["n"], "expect": city["n"]}},
        "route": {"src": f"{cpt[0]:.5f},{cpt[1]:.5f}", "dst": f"{dst[0]:.5f},{dst[1]:.5f}"},
        "named_qids": ",".join([city["q"]] + ([second["q"]] if second else [])),
        "generated": {"by": "region_json.py", "city": city["n"], "class": city.get("s"),
                      "base": os.path.basename(base)},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("base")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    try:
        r = region_file(a.base)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    text = json.dumps(r, ensure_ascii=False, indent=1) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
