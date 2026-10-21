#!/usr/bin/env python3
"""Pre-deploy check of builder-page site files (review r2 nit).

    site_check.py SITE_DIR --base BASE.zim --packs-dir DIR
        [--verdicts DIR] [--gated DIR] [--ia-cache DIR] [--ia-fetch] [--probe-urls]
        [--recipes-dir DIR] [--proxy URL]

SITE_DIR holds recipes/{index,region,refused,<L>}.json (or, with
--recipes-dir, that folder: recipes/<region>/ on the production site) and
optionally region-base-url.json; the entries' "recipe" paths are relative
to SITE_DIR. URLs may start "{proxy}" (the builder page puts its range
proxy there): --proxy says which one the probes use (default: the one in
web/drive/preview-config.js). Read-only: nothing is written except, with --ia-fetch,
archive.org metadata JSON into --ia-cache (a plain GET of
https://archive.org/metadata/<item>). Exit 0 only when every check passes.

Per region (index.json; region.json must equal it):
  * base.file/size/md5 == the local base; base.download and base.url end in
    /<item>/<base.file>; the item lists that file with the local size + file md5.
  * probe.url names a local pack of probe.size bytes, listed on archive.org.
  * region-base-url.json (if present) == base.url.
Per menu entry:
  * the site recipe == the gated recipe ($LANGPACK_WORK/recipes/<tag>/<L>.json) except
    pack URLs; index md5/size/name/file_name == the recipe; gate_verdict path
    is this entry's verdict; download_bytes / packs[] bytes as make_site
    computes them.
  * the verdict is all_pass and is for this recipe (md5, uuid, size, file_md5)
    and its pack md5s == the local packs.
  * recipe.base file/size/uuid == the local base; chunk SHA-256s == the local
    base's; segment geometry adds up to recipe.size; every inline segment's
    SHA-256 matches its data.
  * every pack: local file, size, header UUID, chunk SHA-256s, a passing
    pack gate for these bytes; the URL ends in /<item>/<file>, the item lists
    the file with the same size and md5.
  * with --probe-urls: a Range GET of the first and last 64 KiB of every
    distinct URL equals the local bytes.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack import gatelib  # noqa: E402
from tools.langpack.make_site import touched_bytes  # noqa: E402
from tools.langpack.zimfmt import ZimReader  # noqa: E402

UA = {"User-Agent": "streetzim-site-check/1"}


def chunks(path: str, C: int) -> list[str]:
    out = []
    with open(path, "rb") as fh:
        while b := fh.read(C):
            out.append(hashlib.sha256(b).hexdigest())
    return out


class IA:
    def __init__(self, cache: str | None, fetch: bool):
        self.cache, self.fetch, self.items = cache, fetch, {}

    def files(self, item: str) -> dict[str, tuple[int, str]] | None:
        if item not in self.items:
            d = None
            path = os.path.join(self.cache, f"ia-{item}.json") if self.cache else None
            if self.fetch:
                req = urllib.request.Request(f"https://archive.org/metadata/{item}", headers=UA)
                raw = urllib.request.urlopen(req, timeout=60).read()
                d = json.loads(raw)
                if path and self.cache:
                    os.makedirs(self.cache, exist_ok=True)
                    open(path, "wb").write(raw)
            elif path and os.path.exists(path):
                d = json.load(open(path))
            self.items[item] = None if not d or "files" not in d else {
                x["name"]: (int(x["size"]), x["md5"]) for x in d["files"] if "size" in x and "md5" in x}
        return self.items[item]


def configured_proxy() -> str:
    """window.STREETZIM_PREVIEW_PROXY in web/drive/preview-config.js, or ""."""
    import re
    try:
        src = open(os.path.join(ROOT, "web", "drive", "preview-config.js"), encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r"STREETZIM_PREVIEW_PROXY\s*=\s*(['\"])(.*?)\1", src)
    return m.group(2).strip() if m else ""


def url_item_file(url: str) -> tuple[str, str]:
    parts = url.split("?")[0].rstrip("/").split("/")
    return parts[-2], parts[-1]


def range_bytes(url: str, start: int, n: int) -> bytes:
    req = urllib.request.Request(url, headers=dict(UA, Range=f"bytes={start}-{start + n - 1}"))
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read(n + 1)
        if r.status == 200 and len(body) > n:  # server ignored Range
            return b""
        return body


def local_bytes(path: str, start: int, n: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(start)
        return fh.read(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--base", required=True)
    ap.add_argument("--packs-dir", required=True)
    ap.add_argument("--verdicts")
    ap.add_argument("--gated")
    ap.add_argument("--ia-cache")
    ap.add_argument("--ia-fetch", action="store_true")
    ap.add_argument("--probe-urls", action="store_true")
    ap.add_argument("--recipes-dir", help="index/region/refused.json here (default SITE/recipes)")
    ap.add_argument("--proxy", help="what {proxy} stands for in URLs (default: web/drive/preview-config.js)")
    a = ap.parse_args()
    tag = os.path.basename(os.path.realpath(a.base))[:-4]
    vdir = a.verdicts or os.path.join(gatelib.work_dir(), "verdicts", tag)
    gdir = a.gated or os.path.join(gatelib.work_dir(), "recipes", tag)
    rdir = a.recipes_dir or os.path.join(a.site, "recipes")
    proxy = (a.proxy if a.proxy is not None else configured_proxy()).rstrip("/")
    ia = IA(a.ia_cache, a.ia_fetch)
    fails: list[str] = []
    nchecks = 0

    def need(ok, what):
        nonlocal nchecks
        nchecks += 1
        if not ok:
            fails.append(what)
        return ok

    def ia_listed(url, local, label):
        item, fn = url_item_file(url)
        need(fn == os.path.basename(local), f"{label}: URL file {fn} != {os.path.basename(local)}")
        lst = ia.files(item)
        if need(lst is not None, f"{label}: no archive.org metadata for item {item}") and lst is not None:
            got = lst.get(fn)
            need(got == (os.path.getsize(local), gatelib.md5_file(local)),
                 f"{label}: archive.org {item}/{fn} = {got}, local = {(os.path.getsize(local), gatelib.md5_file(local))}")

    urls: dict[str, str] = {}
    idx = json.load(open(os.path.join(rdir, "index.json")))
    reg_file = json.load(open(os.path.join(rdir, "region.json")))
    need(len(idx["regions"]) == 1 and idx["regions"][0] == reg_file, "region.json != index.json regions[0]")
    refused = json.load(open(os.path.join(rdir, "refused.json")))
    region = idx["regions"][0]
    b = region["base"]
    bz = ZimReader(a.base)
    buuid = bz.header.uuid.hex()
    bsize = os.path.getsize(a.base)
    need(b["file"] == os.path.basename(a.base) and b["size"] == bsize, f"index base {b['file']} {b['size']} != local")
    need(b.get("md5") == gatelib.md5_file(a.base), f"index base md5 {b.get('md5')} != the local file's")
    for k in ("download", "url"):
        if k in b:
            ia_listed(b[k], a.base, f"base.{k}")
            urls[b[k]] = a.base
    need("download" in b, "index base has no download URL")
    side = os.path.join(a.site, "region-base-url.json")
    if os.path.exists(side):
        need(json.load(open(side)).get("url") == b.get("url"), "region-base-url.json != base.url")
    pr = region.get("probe")
    if need(pr is not None, "no probe"):
        lp = os.path.join(a.packs_dir, url_item_file(pr["url"])[1])
        if need(os.path.exists(lp) and os.path.getsize(lp) == pr["size"], f"probe {pr} != a local pack"):
            ia_listed(pr["url"], lp, "probe")
            urls[pr["url"]] = lp
    bch: list[str] | None = None
    rows = []
    for m in region["menu"]:
        L = m["lang"]
        before = len(fails)
        r = json.load(open(os.path.join(a.site, m["recipe"])))
        g = json.load(open(os.path.join(gdir, f"{L}.json")))
        vp = os.path.join(vdir, f"{L}.json")
        v = json.load(open(vp)) if os.path.exists(vp) else {}
        need(not any(x.get("lang") == L for x in refused), f"{L}: also in refused.json")
        # site recipe == gated recipe, except pack URLs
        strip = lambda d: dict(d, packs={k: {kk: vv for kk, vv in p.items() if kk != "url"} for k, p in d["packs"].items()})
        need(strip(r) == strip(g), f"{L}: site recipe != gated recipe")
        for k in ("md5", "size", "name", "file_name"):
            need(r[k] == m.get(k), f"{L}: index {k} {m.get(k)} != recipe {r[k]}")
        need(m.get("gate_verdict", "").endswith(f"/{tag}/{L}.json"), f"{L}: gate_verdict {m.get('gate_verdict')} is not .../{tag}/{L}.json")
        tb = touched_bytes(r)
        need(m["download_bytes"] == sum(tb.values()), f"{L}: download_bytes {m['download_bytes']} != {sum(tb.values())}")
        ipacks = {p["lang"]: p for p in m["packs"] if not p.get("in_base")}
        need(set(ipacks) == set(r["packs"]) and all(ipacks[x]["bytes"] == tb[x] and ipacks[x]["articles"] == r["packs"][x]["articles"]
                                                    for x in ipacks), f"{L}: index packs[] != recipe packs")
        # verdict
        need(bool(v.get("all_pass")), f"{L}: verdict missing or not all_pass")
        need((v.get("recipe_md5"), v.get("recipe_uuid"), v.get("recipe_size"), v.get("recipe_file_md5"))
             == (r["md5"], r["uuid"], r["size"], r.get("file_md5")), f"{L}: verdict is for another recipe")
        # base
        rb = r["base"]
        need((rb["file"], rb["size"], rb["uuid"]) == (b["file"], bsize, buuid), f"{L}: recipe base {rb['file']} {rb['uuid']} != local")
        if bch is None:
            bch = chunks(a.base, rb["chunk"])
        need(rb["sha256"] == bch, f"{L}: base chunk sha256 != local base")
        # segments
        tot, seg_ok = 0, True
        for i, s in enumerate(r["segments"]):
            tot += 16 if s["src"] == "md5" else s["len"]
            if s["src"] == "inline":
                d = base64.b64decode(s["data"])
                seg_ok &= len(d) == s["len"] and hashlib.sha256(d).hexdigest() == s["sha256"]
            elif s["src"] == "base":
                seg_ok &= 0 <= s["off"] and s["off"] + s["len"] <= rb["size"]
            elif s["src"] == "pack":
                seg_ok &= s["pack"] in r["packs"] and s["off"] + s["len"] <= r["packs"][s["pack"]]["size"]
            elif s["src"] == "md5":
                seg_ok &= i == len(r["segments"]) - 1
            else:
                seg_ok = False
        need(seg_ok and tot == r["size"], f"{L}: segments do not add up (total {tot}, size {r['size']})")
        # packs
        need(set(v.get("packs", {})) == {p["file"] for p in r["packs"].values()}, f"{L}: verdict pack set != recipe pack set")
        for pl, p in r["packs"].items():
            lp = os.path.join(a.packs_dir, p["file"])
            if not need(os.path.exists(lp), f"{L}/{pl}: no local pack {lp}"):
                continue
            need(os.path.getsize(lp) == p["size"], f"{L}/{pl}: size")
            need(ZimReader(lp).header.uuid.hex() == p["uuid"].replace("-", ""), f"{L}/{pl}: pack UUID")
            need(chunks(lp, p["chunk"]) == p["sha256"], f"{L}/{pl}: chunk sha256 != local")
            ok, why = gatelib.pack_gate_ok(lp)
            need(ok, f"{L}/{pl}: pack gate: {why}")
            need(v.get("packs", {}).get(p["file"]) == gatelib.md5_file(lp), f"{L}/{pl}: verdict pack md5 != local")
            ia_listed(p["url"], lp, f"{L}/{pl}")
            urls[p["url"]] = lp
        rows.append((L, r["md5"], r.get("file_md5"), {pl: url_item_file(p["url"])[0] for pl, p in r["packs"].items()},
                     "OK" if len(fails) == before else "FAIL"))
    if a.probe_urls:
        for u, lp in sorted(urls.items()):
            size = os.path.getsize(lp)
            for start in (0, max(0, size - 65536)):
                try:
                    if u.startswith("{proxy}") and not proxy:
                        raise ValueError("no range proxy configured for {proxy}")
                    got = range_bytes(u.replace("{proxy}", proxy, 1) if u.startswith("{proxy}") else u, start, 65536)
                except Exception as e:  # noqa: BLE001
                    got = repr(e).encode()
                need(got == local_bytes(lp, start, 65536), f"probe {u} @{start}: served bytes != local")
    for L, md5, fmd5, items, st in rows:
        print(f"{L:3s} zim_checksum={md5} file_md5={fmd5} pack_items={items} {st}")
    print(f"refused.json: {len(refused)} entries; urls probed: {len(urls) if a.probe_urls else 0}")
    for f in fails:
        print("FAIL", f)
    print(f"SUMMARY {nchecks - len(fails)}/{nchecks} checks pass -> {'OK' if not fails else 'FAIL'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
