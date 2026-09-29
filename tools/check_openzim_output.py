#!/usr/bin/env python3
"""Check what a `streetzim` run left in its output folder, the way Zimfarm
would see it (used by CI after building with the openZIM-style command).

    python tools/check_openzim_output.py OUT NAME --title T [--file F] [--routing]
        [--satellite SOURCE] [--terrain]

Checks: exactly one finished .zim and no .tmp left behind; the progress
file reached done == total; openZIM's mandatory metadata is present (Name,
Title, Description, Language, Creator, Publisher, Date, 48x48 PNG
Illustration) with the requested Name and Title; routing is present when
asked for. Satellite imagery: without --satellite, none, and the licence
never claims the non-commercial layer; with --satellite SOURCE, that source
in map-config.json, its licence in License, and the Flavour and tags that
label it (a non-commercial source: "satellite-nc", "non-commercial", and a
License that opens with the restriction). Terrain: tiles present when asked
for (--terrain), and the licence credits the Copernicus DEM exactly when the
ZIM has terrain.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

MANDATORY = ("Name", "Title", "Description", "Language", "Creator", "Publisher",
             "Date", "Illustration_48x48@1")


def satellite_problems(md: dict[str, bytes], cfg: dict[str, Any],
                       source: str | None) -> list[str]:
    """What is wrong with the satellite layer and its labelling."""
    lic = md.get("License", b"").decode()
    tags = md.get("Tags", b"").decode().split(";")
    flavour = md.get("Flavour", b"").decode()
    if not source:
        out = []
        if "NC-SA" in lic:
            out.append("License claims the non-commercial satellite layer")
        if cfg.get("hasSatellite"):
            out.append("map-config has satellite imagery nobody asked for")
        return out
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from streetzim import satellite_sources as ss
    src = ss.get(source)
    out: list[str] = []
    if not cfg.get("hasSatellite") or cfg.get("satelliteSource") != src.key:
        out.append(f"map-config satellite is {cfg.get('satelliteSource')!r}, want {src.key}")
    if cfg.get("satelliteNonCommercial") != src.noncommercial:
        out.append("map-config satelliteNonCommercial is wrong")
    if src.license_metadata not in lic:
        out.append(f"License does not name {src.license_metadata!r}")
    if flavour != ss.flavour(src):
        out.append(f"Flavour is {flavour!r}, want {ss.flavour(src)!r}")
    if not set(ss.tags(src)) <= set(tags):
        out.append(f"Tags {tags} lack {ss.tags(src)}")
    if src.noncommercial:
        if not lic.startswith("Non-commercial use only"):
            out.append("License does not open with the non-commercial notice")
        if "non-commercial" not in md.get("LongDescription", b"").decode():
            out.append("LongDescription does not say the imagery is non-commercial")
    elif "NC" in lic or ss.TAG_NONCOMMERCIAL in tags:
        out.append("a freely licensed build is labelled non-commercial")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    ap.add_argument("out")
    ap.add_argument("name")
    ap.add_argument("--title", required=True)
    ap.add_argument("--file", help="expected file name (default: <name>_<period>.zim)")
    ap.add_argument("--routing", action="store_true")
    ap.add_argument("--terrain", action="store_true")
    ap.add_argument("--stats", default="task_progress.json")
    ap.add_argument("--satellite", metavar="SOURCE",
                    help="expect this satellite source (streetzim/satellite_sources.py)")
    a = ap.parse_args()

    from libzim.reader import Archive
    from PIL import Image

    out = Path(a.out)
    problems: list[str] = []
    zims = sorted(out.glob("*.zim"))
    leftovers = sorted(p.name for p in out.glob("*.tmp"))
    if leftovers:
        problems.append(f"left behind: {leftovers}")
    want = [out / a.file] if a.file else [p for p in zims if p.name.startswith(a.name + "_")]
    if len(want) != 1 or not want[0].exists():
        print(f"FAIL: expected one ZIM for {a.name} in {out}, found {[p.name for p in zims]}")
        return 1
    zim = want[0]

    stats = json.loads((out / a.stats).read_text())
    if not (stats.get("total", 0) > 0 and stats.get("done") == stats["total"]):
        problems.append(f"progress file not complete: {stats}")

    arc = Archive(zim)
    keys = set(arc.metadata_keys)
    missing = [k for k in MANDATORY if k not in keys]
    if missing:
        problems.append(f"missing metadata: {missing}")
    md = {k: arc.get_metadata(k) for k in keys}
    if md.get("Name", b"").decode() != a.name:
        problems.append(f"Name is {md.get('Name')!r}, want {a.name!r}")
    if md.get("Title", b"").decode() != a.title:
        problems.append(f"Title is {md.get('Title')!r}, want {a.title!r}")
    ill = md.get("Illustration_48x48@1")
    if ill:
        img = Image.open(io.BytesIO(ill))
        if (img.format, img.size) != ("PNG", (48, 48)):
            problems.append(f"illustration is {img.format} {img.size}")
    cfg = json.loads(bytes(arc.get_entry_by_path("map-config.json").get_item().content))
    problems += satellite_problems(md, cfg, a.satellite)
    if a.routing and not cfg.get("hasRouting"):
        problems.append("routing was requested but map-config has no hasRouting")
    if a.terrain:
        if not cfg.get("hasTerrain"):
            problems.append("terrain was requested but map-config has no hasTerrain")
        elif not any(arc._get_entry_by_id(i).path.startswith("terrain/")  # pyright: ignore[reportPrivateUsage]
                     for i in range(arc.entry_count)):
            problems.append("map-config has hasTerrain but the ZIM has no terrain/ tiles")
    # "Copernicus DEM", not "Copernicus": EOX's satellite credit names
    # Copernicus Sentinel data.
    if bool(cfg.get("hasTerrain")) != (b"Copernicus DEM" in md.get("License", b"")):
        problems.append("License must credit Copernicus exactly when the ZIM has terrain")

    for p in problems:
        print(f"FAIL: {p}")
    if not problems:
        print(f"ok: {zim.name}: metadata, illustration, progress "
              f"({stats['done']}/{stats['total']})" + (", routing" if a.routing else "")
              + (", terrain" if a.terrain else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
