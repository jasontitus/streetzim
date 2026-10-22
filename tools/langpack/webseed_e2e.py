#!/usr/bin/env python3
"""End-to-end check of language torrents against the build worker, all on
127.0.0.1 (nothing is served beyond this machine).

    webseed_e2e.py SITE_RECIPES_DIR --files-dir DIR [--files-dir DIR ...]
                   [--node NODE] [--langs el,de] [--random 40]
                   [--libtorrent-python PY] [--keep]

SITE_RECIPES_DIR is a region's recipes folder after lang_torrents.py
(<L>.torrent, <L>.ws.json, ws-*.bin, region.json). The check starts:
  * an archive.org stand-in: /download/<item>/<file> redirects to
    /node/<item>/<file>, which answers Range with 206 from the local file of
    that name (found in --files-dir), like archive.org's storage nodes;
  * a site stand-in serving SITE_RECIPES_DIR as /build/recipes/<region>/;
  * the build worker (build-proxy/serve-local.mjs) pointed at both;
and then, for every torrent: fetches every piece through the worker exactly
as a BEP 19 webseed client does (one ranged GET per piece, at the URL the
torrent names with the origin swapped for the local worker), compares each
piece's SHA-1 with the torrent, and the whole stream's MD5 with the recipe's
file_md5; then --random multi-piece ranges. Per request it records the
worker's parts, subrequests and Node CPU time (SZ_STATS).

--libtorrent-python: a Python with the libtorrent module (a venv; nothing is
installed system-wide). It downloads each torrent webseed-only with a
session that listens on 127.0.0.1 only, has DHT, local discovery, UPnP and
NAT-PMP off, the trackers removed and an upload limit of 1 byte/s, removes
the torrent the moment it completes (never seeds), and checks the file's MD5.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack.lang_torrents import bdecode  # noqa: E402
from ops.cloud.build_torrent import bencode  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ranged_server(files: dict[str, str], site_dir: str, region: str, counter: dict):
    """One handler class for both stand-ins (archive.org and the site)."""
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send_file(self, path: str):
            size = os.path.getsize(path)
            rng = self.headers.get("Range")
            m = re.match(r"^bytes=(\d+)-(\d*)$", rng or "")
            if m:
                a = int(m.group(1))
                b = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
                if a >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            else:
                a, b = 0, size - 1
                self.send_response(200)
            self.send_header("Content-Length", str(b - a + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as fh:
                fh.seek(a)
                left = b - a + 1
                while left:
                    chunk = fh.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path).path
            if u.startswith("/download/"):
                counter["download"] += 1
                self.send_response(302)
                self.send_header("Location", "/node/" + u[len("/download/"):])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if u.startswith("/node/"):
                counter["node"] += 1
                p = files.get(u[len("/node/"):])
            elif u.startswith(f"/build/recipes/{region}/"):
                counter["site"] += 1
                name = u.rsplit("/", 1)[1]
                p = os.path.join(site_dir, name) if "/" not in name and not name.startswith(".") else None
            else:
                p = None
            if not p or not os.path.isfile(p):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_file(p)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fetch(host: str, port: int, path: str, start: int, end: int) -> tuple[int, dict, bytes]:
    c = http.client.HTTPConnection(host, port, timeout=300)
    c.request("GET", path, headers={"Range": f"bytes={start}-{end}"})
    r = c.getresponse()
    body = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, body


def libtorrent_download(py: str, torrent_path: str, local_ws: str, out_dir: str, timeout: int) -> dict:
    """Webseed-only download in a child Python with libtorrent; never seeds."""
    t = bdecode(open(torrent_path, "rb").read())
    t.pop(b"announce", None)
    t.pop(b"announce-list", None)
    t[b"url-list"] = [local_ws.encode()]
    tmp_t = os.path.join(out_dir, "local.torrent")
    with open(tmp_t, "wb") as fh:
        fh.write(bencode(t))
    script = r"""
import json, sys, time, libtorrent as lt
tp, save, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
s = lt.session({'listen_interfaces': '127.0.0.1:0', 'enable_dht': False, 'enable_lsd': False,
                'enable_upnp': False, 'enable_natpmp': False, 'upload_rate_limit': 1,
                'alert_mask': lt.alert.category_t.error_notification | lt.alert.category_t.status_notification})
p = lt.add_torrent_params()
p.ti = lt.torrent_info(tp)
p.save_path = save
h = s.add_torrent(p)
t0 = time.time()
errors = []
while True:
    st = h.status()
    for a in s.pop_alerts():
        if a.category() & lt.alert.category_t.error_notification:
            errors.append(a.message())
    if st.is_finished or st.is_seeding:
        break
    if time.time() - t0 > timeout:
        break
    time.sleep(0.5)
st = h.status()
s.remove_torrent(h)        # never seed
print(json.dumps({"done": bool(st.is_finished or st.is_seeding), "s": round(time.time() - t0, 1),
                  "downloaded": st.total_payload_download, "uploaded": st.total_payload_upload,
                  "peers": st.num_peers, "web_seeds": len(h.url_seeds()) if hasattr(h, 'url_seeds') else None,
                  "errors": errors[:5]}))
"""
    r = subprocess.run([py, "-c", script, tmp_t, out_dir, str(timeout)], capture_output=True, text=True,
                       timeout=timeout + 60)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"done": False, "error": (r.stderr or r.stdout)[-800:]}
    name = t[b"info"][b"name"].decode()
    f = os.path.join(out_dir, name)
    if res.get("done") and os.path.exists(f):
        h = hashlib.md5(usedforsecurity=False)
        with open(f, "rb") as fh:
            for b in iter(lambda: fh.read(4 << 20), b""):
                h.update(b)
        res["md5"] = h.hexdigest()
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site_dir")
    ap.add_argument("--files-dir", action="append", required=True)
    ap.add_argument("--node", default=os.environ.get("NODE", "node"))
    ap.add_argument("--langs")
    ap.add_argument("--random", type=int, default=40)
    ap.add_argument("--libtorrent-python")
    ap.add_argument("--lt-timeout", type=int, default=1800)
    ap.add_argument("--keep", action="store_true", help="keep libtorrent's downloads")
    a = ap.parse_args(argv)
    region = json.load(open(os.path.join(a.site_dir, "region.json"), encoding="utf-8"))
    rid = region["id"]
    langs = [m["lang"] for m in region["menu"] if m.get("torrent")]
    if a.langs:
        langs = [x for x in langs if x in a.langs.split(",")]
    by_name = {}
    for d in a.files_dir:
        for f in os.listdir(d):
            if f.endswith(".zim"):
                by_name.setdefault(f, os.path.join(d, f))
    files = {}
    for L in langs:
        ix = json.load(open(os.path.join(a.site_dir, f"{L}.ws.json")))
        for src in ix["sources"].values():
            if src["file"] not in by_name:
                print(f"no local file {src['file']}", file=sys.stderr)
                return 1
            files[f"{src['item']}/{src['file']}"] = by_name[src["file"]]
    counter = {"download": 0, "node": 0, "site": 0}
    srv = ranged_server(files, a.site_dir, rid, counter)
    sport = srv.server_address[1]
    wport = free_port()
    env = {**os.environ, "SITE_ORIGIN": f"http://127.0.0.1:{sport}",
           "UPSTREAM": f"http://127.0.0.1:{sport}/download/", "SZ_STATS": "1"}
    wlog = tempfile.NamedTemporaryFile("w+", suffix=".worker.log", delete=False)
    worker = subprocess.Popen([a.node, os.path.join(ROOT, "build-proxy", "serve-local.mjs"), str(wport)],
                              env=env, stdout=wlog, stderr=subprocess.STDOUT)
    rc = 0
    report = {"langs": {}}
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", wport), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        for L in langs:
            t = bdecode(open(os.path.join(a.site_dir, f"{L}.torrent"), "rb").read())
            info = t[b"info"]
            plen, size = info[b"piece length"], info[b"length"]
            ws = t[b"url-list"][0].decode()
            path = urllib.parse.urlparse(ws).path
            recipe = json.load(open(os.path.join(a.site_dir, f"{L}.json")))
            md5 = hashlib.md5(usedforsecurity=False)
            n = len(info[b"pieces"]) // 20
            bad = []
            t0 = time.time()
            for k in range(n):
                s0, s1 = k * plen, min(size, (k + 1) * plen) - 1
                st, hdrs, body = fetch("127.0.0.1", wport, path, s0, s1)
                if (st != 206 or hdrs.get("content-range") != f"bytes {s0}-{s1}/{size}"
                        or hashlib.sha1(body).digest() != info[b"pieces"][20 * k:20 * k + 20]):
                    bad.append(k)
                md5.update(body)
            dt = time.time() - t0
            rnd = random.Random(L)
            rbad = 0
            for _ in range(a.random):
                s0 = rnd.randrange(size)
                s1 = min(size - 1, s0 + rnd.randrange(8 * plen))
                st, hdrs, body = fetch("127.0.0.1", wport, path, s0, s1)
                # check against the pieces: hash the aligned pieces the range covers
                k0, k1 = s0 // plen, s1 // plen
                if k1 - k0 >= 2:
                    inner0 = (k0 + 1) * plen - s0
                    for k in range(k0 + 1, k1):
                        seg = body[inner0 + (k - k0 - 1) * plen: inner0 + (k - k0) * plen]
                        if hashlib.sha1(seg).digest() != info[b"pieces"][20 * k:20 * k + 20]:
                            rbad += 1
                if st != 206 or len(body) != s1 - s0 + 1:
                    rbad += 1
            ok = not bad and md5.hexdigest() == recipe.get("file_md5") and not rbad
            rc |= 0 if ok else 1
            row = {"pieces": n, "piece_length": plen, "bad_pieces": bad[:10], "md5_ok": md5.hexdigest() == recipe.get("file_md5"),
                   "random_ranges": a.random, "random_bad": rbad, "s": round(dt, 1),
                   "MB_per_s": round(size / 1e6 / max(dt, 0.001), 1), "ok": ok}
            if a.libtorrent_python:
                d = tempfile.mkdtemp(prefix=f"lt-{L}-", dir=os.environ.get("TMPDIR"))
                lres = libtorrent_download(a.libtorrent_python, os.path.join(a.site_dir, f"{L}.torrent"),
                                           f"http://127.0.0.1:{wport}{path}", d, a.lt_timeout)
                lres["md5_ok"] = lres.get("md5") == recipe.get("file_md5")
                row["libtorrent"] = lres
                rc |= 0 if lres["md5_ok"] and lres.get("uploaded", 1) == 0 else 1
                if not a.keep:
                    shutil.rmtree(d, ignore_errors=True)
            report["langs"][L] = row
            print(json.dumps({L: row}), flush=True)
    finally:
        worker.terminate()
        worker.wait(timeout=10)
        srv.shutdown()
    # worker stats: per /merged/ request
    wlog.seek(0)
    ev = [json.loads(x) for x in wlog.read().splitlines() if x.startswith("{")]
    merged = [e for e in ev if "sub" in e]
    cpu = [e["cpu_us"] for e in ev if "cpu_us" in e]
    if merged:
        subs = sorted(e["sub"] for e in merged)
        parts = sorted(e["parts"] for e in merged)
        report["worker"] = {"requests": len(merged), "errors": sum(1 for e in merged if "error" in e),
                            "subrequests_max": subs[-1], "subrequests_p50": subs[len(subs) // 2],
                            "parts_max": parts[-1], "parts_p50": parts[len(parts) // 2],
                            "node_cpu_us_p50": sorted(cpu)[len(cpu) // 2] if cpu else None,
                            "node_cpu_us_max": max(cpu) if cpu else None,
                            "upstream": counter}
    print(json.dumps({"summary": report.get("worker"), "ok": rc == 0}), flush=True)
    os.unlink(wlog.name)
    return rc


if __name__ == "__main__":
    sys.exit(main())
