#!/usr/bin/env python3
"""Mutation tests for the N-pack merger and recipe/2 materialization.

    mutation_test.py BASE.zim LANG PACK... [--workdir DIR]

Part 1: break merge_zim.py one way at a time; verify_merge.py (or zimcheck,
or the merger's own self-check) must fail every mutant.
Part 2: corrupt a recipe or its inputs; materialize() must refuse.
Some mutants need two or more packs. zimcheck runs when the official one is
there ($PACK_GATE_ZIMCHECK, else zimcheck on PATH); verify_merge.py alone
kills every mutant. tests/langpack/test_mutation.py runs this on small ZIMs.
"""
from __future__ import annotations

import argparse
import base64
import copy
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PY = sys.executable
ZC = os.environ.get("PACK_GATE_ZIMCHECK") or shutil.which("zimcheck")
from tools.langpack.gatelib import work_dir, zimcheck_version  # noqa: E402

MUTANTS = {
    "pack k>0 clusters numbered from pack 0": """
import tools.langpack.merge_zim as m
orig = m.open_pack
n = [0]
def op(p):
    r = orig(p); return r
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    if len(plan.packs) > 1:
        for e in plan.entries:
            if e.pack and e.pack > 0 and not e.is_redirect:
                e.cluster -= plan.packs[e.pack].first_cluster - plan.packs[0].first_cluster
    return plan
m.build_plan = bp
""",
    "pack MIME not remapped": """
import tools.langpack.merge_zim as m
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    for e in plan.entries:
        if e.pack is not None and not e.is_redirect:
            d = plan.packs[e.pack].z.dirent_at(e.pack_index)
            e.mime = d.mime
    return plan
m.build_plan = bp
""",
    "pack redirect points at the next entry": """
import tools.langpack.merge_zim as m
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    for e in plan.entries:
        if e.is_redirect:
            e.target += 1
    return plan
m.build_plan = bp
""",
    "v1 listing: last pack's fronts left out": """
import tools.langpack.merge_zim as m
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    lastk = len(plan.packs) - 1
    keep = [(p, j) for p, j in plan.pack_front if plan.entries[j].pack != lastk]
    plan.listing_entries -= len(plan.pack_front) - len(keep)
    plan.pack_front = keep
    plan.blobs[0] = b"\\0" * (4 * plan.listing_entries)
    return plan
m.build_plan = bp
""",
    "v1 listing: pack fronts not merged by title": """
import tools.langpack.merge_zim as m
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    plan.pack_front = [(0, j) for _p, j in plan.pack_front]
    return plan
m.build_plan = bp
""",
    "M/Counter not updated": """
import tools.langpack.merge_zim as m
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    old = m.blob_of(plan.base, m.KEY_COUNTER)
    plan.blobs[1] = old[:len(plan.blobs[1])].ljust(len(plan.blobs[1]), b"0")
    return plan
m.build_plan = bp
""",
    "base UUID reused": """
import tools.langpack.merge_zim as m
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    plan.header.uuid = plan.base.header.uuid
    return plan
m.build_plan = bp
""",
    "map-config.json not overridden (base bytes)": """
import tools.langpack.merge_zim as m, json
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    for i, e in enumerate(plan.entries):
        if e.key == m.KEY_MAPCFG:
            old = m.blob_of(plan.base, m.KEY_MAPCFG)
            plan.blobs[e.blob] = old[:len(plan.blobs[e.blob])].ljust(len(plan.blobs[e.blob]), b" ")
    return plan
m.build_plan = bp
""",
    "Language lists only the map language": """
import tools.langpack.merge_zim as m
orig_bp = m.build_plan
def bp(*a, **k):
    plan = orig_bp(*a, **k)
    for e in plan.entries:
        if e.key == (77, b"Language"):
            v = plan.blobs[e.blob].split(b",")[0]
            plan.blobs[e.blob] = v.ljust(len(plan.blobs[e.blob]), b" ")
    return plan
m.build_plan = bp
""",
    "base path pointers shifted by one entry": """
import tools.langpack.merge_zim as m
orig = m.pieces
def pc(plan):
    seen = False
    for p in orig(plan):
        if isinstance(p, m.Ref) and p.src == "base" and p.off == plan.base.header.path_ptr_pos and not seen:
            seen = True
            p = m.Ref("base", p.off + 8, p.n)
        yield p
m.pieces = pc
""",
    "UUID ignores the generated title/map-config (S3)": """
import tools.langpack.merge_zim as m
orig = m.derive_uuid
m.derive_uuid = lambda b, ps, lang, meta, cfg, contents=None: orig(b, ps, lang, {}, b"", contents)
""",
    "wrong MD5": """
import tools.langpack.merge_zim as m, hashlib
class Bad:
    def __init__(self, *a, **k): self.h = hashlib.md5(*a, **k)
    def update(self, b): self.h.update(b)
    def hexdigest(self): return self.h.hexdigest()
    def digest(self): return bytes(16)
class H:  # the real hashlib except md5().digest() (sha256 for the S3 UUID stays real)
    def __getattr__(self, k): return getattr(hashlib, k)
    md5 = staticmethod(lambda *a, **k: Bad(*a, **k))
m.hashlib = H()
""",
}


LIMIT = (["prlimit", "--as=8589934592", "--"] if shutil.which("prlimit") else []) + ["timeout", "1800", "nice", "-n", "19"]


def run(args, **kw):
    """Every mutant / check runs under an 8 GB address-space cap and a timeout;
    a mutant that hits the cap fails, which counts as killed."""
    return subprocess.run(LIMIT + list(args), capture_output=True, text=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("lang")
    ap.add_argument("packs", nargs="+")
    ap.add_argument("--workdir", default=os.path.join(work_dir(), "mutants"))
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=ROOT)
    zc = ZC if ZC and zimcheck_version(ZC).startswith("zim-tools ") else None
    if zc is None:
        print("INFO  no official zimcheck: verify_merge.py alone judges the mutants")
    survived = killed = 0
    out = os.path.join(a.workdir, "mutant.zim")
    print("== part 1: merger mutants")
    for name, src in MUTANTS.items():
        if os.path.exists(out):
            os.remove(out)
        code = src + f"\nm.merge({a.base!r}, {a.packs!r}, {out!r}, {a.lang!r})\n"
        r = run([PY, "-c", code], env=env, cwd=ROOT)
        if r.returncode or not os.path.exists(out):
            killed += 1
            print(f"KILLED (merger refused)  {name}: {(r.stderr.strip().splitlines() or [''])[-1][:110]}")
            continue
        v = run([PY, os.path.join(HERE, "verify_merge.py"), out, a.base, *a.packs, "--lang", a.lang], env=env)
        fails = [ln for ln in v.stdout.splitlines() if ln.startswith("FAIL")]
        zf = False
        if zc:
            z = run([zc, "-A", out])
            zf = "Overall Test Status: Fail" in z.stdout + z.stderr
        if fails or v.returncode or zf:
            killed += 1
            first = fails[0][:100] if fails else (v.stdout + v.stderr).strip().splitlines()[-1][:100]
            print(f"KILLED  {name}: verify_fails={len(fails)} zimcheck_fail={zf} | {first}")
        else:
            survived += 1
            print(f"SURVIVED  {name}")

    print("== part 2: recipe / input corruption (materialize must refuse)")
    from tools.langpack.recipe2 import build_recipe, materialize
    recipe, plan, _ = build_recipe(a.base, a.packs, a.lang)
    packs = {p.lang: p.path for p in plan.packs}
    tmp = os.path.join(a.workdir, "mat.zim")

    def expect_refusal(name, rec, base=a.base, pk=packs):
        nonlocal killed, survived
        try:
            materialize(rec, base, pk, tmp)
            survived += 1
            print(f"SURVIVED  {name}")
        except Exception as e:  # noqa: BLE001
            killed += 1
            print(f"KILLED  {name}: {str(e)[:100]}")

    r = copy.deepcopy(recipe)
    s = next(s for s in r["segments"] if s["src"] == "inline" and s["len"] > 100)
    d = bytearray(base64.b64decode(s["data"]))
    d[50] ^= 1
    s["data"] = base64.b64encode(bytes(d)).decode()
    expect_refusal("inline segment byte flipped", r)
    bad_base = os.path.join(a.workdir, "base-corrupt.zim")
    shutil.copyfile(a.base, bad_base)
    with open(bad_base, "r+b") as fh:
        off = recipe["base"]["range"][0] + 12345
        fh.seek(off)
        b = fh.read(1)
        fh.seek(off)
        fh.write(bytes([b[0] ^ 0x20]))
    expect_refusal("one byte of the local base file flipped", recipe, base=bad_base)
    r = copy.deepcopy(recipe)
    r["md5"] = "0" * 32
    expect_refusal("recipe MD5 wrong", r)
    r = copy.deepcopy(recipe)
    s = next(s for s in r["segments"] if s["src"] == "base" and s["len"] > 1000)
    s["off"] += 1
    expect_refusal("base segment offset off by one (inputs intact)", r)
    r = copy.deepcopy(recipe)
    segs = r["segments"]
    i = next(i for i, s in enumerate(segs) if s["src"] == "pack")
    if len([s for s in segs if s["src"] == "pack"]) > 1:
        j = next(j for j, s in enumerate(segs) if s["src"] == "pack" and j != i)
        segs[i], segs[j] = segs[j], segs[i]
        expect_refusal("two pack segments swapped", r)
    langs = sorted(packs)
    if len(langs) > 1:
        swapped = dict(packs)
        swapped[langs[0]], swapped[langs[1]] = packs[langs[1]], packs[langs[0]]
        expect_refusal("wrong pack file supplied for a language", recipe, pk=swapped)
    for f in (tmp, bad_base, out):
        if os.path.exists(f):
            os.remove(f)
    print(f"SUMMARY {killed}/{killed + survived} mutants killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
