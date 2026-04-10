#!/usr/bin/env python3
"""Pull download stats for StreetZim maps from Archive.org.

Prints a table of each streetzim-* item showing total downloads, last 30 days,
and last 7 days. Archive.org download counts are the ground truth — Firebase
Analytics only captures clicks from people whose browsers allow telemetry.

Usage:
    python3 web/stats.py              # pretty table
    python3 web/stats.py --json       # raw JSON
    python3 web/stats.py --csv        # CSV for spreadsheets
"""
import argparse
import datetime
import json
import sys
import urllib.request


def fetch_all_items():
    """Query Archive.org for all streetzim-* items with download counts."""
    url = (
        "https://archive.org/advancedsearch.php?"
        "q=identifier%3Astreetzim-*"
        "&fl%5B%5D=identifier"
        "&fl%5B%5D=title"
        "&fl%5B%5D=downloads"
        "&fl%5B%5D=month"
        "&fl%5B%5D=week"
        "&fl%5B%5D=item_size"
        "&fl%5B%5D=publicdate"
        "&sort%5B%5D=downloads+desc"
        "&rows=100"
        "&output=json"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim-stats/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return data.get("response", {}).get("docs", [])


def human_size(bytes_count):
    if not bytes_count:
        return ""
    gb = bytes_count / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = bytes_count / (1024 ** 2)
    return f"{int(round(mb))} MB"


def human_age(public_date_str):
    """Parse Archive.org publicdate and return 'X days ago'."""
    if not public_date_str:
        return ""
    try:
        dt = datetime.datetime.strptime(public_date_str[:10], "%Y-%m-%d")
        days = (datetime.datetime.utcnow() - dt).days
        if days == 0:
            return "today"
        if days == 1:
            return "1 day"
        return f"{days} days"
    except Exception:
        return ""


def print_table(items):
    if not items:
        print("No streetzim-* items found on Archive.org.")
        return

    # Column widths
    cols = [
        ("Region",      18, lambda i: i["identifier"].replace("streetzim-", "")),
        ("Size",         9, lambda i: human_size(i.get("item_size", 0))),
        ("Age",          8, lambda i: human_age(i.get("publicdate"))),
        ("Total DLs",   11, lambda i: f"{i.get('downloads', 0):,}"),
        ("Last 30d",    10, lambda i: f"{i.get('month', 0):,}"),
        ("Last 7d",      9, lambda i: f"{i.get('week', 0):,}"),
    ]

    # Header
    header = " ".join(name.ljust(w) for name, w, _ in cols)
    sep    = " ".join("-" * w for _, w, _ in cols)
    print(header)
    print(sep)

    total_downloads = 0
    total_week = 0
    total_month = 0
    for item in items:
        row = " ".join(str(fn(item)).ljust(w) for _, w, fn in cols)
        print(row)
        total_downloads += item.get("downloads", 0) or 0
        total_month     += item.get("month", 0) or 0
        total_week      += item.get("week", 0) or 0

    print(sep)
    totals = "Totals".ljust(cols[0][1])
    totals += " " + "".ljust(cols[1][1])
    totals += " " + "".ljust(cols[2][1])
    totals += " " + f"{total_downloads:,}".ljust(cols[3][1])
    totals += " " + f"{total_month:,}".ljust(cols[4][1])
    totals += " " + f"{total_week:,}".ljust(cols[5][1])
    print(totals)
    print()
    print(f"Data from Archive.org at {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")


def print_json(items):
    rows = []
    for item in items:
        rows.append({
            "region": item["identifier"].replace("streetzim-", ""),
            "identifier": item["identifier"],
            "size_bytes": item.get("item_size", 0),
            "downloads_total": item.get("downloads", 0),
            "downloads_last_30d": item.get("month", 0),
            "downloads_last_7d": item.get("week", 0),
            "published": item.get("publicdate", ""),
        })
    print(json.dumps(rows, indent=2))


def print_csv(items):
    import csv
    writer = csv.writer(sys.stdout)
    writer.writerow(["region", "identifier", "size_gb", "downloads_total",
                     "downloads_last_30d", "downloads_last_7d", "published"])
    for item in items:
        size_gb = round((item.get("item_size", 0) or 0) / (1024 ** 3), 2)
        writer.writerow([
            item["identifier"].replace("streetzim-", ""),
            item["identifier"],
            size_gb,
            item.get("downloads", 0),
            item.get("month", 0),
            item.get("week", 0),
            item.get("publicdate", ""),
        ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--csv",  action="store_true", help="Output CSV")
    args = parser.parse_args()

    items = fetch_all_items()
    if args.json:
        print_json(items)
    elif args.csv:
        print_csv(items)
    else:
        print_table(items)
