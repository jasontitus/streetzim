#!/usr/bin/env python3
"""Minimal stand-in for Firebase Hosting's cleanUrls + trailingSlash
rewrites, so cloud/pwa_smoke_test.mjs sees the same URL shapes locally
that it does on streetzim.web.app (plain http.server 404s on
/drive/viewer/places/, which the Find page and the routing bootstrap
both fetch).

Sets HTTP/1.1 so connections are kept alive: the Find page issues hundreds
of chip requests and a fresh connection per request dominated the timing.

Also implements HTTP Range, which `SimpleHTTPRequestHandler` ignores
(it answers 200 with the whole body). Nothing in the product asks for a
range today — the viewer's ZIM reader takes a File/Blob and reads through
`.slice()`, and sw.js answers its virtual URLs from memory, so a
SERVE_LOG=1 run shows every request arriving with no Range header. This
is here so the harness matches Firebase Hosting and archive.org, both of
which serve 206, rather than to fix any observed failure. (The 12 GB
east-coast-us gate failure had two other causes: the smoke buffering the
whole ZIM via resp.blob(), and a routing cell-cache budget too small to
hold the route's working set.)
"""
import http.server, os, re, sys

ROOT = sys.argv[2]

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class H(http.server.SimpleHTTPRequestHandler):
    # Keep-alive matters here: the Find page issues hundreds of chip
    # requests and a new connection per request dominates the timing.
    protocol_version = "HTTP/1.1"

    def translate_path(self, path):
        p = path.split('?', 1)[0].split('#', 1)[0]
        fs = os.path.normpath(os.path.join(ROOT, p.lstrip('/')))
        # normpath collapses ../ but does not stop it escaping: a request for
        # /../../../etc/hostname resolved and was served. Loopback-only, but
        # the Range branch serves arbitrary files too, so contain it.
        if fs != ROOT and not fs.startswith(ROOT.rstrip(os.sep) + os.sep):
            return ROOT
        if os.path.isdir(fs):
            for cand in ("index.html",):
                if os.path.isfile(os.path.join(fs, cand)):
                    return os.path.join(fs, cand)
            # /drive/viewer/places/ → drive/viewer/places.html
            if os.path.isfile(fs.rstrip('/') + ".html"):
                return fs.rstrip('/') + ".html"
        if not os.path.exists(fs) and os.path.isfile(fs + ".html"):
            return fs + ".html"          # cleanUrls
        if not os.path.exists(fs) and fs.endswith(os.sep):
            alt = fs.rstrip(os.sep) + ".html"
            if os.path.isfile(alt):
                return alt
        return fs

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.isfile(path):
            return super().send_head()

        m = _RANGE_RE.match(rng.strip())
        if not m:
            return super().send_head()      # multi-range etc: serve whole
        size = os.path.getsize(path)
        first, last = m.group(1), m.group(2)
        if first == "":
            if last == "":
                return super().send_head()
            length = min(int(last), size)   # suffix range: last N bytes
            start = size - length
            end = size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
            if end >= size:
                end = size - 1
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        # do_HEAD calls send_head() too. Writing a body there would leave the
        # bytes sitting in the keep-alive connection, and the client would read
        # them as the start of the next response.
        if self.command == "HEAD":
            return None
        # copyfile() would send to EOF; cap it at the requested length.
        remaining = length
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        return None

    def end_headers(self):
        if "Accept-Ranges" not in self._headers_buffer_str():
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def _headers_buffer_str(self):
        return b"".join(getattr(self, "_headers_buffer", []) or []).decode(
            "latin-1", "replace")

    def log_message(self, fmt, *a):
        if os.environ.get("SERVE_LOG"):
            sys.stderr.write("%s %s Range=%s\n" % (
                self.command, self.path, self.headers.get("Range")))
            sys.stderr.flush()


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
