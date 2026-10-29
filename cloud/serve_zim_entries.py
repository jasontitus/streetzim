"""Serve a ZIM's entries over HTTP the way a Kiwix reader does, so a real
browser can run the viewer baked into the archive (not the copy on disk).

    python3 cloud/serve_zim_entries.py osm-monaco.zim --port 8899
    ZIM_ORIGIN=http://localhost:8899 node cloud/zim_viewer_smoke.mjs

`/` redirects to the main entry; `/<path>` returns that entry with its
MIME type (redirect entries are followed; HEAD works too); anything else
is a 404. Range requests are not needed by the viewer and are not
supported. Stdlib plus python-libzim only.

`--overlay DIR`: a path that is a file under DIR is served from there in
place of the ZIM's entry (or as one the ZIM lacks): the map as it would be
with another viewer or with the sign reader's models (tools/ocr_photos.py).
"""
from __future__ import annotations

import argparse
import http.server
import mimetypes
import os
import socketserver
import urllib.parse

from libzim.reader import Archive


OVERLAY_TYPES = {".js": "application/javascript", ".mjs": "application/javascript",
                 ".json": "application/json", ".wasm": "application/wasm", ".onnx": "application/octet-stream",
                 ".txt": "text/plain", ".html": "text/html"}


def overlay_file(overlay: str | None, path: str) -> str | None:
    """The file under ``overlay`` that serves ``path``, if there is one. A
    path with a ".." or an empty part is never looked up (the overlay's own
    symlinks, to models kept elsewhere, are followed)."""
    parts = path.split("/")
    if not overlay or not path or any(x in ("", ".", "..") for x in parts) or "\\" in path:
        return None
    f = os.path.join(overlay, *parts)
    return f if os.path.isfile(f) else None


def make_handler(arc: Archive, overlay: str | None = None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _serve(self, send_body: bool):
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).lstrip("/")
            if not path:
                self.send_response(302)
                self.send_header("Location", "/" + arc.main_entry.get_item().path)
                self.end_headers()
                return
            local = overlay_file(overlay, path)
            try:
                if local:
                    with open(local, "rb") as fh:
                        body = fh.read()
                    mimetype = (OVERLAY_TYPES.get(os.path.splitext(path)[1].lower())
                                or mimetypes.guess_type(path)[0] or "application/octet-stream")
                else:
                    item = arc.get_entry_by_path(path).get_item()
                    body, mimetype = bytes(item.content), item.mimetype
            except KeyError:
                self.send_error(404)
                return
            except Exception as exc:  # corrupt cluster etc.: say so, don't hang up
                self.send_error(500, explain=f"{type(exc).__name__}: {exc}")
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            self._serve(True)

        def do_HEAD(self):  # noqa: N802 (http.server API)
            self._serve(False)

        def log_message(self, fmt, *args):
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("zim")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--overlay", help="a folder whose files are served in place of the ZIM's entries")
    args = ap.parse_args()
    arc = Archive(args.zim)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((args.host, args.port), make_handler(arc, args.overlay)) as srv:
        print(f"serving {args.zim} on http://{args.host}:{args.port}/", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
