#!/usr/bin/env python3
"""Gate one Wikipedia pack; write <pack>.gate.json (review S1).

    pack_gate.py PACK.zim [--base BASE.zim]

A pack is usable only with {"ok": true} in its gate file AND the gate's
pack_md5 equal to the pack's current MD5 (recipe2 menu / make_site check
both). Checks:
  * the official zimcheck (zim-tools, e.g. 3.8.0 with libzim 9.8.2; set
    $PACK_GATE_ZIMCHECK, else zimcheck on PATH) -A passes, except
    the one expected finding: dangling back-to-map links, exactly
    '../../../index.html' (nested titles: more ../), at most one per article.
    (zimru's zimcheck does not decode &amp; / &#39; in hrefs and reports
    links like ./Comté_d& as missing; the official one is used instead.)
  * libzim Archive.check(); one ~w/<xx>/ prefix; pack.json with lang ==
    prefix, base_uuid, articles; metadata Title <= 30 UTF-16 units,
    Description <= 80, Language ISO 639-3, Name osm_<region>_wiki-<xx>.
  * with --base: pack.json base_uuid is that base or one of its recorded
    parents (S4).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from libzim.reader import Archive  # noqa: E402

from tools.langpack.merge_zim import MergeError, base_lineage, blob_of, iso3, open_pack, utf16_units  # noqa: E402
from tools.langpack.gatelib import zimcheck_version  # noqa: E402
from tools.langpack.zimfmt import ZimReader  # noqa: E402

# The official zimcheck (zim-tools); $PACK_GATE_ZIMCHECK, else zimcheck on PATH.
# zimru's zimcheck is refused: it misreads hrefs with HTML entities.
ZC = os.environ.get("PACK_GATE_ZIMCHECK") or shutil.which("zimcheck") or "zimcheck"
HEADER = re.compile(r"^\[ERROR\] Internal URL: Dangling link\(s\) in article '(.*)':$")
LINK = re.compile(r"^\s+- '(.*)' \(resolves to '(.*)'\)$")
BACKLINK = re.compile(r"(\.\./){3,}index\.html(#.*)?")


def parse_zimcheck(out: str, z, prefix: bytes) -> dict:
    """Parse zimcheck -A output line by line (titles may contain quotes).
    OK only when: the run finished ("Overall Test Status" present); every
    non-[INFO] line ([ERROR], [WARNING], ...) is a Dangling-link header; every header parses and lists only the
    back-to-map link, resolving to the root index.html; and there is exactly
    one header per pack article (every article has the bar, none twice)."""
    lines = out.splitlines()
    overall = next((ln for ln in lines if "Overall Test Status:" in ln), None)
    problems, other, arts, headers, parsed = [], [], [], 0, 0
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("[") and not ln.startswith("[INFO] "):  # [ERROR], [WARNING], anything not INFO
            if ln.startswith("[ERROR] Internal URL: Dangling link(s) in article "):
                headers += 1
                m = HEADER.match(ln)
                links = []
                j = i + 1
                while j < len(lines) and lines[j].startswith("  "):
                    links.append(lines[j]); j += 1
                ok = bool(m) and len(links) == 1
                if ok:
                    lm = LINK.match(links[0])
                    ok = bool(lm) and lm.group(2) == "index.html" and bool(BACKLINK.fullmatch(lm.group(1)))
                if ok and m:
                    parsed += 1
                    arts.append(m.group(1))
                else:
                    problems.append(f"unexpected dangling block: {ln[:120]} {links[:2]}")
                i = j
                continue
            other.append(ln)
        i += 1
    v1 = z.v1_listing()
    front = {z.dirent_at(k).path.decode() for k in z.iter_u32_blob(*v1)} if v1 else set()
    want = {f for f in front if f.startswith(prefix.decode() + "wiki-article/")}
    if overall is None:
        problems.append("zimcheck produced no 'Overall Test Status' line")
    if other:
        problems += other[:5]
    if len(arts) != len(set(arts)):
        problems.append("an article has more than one Dangling header")
    if set(arts) != want:
        problems.append(f"Dangling headers {len(set(arts))} != pack articles {len(want)} "
                        f"(missing {sorted(want - set(arts))[:2]}, extra {sorted(set(arts) - want)[:2]})")
    return {"ok": not problems, "problems": problems, "overall": overall, "headers": headers, "parsed": parsed,
            "articles": len(want), "other_errors": len(other)}


LIMIT = (["prlimit", "--as=8589934592", "--"] if shutil.which("prlimit") else []) + ["timeout", "3600", "nice", "-n", "19"]


def md5_file(path: str) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        while b := fh.read(16 << 20):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack")
    ap.add_argument("--base")
    a = ap.parse_args()
    checks, problems = {}, []
    p = open_pack(a.pack)
    z = p.z
    meta = {k: (blob_of(z, (ord("M"), k.encode())) or b"").decode() for k in ("Name", "Title", "Language", "Description")}
    pj = json.loads(blob_of(z, (ord("C"), p.prefix + b"pack.json")) or b"{}")
    checks["pack_json"] = pj.get("lang") == p.lang and bool(pj.get("base_uuid")) and isinstance(pj.get("articles"), int)
    try:
        want_lang = iso3(p.lang)
    except MergeError:
        want_lang = None
    checks["metadata"] = (utf16_units(meta["Title"]) <= 30 and 0 < len(meta["Description"]) <= 80
                          and meta["Language"] == want_lang and bool(re.match(rf"^osm_[a-z0-9-]+_wiki-{p.lang}$", meta["Name"])))
    if not checks["metadata"]:
        problems.append(f"metadata {meta}")
    checks["archive_check"] = Archive(a.pack).check()
    v1 = z.v1_listing()
    articles = v1[1] if v1 else 0
    zver = zimcheck_version(ZC)
    checks["zimcheck_is_official"] = zver.startswith("zim-tools ")
    if not checks["zimcheck_is_official"]:
        problems.append(f"{ZC} is not the official zimcheck ({zver!r}); set PACK_GATE_ZIMCHECK to zim-tools' zimcheck")
        out = ""
    else:
        r = subprocess.run(LIMIT + [ZC, "-A", a.pack], capture_output=True, text=True)
        out = r.stdout + r.stderr
    zres = parse_zimcheck(out, z, p.prefix)
    checks["zimcheck_official"] = zres["ok"]
    problems += zres["problems"]
    if a.base:
        lin = base_lineage(a.base, ZimReader(a.base).header.uuid, check_md5=True)
        checks["base_uuid_fits"] = p.base_uuid in lin
        if not checks["base_uuid_fits"]:
            problems.append(f"pack.json base_uuid {p.base_uuid} not in base lineage {sorted(lin)}")
    res = {"pack": os.path.basename(a.pack), "lang": p.lang, "pack_md5": md5_file(a.pack),
           "zimcheck_summary": {k: zres[k] for k in ("overall", "headers", "parsed", "articles", "other_errors")},
           "size": os.path.getsize(a.pack), "articles": articles, "pack_json_articles": pj.get("articles"),
           "base_uuid": p.base_uuid, "metadata": meta, "zimcheck": f"{zver} -A",
           "zimcheck_backlink_findings": zres["parsed"], "checks": checks, "problems": problems,
           "ok": all(checks.values())}
    with open(a.pack + ".gate.json", "w") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    print(json.dumps({k: res[k] for k in ("pack", "ok", "pack_md5", "size", "articles", "zimcheck_backlink_findings", "checks")}))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
