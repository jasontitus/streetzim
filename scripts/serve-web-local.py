#!/usr/bin/env python3
"""Minimal stand-in for Firebase Hosting's cleanUrls + trailingSlash
rewrites, so cloud/pwa_smoke_test.mjs sees the same URL shapes locally
that it does on streetzim.web.app (plain http.server 404s on
/drive/viewer/places/, which the Find page and the routing bootstrap
both fetch)."""
import http.server, os, sys, functools

ROOT = sys.argv[2]

class H(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = path.split('?', 1)[0].split('#', 1)[0]
        fs = os.path.normpath(os.path.join(ROOT, p.lstrip('/')))
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
    def log_message(self, *a):
        pass

http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
