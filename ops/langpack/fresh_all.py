#!/usr/bin/env python3
"""Today's viewer in every live map's download (docs/fresh-viewer.md, "Rollout").

    ops/langpack/fresh_all.py [--regions a,b] [--maps DIR] [--site DIR]
                              [--preview DIR] [--deploy] [--dry-run]

For each region of cloud/regions.tsv (or --regions, in that order): its live
file on archive.org (item streetzim-<region>, the newest dated ZIM, as
web/generate.py picks it) and that file's MD5 and ETag; then
tools/langpack/fresh_viewer.py with the local copy in MAPS (its gates
included) into SITE/<region>-viewer/, copied to PREVIEW/<region>-viewer/ (the
preview channel the build worker reads). A region whose fresh.json already
names that live file and today's viewer code is skipped, as is one without a
local copy (said so). --deploy then deploys the preview channel and, under
the site-deploy lock, the live site (web/generate.py, firebase deploy), as
ops/langpack/pack_queue.sh does. Exit 0 when every region is done or
skipped for being current, 1 when any failed or had no local copy.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from cloud.viewer_slots import viewer_code  # noqa: E402

DATED = re.compile(r"-(\d{4}-\d{2}(?:-\d{2})?[a-z]?)\.zim$")
WEBSEED = "https://streetzim-build-proxy.tiltastech.workers.dev"
PREVIEW = os.environ.get("PREVIEW_DIR") or "/storage/experiments/build-preview/streetzim"   # pack_queue.sh's
MAPS = os.environ.get("MAPS_DIR") or "/storage/streetzim"


def say(*a: object) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def regions(path: str) -> list[str]:
    out = []
    for line in open(path, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        out.append(line.split("\t", 1)[0].strip())
    return out


def item_meta(item: str, meta_dir: str | None) -> dict | None:
    """archive.org's metadata for ITEM (META_DIR/<item>.json in tests)."""
    if meta_dir:
        p = os.path.join(meta_dir, f"{item}.json")
        return json.load(open(p)) if os.path.exists(p) else None
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"https://archive.org/metadata/{item}",
                                         headers={"User-Agent": "streetzim-fresh-all/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 (network: retried, then reported)
            if attempt == 2:
                say(f"{item}: archive.org metadata failed: {e}")
            time.sleep(2 ** attempt)
    return None


def live_file(meta: dict) -> dict | None:
    """The item's live ZIM as web/generate.py picks it: dated names first,
    the newest date (a same-day re-roll's letter after it), then size."""
    def size(f: dict) -> int:
        try:
            return int(f.get("size") or 0)
        except (TypeError, ValueError):
            return 0

    def key(f: dict) -> tuple:
        m = DATED.search(f.get("name", ""))
        return (1, m.group(1), size(f)) if m else (0, "", size(f))
    zims = [f for f in meta.get("files") or [] if isinstance(f, dict) and str(f.get("name", "")).endswith(".zim")
            and "/" not in f["name"]]
    return max(zims, key=key) if zims else None


def map_language(zim: str) -> str:
    from libzim.reader import Archive
    try:
        cfg = json.loads(bytes(Archive(zim).get_entry_by_path("map-config.json").get_item().content))
        return str(cfg.get("language") or "en")
    except (KeyError, RuntimeError, ValueError):
        return "en"


def current(site_dir: str, file: str, md5: str, code: str) -> bool:
    try:
        fr = json.load(open(os.path.join(site_dir, "fresh.json")))
    except (OSError, ValueError):
        return False
    b = fr.get("base") or {}
    return (b.get("file") == file and str(b.get("md5", "")).lower() == md5.lower() and fr.get("viewer_code") == code
            and os.path.isfile(os.path.join(site_dir, f"{fr.get('lang')}.ws.json")))


def deploy(preview: str, prod: str, lock: str, firebase: str) -> bool:
    ok = True
    say(f"deploy: the preview channel (from {preview})")
    r = subprocess.run([firebase, "hosting:channel:deploy", "swiss-languages", "--expires", "30d", "--project",
                        "streetzim"], cwd=preview)
    ok &= r.returncode == 0
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "a") as fh:
        say(f"deploy: waiting for the site-deploy lock {lock}")
        fcntl.flock(fh, fcntl.LOCK_EX)
        r = subprocess.run([sys.executable, os.path.join(prod, "web", "generate.py")], cwd=prod)
        if r.returncode == 0:
            r = subprocess.run([firebase, "deploy", "--only", "hosting", "--project", "streetzim"], cwd=prod)
        ok &= r.returncode == 0
    say("deploy:", "done" if ok else "FAILED")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--regions", help="comma-separated region ids, in this order (default: cloud/regions.tsv)")
    ap.add_argument("--maps", default=MAPS, help="the local copies of the live files")
    ap.add_argument("--site", default=os.path.join(ROOT, "web", "build", "recipes"))
    ap.add_argument("--preview", default=os.path.join(PREVIEW, "web", "build", "recipes"), help="'' for none")
    ap.add_argument("--work", help="where the composed files are made (default MAPS/langpack-work/fresh-viewer)")
    ap.add_argument("--webseed", default=WEBSEED)
    ap.add_argument("--meta-dir", help=argparse.SUPPRESS)               # tests: <item>.json instead of archive.org
    ap.add_argument("--fresh-tool", default=os.path.join(ROOT, "tools", "langpack", "fresh_viewer.py"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--deploy", action="store_true", help="deploy the preview channel and the live site at the end")
    ap.add_argument("--dry-run", action="store_true", help="say what would be made, make nothing")
    a = ap.parse_args(argv)
    code = viewer_code(open(os.path.join(ROOT, "resources", "viewer", "index.html"), "rb").read())
    if not code:
        say("no viewer code (SZ_VIEWER_BUILD) in resources/viewer/index.html: build the viewer first")
        return 1
    ids = a.regions.split(",") if a.regions else regions(os.path.join(ROOT, "cloud", "regions.tsv"))
    work = a.work or os.path.join(a.maps, "langpack-work", "fresh-viewer")
    made, failed, missing = [], [], []
    for rid in ids:
        if not re.fullmatch(r"[a-z0-9-]+", rid):
            say(f"{rid!r}: not a region id; skipped"); continue
        item = f"streetzim-{rid}"
        meta = item_meta(item, a.meta_dir)
        f = live_file(meta) if meta else None
        if not f or not f.get("md5"):
            say(f"{rid}: no live ZIM on archive.org ({item}); skipped"); continue
        name, md5 = f["name"], str(f["md5"]).lower()
        try:
            etag = '"%x-%x"' % (int(f["mtime"]), int(f["size"]))
        except (KeyError, TypeError, ValueError):
            say(f"{rid}: {name} has no mtime/size in the listing; skipped"); failed.append(rid); continue
        site_dir = os.path.join(a.site, f"{rid}-viewer")
        if current(site_dir, name, md5, code):
            say(f"{rid}: {name} already has viewer {code}"); continue
        local = os.path.join(a.maps, name)
        if not os.path.isfile(local) or os.path.getsize(local) != int(f["size"]):
            say(f"{rid}: no local copy of {name} ({int(f['size']) / 1e9:.2f} GB) in {a.maps}"); missing.append(rid); continue
        lang = map_language(local)
        say(f"{rid}: {name} ({int(f['size']) / 1e9:.2f} GB, {lang}) -> viewer {code}")
        if a.dry_run:
            continue
        r = subprocess.run([sys.executable, a.fresh_tool, rid, local, "--item", item, "--lang", lang, "--out", site_dir,
                            "--webseed", a.webseed, "--md5", md5, "--etag", etag, "--work", work],
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if r.returncode:
            say(f"{rid}: fresh_viewer.py rc={r.returncode}"); failed.append(rid); continue
        if a.preview:
            dst = os.path.join(a.preview, f"{rid}-viewer")
            tmp = dst + ".new"
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(site_dir, tmp)
            old = dst + ".old"
            shutil.rmtree(old, ignore_errors=True)
            if os.path.exists(dst):
                os.rename(dst, old)
            os.rename(tmp, dst)
            shutil.rmtree(old, ignore_errors=True)
        made.append(rid)
        say(f"{rid}: done")
    say(f"made {len(made)}: {' '.join(made) or '-'}; failed {len(failed)}: {' '.join(failed) or '-'}; "
        f"no local copy {len(missing)}: {' '.join(missing) or '-'}")
    if a.deploy and made and not a.dry_run:
        prod = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(a.site))))
        preview_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(a.preview)))) if a.preview else None
        if preview_root and not deploy(preview_root, prod, os.path.join(prod, "tmp", ".site-deploy.lock"),
                                       os.environ.get("FIREBASE", "firebase")):
            return 1
    return 1 if failed or missing else 0


if __name__ == "__main__":
    sys.exit(main())
