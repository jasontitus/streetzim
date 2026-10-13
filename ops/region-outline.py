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

Geofabrik serves some .poly files empty or as "none" (asia/japan.poly: 0
bytes since 2026-10-09; asia/japan/chubu.poly: "none"), while its index
(index-v1.json) still carries the same region's polygon. Such an outline is
taken from the index instead, and its cached copy says so on its first line.

Usage: region-outline.py [--outline-id <table id>] [--refresh] -- <id> <bbox>
(`--` because a western bbox such as -74.0,-34.0,... starts with a minus sign.)
Prints the outline's path. Exit 3 when the table has no row for the id.
STREETZIM_OUTLINE_BASE replaces https://download.geofabrik.de (tests).
"""
import argparse
import hashlib
import json
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


class NotWholePoly(SystemExit):
    """A download that is not a whole Osmosis .poly. `none` when Geofabrik
    serves no outline at all (an empty file, or "none"), rather than a
    download cut off: only then is its index asked instead."""

    def __init__(self, msg, none=False):
        super().__init__(msg)
        self.none = none


def _no_outline(data):
    lines = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
    return not lines or (lines[0] == "none" and lines[1:] in ([], ["END"]))


def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _save(data, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    part = f"{dst}.part.{os.getpid()}"
    with open(part, "wb") as fh:
        fh.write(data)
    os.replace(part, dst)
    return data


def fetch(url, dst):
    """Download url to dst (via a private .part + rename) and return its bytes."""
    data = _get(url, 120)
    if not _complete(data):
        raise NotWholePoly(f"{url}: not a whole Osmosis .poly", none=_no_outline(data))
    return _save(data, dst)


def poly_from_index(index, base, path):
    """The outline Geofabrik's index gives `path` (the feature whose pbf URL
    is <base>/<path>-latest.osm.pbf), as Osmosis .poly bytes, or None."""
    want = f"{base}/{path}-latest.osm.pbf"
    for feat in index.get("features", []) if isinstance(index, dict) else []:
        urls = (feat.get("properties") or {}).get("urls") or {}
        if urls.get("pbf") != want:
            continue
        geom = feat.get("geometry") or {}
        polys = {"Polygon": [geom.get("coordinates")],
                 "MultiPolygon": geom.get("coordinates")}.get(geom.get("type")) or []
        lines, n = [f"{path} (from {base}/index-v1.json)"], 0
        try:
            for poly in polys:
                for k, ring in enumerate(poly or []):
                    n += 1
                    lines.append(f"!{n}" if k else str(n))
                    lines += [f"   {float(pt[0])!r}   {float(pt[1])!r}" for pt in ring]
                    lines.append("END")
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        if not n:
            return None
        return ("\n".join(lines + ["END"]) + "\n").encode()
    return None


def fetch_from_index(base, path, dst, err):
    """The outline for `path` from Geofabrik's index, saved to dst; `err`
    (the failed .poly download) is raised when the index has none."""
    try:
        index = json.loads(_get(f"{base}/index-v1.json", 300))
    except (OSError, ValueError) as e:
        raise NotWholePoly(f"{err}; and no index-v1.json ({e})") from None
    data = poly_from_index(index, base, path)
    if data is None or not _complete(data):
        raise NotWholePoly(f"{err}; and index-v1.json has no outline for {path}")
    return _save(data, dst)


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
            try:
                data = fetcher(url, raw)
            except NotWholePoly as e:
                if not e.none:
                    raise
                data = fetch_from_index(base, path, raw, e)
        if data.split(b"\n", 1)[0].endswith(b"/index-v1.json)"):
            url = f"{base}/index-v1.json#{path}"
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
