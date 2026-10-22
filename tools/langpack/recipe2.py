#!/usr/bin/env python3
"""streetzim-recipe/2: build, materialize and check recipes.

    recipe2.py build BASE.zim PACK... --lang L -o RECIPE.json [--region R] [--title T]
                     [--check MERGED.zim] [--index SEARCH.zim]
    recipe2.py menu BASE.zim --packs-dir DIR --langs de,en,fr,th --region-langs de
                     --out-dir DIR [--region R]
    recipe2.py materialize RECIPE.json BASE.zim PACK... -o OUT.zim

Recipe (docs/language-matrix-prototype.md, "Recipes"):
  {"format": "streetzim-recipe/2", "uuid", "name", "size", "md5",
   "base": {"file", "size", "range": [cluster_start, dirents_end], "chunk", "sha256": [...]},
   "packs": {"<lang>": {"file", "url", "size", "chunk", "sha256": [...]},
             "search": {..., "kind": "search"}},     (the entry's search pack, index_pack.py)
   "segments": [{"src": "inline", "len", "data": b64, "sha256"},
                {"src": "base", "off", "len"}, {"src": "pack", "pack", "off", "len"},
                {"src": "md5"}]}

Chunk hashes cover each WHOLE input file from offset 0 in 16 MiB chunks, so
the browser can check any range it reads. materialize() checks every chunk a
segment touches and every inline segment before writing it, recomputes the
MD5 and requires it to equal the recipe's; nothing is trusted.

Viewer slots (plan review B2): the base's three viewer files sit in
fixed-size uncompressed slots that cloud/patch_viewer_inplace.py overwrites
without changing anything else. The recipe carries them inline (from the
base it was made from) and lists their ranges, and the base's own trailing
checksum, as base.mask; the base's chunk hashes are taken with those ranges
zeroed, and no base segment reads them. So a visitor's copy one viewer patch older (or newer) still builds
the same file, and a patched base needs only new recipes: their
"fingerprint" (everything that decides the merged bytes apart from the
slots and the UUID/MD5 they change) stays the same, which run_gates.sh's
light gate checks.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack.gatelib import pack_gate_ok, work_dir  # noqa: E402
from tools.langpack.merge_zim import (  # noqa: E402
    MERGER_VERSION, SEARCH, MergeError, Plan, Ref, build_plan, peak_rss_mb, pieces, ref_path)

FORMAT = "streetzim-recipe/2"
CHUNK = int(os.environ.get("LANGPACK_RECIPE_CHUNK") or 16 << 20)   # tests set a small one
INLINE_MAX_REF = 256         # verbatim runs shorter than this are inlined (bounds the segment count)
PROXY = "{proxy}"            # the builder page substitutes its range proxy
_LAST_FILE_MD5: list[str | None] = [None]


def masked(b: bytes, start: int, mask: list | None) -> bytes:
    """b (the file's bytes from `start`) with the mask's ranges zeroed."""
    if not mask:
        return b
    out = None
    end = start + len(b)
    for off, n in mask:
        lo, hi = max(off, start), min(off + n, end)
        if lo < hi:
            if out is None:
                out = bytearray(b)
            out[lo - start:hi - start] = bytes(hi - lo)
    return bytes(out) if out is not None else b


def viewer_slots(base_path: str) -> list[dict]:
    """The base's viewer slots (cloud/viewer_slots.py): [{"name", "off", "len"}]
    by offset, for each viewer file stored as a slot (an uncompressed blob
    ending in its slot marker and closer); [] for a base without slots."""
    from cloud.viewer_slots import VIEWER_FILES, comment_delims, slot_needle
    from tools.langpack.merge_zim import ZimReader
    z = ZimReader(base_path)
    out = []
    for name in VIEWER_FILES:
        i = z.find(ord("C"), name.encode())
        if i is None:
            continue
        d = z.dirent_at(i)
        if d.is_redirect or d.cluster is None or d.blob is None or z.cluster_info(d.cluster) & 0x0F not in (0, 1):
            continue
        off, n = z.raw_blob(d.cluster, d.blob)
        blob = z.read_at(off, n)                 # at most 1 MB: the marker follows the content
        if slot_needle(name) in blob and blob.endswith(comment_delims(name)[1]):
            out.append({"name": name, "off": off, "len": n})
    return sorted(out, key=lambda x: x["off"])


def chunk_hashes(path: str, chunk: int = CHUNK, mask: list | None = None) -> list[str]:
    """SHA-256 per chunk of the whole file (with the mask's [off, len] ranges
    zeroed), cached under $LANGPACK_WORK/sha-cache."""
    st = os.stat(path)
    cdir = os.path.join(work_dir(), "sha-cache")
    os.makedirs(cdir, exist_ok=True)
    mtag = ("." + hashlib.sha256(json.dumps(mask).encode()).hexdigest()[:12]) if mask else ""
    key = os.path.join(cdir, f"{os.path.basename(path)}.{st.st_size}.{st.st_mtime_ns}.{chunk}{mtag}.json")
    if os.path.exists(key):
        return json.load(open(key))
    out = []
    pos = 0
    with open(path, "rb", buffering=0) as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            out.append(hashlib.sha256(masked(b, pos, mask)).hexdigest())
            pos += len(b)
    with open(key, "w") as fh:
        json.dump(out, fh)
    return out


def build_recipe(base_path: str, pack_paths: list[str], lang: str, url_template: str = PROXY + "/{file}",
                 url_overrides: dict | None = None, **kw) -> tuple[dict, Plan, dict]:
    """url_template: each pack's absolute Range URL, {file} = the pack's file
    name (the builder page uses it as is; "{proxy}" is left for a page that
    substitutes its own proxy)."""
    t0 = time.perf_counter()
    plan = build_plan(base_path, pack_paths, lang, **kw)
    t_plan = time.perf_counter() - t0
    slots = viewer_slots(base_path)
    # masked: the viewer slots and, when the base has them, its own checksum
    # (the last 16 bytes, which an in-place patch recomputes; no merge reads it)
    mask = [[x["off"], x["len"]] for x in slots]
    if slots:
        mask.append([plan.base.header.checksum_pos, 16])
    segs: list[dict] = []
    pend = bytearray()
    fds = {}

    def flush():
        if pend:
            segs.append({"src": "inline", "len": len(pend), "data": bytes(pend)})
            pend.clear()

    def read(r: Ref) -> bytes:
        path = ref_path(plan, r)
        if path not in fds:
            fds[path] = os.open(path, os.O_RDONLY)
        return os.pread(fds[path], r.n, r.off)

    def split(pc: Ref):
        """A base range with the viewer slots cut out: (Ref, is_slot) pieces."""
        if not isinstance(pc.src, str) or not mask:
            yield pc, False
            return
        pos, end = pc.off, pc.off + pc.n
        for off, n in mask:
            lo, hi = max(off, pos), min(off + n, end)
            if lo >= hi:
                continue
            if lo > pos:
                yield Ref(pc.src, pos, lo - pos), False
            yield Ref(pc.src, lo, hi - lo), True
            pos = hi
        if pos < end:
            yield Ref(pc.src, pos, end - pos), False

    for whole in pieces(plan):
        if not isinstance(whole, Ref):
            pend += whole
            continue
        for pc, is_slot in split(whole):
            if pc.n == 0:
                continue
            if is_slot or pc.n < INLINE_MAX_REF:     # the viewer slots travel inline
                pend += read(pc)
                continue
            flush()
            if isinstance(pc.src, str):
                segs.append({"src": "base", "off": pc.off, "len": pc.n})
            elif pc.src[0] == "index":
                segs.append({"src": "pack", "pack": SEARCH, "off": pc.off, "len": pc.n})
            else:
                segs.append({"src": "pack", "pack": plan.packs[pc.src[1]].lang, "off": pc.off, "len": pc.n})
    flush()
    segs.append({"src": "md5"})
    for fd in fds.values():
        os.close(fd)
    for s in segs:
        if s["src"] == "inline":
            s["sha256"] = hashlib.sha256(s["data"]).hexdigest()
            s["data"] = base64.b64encode(s["data"]).decode()
    L = plan.layout
    base_sha = chunk_hashes(base_path, mask=mask)
    recipe = {
        "format": FORMAT, "merger": MERGER_VERSION,
        "uuid": str(__import__("uuid").UUID(bytes=plan.header.uuid)),
        "name": plan.meta["Name"],
        # the download's file name: Name + the base's date (builder.js: recipe.file_name || recipe.name)
        "file_name": plan.meta["Name"] + (("_" + _date.group(1)) if (_date := __import__("re").search(
            r"(\d{4}-\d{2}-\d{2})", os.path.basename(os.path.realpath(base_path)))) else ""),
        "title": plan.meta["Title"], "language": plan.meta["Language"],
        "flavour": plan.meta["Flavour"], "map_language": lang,
        "size": L["total"], "md5": None, "file_md5": None,
        "base": {"file": os.path.basename(os.path.realpath(base_path)), "size": os.path.getsize(base_path),
                 "uuid": plan.base.header.uuid.hex(),
                 "range": [L["cluster_start"], L["base_dirents_end"]], "chunk": CHUNK,
                 "sha256": base_sha, "mask": mask, "slots": slots},
        "packs": {p.lang: {"file": os.path.basename(p.path), "uuid": p.uuid.hex(),
                           "url": ((url_overrides or {}).get(p.lang) or url_template).format(
                               file=os.path.basename(p.path), proxy=PROXY),
                           "size": os.path.getsize(p.path), "chunk": CHUNK, "articles": p.articles,
                           "sha256": chunk_hashes(p.path)} for p in plan.packs},
        "segments": segs,
    }
    if plan.index is not None:
        ix = plan.index
        recipe["packs"][SEARCH] = {"file": os.path.basename(ix.path), "uuid": ix.uuid.hex(), "kind": "search",
                                   "url": url_template.format(file=os.path.basename(ix.path), proxy=PROXY),
                                   "size": os.path.getsize(ix.path), "chunk": CHUNK, "articles": 0,
                                   "sha256": chunk_hashes(ix.path)}
    recipe["fingerprint"] = fingerprint(recipe, plan)
    # exact MD5: stream the recipe once (the same code path the browser follows)
    t1 = time.perf_counter()
    recipe["md5"] = materialize(recipe, base_path, plan_inputs(plan), out=None, trust_md5=False)
    # md5 of the whole file as `md5sum` prints it (the ZIM checksum appended)
    recipe["file_md5"] = _LAST_FILE_MD5[0]
    stats = {"plan_s": round(t_plan, 4), "md5_pass_s": round(time.perf_counter() - t1, 2),
             "segments": len(segs),
             "inline_segments": sum(1 for s in segs if s["src"] == "inline"),
             "inline_bytes": sum(s["len"] for s in segs if s["src"] == "inline"),
             "slot_bytes": sum(x["len"] for x in slots),
             "base_bytes": sum(s["len"] for s in segs if s["src"] == "base"),
             "pack_bytes": sum(s["len"] for s in segs if s["src"] == "pack"),
             "generated_tail": L["generated_tail"], "size": L["total"]}
    return recipe, plan, stats


def fingerprint(recipe: dict, plan: Plan) -> str:
    """Everything that decides a merged file's bytes apart from the viewer
    slots and what they change (the UUID, which follows the base's MD5, and
    the checksum): the base's masked chunk hashes, size and slot ranges,
    every pack's chunk hashes, the map language, the generated metadata and
    map-config, the merger. A base patched in place (cloud/patch_viewer_inplace.py)
    keeps it; anything else changes it."""
    b = recipe["base"]
    doc = {"base": [b["size"], b["mask"], b["sha256"]],
           "packs": {k: [p["size"], p["sha256"]] for k, p in sorted(recipe["packs"].items())},
           # the generated parts say the rest (title, language list, wikiPacks order)
           "lang": recipe["map_language"], "meta": plan.meta, "mapcfg": plan.layout["mapcfg"].decode("utf-8", "replace"),
           "merger": MERGER_VERSION}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plan_inputs(plan: Plan) -> dict[str, str]:
    """{recipe pack key: file} of a plan: its packs and its search pack."""
    out = {p.lang: p.path for p in plan.packs}
    if plan.index is not None:
        out[SEARCH] = plan.index.path
    return out


def available_packs(packs_dir: str, base_path: str, region: str | None) -> dict[str, str]:
    """{lang: file} of the gated Wikipedia packs in packs_dir for this base
    (or region): one per language, the published naming osm_<region>_wiki-<xx>
    first, then the newest."""
    from tools.langpack.merge_zim import ZimReader, blob_of, open_pack
    base_uuid = ZimReader(base_path).header.uuid.hex()
    cands: dict[str, list] = {}
    for f in sorted(os.listdir(packs_dir)):
        fp = os.path.join(packs_dir, f)
        if not f.endswith(".zim") or not os.path.isfile(fp) or "_search-" in f:
            continue
        try:
            p = open_pack(fp)
        except Exception as e:  # noqa: BLE001  (partial / foreign files)
            print(f"skip {f}: {e}", file=sys.stderr)
            continue
        pj = json.loads(blob_of(p.z, (67, p.prefix + b"pack.json")) or b"{}")
        same_base = pj.get("base_uuid", "").replace("-", "") == base_uuid
        if not same_base and region and pj.get("region") != region and f"_{region}_" not in f:
            continue
        ok, why = pack_gate_ok(fp)            # review S1: a pack must pass its own gate
        if not ok:
            print(f"skip {f}: {why}", file=sys.stderr)
            continue
        cands.setdefault(p.lang, []).append((f.startswith(f"osm_{region}_wiki-"), os.path.getmtime(fp), fp))
    return {lang: max(c)[2] for lang, c in cands.items()}


def find_search_pack(packs_dir: str, base_path: str, pack_paths: list[str]) -> str | None:
    """The search pack (index_pack.py) in packs_dir built for exactly this
    base and these packs (its sidecar names their files and MD5s) and with a
    passing gate for its bytes; the newest such, or None."""
    from tools.langpack.gatelib import md5_file
    from tools.langpack.merge_zim import ZimReader, base_lineage
    lineage = base_lineage(base_path, ZimReader(base_path).header.uuid)
    want = {os.path.basename(p): md5_file(p) for p in pack_paths}
    found = []
    for f in os.listdir(packs_dir):
        fp = os.path.join(packs_dir, f)
        if not (f.endswith(".zim") and "_search-" in f and os.path.isfile(fp)):
            continue
        try:
            side = json.load(open(fp + ".json"))
        except (OSError, ValueError):
            continue
        if side.get("packs") != want or str(side.get("base_uuid", "")).replace("-", "") not in lineage:
            continue
        if pack_gate_ok(fp)[0]:
            found.append((os.path.getmtime(fp), fp))
    return max(found)[1] if found else None


class ChunkReader:
    """Reads ranges of one input, checking each 16 MiB chunk against its hash once."""

    def __init__(self, path: str, size: int, chunk: int, hashes: list[str], label: str, mask: list | None = None):
        self.mask = mask
        self.fd = os.open(path, os.O_RDONLY)
        if os.fstat(self.fd).st_size != size:
            raise MergeError(f"{label}: size {os.fstat(self.fd).st_size} != recipe {size}")
        self.chunk, self.hashes, self.label = chunk, hashes, label
        self.verified: set[int] = set()
        self.cache: tuple[int, bytes] | None = None

    def _chunk(self, k: int) -> bytes:
        if self.cache and self.cache[0] == k:
            return self.cache[1]
        b = os.pread(self.fd, self.chunk, k * self.chunk)
        if k not in self.verified:
            if hashlib.sha256(masked(b, k * self.chunk, self.mask)).hexdigest() != self.hashes[k]:
                raise MergeError(f"{self.label}: chunk {k} fails its SHA-256")
            self.verified.add(k)
        self.cache = (k, b)
        return b

    def read(self, off: int, n: int):
        end = off + n
        while off < end:
            k = off // self.chunk
            b = self._chunk(k)
            lo = off - k * self.chunk
            hi = min(len(b), end - k * self.chunk)
            if hi <= lo:
                raise MergeError(f"{self.label}: read past end")
            yield b[lo:hi]
            off += hi - lo


def materialize(recipe: dict, base_path: str, pack_paths: dict[str, str], out: str | None,
                trust_md5: bool = True) -> str:
    """Write (or, with out=None, only hash) the merged file; returns the MD5 hex.
    Every input chunk and inline segment is checked; the MD5 must match the
    recipe's unless trust_md5 is False (used while computing it)."""
    if recipe.get("format") != FORMAT:
        raise MergeError("not a streetzim-recipe/2")
    b = recipe["base"]
    readers = {"base": ChunkReader(base_path, b["size"], b["chunk"], b["sha256"], "base", b.get("mask"))}
    for lang, p in recipe["packs"].items():
        readers[lang] = ChunkReader(pack_paths[lang], p["size"], p["chunk"], p["sha256"], f"pack {lang}")
    md5 = hashlib.md5(usedforsecurity=False)
    part = out + ".part" if out else None
    fh = open(part, "wb", buffering=0) if part else None
    n = 0
    try:
        for s in recipe["segments"]:
            if s["src"] == "inline":
                data = base64.b64decode(s["data"])
                if len(data) != s["len"] or hashlib.sha256(data).hexdigest() != s["sha256"]:
                    raise MergeError(f"inline segment at {n} fails its SHA-256")
                parts = (data,)
            elif s["src"] == "base":
                for off, ln in b.get("mask") or []:
                    if s["off"] < off + ln and off < s["off"] + s["len"]:
                        # masked bytes are not covered by the chunk hashes
                        raise MergeError(f"base segment at {s['off']} reads the masked viewer slot at {off}")
                parts = readers["base"].read(s["off"], s["len"])
            elif s["src"] == "pack":
                parts = readers[s["pack"]].read(s["off"], s["len"])
            elif s["src"] == "md5":
                digest = md5.hexdigest()
                if trust_md5 and digest != recipe["md5"]:
                    raise MergeError(f"MD5 {digest} != recipe {recipe['md5']}")
                fmd5 = md5.copy()
                fmd5.update(bytes.fromhex(digest))
                _LAST_FILE_MD5[0] = fmd5.hexdigest()
                if trust_md5 and recipe.get("file_md5") and _LAST_FILE_MD5[0] != recipe["file_md5"]:
                    raise MergeError("file MD5 mismatch")
                if fh:
                    fh.write(bytes.fromhex(digest))
                n += 16
                continue
            else:
                raise MergeError(f"unknown segment {s['src']}")
            for p in parts:
                md5.update(p)
                if fh:
                    fh.write(p)
                n += len(p)
        if n != recipe["size"]:
            raise MergeError(f"materialized {n} != recipe size {recipe['size']}")
    except BaseException:
        if fh and part:
            fh.close()
            os.remove(part)
        raise
    if fh and part and out:
        fh.close()
        os.replace(part, out)
    return digest


def blob_of_mapcfg(plan) -> bytes:
    return plan.layout["mapcfg"]


def menu_packs(region_langs: list[str], base_wiki: str | None, L: str, available: dict[str, str]) -> list[str]:
    want = (set(region_langs) | {L, "en"}) - ({base_wiki} if base_wiki else set())
    return sorted(x for x in want if x in available)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("base")
    b.add_argument("packs", nargs="*")
    b.add_argument("--lang", required=True)
    b.add_argument("-o", "--out", required=True)
    b.add_argument("--region")
    b.add_argument("--title")
    b.add_argument("--region-langs", default="")
    b.add_argument("--check", help="compare the materialized recipe with this merge_zim.py output")
    b.add_argument("--index", help="the entry's search pack (index_pack.py)")
    mn = sub.add_parser("menu")
    mn.add_argument("base")
    mn.add_argument("--packs-dir", required=True)
    mn.add_argument("--langs", required=True)
    mn.add_argument("--region-langs", required=True)
    mn.add_argument("--out-dir", required=True)
    mn.add_argument("--region")
    mn.add_argument("--titles", default="{}", help='JSON {"th": "..."}')
    mn.add_argument("--language-meta", choices=("list", "map"), default="list")
    m = sub.add_parser("materialize")
    m.add_argument("recipe")
    m.add_argument("base")
    m.add_argument("packs", nargs="*")
    m.add_argument("-o", "--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "build":
        recipe, plan, st = build_recipe(a.base, a.packs, a.lang, region=a.region, title=a.title,
                                        region_langs=[x for x in a.region_langs.split(",") if x],
                                        index_path=a.index)
        with open(a.out, "w") as fh:
            json.dump(recipe, fh, ensure_ascii=False)
        st["recipe_json_bytes"] = os.path.getsize(a.out)
        st["md5"] = recipe["md5"]
        if a.check:
            t = time.perf_counter()
            tmp = a.out + ".materialized.zim"
            materialize(recipe, a.base, plan_inputs(plan), tmp)
            same = open(tmp, "rb").read() == open(a.check, "rb").read() if os.path.getsize(tmp) < (2 << 30) \
                else os.system(f"cmp -s '{tmp}' '{a.check}'") == 0
            os.remove(tmp)
            st["materialized_equals_merge"] = same
            st["materialize_s"] = round(time.perf_counter() - t, 2)
        st["peak_rss_MB"] = round(peak_rss_mb(), 1)
        print(json.dumps(st, indent=1))
    elif a.cmd == "menu":
        from tools.langpack.merge_zim import open_pack
        from tools.langpack.merge_zim import ZimReader, blob_of, KEY_MAPCFG
        region = a.region
        if region is None:
            import re as _re
            nm = blob_of(ZimReader(a.base), (77, b"Name")) or b""
            m_ = _re.match(rb"^osm_[a-z]{2,3}_(.+)$", nm)
            region = m_.group(1).decode() if m_ else None
        avail = available_packs(a.packs_dir, a.base, region)
        print(f"packs: {json.dumps(avail)}", file=sys.stderr)
        titles = json.loads(a.titles)
        os.makedirs(a.out_dir, exist_ok=True)
        cfg = json.loads(blob_of(ZimReader(a.base), KEY_MAPCFG) or b"{}")
        base_wiki = (cfg.get("language") or "en") if cfg.get("hasWikiArticles") else None
        rows = []
        for L in a.langs.split(","):
            packs = menu_packs(a.region_langs.split(","), base_wiki, L, avail)
            # the search pack built for exactly these packs (index_pack.py), if any:
            # without one Kiwix finds no pack article and search_gate.py fails the entry
            index = find_search_pack(a.packs_dir, a.base, [avail[x] for x in packs])
            recipe, plan, st = build_recipe(a.base, [avail[x] for x in packs], L, region=region,
                                            title=titles.get(L), region_langs=a.region_langs.split(","),
                                            language_meta=a.language_meta, index_path=index)
            path = os.path.join(a.out_dir, f"{L}.json")
            with open(path, "w") as fh:
                json.dump(recipe, fh, ensure_ascii=False)
            with open(os.path.join(a.out_dir, f"{L}.map-config.json"), "wb") as fh:
                fh.write(plan.layout["mapcfg"])
            want = (set(a.region_langs.split(",")) | {L, "en"}) - ({base_wiki} if base_wiki else set())
            st.update(L=L, packs=packs, pack_files=[avail[x] for x in packs], index_file=index,
                      missing_packs=sorted(want - set(packs)), recipe=path,
                      recipe_json_bytes=os.path.getsize(path),
                      wikiPacks=[w["lang"] for w in json.loads(blob_of_mapcfg(plan))["wikiPacks"]],
                      name=recipe["name"], md5=recipe["md5"])
            rows.append(st)
            print(json.dumps(st), flush=True)
    else:
        recipe = json.load(open(a.recipe))
        packs = {}
        from tools.langpack.merge_zim import open_index, open_pack
        for p in a.packs:
            try:
                packs[open_pack(p).lang] = p
            except MergeError:
                open_index(p)                       # a search pack
                packs[SEARCH] = p
        t = time.perf_counter()
        d = materialize(recipe, a.base, packs, a.out)
        print(json.dumps({"out": a.out, "md5": d, "s": round(time.perf_counter() - t, 2)}))


if __name__ == "__main__":
    sys.exit(main())
