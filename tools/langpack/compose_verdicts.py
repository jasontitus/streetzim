#!/usr/bin/env python3
"""One verdict per menu entry from run_gates.sh's outputs (review S2).

    compose_verdicts.py GATES_OUT_DIR BASE.zim LANGS

Reads the gated recipes in $LANGPACK_WORK/recipes/<base>/ and writes
$LANGPACK_WORK/verdicts/<base>/<L>.json:
  {"lang", "recipe_md5", "recipe_uuid", "recipe_size", "packs": {lang: md5},
   "zimcheck_version", "checks": {logs_for_recipe, recipe_eq_merge, verify, zimcheck,
   route, kiwix, search, viewer, ui, smoke, base_logs_current, base_smoke,
   base_viewer, base_ui}, "all_pass",
   "advisory": [checks recorded but not required], "evidence": {...}}
search (search_gate.py: Kiwix's search finds pack-only titles) is required
unless LANGPACK_SEARCH_GATE=report, which records it as advisory.
zimcheck passes only when its log says "Overall Test Status: Pass" AND the
zimcheck that wrote it was the official one (zimcheck.version: "zim-tools ...").
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
from tools.langpack.merge_zim import file_md5  # noqa: E402


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


# What a light gate carries over from the last passing verdict: the checks a
# new viewer cannot change (it lives in three uncompressed slots of the base).
# Not zimcheck: its link checks read the viewer's HTML (28 s on Greece).
CARRIED = ("route", "kiwix", "search")


def mark(b):
    return "PASS" if b else "FAIL"


def main():
    out, base, langs = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
    tag = os.path.basename(os.path.realpath(base))[:-4]
    st = os.stat(base)
    base_checks = {"base_logs_current": read(f"{out}/base.identity").strip() == f"{st.st_size} {int(st.st_mtime)}",
                   "base_smoke": smoke_ok(f"{out}/base.smoke.log")[0],
                   "base_viewer": bool(jline(f"{out}/base.viewer.json").get("pass")),
                   "base_ui": bool(jline(f"{out}/base.ui.json").get("pass"))}
    zver = read(f"{out}/zimcheck.version").strip()      # run_gates.sh: `zimcheck --version`
    base_md5 = file_md5(base)                           # a light gate needs it changed (patched in place)
    official = zver.startswith("zim-tools ")
    advisory = ["search"] if os.environ.get("LANGPACK_SEARCH_GATE") == "report" else []
    rows = [["L", "packs", "recipe=merge", "verify", "zimcheck", "route", "kiwix", "search", "viewer", "ui", "smoke", "ALL"]]
    for L in langs:
        rp = os.path.join(work_dir(), "recipes", tag, f"{L}.json")
        if not os.path.exists(rp):
            rows.append([L, "-", "NO RECIPE"] + [""] * 9)
            continue
        r = json.load(open(rp))
        menu = [json.loads(x) for x in read(f"{out}/menu.jsonl").splitlines() if x.strip()]
        ent = next((m for m in menu if m["L"] == L), {})
        pack_md5 = {os.path.basename(f): md5_file(f) for f in ent.get("pack_files", []) + [ent.get("index_file")] if f}
        vlog = read(f"{out}/{L}.verify.log")
        vm = re.search(r"SUMMARY (\d+)/(\d+) PASS", vlog)
        sm = smoke_ok(f"{out}/{L}.smoke.log")
        kj, vj, uj = jline(f"{out}/{L}.kiwix.json"), jline(f"{out}/{L}.viewer.json"), jline(f"{out}/{L}.ui.json")
        sj = jline(f"{out}/{L}.search.json")
        checks = {
            # run_gates.sh stamps a language's logs with the recipe it checked
            "logs_for_recipe": read(f"{out}/{L}.stamp").split() == [r["md5"], r["uuid"]],
            "recipe_eq_merge": read(f"{out}/{L}.same").strip().endswith("PASS"),
            "verify": bool(vm and vm.group(1) == vm.group(2)),
            "zimcheck": official and "Overall Test Status: Pass" in read(f"{out}/{L}.zimcheck.log"),
            "route": "route OK" in read(f"{out}/{L}.route.log"),
            "kiwix": bool(kj.get("pass")),
            "search": bool(sj.get("pass")),
            "viewer": bool(vj.get("pass")),
            "ui": bool(uj.get("pass")),
            "smoke": sm[0],
        }
        # light gate (run_gates.sh): the base changed only in its viewer slots;
        # the checks the viewer cannot change come from the last passing
        # verdict for a recipe with this fingerprint
        light = {}
        try:
            light = json.load(open(f"{out}/{L}.light.json"))
        except (OSError, ValueError):
            light = {}
        carried = []
        if light:
            # run_gates.sh decided; checked again: the same fingerprint, a passing
            # verdict, and a base whose MD5 is not the one that verdict checked
            same = (bool(r.get("fingerprint")) and light.get("fingerprint") == r.get("fingerprint")
                    and light.get("all_pass") and bool(light.get("base_md5")) and light["base_md5"] != base_md5)
            for k in CARRIED:
                checks[k] = bool(same and (light.get("checks") or {}).get(k))
                carried.append(k)
        checks.update(base_checks)
        if carried:
            evidence_light = {"from_recipe_md5": light.get("recipe_md5"), "carried": carried}
        else:
            evidence_light = None
        v = {"tag": tag, "lang": L, "base_md5": base_md5,
             "recipe_md5": r["md5"], "recipe_uuid": r["uuid"], "recipe_size": r["size"],
             "recipe_file_md5": r.get("file_md5"), "fingerprint": r.get("fingerprint"),
             "packs": pack_md5, "zimcheck_version": zver, "checks": checks,
             "all_pass": all(ok for k, ok in checks.items() if k not in advisory), "advisory": advisory,
             "composed": datetime.datetime.now().isoformat(timespec="seconds"),
             "evidence": {"verify": vm.group(0) if vm else None, "smoke": sm[1],
                          "kiwix": kj.get("checks"), "catalog": kj.get("catalog"),
                          "search": {k: sj.get(k) for k in ("lang", "titles", "suggest_found", "fulltext_found")},
                          "viewer": vj.get("checks"), "viewer_named": (vj.get("named") or {}).get("links", [None])[0],
                          "ui": uj.get("checks"), "ui_labels": (uj.get("labels") or {}).get("cityShown"),
                          "ui_bilingual": (uj.get("labels") or {}).get("bilingualRendered"),
                          "ui_article": ((uj.get("popup_article") or {}).get("article") or {}).get("path"),
                          "ui_search": [(uj.get(k) or {}).get("n") for k in ("search_native", "search_english")],
                          "ui_search_map_lang": (uj.get("search_map_lang") or {}).get("n"),
                          "html_lang": (uj.get("html_lang") or {}).get("htmlLang"),
                          "light_gate": evidence_light}}
        vp = verdict_path(base, L)
        os.makedirs(os.path.dirname(vp), exist_ok=True)
        with open(vp, "w") as fh:
            json.dump(v, fh, ensure_ascii=False, indent=1)
        rows.append([L, ",".join(r["packs"]) or "-"] + [mark(checks[k]) for k in
                    ("recipe_eq_merge", "verify", "zimcheck", "route", "kiwix", "search", "viewer", "ui", "smoke")]
                    + [mark(v["all_pass"])])
    rows.append(["base", "-"] + [""] * 5 + ["", mark(base_checks["base_viewer"]), mark(base_checks["base_ui"]),
                                            mark(base_checks["base_smoke"]), ""])
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))


if __name__ == "__main__":
    main()
