#!/usr/bin/env python3
"""After deploying the build worker or publishing a region: check its
language torrents against the live webseed (read only; GET and HEAD).

    webseed_smoke.py --site https://streetzim.web.app --region greece
                     [--webseed https://streetzim-build-proxy.<acct>.workers.dev]
                     [--langs el,de] [--pieces 6] [--via ORIGIN]

For every menu entry of /build/recipes/<region>/region.json that has a
torrent: the torrent's webseed origin is --webseed (when given); a HEAD of
the webseed URL answers 200 with the torrent's length and Accept-Ranges;
the first, the last and --pieces spread pieces, and one range across every
part boundary of the entry's segment index (<L>.ws.json), come back as 206
with the right Content-Range and hash as the torrent says. --via fetches the
webseed URLs through another origin with the same path (local tests).
One JSON line per entry; exit 0 when every check passes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack.lang_torrents import bdecode  # noqa: E402

UA = {"User-Agent": "streetzim-webseed-smoke/1"}


def get(url: str, rng: tuple[int, int] | None = None, method: str = "GET", timeout: int = 120):
    h = dict(UA)
    if rng:
        h["Range"] = f"bytes={rng[0]}-{rng[1]}"
    req = urllib.request.Request(url, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, (r.read() if method == "GET" else b"")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, b""


def check_entry(site: str, region: str, m: dict, webseed: str | None, pieces: int, via: str | None,
                timeout: int) -> dict:
    fails: list[str] = []
    L = m["lang"]
    st, _, raw = get(f"{site}/build/{m['torrent']}", timeout=timeout)
    if st != 200:
        return {"lang": L, "ok": False, "fails": [f"torrent: HTTP {st}"]}
    t = bdecode(raw)
    info = t[b"info"]
    size, plen = info[b"length"], info[b"piece length"]
    ws = t[b"url-list"][0].decode()
    u = urllib.parse.urlparse(ws)
    origin = f"{u.scheme}://{u.netloc}"
    if webseed and origin != webseed.rstrip("/"):
        fails.append(f"webseed origin {origin} != {webseed}")
    url = (via.rstrip("/") + u.path) if via else ws
    st, h, _ = get(url, method="HEAD", timeout=timeout)
    if st != 200 or h.get("content-length") != str(size) or h.get("accept-ranges") != "bytes":
        fails.append(f"HEAD: HTTP {st}, length {h.get('content-length')}, ranges {h.get('accept-ranges')}")
    n = len(info[b"pieces"]) // 20
    ks = sorted({0, n - 1, *[(n - 1) * i // max(1, pieces - 1) for i in range(pieces)]})
    for k in ks:
        a, b = k * plen, min(size, (k + 1) * plen) - 1
        st, h, body = get(url, (a, b), timeout=timeout)
        if st != 206 or h.get("content-range") != f"bytes {a}-{b}/{size}":
            fails.append(f"piece {k}: HTTP {st} {h.get('content-range')}")
        elif hashlib.sha1(body).digest() != info[b"pieces"][20 * k:20 * k + 20]:
            fails.append(f"piece {k}: wrong bytes")
    boundaries = 0
    st, _, raw = get(f"{site}/build/{m['torrent'][:-len('.torrent')]}.ws.json", timeout=timeout)
    if st == 200:
        ix = json.loads(raw)
        for p in ix["parts"][1:]:
            a, b = max(0, p[0] - 64), min(size - 1, p[0] + 63)
            st, h, body = get(url, (a, b), timeout=timeout)
            k0, k1 = a // plen, b // plen
            ok = st == 206 and len(body) == b - a + 1
            if ok and k0 == k1 == (size - 1) // plen and a == k0 * plen:      # the last piece, whole
                ok = hashlib.sha1(body).digest() == info[b"pieces"][20 * k0:20 * k0 + 20]
            if not ok:
                fails.append(f"part boundary at {p[0]}: HTTP {st}, {len(body)} bytes")
            boundaries += 1
    else:
        fails.append(f"index: HTTP {st}")
    return {"lang": L, "ok": not fails, "pieces_checked": len(ks), "boundaries_checked": boundaries,
            "fails": fails[:10]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--webseed")
    ap.add_argument("--langs")
    ap.add_argument("--pieces", type=int, default=6)
    ap.add_argument("--via")
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args(argv)
    site = a.site.rstrip("/")
    st, _, raw = get(f"{site}/build/recipes/{a.region}/region.json", timeout=a.timeout)
    if st != 200:
        print(json.dumps({"region": a.region, "ok": False, "fails": [f"region.json: HTTP {st}"]}), flush=True)
        return 1
    region = json.loads(raw)
    menu = [m for m in region["menu"] if m.get("torrent") and (not a.langs or m["lang"] in a.langs.split(","))]
    if not menu:
        print(json.dumps({"region": a.region, "ok": False, "fails": ["no entry with a torrent"]}), flush=True)
        return 1
    rc = 0
    for m in menu:
        try:
            row = check_entry(site, a.region, m, a.webseed, a.pieces, a.via, a.timeout)
        except Exception as e:  # noqa: BLE001 (one line per entry, whatever went wrong)
            row = {"lang": m["lang"], "ok": False, "fails": [repr(e)]}
        rc |= 0 if row["ok"] else 1
        print(json.dumps(row), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
