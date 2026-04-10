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
        "description": "United Kingdom, Ireland, France, Spain, Portugal, Germany, Italy, Netherlands, Belgium, Switzerland, Austria, Poland, Greece, Sweden, Norway, Finland, Denmark, the Baltics, Ukraine, and dozens more.",
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
        "id": "washington-dc",
        "title": "Washington, D.C.",
        "zim_file": "osm-washington-dc.zim",
        "description": "Washington, D.C. &mdash; the U.S. capital and surrounding metro area.",
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
    url = (
        "https://archive.org/advancedsearch.php?"
        "q=identifier%3Astreetzim-*"
        "&fl%5B%5D=identifier"
        "&fl%5B%5D=title"
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


def render_live_card(region, size_label):
    """Render a map card with active download/torrent/details buttons."""
    item_id = f"streetzim-{region['id']}"
    zim_file = region["zim_file"]
    title_attr = escape(region["title"], quote=True)
    return f"""      <div class="map-card">
        <div class="map-card-head">
          <div class="map-card-title">{escape(region["title"])}</div>
          <div class="map-card-size">{size_label}</div>
        </div>
        <p class="map-card-desc">{region["description"]}</p>
        <div class="map-card-links">
          <a class="btn btn-primary" href="https://archive.org/download/{item_id}/{zim_file}" data-track="download" data-region="{region["id"]}" data-title="{title_attr}">Download</a>
          <a class="btn btn-secondary" href="https://archive.org/download/{item_id}/{item_id}_archive.torrent" data-track="torrent" data-region="{region["id"]}" data-title="{title_attr}">Torrent</a>
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
            # Get the actual ZIM file size (item_size includes torrent + xml overhead)
            details = fetch_item_details(f"streetzim-{region['id']}")
            zim_size = None
            if details:
                for f in details.get("files", []):
                    if f.get("name") == region["zim_file"]:
                        try:
                            zim_size = int(f.get("size", 0))
                        except (TypeError, ValueError):
                            pass
                        break
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
            cards.append(render_live_card(region, human_size(zim_size)))
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
