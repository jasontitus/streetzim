#!/usr/bin/env python3
"""The border outline a region's build is cut to (--clip-poly).

cloud/region-outlines.tsv names one or more Geofabrik outlines per region id
("europe+asia/armenia+asia/azerbaijan": Geofabrik files those two under
Asia). Each is fetched once into world-data/outlines/<path>.poly, keyed by
its path, so changing a row fetches the new outline. They are joined and cut
to the build's box (streetzim.clip.boxed_outline: Geofabrik splits Russia's
and the US's outlines at the antimeridian, which --clip-poly refuses as they
come), and written to world-data/regions/<id>.clip.poly with two sidecars:
  .bbox    the registry bbox it was cut for
  .source  one line per outline: its URL and sha256
build-region-fast.sh (ops/region-clip.sh) runs this before every listed
build; the cut takes about a second, so an outline is never stale.

Usage: region-outline.py [--outline-id <table id>] [--refresh] -- <id> <bbox>
(`--` because a western bbox such as -74.0,-34.0,... starts with a minus sign.)
Prints the outline's path. Exit 3 when the table has no row for the id.
STREETZIM_OUTLINE_BASE replaces https://download.geofabrik.de (tests).
"""
import argparse
import hashlib
import os
import sys
import urllib.request


def _streetzim_root():
    """The streetzim checkout (builder, data, ZIMs): $STREETZIM_ROOT, else the
    nearest directory above this file that holds create_osm_zim.py. (This file
    lives in ops/ and usually runs through a symlink at its old path.)"""
    env = os.environ.get("STREETZIM_ROOT")
    if env:
        return os.path.abspath(env)
    d = os.path.dirname(os.path.realpath(__file__))
    while d != os.path.dirname(d):
        if os.path.isfile(os.path.join(d, "create_osm_zim.py")):
            return d
        d = os.path.dirname(d)
    raise SystemExit("cannot find the streetzim checkout; set STREETZIM_ROOT")


ROOT = _streetzim_root()
# The code is this file's own checkout (ROOT may point at other data).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
GEOFABRIK = "https://download.geofabrik.de"


def outline_paths(table, rid):
    """The Geofabrik paths (e.g. ["north-america/us"]) the table names for rid."""
    with open(table, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0] == rid:
                return [p.strip() for p in parts[1].split("+") if p.strip()]
    return None


def _complete(data):
    """Whether data is a whole .poly: its last two lines are END (the last
    ring's and the file's). A file cut off after an inner ring is not."""
    lines = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
    return len(lines) >= 3 and lines[-1] == "END" and lines[-2] == "END"


def fetch(url, dst):
    """Download url to dst (via a private .part + rename) and return its bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    if not _complete(data):
        raise SystemExit(f"{url}: not a whole Osmosis .poly")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    part = f"{dst}.part.{os.getpid()}"
    with open(part, "wb") as fh:
        fh.write(data)
    os.replace(part, dst)
    return data


def make(rid, bbox_text, *, outline_id=None, refresh=False, root=ROOT,
         table=None, fetcher=fetch, base=None):
    """Write world-data/regions/<rid>.clip.poly (+ .bbox, .source); return
    its path, or None when the table lists no outline for outline_id."""
    from streetzim.clip import _write_poly, boxed_outline
    table = table or os.path.join(root, "cloud", "region-outlines.tsv")
    base = base or os.environ.get("STREETZIM_OUTLINE_BASE") or GEOFABRIK
    paths = outline_paths(table, outline_id or rid)
    if not paths:
        return None
    texts, source = [], []
    for path in paths:
        url = f"{base}/{path}.poly"
        raw = os.path.join(root, "world-data", "outlines", path.replace("/", "__") + ".poly")
        data = None
        if not refresh and os.path.isfile(raw):
            with open(raw, "rb") as fh:
                data = fh.read()
            if not _complete(data):
                data = None
        if data is None:
            data = fetcher(url, raw)
        texts.append(data.decode("utf-8"))
        source.append(f"{url}\tsha256={hashlib.sha256(data).hexdigest()}")
    bbox = tuple(float(v) for v in bbox_text.split(","))
    geom = boxed_outline(texts, bbox)
    out = os.path.join(root, "world-data", "regions", f"{rid}.clip.poly")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = f"{out}.part.{os.getpid()}"
    _write_poly(geom, tmp)
    os.replace(tmp, out)
    for ext, text in ((".bbox", bbox_text + "\n"), (".source", "\n".join(source) + "\n")):
        with open(f"{tmp}{ext}", "w") as fh:
            fh.write(text)
        os.replace(f"{tmp}{ext}", out + ext)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("id")
    ap.add_argument("bbox")
    ap.add_argument("--outline-id", help="the table row to use (a variant's parent)")
    ap.add_argument("--refresh", action="store_true", help="fetch the outlines again")
    a = ap.parse_args(argv)
    out = make(a.id, a.bbox, outline_id=a.outline_id, refresh=a.refresh)
    if out is None:
        print(f"no outline for {a.outline_id or a.id} in cloud/region-outlines.tsv", file=sys.stderr)
        return 3
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
