"""Tiny LAN HTTP server that lists every *.zim in the streetzim repo and
serves them for download — so a phone on the same Wi-Fi can grab a
candidate ZIM without the USB shuffle.

Usage:
    python3 cloud/serve_zims.py              # port 8765, all interfaces
    python3 cloud/serve_zims.py --port 8080  # custom port

Then open http://macstudio.local:8765 on the phone. The page is a table:

    File                          Size    Modified
    osm-egypt-shipped.zim         1.4 GB  2026-04-24 09:58  [Download]
    osm-iran-shipped.zim          2.5 GB  2026-04-24 11:46  [Download]
    ...

Sorted by modification time (newest first). Clicking the download link
hands the file off with full HTTP range support, so large ZIMs resume
correctly if the phone drops the connection.

Design choices:
  * Stdlib only (http.server). No flask / uvicorn dependency.
  * Binds 0.0.0.0 so the Mac's mDNS name (macstudio.local) resolves.
  * Only serves *.zim files. Avoids accidentally exposing logs or
    build scripts to the LAN.
"""
from __future__ import annotations

import argparse
import datetime
import html
import http.server
import os
import socketserver
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def fmt_size(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


def scan() -> list[tuple[Path, int, float]]:
    out = []
    for p in ROOT.glob("osm-*.zim"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p, st.st_size, st.st_mtime))
    out.sort(key=lambda t: -t[2])  # newest first
    return out


def render_index() -> bytes:
    rows = scan()
    body = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>StreetZim candidates</title>",
        "<style>",
        " body{font:15px -apple-system,sans-serif;padding:12px;max-width:900px;margin:0 auto;}",
        " h1{font-size:18px;margin:8px 0 16px;}",
        " table{width:100%;border-collapse:collapse;}",
        " th,td{text-align:left;padding:10px 6px;border-bottom:1px solid #eee;}",
        " th{font-size:12px;text-transform:uppercase;color:#888;letter-spacing:.5px;}",
        " td.num{text-align:right;font-variant-numeric:tabular-nums;}",
        " a{color:#06c;text-decoration:none;} a:hover{text-decoration:underline;}",
        " .muted{color:#888;font-size:13px;}",
        " .fresh{background:#f0fff0;}",
        "</style></head><body>",
        f"<h1>StreetZim candidates <span class='muted'>({len(rows)} files)</span></h1>",
        "<table><thead><tr><th>File</th><th class='num'>Size</th>"
        "<th>Modified</th><th></th></tr></thead><tbody>",
    ]
    now = datetime.datetime.now().timestamp()
    for path, size, mtime in rows:
        name = path.name
        when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        # Highlight last-hour files so you can see the rollout as it lands.
        row_cls = " class='fresh'" if (now - mtime) < 3600 else ""
        href = "/" + urllib.parse.quote(name)
        body.append(
            f"<tr{row_cls}>"
            f"<td><a href='{href}'>{html.escape(name)}</a></td>"
            f"<td class='num'>{fmt_size(size)}</td>"
            f"<td>{when}</td>"
            f"<td><a href='{href}' download>Download</a></td>"
            f"</tr>"
        )
    body.append("</tbody></table></body></html>")
    return "\n".join(body).encode("utf-8")


class ZimOnlyHandler(http.server.SimpleHTTPRequestHandler):
    """Serves only the index page and *.zim files in ROOT — nothing
    else. Keeps the LAN surface small."""

    # Override so the directory is ROOT, not CWD.
    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        path = urllib.parse.unquote(path)
        path = path.lstrip("/")
        return str(ROOT / path)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = render_index()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Never cache the listing — rollouts add files during a
            # test session and we want the phone to see them.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        # Only *.zim files allowed; avoid accidentally exposing logs.
        name = urllib.parse.unquote(self.path.lstrip("/"))
        if not name.startswith("osm-") or not name.endswith(".zim"):
            self.send_error(404, "Not found")
            return
        # Defer to the stdlib handler for file + range handling.
        super().do_GET()

    def log_message(self, fmt, *args):
        # Short single-line access log to stderr.
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="Interface to bind (0.0.0.0 = all, for LAN). "
                         "Use 127.0.0.1 to stay local-only.")
    args = ap.parse_args()
    # Reuse address so Ctrl-C + restart doesn't sit in TIME_WAIT.
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(
        (args.bind, args.port), ZimOnlyHandler
    ) as srv:
        # Resolve the local hostname so we can print a phone-friendly URL.
        import socket
        host = socket.gethostname()
        if not host.endswith(".local"):
            host = host + ".local"
        print(f"serving {len(list(scan()))} ZIMs from {ROOT}")
        print(f"  http://{host}:{args.port}/")
        print(f"  http://{args.bind}:{args.port}/  (direct)")
        print("Ctrl-C to stop")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nshutdown")


if __name__ == "__main__":
    main()
