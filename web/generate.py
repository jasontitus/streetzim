#!/usr/bin/env python3
"""Regenerate web/index.html from Archive.org.

Queries Archive.org for all items with identifiers starting with "streetzim-",
merges with the REGIONS registry, and outputs a fresh index.html from
web/template.html. Then (optionally) runs `firebase deploy`.

Usage:
    python3 web/generate.py          # generate only
    python3 web/generate.py --deploy # generate and deploy to Firebase
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request
from html import escape

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "template.html")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "index.html")
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

# Static registry of regions with their display names and descriptions.
# The "order" field controls display order. New regions go here.
# `zim_file` must match the filename we upload to Archive.org.
REGIONS = [
    {
        "id": "europe",
        "title": "Europe",
        "zim_file": "osm-europe.zim",
        "description": "United Kingdom, Ireland, France, Spain, Portugal, Germany, Italy, Netherlands, Belgium, Switzerland, Austria, Poland, Greece, Sweden, Norway, Finland, Denmark, the Baltics, Ukraine, Georgia, Armenia, Azerbaijan, Cyprus, and dozens more.",
    },
    {
        "id": "united-states",
        "title": "United States",
        "zim_file": "osm-united-states.zim",
        "description": "Continental United States &mdash; all 48 contiguous states and Washington, D.C.",
    },
    {
        "id": "west-asia",
        "title": "West Asia",
        "zim_file": "osm-west-asia.zim",
        "description": "Turkey, Syria, Lebanon, Israel, Palestine, Jordan, Iraq, Iran, Kuwait, Saudi Arabia, Bahrain, Qatar, UAE, Oman, Yemen, and parts of Egypt, Afghanistan, and Pakistan.",
    },
    {
        "id": "africa",
        "title": "Africa",
        "zim_file": "osm-africa.zim",
        "description": "All of Africa &mdash; Algeria, Egypt, Ethiopia, Kenya, Morocco, Nigeria, South Africa, Tanzania, and 40+ more countries.",
    },
    {
        "id": "indian-subcontinent",
        "title": "Indian Subcontinent",
        "zim_file": "osm-indian-subcontinent.zim",
        "description": "India, Pakistan, Bangladesh, Sri Lanka, Nepal, Bhutan, and the Maldives.",
    },
    {
        "id": "midwest-us",
        "title": "Midwest United States",
        "zim_file": "osm-midwest-us.zim",
        "description": "Ohio, Indiana, Illinois, Michigan, Wisconsin, Minnesota, Iowa, Missouri, North Dakota, South Dakota, Nebraska, and Kansas.",
    },
    {
        "id": "california",
        "title": "California",
        "zim_file": "osm-california.zim",
        "description": "All of California &mdash; from the Oregon border to Mexico, the Pacific coast to the Sierra Nevada.",
    },
    {
        "id": "colorado",
        "title": "Colorado",
        "zim_file": "osm-colorado.zim",
        "description": "The Rocky Mountain state &mdash; Denver, Aspen, Vail, Rocky Mountain National Park, and the Continental Divide.",
    },
    {
        "id": "iran",
        "title": "Iran",
        "zim_file": "osm-iran.zim",
        "description": "Iran &mdash; from the Caspian Sea to the Persian Gulf, including Tehran, Isfahan, Shiraz, and Mashhad.",
    },
    {
        "id": "hispaniola",
        "title": "Hispaniola",
        "zim_file": "osm-hispaniola.zim",
        "description": "The Caribbean island of Hispaniola &mdash; Haiti and the Dominican Republic.",
    },
    {
        "id": "texas",
        "title": "Texas",
        "zim_file": "osm-texas.zim",
        "description": "Texas, USA &mdash; from the Gulf Coast to the Rio Grande, including Houston, Dallas, San Antonio, Austin, Fort Worth, and El Paso.",
    },
    {
        "id": "west-coast-us",
        "title": "West Coast US",
        "zim_file": "osm-west-coast-us.zim",
        "description": "U.S. West Coast: Washington, Oregon, and California &mdash; Seattle, Portland, San Francisco, Los Angeles, San Diego, and everything in between.",
    },
    {
        "id": "east-coast-us",
        "title": "East Coast US",
        "zim_file": "osm-east-coast-us.zim",
        "description": "U.S. East Coast from Maine to Florida &mdash; New York, Boston, Philadelphia, Washington D.C., Atlanta, Miami, and the entire Eastern Seaboard.",
    },
    {
        "id": "australia-nz",
        "title": "Australia & New Zealand",
        "zim_file": "osm-australia-nz.zim",
        "description": "Australia and New Zealand &mdash; Sydney, Melbourne, Brisbane, Perth, Auckland, Wellington, the Great Barrier Reef, Outback, and Southern Alps.",
    },
    {
        "id": "japan",
        "title": "Japan",
        "zim_file": "osm-japan.zim",
        "description": "Japan &mdash; all four main islands (Honshu, Hokkaido, Kyushu, Shikoku), Okinawa, and the Ryukyu archipelago. Tokyo, Osaka, Kyoto, Nagoya, Sapporo, Fukuoka, Hiroshima, and more.",
    },
    {
        "id": "washington-dc",
        "title": "Washington, D.C.",
        "zim_file": "osm-washington-dc.zim",
        "description": "Washington, D.C. &mdash; the U.S. capital and surrounding metro area.",
    },
    {
        "id": "baltics",
        "title": "Baltics",
        "zim_file": "osm-baltics.zim",
        "description": "Estonia, Latvia, and Lithuania &mdash; Tallinn, Riga, Vilnius, and the Baltic Sea coast.",
    },
    {
        "id": "silicon-valley",
        "title": "Silicon Valley",
        "zim_file": "osm-silicon-valley.zim",
        "description": "San Francisco Bay Area &mdash; San Francisco, Oakland, Palo Alto, Mountain View, Stanford, Cupertino, San Jose, and the Peninsula.",
    },
    {
        "id": "central-us",
        "title": "Central US",
        "zim_file": "osm-central-us.zim",
        "description": "The Mountain West and surrounds &mdash; Utah, Colorado, Wyoming, Montana, Idaho, Nevada, Arizona, and New Mexico. Salt Lake City, Denver, Phoenix, Albuquerque, Yellowstone, Grand Canyon, and the Rockies.",
    },
    {
        "id": "egypt",
        "title": "Egypt",
        "zim_file": "osm-egypt.zim",
        "description": "Egypt &mdash; Cairo, Alexandria, Giza, Luxor, Aswan, the Nile Valley, Sinai Peninsula, and the Red Sea coast.",
    },
    {
        "id": "canada",
        "title": "Canada",
        "zim_file": "osm-canada.zim",
        "description": "All of Canada &mdash; Toronto, Montreal, Vancouver, Calgary, Ottawa, Quebec City, Edmonton, Halifax, the Rockies, Banff, the Great Lakes, the Maritimes, and the Yukon and Northwest Territories.",
    },
    {
        "id": "central-asia",
        "title": "Central Asia",
        "zim_file": "osm-central-asia.zim",
        "description": "Central Asia &mdash; Kazakhstan, Uzbekistan, Turkmenistan, Tajikistan, Kyrgyzstan, Afghanistan, and the Caucasus. Almaty, Tashkent, Bishkek, Ashgabat, Dushanbe, Kabul, the Pamirs, and the Silk Road.",
    },
    {
        "id": "central-america-caribbean",
        "title": "Central America & Caribbean",
        "zim_file": "osm-central-america-caribbean.zim",
        "description": "Central America and the Caribbean &mdash; Yucatán, Belize, Guatemala, Honduras, El Salvador, Nicaragua, Costa Rica, Panama, Cuba, Jamaica, Hispaniola, Cayman Islands, Puerto Rico, the Lesser Antilles, and the southern Bahamas.",
    },
    {
        "id": "himalayas",
        "title": "Himalayas",
        "zim_file": "osm-himalayas.zim",
        "description": "The Himalayas, Karakoram, Hindu Kush, and Pamir &mdash; Nepal, Bhutan, Tibet, Sikkim, Ladakh, Kashmir, the Indus and Brahmaputra valleys. Kathmandu, Pokhara, Lhasa, Thimphu, Leh, Hunza, Everest, K2, Kanchenjunga, and Annapurna.",
    },
]


def human_size(bytes_count):
    """Format bytes as 'X.X GB' or 'X MB'."""
    if bytes_count is None or bytes_count <= 0:
        return ""
    gb = bytes_count / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = bytes_count / (1024 ** 2)
    return f"{int(round(mb))} MB"


def fetch_archive_items():
    """Query Archive.org for all streetzim-* items. Returns {region_id: metadata}."""
    # NOTE: don't include `&fl[]=title` here. archive.org's search index
    # populates fields incrementally for new items — `title` can lag the
    # `identifier`/`item_size` fields by hours. With `title` in the
    # field list, a brand-new item is silently filtered OUT of the
    # response. We don't actually use the search-side title (live-card
    # rendering uses the static REGIONS[].title), so dropping it makes
    # generate.py see new items as soon as their identifier indexes.
    # Bug seen 2026-04-25 with Egypt — first ~hour after upload it had
    # a title in metadata but not in the search index.
    url = (
        "https://archive.org/advancedsearch.php?"
        "q=identifier%3Astreetzim-*"
        "&fl%5B%5D=identifier"
        "&fl%5B%5D=item_size"
        "&fl%5B%5D=publicdate"
        "&rows=100"
        "&output=json"
    )
    print(f"Querying Archive.org: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim-generate/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    docs = data.get("response", {}).get("docs", [])
    by_id = {}
    for doc in docs:
        identifier = doc.get("identifier", "")
        if not identifier.startswith("streetzim-"):
            continue
        region_id = identifier.replace("streetzim-", "", 1)
        by_id[region_id] = doc
    print(f"Found {len(by_id)} items on Archive.org")
    return by_id


def fetch_item_details(identifier):
    """Fetch the file list for an item to find the actual ZIM file size."""
    url = f"https://archive.org/metadata/{identifier}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "streetzim-generate/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except Exception as e:
        print(f"  Warning: failed to fetch {identifier}: {e}")
        return None


# Per-feature badge rendered on each region card. Keys mirror the
# metadata fields `cloud/stamp_item_metadata.py` sets on archive.org
# items after a successful upload. When a field is absent from the
# item metadata the badge is hidden — users on older pre-stamping
# ZIMs get no false advertising.
FEATURE_BADGES = [
    ("streetzim_routing",   "Routing & Directions", "nav"),
    ("streetzim_overture",  "Rich place info",      "overture"),
    ("streetzim_terrain",   "3D Terrain",           "terrain"),
    ("streetzim_satellite", "Satellite",            "satellite"),
    ("streetzim_wikidata",  "Wikipedia links",      "wiki"),
]


def render_feature_badges(item_meta):
    """Return an HTML fragment listing the features an item advertises.

    `item_meta` is the top-level `metadata` block of an archive.org
    metadata response (each field is a string). We treat "yes" (case-
    insensitive) as enabled and anything else — including the field
    being absent entirely — as disabled. No badges → empty string so
    the card doesn't get an empty row on stamping-less older items.
    """
    if not item_meta:
        return ""
    pills = []
    for key, label, css in FEATURE_BADGES:
        val = str(item_meta.get(key, "")).lower()
        if val == "yes":
            pills.append(
                f'<span class="map-badge badge-{css}">{escape(label)}</span>'
            )
    if not pills:
        return ""
    return '\n        <div class="map-card-badges">' + "".join(pills) + "</div>"


def render_live_card(region, size_label, item_meta=None):
    """Render a map card with active download/torrent/details buttons."""
    item_id = f"streetzim-{region['id']}"
    zim_file = region["zim_file"]
    title_attr = escape(region["title"], quote=True)
    badges_html = render_feature_badges(item_meta)
    return f"""      <div class="map-card">
        <div class="map-card-head">
          <div class="map-card-title">{escape(region["title"])}</div>
          <div class="map-card-size">{size_label}</div>
        </div>
        <p class="map-card-desc">{region["description"]}</p>{badges_html}
        <div class="map-card-links">
          <a class="btn btn-primary" href="https://archive.org/download/{item_id}/{zim_file}" data-track="download" data-region="{region["id"]}" data-title="{title_attr}">Download</a>
          <a class="btn btn-secondary" href="/torrents/{region["id"]}.torrent" data-track="torrent" data-region="{region["id"]}" data-title="{title_attr}">Torrent</a>
          <a class="btn btn-secondary" href="https://archive.org/details/{item_id}" data-track="details" data-region="{region["id"]}" data-title="{title_attr}">Info</a>
        </div>
      </div>"""


def render_upcoming_card(region):
    """Render a dimmed card for regions that haven't been uploaded yet."""
    return f"""      <div class="map-card upcoming">
        <div class="map-card-head">
          <div class="map-card-title">{escape(region["title"])}</div>
          <div class="map-card-size">building</div>
        </div>
        <p class="map-card-desc">{region["description"]}</p>
      </div>"""


def build_page():
    archive_items = fetch_archive_items()
    cards = []
    live_count = 0
    upcoming_count = 0

    for region in REGIONS:
        item = archive_items.get(region["id"])
        if item:
            # Get the actual ZIM file name + size from Archive.org metadata.
            # Filenames are now dated (e.g. osm-europe-2026-04.zim) so we
            # find the .zim file dynamically rather than hardcoding.
            details = fetch_item_details(f"streetzim-{region['id']}")
            zim_size = None
            zim_filename = None
            if details:
                # Find the .zim file (may be dated like osm-europe-2026-04.zim
                # or undated like osm-europe.zim). Pick the largest .zim.
                zim_files = [f for f in details.get("files", [])
                             if f.get("name", "").endswith(".zim")
                             and "history/" not in f.get("name", "")]
                if zim_files:
                    # Prefer dated filenames (e.g. osm-europe-2026-04-22.zim)
                    # over undated (osm-europe.zim). Among dated, pick the
                    # NEWEST by embedded date, not the largest — rebuilds
                    # can shrink a ZIM (e.g. when Overture deduping trims
                    # OSM duplicates). Old size-based tie-breaker was
                    # stale-stickying: a 5.5 GB April-13 ZIM beat a fresh
                    # 210 MB April-22 one because of byte size alone.
                    # Size only breaks ties among same-date uploads.
                    import re as _re
                    def _sort_key(f):
                        name = f.get("name", "")
                        # Match dated filenames optionally followed by a
                        # single-letter "same-day re-roll" suffix (b, c, d…).
                        # `2026-04-26 < 2026-04-26b < 2026-04-26c` by
                        # lexicographic compare, so the suffix wins as
                        # intended. Without the [a-z]? part, the c suffix
                        # didn't match the regex and the script treated
                        # the file as undated — letting the no-suffix
                        # version win on size alone.
                        m = _re.search(
                            r'-(\d{4}-\d{2}(?:-\d{2})?[a-z]?)\.zim$', name)
                        if m:
                            # (dated=1, date_str, size). Lexicographic
                            # comparison is correct: YYYY-MM < YYYY-MM-DD
                            # because the shorter prefix sorts first.
                            return (1, m.group(1), int(f.get("size", 0)))
                        return (0, "", int(f.get("size", 0)))
                    best = max(zim_files, key=_sort_key)
                    zim_filename = best.get("name")
                    try:
                        zim_size = int(best.get("size", 0))
                    except (TypeError, ValueError):
                        pass
            # Override the static zim_file with what's actually on Archive.org
            if zim_filename:
                region = {**region, "zim_file": zim_filename}
            if zim_size is None:
                try:
                    zim_size = int(item.get("item_size", 0))
                except (TypeError, ValueError):
                    zim_size = None
            if not zim_size or zim_size <= 0:
                # Item exists on Archive.org but the file is still uploading /
                # being processed — treat as upcoming so the page doesn't
                # show "0 MB" until the upload finalizes.
                print(f"  {region['id']}: file not yet finalized, treating as upcoming")
                cards.append(render_upcoming_card(region))
                upcoming_count += 1
                continue
            cards.append(render_live_card(
                region, human_size(zim_size),
                item_meta=(details or {}).get("metadata") if details else None))
            live_count += 1
        else:
            cards.append(render_upcoming_card(region))
            upcoming_count += 1

    print(f"Rendered {live_count} live, {upcoming_count} upcoming")

    with open(TEMPLATE_PATH) as f:
        template = f.read()

    updated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = template.replace("{{MAPS}}", "\n".join(cards))
    html = html.replace("{{UPDATED}}", updated)

    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT_PATH}")


def deploy():
    print("Deploying to Firebase...")
    subprocess.run(
        ["firebase", "deploy", "--only", "hosting"],
        cwd=PROJECT_DIR,
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy", action="store_true", help="Deploy to Firebase after generating")
    args = parser.parse_args()
    build_page()
    if args.deploy:
        deploy()
