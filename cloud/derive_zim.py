#!/usr/bin/env python3
"""streetzim-derive: make a variant of an existing ZIM without re-packing it.

    python3 cloud/derive_zim.py SRC.zim DST.zim --light
    python3 cloud/derive_zim.py SRC.zim DST.zim --no-satellite --max-tile-zoom 13
    python3 cloud/derive_zim.py SRC.zim DST.zim --regroup-tiles --tile-order hilbert
    python3 cloud/derive_zim.py SRC.zim DST.zim --light --dry-run

How it works (see docs/zim-variants.md): every dirent is classified keep or
drop by path. A cluster whose blobs are all kept is copied byte-for-byte, so
no decompression and no recompression. A cluster that is partly dropped, or
holds a blob we rewrite (map-config.json, metadata, wiki-geo-index.json), is
re-encoded with only the kept blobs, keeping its compression kind. A cluster
with nothing kept disappears. Dirents are re-sorted, redirect and cluster
indexes remapped, the title listing regenerated and the MD5 rewritten.

``--regroup-tiles`` additionally pulls every tiles/satellite/terrain blob out
of its cluster and re-buckets it zoom-major (one cluster run per zoom, never
mixing zooms or components), ordered within a zoom by --tile-order. That is
the layout the ordering contract asks the builder for; here it lets the
access simulator (cloud/zim_access_sim.py) measure it on real files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import tempfile
import time
import uuid as uuidlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud.tile_order import tile_sort_key as _tile_sort_key  # noqa: E402
from cloud.search_shards import TIER_TYPES  # noqa: E402
from cloud.zimfmt import (  # noqa: E402
    COMP_NONE, MIME_REDIRECT, NO_MAIN_PAGE, TITLE_LISTING_V0, TITLE_LISTING_V1,
    Dirent, ZimReader, ZimWriter, encode_cluster, sort_key_title, sort_key_url)

TILE_RE = re.compile(r"^(tiles|satellite|terrain)/(\d+)/(\d+)/(\d+)\.\w+$")
DEFAULT_CLUSTER_TARGET = 8 << 20   # uncompressed bytes, matches manifest_writer


def _encode_job(args):
    blobs, compress, level = args
    return encode_cluster(blobs, compress, level)


class SpillStore:
    """Regrouped blobs on disk, bucketed by the high bits of their sort key so
    each bucket can be sorted in memory on its own. A continent's z14 tiles
    are tens of GB; a bucket is 1/256 of a zoom level. Record layout:
    key u64, src cluster u32, src blob u32, len u32, data."""

    BUCKET_BITS = 8

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._fh: dict[tuple, object] = {}
        self.count: Counter = Counter()
        self.bytes = 0

    def _bucket(self, z: int, key: int) -> int:
        shift = max(0, 2 * z - self.BUCKET_BITS)
        return key >> shift

    def add(self, comp: str, z: int, key: int, data: bytes, ref: tuple[int, int]):
        k = (comp, z, self._bucket(z, key))
        fh = self._fh.get(k)
        if fh is None:
            fh = self._fh[k] = open(self.root / f"{comp}-{z}-{k[2]:04d}.spill", "ab")
        fh.write(struct.pack("<QIII", key, ref[0], ref[1], len(data)))
        fh.write(data)
        self.count[(comp, z)] += 1
        self.bytes += len(data)

    def groups(self) -> list[tuple[str, int]]:
        return sorted(self.count, key=lambda k: (("tiles", "satellite", "terrain").index(k[0]), k[1]))

    def iter_sorted(self, comp: str, z: int):
        """Yield (key, data, ref) in key order across the group's buckets."""
        for fh in self._fh.values():
            fh.flush()
        names = sorted(p for p in self.root.iterdir() if p.name.startswith(f"{comp}-{z}-"))
        for name in names:
            buf = name.read_bytes()
            items = []
            off = 0
            while off < len(buf):
                key, c, b, n = struct.unpack_from("<QIII", buf, off)
                off += 20
                items.append((key, buf[off:off + n], (c, b)))
                off += n
            items.sort(key=lambda t: t[0])
            yield from items
            name.unlink()

    def close(self):
        for fh in self._fh.values():
            fh.close()
        for p in self.root.iterdir():
            p.unlink()
        self.root.rmdir()


# ------------------------------------------------------------------ plan --

@dataclass
class Recipe:
    drop_prefixes: list[str] = field(default_factory=list)
    max_tile_zoom: int | None = None
    satellite_max_zoom: int | None = None     # -1 = drop all
    terrain_max_zoom: int | None = None
    no_routing: bool = False
    no_wiki: bool = False
    strip_addresses: bool = False
    regroup_tiles: bool = False
    tile_order: str = "hilbert"
    cluster_target: int = DEFAULT_CLUSTER_TARGET
    level: int = 22
    name: str | None = None
    title: str | None = None
    description: str | None = None
    flavour: str | None = None
    config_patch: dict = field(default_factory=dict)
    keep_uuid: bool = False

    def describe(self) -> list[str]:
        out = []
        if self.satellite_max_zoom == -1: out.append("drop satellite")
        elif self.satellite_max_zoom is not None: out.append(f"satellite <= z{self.satellite_max_zoom}")
        if self.terrain_max_zoom == -1: out.append("drop terrain")
        elif self.terrain_max_zoom is not None: out.append(f"terrain <= z{self.terrain_max_zoom}")
        if self.max_tile_zoom is not None: out.append(f"tiles <= z{self.max_tile_zoom}")
        if self.no_routing: out.append("drop routing")
        if self.no_wiki: out.append("drop wiki articles/images")
        if self.strip_addresses: out.append("strip address records from search-data")
        for p in self.drop_prefixes: out.append(f"drop {p}*")
        if self.regroup_tiles: out.append(f"regroup tiles zoom-major ({self.tile_order})")
        return out


def keep_entry(d: Dirent, r: Recipe) -> bool:
    """Path rules. Redirects are decided later from their target."""
    if d.namespace != "C":
        return True
    p = d.path
    m = TILE_RE.match(p)
    if m:
        kind, z = m.group(1), int(m.group(2))
        if kind == "tiles":
            return r.max_tile_zoom is None or z <= r.max_tile_zoom
        if kind == "satellite":
            return r.satellite_max_zoom is None or (r.satellite_max_zoom >= 0 and z <= r.satellite_max_zoom)
        if kind == "terrain":
            return r.terrain_max_zoom is None or (r.terrain_max_zoom >= 0 and z <= r.terrain_max_zoom)
    if r.no_routing and (p.startswith("routing-data/") or p == "routing-worker.js"):
        # keep the worker: it is a slot and 68 KB; hasRouting=false disables it
        return p == "routing-worker.js"
    if r.no_wiki and (p.startswith("wiki-article/") or p.startswith("wiki-image/")):
        return False
    for pre in r.drop_prefixes:
        if p.startswith(pre):
            return False
    return True


def tile_sort_key(order: str, z: int, x: int, y: int) -> int:
    return _tile_sort_key(order, z, x, y)


# --------------------------------------------------------------- rewrite --

def _patch_map_config(raw: bytes, recipe: Recipe, dropped_components: set[str]) -> bytes:
    cfg = json.loads(raw)
    if recipe.max_tile_zoom is not None:
        cfg["maxZoom"] = min(int(cfg.get("maxZoom", 14)), recipe.max_tile_zoom)
    if recipe.satellite_max_zoom == -1 or "satellite" in dropped_components:
        cfg["hasSatellite"] = False
        cfg.pop("satelliteMaxZoom", None)
    elif recipe.satellite_max_zoom is not None and cfg.get("hasSatellite"):
        cfg["satelliteMaxZoom"] = min(int(cfg.get("satelliteMaxZoom", 14)), recipe.satellite_max_zoom)
    if recipe.terrain_max_zoom == -1 or "terrain" in dropped_components:
        cfg["hasTerrain"] = False
        cfg.pop("terrainMaxZoom", None)
    elif recipe.terrain_max_zoom is not None and cfg.get("hasTerrain"):
        cfg["terrainMaxZoom"] = min(int(cfg.get("terrainMaxZoom", 12)), recipe.terrain_max_zoom)
    if recipe.no_routing:
        cfg["hasRouting"] = False
    if recipe.no_wiki:
        cfg["hasWikiArticles"] = False
    if recipe.strip_addresses:
        cfg["hasOvertureAddresses"] = False   # attribution section; the data is gone
        cfg["hasAddresses"] = False
    cfg.update(recipe.config_patch)
    derived = cfg.setdefault("derived", {})
    derived["recipe"] = recipe.describe()
    return json.dumps(cfg, indent=2).encode("utf-8")


def _patch_streetzim_meta(raw: bytes, recipe: Recipe, src_uuid: bytes, src_name: str) -> bytes:
    try:
        meta = json.loads(raw)
    except Exception:
        return raw
    meta["derivedFrom"] = {"uuid": str(uuidlib.UUID(bytes=src_uuid)), "file": src_name,
                           "recipe": recipe.describe(),
                           "date": time.strftime("%Y-%m-%d")}
    if recipe.satellite_max_zoom == -1: meta["hasSatellite"] = False
    if recipe.terrain_max_zoom == -1: meta["hasTerrain"] = False
    if recipe.no_routing: meta["hasRouting"] = False
    if recipe.strip_addresses:
        meta["hasAddresses"] = False
        meta["hasOvertureAddresses"] = False
        counts = meta.get("counts")
        if isinstance(counts, dict):
            counts["addresses"] = 0
            by = counts.get("byType")
            if isinstance(by, dict):
                for t in ADDRESS_TYPES:
                    by.pop(t, None)
            counts["total"] = sum(v for v in (by or {}).values() if isinstance(v, int))
    return json.dumps(meta, separators=(",", ":")).encode("utf-8")


ADDRESS_TYPES = TIER_TYPES["a"]
SEARCH_PREFIX = b"search-data/"
SEARCH_MANIFEST = b"search-data/manifest.json"


def _is_address_leaf_name(name: str) -> bool:
    """Character-split leaves carry their tier as the last '~' token; tier
    'a' holds only address records (cloud/search_shards.py)."""
    return name.rsplit("~", 1)[-1] == "a" and "~" in name


def strip_address_records(raw: bytes) -> tuple[bytes, int, int]:
    """Drop ``t`` in ADDRESS_TYPES from one leaf. Returns (new bytes, kept,
    dropped). Legacy leaves (hash buckets, no tier) mix types, so every
    record is inspected; the compact ``t`` key is what the builder writes,
    ``type`` is accepted for older files."""
    recs = json.loads(raw)
    if not isinstance(recs, list):
        return raw, 0, 0
    kept = [r for r in recs if not (isinstance(r, dict)
                                    and (r.get("t") or r.get("type")) in ADDRESS_TYPES)]
    dropped = len(recs) - len(kept)
    if dropped == 0:
        return raw, len(recs), 0
    return json.dumps(kept, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), len(kept), dropped


# ----------------------------------------------------------------- derive --

def derive(src_path: str, dst_path: str, recipe: Recipe, *, dry_run: bool = False,
           verbose: bool = True) -> dict:
    t0 = time.time()
    r = ZimReader(src_path)
    h = r.header
    log = (lambda *a: print(*a, flush=True)) if verbose else (lambda *a: None)
    log(f"source: {src_path} ({r.size/1e9:.3f} GB, {h.entry_count} entries, {h.cluster_count} clusters)")
    log("recipe: " + ("; ".join(recipe.describe()) or "copy"))

    # 1. classify dirents
    dirents = [d for _, d in r.dirents()]
    keep = [False] * len(dirents)
    for i, d in enumerate(dirents):
        if d.is_redirect:
            continue
        if d.namespace == "X" and d.url in (TITLE_LISTING_V0, TITLE_LISTING_V1):
            continue  # regenerated
        keep[i] = keep_entry(d, recipe)
    for i, d in enumerate(dirents):
        if d.is_redirect:
            keep[i] = keep[d.redirect]
    dropped_components = set()
    present_components = set()
    for i, d in enumerate(dirents):
        m = TILE_RE.match(d.path) if d.namespace == "C" else None
        if m:
            present_components.add(m.group(1))
            if keep[i]:
                dropped_components.discard(m.group(1))
            elif m.group(1) not in dropped_components and not any(
                    keep[j] for j in ()):  # placeholder, resolved below
                pass
    kept_components = {TILE_RE.match(d.path).group(1) for i, d in enumerate(dirents)
                       if keep[i] and d.namespace == "C" and TILE_RE.match(d.path)}
    dropped_components = present_components - kept_components

    # 2. rewrites (path -> bytes); their clusters must be re-encoded
    rewrites: dict[tuple[str, bytes], bytes] = {}
    src_name = os.path.basename(src_path)

    def _get(ns, url):
        i = r.find(ns, url)
        return None if i < 0 else r.content(dirents[i])

    mc = _get("C", b"map-config.json")
    if mc is not None:
        rewrites[("C", b"map-config.json")] = _patch_map_config(mc, recipe, dropped_components)
    sm = _get("C", b"streetzim-meta.json")
    if sm is not None:
        rewrites[("C", b"streetzim-meta.json")] = _patch_streetzim_meta(sm, recipe, h.uuid, src_name)
    if recipe.no_wiki:
        geo = _get("C", b"wiki-geo-index.json")
        if geo is not None:
            rewrites[("C", b"wiki-geo-index.json")] = b"{}"
    addr_dropped = addr_leaves = 0
    if recipe.strip_addresses:
        man_i = r.find("C", SEARCH_MANIFEST)
        if man_i < 0:
            raise SystemExit("--strip-addresses: source has no search-data/manifest.json")
        manifest = json.loads(r.content(dirents[man_i]))
        chunks = manifest.get("chunks")
        if not isinstance(chunks, dict):
            raise SystemExit("--strip-addresses: manifest has no 'chunks' map")
        empty = json.dumps([]).encode()
        for i, d in enumerate(dirents):
            if d.is_redirect or d.namespace != "C" or not d.url.startswith(SEARCH_PREFIX) \
                    or d.url == SEARCH_MANIFEST or not d.url.endswith(b".json"):
                continue
            name = d.path[len("search-data/"):-len(".json")]
            if _is_address_leaf_name(name):
                # whole tier-a leaf: keep the entry as [] so the viewer's manifest
                # lookup still hits directly (a missing name triggers its slow
                # miss-branch scans) but the bytes go
                n = chunks.get(name)
                addr_dropped += n if isinstance(n, int) else 0
                addr_leaves += 1
                rewrites[("C", d.url)] = empty
                if name in chunks:
                    chunks[name] = 0
                continue
            new, kept, dropped = strip_address_records(r.content(d))
            if dropped:
                addr_dropped += dropped
                addr_leaves += 1
                rewrites[("C", d.url)] = new
                if name in chunks:
                    chunks[name] = kept
            r._blob_cache = None
        # 'total' counts unique features while chunk counts count leaf records
        # (a record sits in every character path that reaches it), so derive
        # the new total from the build's own unique address count.
        meta_i = r.find("C", b"streetzim-meta.json")
        uniq_addr = None
        if meta_i >= 0:
            try:
                uniq_addr = json.loads(r.content(dirents[meta_i])).get("counts", {}).get("addresses")
            except Exception:  # noqa: BLE001
                uniq_addr = None
        if isinstance(manifest.get("total"), int) and isinstance(uniq_addr, int):
            manifest["total"] = max(0, manifest["total"] - uniq_addr)
        manifest["addresses_stripped"] = True
        rewrites[("C", SEARCH_MANIFEST)] = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        log(f"  addresses: {addr_dropped} leaf records removed from {addr_leaves} leaves "
            f"({uniq_addr if uniq_addr is not None else '?'} unique addresses); "
            f"manifest total {manifest.get('total')}")
    for key, val in (("Name", recipe.name), ("Title", recipe.title),
                     ("Description", recipe.description), ("Flavour", recipe.flavour)):
        if val is not None:
            rewrites[("M", key.encode())] = val.encode("utf-8")
    # Counter metadata: mime=count over kept C items
    counter = Counter()
    for i, d in enumerate(dirents):
        if keep[i] and d.namespace == "C" and not d.is_redirect:
            counter[r.mime_of(d)] += 1
    rewrites[("M", b"Counter")] = ";".join(f"{m}={n}" for m, n in counter.items()).encode()

    # 3. cluster plan
    kept_blobs: dict[int, set[int]] = defaultdict(set)
    total_blobs: dict[int, int] = {}
    for i, d in enumerate(dirents):
        if not d.is_redirect and keep[i]:
            kept_blobs[d.cluster].add(d.blob)
    regroup_refs: dict[int, dict[int, tuple]] = defaultdict(dict)  # cluster -> blob -> (comp, z, key)
    rewrite_refs: dict[int, dict[int, bytes]] = defaultdict(dict)
    by_ns_url = {(d.namespace, d.url): i for i, d in enumerate(dirents)}
    for key, data in rewrites.items():
        i = by_ns_url.get(key)
        if i is None:
            continue
        d = dirents[i]
        rewrite_refs[d.cluster][d.blob] = data
    if recipe.regroup_tiles:
        for i, d in enumerate(dirents):
            if not keep[i] or d.is_redirect or d.namespace != "C":
                continue
            m = TILE_RE.match(d.path)
            if m:
                comp, z, x, y = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
                regroup_refs[d.cluster][d.blob] = (comp, z, tile_sort_key(recipe.tile_order, z, x, y))

    plan = {"copy": [], "reencode": [], "drop": []}
    plan_bytes = Counter()
    for c in range(h.cluster_count):
        ci = r.cluster_info(c)
        n = r.blob_count(c) if (c in kept_blobs or c in regroup_refs) else None
        kb = kept_blobs.get(c, set())
        if not kb:
            plan["drop"].append(c)
            plan_bytes["drop"] += ci.size
        elif len(kb) == n and c not in rewrite_refs and c not in regroup_refs:
            plan["copy"].append(c)
            plan_bytes["copy"] += ci.size
        else:
            plan["reencode"].append(c)
            plan_bytes["reencode"] += ci.size
    log(f"plan: copy {len(plan['copy'])} clusters ({plan_bytes['copy']/1e6:.1f} MB), "
        f"re-encode {len(plan['reencode'])} ({plan_bytes['reencode']/1e6:.1f} MB), "
        f"drop {len(plan['drop'])} ({plan_bytes['drop']/1e6:.1f} MB); "
        f"entries kept {sum(keep)} / {len(dirents)}")
    result = {"plan": {k: len(v) for k, v in plan.items()}, "plan_bytes": dict(plan_bytes),
              "entries_kept": sum(keep), "entries_src": len(dirents),
              "rewrites": [k[1].decode() for k in rewrites]}
    if dry_run:
        return result

    # 4. emit
    new_uuid = h.uuid if recipe.keep_uuid else uuidlib.uuid4().bytes
    w = ZimWriter(dst_path, r.mimes, uuid=new_uuid, major=h.major, minor=h.minor)
    w.begin()
    # (old cluster, old blob) -> (new cluster, new blob)
    remap: dict[tuple[int, int], tuple[int, int]] = {}
    spill = SpillStore(Path(tempfile.mkdtemp(prefix="derive-spill-", dir=os.path.dirname(os.path.abspath(dst_path)))))
    regroup_emitted = False
    first_regroup_cluster = min(regroup_refs) if regroup_refs else None
    tiles_regrouped = 0

    def flush_regroup():
        """Emit the regrouped components: one run of clusters per (component,
        zoom), never mixing zooms. Clusters are encoded in parallel (zstd-22
        runs at ~3 MB/s per core) and appended in order."""
        nonlocal regroup_emitted
        tiles_regrouped_ref = [0]
        from multiprocessing import Pool
        order = ("tiles", "satellite", "terrain")
        with Pool(max(1, os.cpu_count() or 1)) as pool:
            for (comp, z) in spill.groups():
                compress = comp == "tiles"   # satellite/terrain are pre-compressed images
                n_clusters = 0

                def batches():
                    cur: list[bytes] = []; cur_refs: list[tuple] = []; size = 0
                    for _key, data, ref in spill.iter_sorted(comp, z):
                        cur.append(data); cur_refs.append(ref); size += len(data)
                        if size >= recipe.cluster_target:
                            yield cur, cur_refs
                            cur, cur_refs, size = [], [], 0
                    if cur:
                        yield cur, cur_refs
                # keep a bounded window of encode jobs in flight
                window = 2 * (os.cpu_count() or 1)
                pending: list[tuple[list, object]] = []

                def drain(all_: bool):
                    nonlocal n_clusters
                    while pending and (all_ or len(pending) >= window):
                        refs, fut = pending.pop(0)
                        nc = w.add_cluster(fut.get())
                        for bi, ref in enumerate(refs):
                            remap[ref] = (nc, bi)
                        tiles_regrouped_ref[0] += len(refs)
                        n_clusters += 1
                for blobs, refs in batches():
                    pending.append((refs, pool.apply_async(_encode_job, ((blobs, compress, recipe.level),))))
                    drain(False)
                drain(True)
                log(f"  {comp}/z{z}: {spill.count[(comp, z)]} blobs -> {n_clusters} clusters")
        spill.close()
        regroup_emitted = True
        return tiles_regrouped_ref[0]

    copied = reenc = 0
    from multiprocessing import Pool
    pool = Pool(max(1, os.cpu_count() or 1))
    pending: list[tuple[list[tuple[int, int]], object]] = []   # ordered encode jobs
    window = 2 * (os.cpu_count() or 1)

    def drain(all_: bool):
        nonlocal reenc
        while pending and (all_ or len(pending) >= window):
            refs, fut = pending.pop(0)
            nc = w.add_cluster(fut.get())
            for bi, ref in enumerate(refs):
                remap[ref] = (nc, bi)
            reenc += 1

    drop_set = set(plan["drop"])
    copy_set = set(plan["copy"])
    for c in range(h.cluster_count):
        if c in drop_set and c not in regroup_refs:
            continue
        ci = r.cluster_info(c)
        kb = kept_blobs.get(c, set())
        if c in copy_set:
            drain(True)   # keep cluster order: pending re-encodes land before this copy
            nc = w.copy_cluster_from(r, c)
            for b in kb:
                remap[(c, b)] = (nc, b)
            copied += 1
            continue
        # partial / rewritten / regroup source: decode
        offs, body = r.cluster_offsets(c)
        blobs: list[bytes] = []
        refs: list[tuple[int, int]] = []
        for b in sorted(kb):
            data = body[offs[b]:offs[b + 1]]
            if b in rewrite_refs.get(c, {}):
                data = rewrite_refs[c][b]
            if b in regroup_refs.get(c, {}):
                comp, z, key = regroup_refs[c][b]
                spill.add(comp, z, key, bytes(data), (c, b))
                continue
            blobs.append(bytes(data))
            refs.append((c, b))
        if blobs:
            pending.append((refs, pool.apply_async(
                _encode_job, ((blobs, ci.compression != COMP_NONE, recipe.level),))))
            drain(False)
        r._blob_cache = None
    drain(True)
    pool.close(); pool.join()
    if regroup_refs:
        log(f"regrouping {sum(spill.count.values())} tiles "
            f"({spill.bytes/1e6:.1f} MB uncompressed, spilled to disk) ...")
        tiles_regrouped = flush_regroup()
    else:
        spill.close()

    # 5. dirents
    old_to_new_index: dict[int, int] = {}
    new_dirents: list[Dirent] = []
    kept_idx = [i for i in range(len(dirents)) if keep[i]]
    # add regenerated title listing entry (X namespace); filled after sort
    listing_mime = r.mimes.index("application/octet-stream+zimlisting") \
        if "application/octet-stream+zimlisting" in r.mimes else None
    kept_idx.sort(key=lambda i: sort_key_url(dirents[i]))
    for i in kept_idx:
        d = dirents[i]
        nd = Dirent(d.mime, d.namespace, d.revision, d.url, d.title, d.parameter)
        if d.is_redirect:
            nd.redirect = d.redirect  # remapped below
        else:
            nd.cluster, nd.blob = remap[(d.cluster, d.blob)]
        old_to_new_index[i] = len(new_dirents)
        new_dirents.append(nd)
    for nd in new_dirents:
        if nd.is_redirect:
            nd.redirect = old_to_new_index[nd.redirect]
    main_page = old_to_new_index.get(h.main_page, NO_MAIN_PAGE) if h.main_page != NO_MAIN_PAGE else NO_MAIN_PAGE
    if listing_mime is not None:
        # v1 = every entry except the listing itself, by (namespace, title): what
        # the source carried (verified on washington-dc: 9009 of 9010).
        placeholder = Dirent(listing_mime, "X", 0, TITLE_LISTING_V1, b"")
        new_dirents.append(placeholder)
        new_dirents.sort(key=sort_key_url)
        li = new_dirents.index(placeholder)
        # inserting shifts indexes >= li by one; fix redirects and main page
        def shift(i): return i + 1 if i >= li else i
        for nd in new_dirents:
            if nd.is_redirect and nd is not placeholder:
                nd.redirect = shift(nd.redirect)
        if main_page != NO_MAIN_PAGE:
            main_page = shift(main_page)
        order = [i for i in range(len(new_dirents)) if i != li]
        order.sort(key=lambda i: sort_key_title(new_dirents[i]))
        import numpy as np
        listing = np.asarray(order, dtype="<u4").tobytes()
        nc = w.add_cluster(encode_cluster([listing], False))
        placeholder.cluster, placeholder.blob = nc, 0
    hdr = w.finish(new_dirents, main_page=main_page)
    dt = time.time() - t0
    out_size = os.path.getsize(dst_path)
    import resource
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    log(f"peak RSS {peak_mb:.0f} MB (main process)")
    log(f"wrote {dst_path}: {out_size/1e9:.3f} GB ({100*out_size/r.size:.1f}% of source), "
        f"{hdr.entry_count} entries, {hdr.cluster_count} clusters "
        f"(copied {copied}, re-encoded {reenc}{', regrouped ' + str(tiles_regrouped) + ' tiles' if tiles_regrouped else ''}) in {dt:.1f} s")
    result.update({"out_size": out_size, "seconds": dt, "copied": copied, "reencoded": reenc,
                   "uuid": str(uuidlib.UUID(bytes=new_uuid))})
    return result


# -------------------------------------------------------------------- cli --

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="streetzim-derive", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    g = ap.add_argument_group("presets")
    g.add_argument("--light", action="store_true", help="= --no-satellite --max-tile-zoom 13")
    g = ap.add_argument_group("content")
    g.add_argument("--no-satellite", action="store_true")
    g.add_argument("--satellite-max-zoom", type=int)
    g.add_argument("--no-terrain", action="store_true")
    g.add_argument("--terrain-max-zoom", type=int)
    g.add_argument("--max-tile-zoom", type=int, help="drop vector tiles deeper than this")
    g.add_argument("--no-routing", action="store_true")
    g.add_argument("--no-wiki", action="store_true", help="drop wiki-article/ and wiki-image/")
    g.add_argument("--strip-addresses", action="store_true",
                   help="remove address records from search-data (tier-a leaves become []; "
                        "legacy mixed leaves are filtered); re-encodes the search clusters")
    g.add_argument("--drop-prefix", action="append", default=[], metavar="PREFIX")
    g = ap.add_argument_group("layout")
    g.add_argument("--regroup-tiles", action="store_true",
                   help="re-cluster tiles/satellite/terrain zoom-major (re-encodes them)")
    g.add_argument("--tile-order", choices=["hilbert", "xy", "yx"], default="hilbert")
    g.add_argument("--cluster-target", type=int, default=DEFAULT_CLUSTER_TARGET,
                   help="uncompressed bytes per regrouped cluster (default 8 MiB)")
    g.add_argument("--level", type=int, default=22, help="zstd level for re-encoded clusters")
    g = ap.add_argument_group("identity")
    g.add_argument("--name"); g.add_argument("--title"); g.add_argument("--description")
    g.add_argument("--flavour")
    g.add_argument("--set-config", action="append", default=[], metavar="KEY=JSON",
                   help="patch map-config.json, e.g. maxZoom=13 or hasSatellite=false")
    g.add_argument("--keep-uuid", action="store_true",
                   help="keep the source UUID (Kiwix then treats it as the same book)")
    ap.add_argument("--dry-run", action="store_true", help="print the cluster plan only")
    ap.add_argument("-q", "--quiet", action="store_true")
    return ap


def recipe_from_args(a) -> Recipe:
    rec = Recipe(drop_prefixes=a.drop_prefix, max_tile_zoom=a.max_tile_zoom,
                 no_routing=a.no_routing, no_wiki=a.no_wiki, strip_addresses=a.strip_addresses,
                 regroup_tiles=a.regroup_tiles,
                 tile_order=a.tile_order, cluster_target=a.cluster_target, level=a.level,
                 name=a.name, title=a.title, description=a.description, flavour=a.flavour,
                 keep_uuid=a.keep_uuid)
    if a.light:
        rec.satellite_max_zoom = -1
        rec.max_tile_zoom = 13 if a.max_tile_zoom is None else a.max_tile_zoom
    if a.no_satellite: rec.satellite_max_zoom = -1
    elif a.satellite_max_zoom is not None: rec.satellite_max_zoom = a.satellite_max_zoom
    if a.no_terrain: rec.terrain_max_zoom = -1
    elif a.terrain_max_zoom is not None: rec.terrain_max_zoom = a.terrain_max_zoom
    for kv in a.set_config:
        k, _, v = kv.partition("=")
        try:
            rec.config_patch[k] = json.loads(v)
        except json.JSONDecodeError:
            rec.config_patch[k] = v
    return rec


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if not a.dst and not a.dry_run:
        print("error: DST required unless --dry-run", file=sys.stderr)
        return 2
    res = derive(a.src, a.dst or "", recipe_from_args(a), dry_run=a.dry_run, verbose=not a.quiet)
    if a.quiet:
        print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
