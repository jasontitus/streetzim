#!/usr/bin/env python3
"""One verdict per menu entry from run_gates.sh's outputs (review S2).

    compose_verdicts.py GATES_OUT_DIR BASE.zim LANGS

Reads the gated recipes in $LANGPACK_WORK/recipes/<base>/ and writes
$LANGPACK_WORK/verdicts/<base>/<L>.json:
  {"lang", "recipe_md5", "recipe_uuid", "recipe_size", "packs": {lang: md5},
   "checks": {recipe_eq_merge, verify, zimcheck, route, kiwix, viewer, ui, smoke,
              base_smoke, base_viewer, base_ui}, "all_pass", "evidence": {...}}
and prints a table. make_site.py publishes an entry only with all_pass and a
recipe md5/uuid/size equal to the one it is about to publish.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack.gatelib import md5_file, verdict_path, work_dir  # noqa: E402


def read(p):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def jline(p):
    for ln in reversed(read(p).splitlines()):
        if ln.startswith("{"):
            try:
                return json.loads(ln)
            except ValueError:
                pass
    return {}


def smoke_ok(p):
    m = re.search(r"^(\d+) passed, (\d+) failed", read(p), re.M)
    return bool(m and int(m.group(2)) == 0 and int(m.group(1)) > 0), (m.group(0) if m else "no result")


def mark(b):
    return "PASS" if b else "FAIL"


def main():
    out, base, langs = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
    tag = os.path.basename(os.path.realpath(base))[:-4]
    base_checks = {"base_smoke": smoke_ok(f"{out}/base.smoke.log")[0],
                   "base_viewer": bool(jline(f"{out}/base.viewer.json").get("pass")),
                   "base_ui": bool(jline(f"{out}/base.ui.json").get("pass"))}
    rows = [["L", "packs", "recipe=merge", "verify", "zimcheck", "route", "kiwix", "viewer", "ui", "smoke", "ALL"]]
    for L in langs:
        rp = os.path.join(work_dir(), "recipes", tag, f"{L}.json")
        if not os.path.exists(rp):
            rows.append([L, "-", "NO RECIPE"] + [""] * 8)
            continue
        r = json.load(open(rp))
        menu = [json.loads(x) for x in read(f"{out}/menu.jsonl").splitlines() if x.strip()]
        ent = next((m for m in menu if m["L"] == L), {})
        pack_md5 = {os.path.basename(f): md5_file(f) for f in ent.get("pack_files", [])}
        vlog = read(f"{out}/{L}.verify.log")
        vm = re.search(r"SUMMARY (\d+)/(\d+) PASS", vlog)
        sm = smoke_ok(f"{out}/{L}.smoke.log")
        kj, vj, uj = jline(f"{out}/{L}.kiwix.json"), jline(f"{out}/{L}.viewer.json"), jline(f"{out}/{L}.ui.json")
        checks = {
            "recipe_eq_merge": read(f"{out}/{L}.same").strip().endswith("PASS"),
            "verify": bool(vm and vm.group(1) == vm.group(2)),
            "zimcheck": "Overall Test Status: Pass" in read(f"{out}/{L}.zimcheck.log"),
            "route": "route OK" in read(f"{out}/{L}.route.log"),
            "kiwix": bool(kj.get("pass")),
            "viewer": bool(vj.get("pass")),
            "ui": bool(uj.get("pass")),
            "smoke": sm[0],
        }
        checks.update(base_checks)
        v = {"tag": tag, "lang": L, "recipe_md5": r["md5"], "recipe_uuid": r["uuid"], "recipe_size": r["size"],
             "recipe_file_md5": r.get("file_md5"), "packs": pack_md5, "checks": checks,
             "all_pass": all(checks.values()), "composed": datetime.datetime.now().isoformat(timespec="seconds"),
             "evidence": {"verify": vm.group(0) if vm else None, "smoke": sm[1],
                          "kiwix": kj.get("checks"), "catalog": kj.get("catalog"),
                          "viewer": vj.get("checks"), "viewer_named": (vj.get("named") or {}).get("links", [None])[0],
                          "ui": uj.get("checks"), "ui_labels": (uj.get("labels") or {}).get("cityShown"),
                          "ui_bilingual": (uj.get("labels") or {}).get("bilingualRendered"),
                          "ui_article": ((uj.get("popup_article") or {}).get("article") or {}).get("path"),
                          "ui_search": [(uj.get(k) or {}).get("n") for k in ("search_native", "search_english")],
                          "ui_search_map_lang": (uj.get("search_map_lang") or {}).get("n"),
                          "html_lang": (uj.get("html_lang") or {}).get("htmlLang")}}
        vp = verdict_path(base, L)
        os.makedirs(os.path.dirname(vp), exist_ok=True)
        with open(vp, "w") as fh:
            json.dump(v, fh, ensure_ascii=False, indent=1)
        rows.append([L, ",".join(r["packs"]) or "-"] + [mark(checks[k]) for k in
                    ("recipe_eq_merge", "verify", "zimcheck", "route", "kiwix", "viewer", "ui", "smoke")] + [mark(v["all_pass"])])
    rows.append(["base", "-"] + [""] * 4 + ["", mark(base_checks["base_viewer"]), mark(base_checks["base_ui"]),
                                            mark(base_checks["base_smoke"]), ""])
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))


if __name__ == "__main__":
    main()
