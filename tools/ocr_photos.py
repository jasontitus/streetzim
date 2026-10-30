#!/usr/bin/env python3
"""Score the sign reader on the labelled photo set (docs/ocr-test-set.md).

For each map the set's photos belong to (their `region`), the newest local
ZIM of it is served (cloud/serve_zim_entries.py) with today's viewer and the
reader's models on top (--overlay), so any map can be scored as a map built
now would be, and tools/ocr_photos.mjs hands each photo to the viewer as a
phone's photo picker would. Each photo is scored on:

- read: the share of the sign's labelled lines (`lines`) found among the
  lines read (a line counts when 80% of its letters are read in order);
- query: whether the line searched for is the labelled one (`expect.query`,
  or one of the labelled lines);
- found@1 / found@3: whether a result among the first one / three is the
  labelled place: its name or own-script name (a Taipei street is
  "Zhulun Street", 朱崙街) holds `expect.record` or `expect.query`, or the
  other way around, or is one of the labelled lines exactly (a line alone,
  "Medical centre", would match any such place by containment). Many set
  photos are of places a map may not have, and until the labels name their
  records this is approximate: read `found` with `expect.in_map` in mind;
- at@1 / at@3: with `expect.near` (the place's coordinates: a photo of a
  hotel key or a poster is of a place elsewhere), whether a result among
  the first one / three is within 150 m of it (a street: 1 km), whatever
  its name: the right place, not one of its name;
- negatives (kind "negative"): pass when nothing is found (a negative's
  photo may also hold signs the map has: read its row).

    python3 tools/ocr_photos.py --set tests/fixtures/ocr-signs --maps . --out OUT
    python3 tools/ocr_photos.py ... --regions bahrain,taipei --ids bh-0001
    python3 tools/ocr_photos.py --score OUT       # score saved results again

Recognizers: a map's own (its ocr/manifest.json) when it has the reader;
else the Latin/CJK one and those reading the scripts of its photos (what a
map built with --ocr would most likely carry); --recognizers all for every
one. Writes OUT/raw.jsonl (what the reader did), OUT/scores.jsonl and
OUT/report.md. Needs NODE (or node on PATH), PLAYWRIGHT_CORE, CHROME_PATH
(and BROWSER_LIBS where chromium needs them).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import socketserver
import statistics
import subprocess
import sys
import threading
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

READ_MIN = 0.8
_DIGITS = {**{0x660 + i: str(i) for i in range(10)}, **{0x6F0 + i: str(i) for i in range(10)}}
# Accents on Latin, Greek and Cyrillic letters, Arabic vowel signs and the
# tatweel: written or not, read or not, the same name ("Zurich", "Zürich").
_DROP = {*range(0x300, 0x370), *range(0x64B, 0x660), 0x670, 0x640}


def norm(s: str) -> str:
    """Letters, marks and digits only, folded: what two readings of one sign share."""
    s = unicodedata.normalize("NFKD", s or "").translate(_DIGITS).casefold()
    s = "".join(c for c in s if ord(c) not in _DROP and unicodedata.category(c)[0] in "LMN")
    return unicodedata.normalize("NFC", s)


def coverage(want: str, got: str) -> float:
    """The share of ``want``'s letters found in ``got``, in order, in runs of
    two or more (single letters scattered over a long line are no reading:
    "GST" in "OFFICE NAGPUR SODS SERVICESTRANSPOR")."""
    a, b = norm(want), norm(got)
    if not a:
        return 0.0
    if a in b:
        return 1.0
    run = 2 if len(a) > 2 else 1
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(blk.size for blk in m.get_matching_blocks() if blk.size >= run) / len(a)


def same_place(name: str, want: str) -> bool:
    """A result's name and a labelled name: equal, or one holding the other
    when the shorter is at least half the longer ("Starbucks" in
    "Starbucks Seef Mall", not "Hamad" in "Madinat Hamad")."""
    a, b = norm(name), norm(want)
    if len(a) < 2 or len(b) < 2:
        return a == b and bool(a)
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return short in long_ and len(short) / len(long_) >= 0.5


def km(a: list[float] | tuple[float, float], b: list[float] | tuple[float, float]) -> float:
    """Great-circle distance in km between two [lat, lon]."""
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def at_place(rows: list[dict[str, Any]], near: Any, k: int) -> bool | None:
    """Whether a result among the first `k` is the place at `near` (150 m;
    a street 1 km); None without `near`."""
    if not (isinstance(near, (list, tuple)) and len(near) == 2):
        return None
    return any(km(near, (r["lat"], r["lon"])) <= (1.0 if r.get("type") == "street" else 0.15)
               for r in rows[:k] if r.get("lat") is not None and r.get("lon") is not None)


def wanted_names(rec: dict[str, Any]) -> list[str]:
    """The labelled place's names: the record's and the query."""
    exp = rec.get("expect") or {}
    return [x for x in (exp.get("record") or "", exp.get("query") or "") if norm(x)]


def result_names(r: dict[str, Any]) -> list[str]:
    """A result row's names: its record's and the own-script one the row
    shows after it ("Zhulun Street 朱崙街")."""
    name, shown = r.get("name", ""), r.get("shown", "").strip()
    own = shown[len(name):].strip() if name and shown.startswith(name) else shown
    return [x for x in (name, own) if norm(x)]


def label_lines(rec: dict[str, Any]) -> list[str]:
    return [ln.get("text", "") for ln in rec.get("lines") or [] if norm(ln.get("text", ""))]


def score(rec: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """One photo's scores (module docstring)."""
    lines = raw.get("lines") or []
    # The field holds what was searched for only when a search ran (else the
    # last photo's).
    field = raw.get("field", "") if raw.get("status") == "searched" else ""
    want_lines = label_lines(rec)
    read = [max((coverage(w, g) for g in lines), default=0.0) for w in want_lines]
    rows = raw.get("results") or []
    names = [r.get("name", "") for r in rows]
    wanted = wanted_names(rec)
    exact = {norm(x) for x in want_lines}
    queries = wanted + want_lines + [ln.get("latin", "") for ln in rec.get("lines") or [] if ln.get("latin")]

    def found(k: int) -> bool:
        return any(same_place(n, w) for r in rows[:k] for n in result_names(r) for w in wanted) or \
            any(norm(n) in exact for r in rows[:k] for n in result_names(r))

    out: dict[str, Any] = {
        "id": rec["id"], "region": rec.get("region"), "country": rec.get("country"), "kind": rec.get("kind"),
        "difficulty": rec.get("difficulty"), "status": raw.get("status", "not run"), "ms": raw.get("ms"),
        "read": round(sum(1 for x in read if x >= READ_MIN) / len(read), 3) if read else None,
        "read_lines": [round(x, 2) for x in read],
        "query": any(coverage(q, field) >= READ_MIN and coverage(field, q) >= 0.5
                     for q in queries if norm(q)) if field else False,
        "found1": found(1), "found3": found(3), "in_map": (rec.get("expect") or {}).get("in_map"),
        "at1": at_place(rows, (rec.get("expect") or {}).get("near"), 1),
        "at3": at_place(rows, (rec.get("expect") or {}).get("near"), 3),
        "field": field, "top": names[:3], "lines": lines,
    }
    if rec.get("kind") == "negative":
        out["negative_ok"] = raw.get("status") in ("none", "searched") and not names
    return out


# ---- running ---------------------------------------------------------------

def newest_map(maps: Path, region: str) -> Path | None:
    pat = re.compile(rf"osm-{re.escape(region)}-(\d{{4}}-\d\d-\d\d)([a-z]?)\.zim")
    found = sorted((m.group(1), m.group(2), p) for p in maps.glob(f"osm-{region}-*.zim")
                   if (m := pat.fullmatch(p.name)))
    return found[-1][2] if found else None


def map_entry(arc: Any, path: str) -> bytes | None:
    try:
        return bytes(arc.get_entry_by_path(path).get_item().content)
    except KeyError:
        return None


def overlay_for(arc: Any, records: list[dict[str, Any]], models: Path, dest: Path, how: str) -> list[str]:
    """dest/: today's viewer pages for this map, its map-config.json with
    the reader on, and ocr/ (the manifest of the recognizers chosen, links
    to the exported models). Returns the recognizers."""
    from cloud.viewer_slots import viewer_page
    from streetzim import ocr as _ocr
    from streetzim.common import VIEWER_DIR
    cfg = json.loads(map_entry(arc, "map-config.json") or b"{}")
    full = json.loads((models / _ocr.MANIFEST_ENTRY).read_text(encoding="utf-8"))
    own = map_entry(arc, _ocr.MANIFEST_ENTRY)
    if how == "all":
        names = [r["name"] for r in full["rec"]]
    elif own:
        names = [r["name"] for r in json.loads(own)["rec"]]
    else:
        by_script = _ocr.recognizers_by_script()
        names = [by_script["Latn"]]
        for rec in records:
            for s in rec.get("scripts") or []:
                if s in by_script and by_script[s] not in names:
                    names.append(by_script[s])
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "places.html", "routing-worker.js"):     # as a viewer swap writes them
        (dest / name).write_bytes(viewer_page(name, VIEWER_DIR, cfg))
    cfg["ocr"] = _ocr.MANIFEST_ENTRY
    (dest / "map-config.json").write_text(json.dumps(cfg), encoding="utf-8")
    od = dest / _ocr.ENTRY_DIR
    od.mkdir(exist_ok=True)
    for f in (models / _ocr.ENTRY_DIR).iterdir():
        link = od / f.name
        if f.name != "manifest.json" and not link.exists():
            link.symlink_to(f.resolve())
    manifest = dict(full, rec=[r for r in full["rec"] if r["name"] in names])
    (od / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return names


def run_region(zim: Path, records: list[dict[str, Any]], set_dir: Path, models: Path, out: Path,
               how: str, at_photo: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    from libzim.reader import Archive

    from cloud.serve_zim_entries import make_handler
    arc = Archive(str(zim))
    region = records[0]["region"]
    recs = overlay_for(arc, records, models, out / "overlay" / region, how)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), make_handler(arc, str(out / "overlay" / region)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    photos = out / f"photos-{region}.json"
    photos.write_text(json.dumps([dict({"id": r["id"], "path": str(set_dir / r["file"])},
                                       **({"at": r["location"]} if at_photo and r.get("location") else {}))
                                  for r in records]), encoding="utf-8")
    env = dict(os.environ, ZIM_ORIGIN=f"http://127.0.0.1:{srv.server_address[1]}", PHOTOS=str(photos))
    raws: list[dict[str, Any]] = []

    def take(out: str | bytes | None) -> None:
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else out or ""
        for line in text.splitlines():
            if line.startswith("{"):
                raws.append(json.loads(line))
    try:
        p = subprocess.run([os.environ.get("NODE", "node"), str(ROOT / "tools" / "ocr_photos.mjs")],
                           env=env, capture_output=True, text=True,
                           timeout=600 + 200 * len(records))
        take(p.stdout)
        if p.returncode and not raws:
            raws.append({"error": (p.stderr or p.stdout)[-400:]})
    except subprocess.TimeoutExpired as e:
        take(e.stdout)                       # the photos read before it hung
        raws.append({"error": "timed out"})
    finally:
        srv.shutdown()
        srv.server_close()
    for r in raws:
        r.update(region=region, map=zim.name, recognizers=recs)
    return raws, recs


# ---- the report ------------------------------------------------------------

def _pct(xs: list[bool]) -> str:
    return f"{100 * sum(xs) / len(xs):.0f}%" if xs else "-"


def report(records: list[dict[str, Any]], raws: dict[str, dict[str, Any]], notes: dict[str, str]) -> tuple[list[dict[str, Any]], str]:
    scores = [score(r, raws.get(r["id"], {})) for r in records]
    by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in scores:
        by_region[s["region"]].append(s)
    pos = [s for s in scores if s["kind"] != "negative" and s["status"] != "not run"]
    md = ["# Sign reader on the photo set", "",
          f"{len(pos)} photos of a place read ({len(scores) - len(pos)} more: negatives, or not run): lines read {_pct([(s['read'] or 0) >= 0.5 for s in pos])} "
          f"(half or more of a sign's lines), the labelled line searched for {_pct([s['query'] for s in pos])}, "
          f"the place first {_pct([s['found1'] for s in pos])}, in the first three {_pct([s['found3'] for s in pos])}.",
          "", "| map | photos | recognizers | read | query | found@1 | found@3 | median s |",
          "|---|---:|---|---:|---:|---:|---:|---:|"]
    for region in sorted(by_region):
        ss = [s for s in by_region[region] if s["kind"] != "negative" and s["status"] != "not run"]
        ms = [s["ms"] for s in ss if s.get("ms")]
        md.append(f"| {region} | {len(ss)} | {notes.get(region, '')} | {_pct([(s['read'] or 0) >= 0.5 for s in ss])} | "
                  f"{_pct([s['query'] for s in ss])} | {_pct([s['found1'] for s in ss])} | "
                  f"{_pct([s['found3'] for s in ss])} | {statistics.median(ms) / 1000:.1f} |" if ss else
                  f"| {region} | 0 | {notes.get(region, '')} | | | | | |")
    kinds: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in pos:
        kinds[s["kind"]].append(s)
    md += ["", "| kind | photos | read | query | found@3 |", "|---|---:|---:|---:|---:|"]
    for k in sorted(kinds):
        ss = kinds[k]
        md.append(f"| {k} | {len(ss)} | {_pct([(s['read'] or 0) >= 0.5 for s in ss])} | "
                  f"{_pct([s['query'] for s in ss])} | {_pct([s['found3'] for s in ss])} |")
    placed = [s for s in pos if s.get("at3") is not None]
    if placed:
        md += ["", f"With the place's coordinates (`expect.near`), {len(placed)} photos: the place first "
                   f"{_pct([bool(s['at1']) for s in placed])}, in the first three {_pct([bool(s['at3']) for s in placed])}."]
    neg = [s for s in scores if s["kind"] == "negative" and s["status"] != "not run"]
    if neg:
        md += ["", f"Negatives: {sum(1 for s in neg if s.get('negative_ok'))} of {len(neg)} found nothing "
                   "(a negative's photo may also hold signs the map has)."]
    md += ["", "| photo | kind | status | read | searched for | first results |", "|---|---|---|---:|---|---|"]
    cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")[:60]  # noqa: E731
    for s in scores:
        md.append(f"| {s['id']} | {s['kind']} | {s['status']} | {s['read'] if s['read'] is not None else '-'} | "
                  f"{cell(s['field'])}{' ✓' if s['query'] else ''} | {cell(' / '.join(s['top']))}"
                  f"{' ✓' if s['found3'] else ''} |")
    return scores, "\n".join(md) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--set", type=Path, default=ROOT / "tests" / "fixtures" / "ocr-signs",
                    help="the photo set's folder (its manifest.jsonl)")
    ap.add_argument("--maps", type=Path, default=Path("."), help="the folder of the maps (osm-<region>-<date>.zim)")
    ap.add_argument("--out", type=Path, help="where results go (required unless --score)")
    ap.add_argument("--regions", help="only these maps (comma-separated)")
    ap.add_argument("--ids", help="only these photos (comma-separated)")
    ap.add_argument("--recognizers", choices=("map", "all"), default="map")
    ap.add_argument("--at-photo", action="store_true",
                    help="put the map where each photo was taken (its `location`) first, as a phone with its "
                         "user's position does; photos with no location as usual")
    ap.add_argument("--score", type=Path, help="score the results saved in this folder again")
    args = ap.parse_args(argv)
    records = [json.loads(x) for x in (args.set / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    if args.ids:
        keep = set(args.ids.split(","))
        records = [r for r in records if r["id"] in keep]
    if args.regions:
        keep = set(args.regions.split(","))
        records = [r for r in records if r.get("region") in keep]
    out = args.score or args.out
    if out is None:
        ap.error("--out is required")
    out.mkdir(parents=True, exist_ok=True)
    raw_path = out / "raw.jsonl"
    raws: dict[str, dict[str, Any]] = {}
    notes: dict[str, str] = {}
    if args.score:
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("id"):
                raws[r["id"]] = r
                notes[r["region"]] = ", ".join(r.get("recognizers") or [])
    else:
        from streetzim import ocr as _ocr
        models = out / "models"
        if not (models / _ocr.MANIFEST_ENTRY).exists():
            _ocr.export(str(models), _ocr.all_models())
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            groups[r.get("region") or ""].append(r)
        with raw_path.open("w", encoding="utf-8") as fh:
            for region, recs in sorted(groups.items()):
                zim = newest_map(args.maps, region)
                if zim is None:
                    notes[region] = "no map"
                    print(f"{region}: no osm-{region}-<date>.zim in {args.maps}", flush=True)
                    continue
                print(f"{region}: {len(recs)} photos on {zim.name}", flush=True)
                got, names = run_region(zim, recs, args.set, models, out, args.recognizers, args.at_photo)
                notes[region] = ", ".join(names)
                for r in got:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    fh.flush()
                    if r.get("id"):
                        raws[r["id"]] = r
                        print(f"  {r['id']}: {r.get('status')} in {r.get('ms', 0) / 1000:.1f} s: "
                              f"{r.get('field', '')!r} -> {[x.get('name') for x in r.get('results') or []][:3]}",
                              flush=True)
                    else:
                        print(f"  {region}: {r.get('error')}", flush=True)
    scores, md = report(records, raws, notes)
    with (out / "scores.jsonl").open("w", encoding="utf-8") as fh:
        for s in scores:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    (out / "report.md").write_text(md, encoding="utf-8")
    print(md.split("\n\n| photo")[0], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
