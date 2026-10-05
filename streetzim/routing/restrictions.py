"""OSM turn restrictions for the routing graph.

A restriction becomes a *node path* in graph numbering: ``[u, v, w]`` for a
via node (arrive at junction ``v`` from neighbour ``u``, leave towards ``w``),
``[u, a, ..., b, w]`` for via ways (the junctions along the via ways). Kind
NO bans the last step; kind ONLY allows only it. Flags say which modes it
binds: cars, bikes (walkers are never bound).

Storage (docs/formats.md, "Turn restrictions"):

* the builder writes ``routing-restrictions.bin`` beside
  ``routing-graph.bin`` (paths in SZRG node numbering);
* ``build_spatial`` renumbers the paths and appends, to each SZRC cell that
  holds a path's first via node (``path[1]``), an ``SZTR`` trailer after the
  geometry blob. Readers that predate it stop at the geometry blob.

Both files share one layout::

    b"SZTR" u32 version=1, u32 R, u32 P
    u32 offsets[R + 1]      -- into the node pool; path r = pool[off[r]:off[r+1]]
    u8  flags[R], padded to a multiple of 4
    u32 pool[P]

Routers (spatial_astar.find_route_spatial, routing-worker.js) match paths
as they search; see ``TurnState`` in spatial_astar for the rules.
"""
from __future__ import annotations

import struct
from collections import Counter

import numpy as np

SZTR_MAGIC = b"SZTR"
SZTR_VERSION = 1

ONLY = 1          # flags bit 0: allow only the last step (else: ban it)
CAR = 2           # bit 1: binds cars
BIKE = 4          # bit 2: binds bikes

U_TURN_PENALTY_S = {"drive": 45.0, "bike": 20.0, "walk": 0.0}

_NO = ("no_left_turn", "no_right_turn", "no_straight_on", "no_u_turn",
       "no_entry", "no_exit")
_ONLY = ("only_left_turn", "only_right_turn", "only_straight_on", "only_u_turn")


def mode_bit(travel_mode: str) -> int:
    return CAR if travel_mode == "drive" else BIKE if travel_mode == "bike" else 0


def _kind(value):
    if value is None:
        return None
    v = value.strip()
    if v in _NO:
        return "no"
    if v in _ONLY:
        return "only"
    return None          # *_on_red, conditional '@' values, unknown


def _except(tags):
    return {s.strip() for s in (tags.get("except") or "").split(";") if s.strip()}


def kinds_for(tags) -> dict:
    """{'car': 'no'|'only'|None, 'bike': ...} for a restriction relation's
    tags. Cars: restriction:motorcar > :motor_vehicle > :vehicle >
    restriction, unless except names them. Bikes: restriction:bicycle >
    :vehicle > restriction, unless except=bicycle; a plain only_* binds
    bikes only when tagged restriction:bicycle (an only_* for cars would
    otherwise ban the cycle track beside it)."""
    exc = _except(tags)
    car = None
    for k in ("restriction:motorcar", "restriction:motor_vehicle",
              "restriction:vehicle", "restriction"):
        if k in tags:
            car = _kind(tags[k])
            break
    if exc & {"motorcar", "motor_vehicle", "vehicle"}:
        car = None
    bike = None
    if "restriction:bicycle" in tags:
        bike = _kind(tags["restriction:bicycle"])
    else:
        for k in ("restriction:vehicle", "restriction"):
            if k in tags:
                bike = _kind(tags[k])
                break
        if bike == "only":
            bike = None
    if "bicycle" in exc:
        bike = None
    return {"car": car, "bike": bike}


class Collector:
    """Restriction relations, read in Pass 1 (relations follow ways in a
    PBF, so they are all known before Pass 2)."""

    def __init__(self):
        self.raw = []                 # (kinds, from_ways, via_node|None, via_ways, to_ways)
        self.member_ways = set()
        self.skipped = Counter()

    def relation(self, r):
        if r.tags.get("type") != "restriction":
            return
        d = {t.k: t.v for t in r.tags}
        kinds = kinds_for(d)
        if kinds["car"] is None and kinds["bike"] is None:
            if any(k.endswith(":conditional") for k in d):
                self.skipped["conditional (not supported yet)"] += 1
            else:
                self.skipped["not for cars or bikes, on red, or unknown"] += 1
            return
        frm, to, via_ways, via_nodes = [], [], [], []
        for m in r.members:
            if m.role == "from" and m.type == "w":
                frm.append(m.ref)
            elif m.role == "to" and m.type == "w":
                to.append(m.ref)
            elif m.role == "via" and m.type == "n":
                via_nodes.append(m.ref)
            elif m.role == "via" and m.type == "w":
                via_ways.append(m.ref)
        if not frm or not to or (len(via_nodes) + (1 if via_ways else 0)) != 1 \
                or len(via_nodes) > 1:
            self.skipped["malformed members"] += 1
            return
        self.raw.append((kinds, frm, via_nodes[0] if via_nodes else None,
                         via_ways, to))
        self.member_ways.update(frm)
        self.member_ways.update(to)
        self.member_ways.update(via_ways)


def _arrive_from(seq, oneway, v):
    """Neighbouring junction a traveller on this way comes from when it
    reaches graph node v; None if v is not on it or the direction is
    ambiguous (v inside a two-way way)."""
    pos = [i for i, (n, _r) in enumerate(seq) if n == v]
    out = set()
    for i in pos:
        if oneway == 1 and i > 0:
            out.add(seq[i - 1][0])
        elif oneway == -1 and i < len(seq) - 1:
            out.add(seq[i + 1][0])
        elif oneway == 0:
            if i == len(seq) - 1 and i > 0:
                out.add(seq[i - 1][0])
            elif i == 0 and len(seq) > 1:
                out.add(seq[1][0])
            else:
                return None
    return out.pop() if len(out) == 1 else None


def _leave_to(seq, oneway, v):
    rev = {1: -1, -1: 1, 0: 0}[oneway]
    return _arrive_from(seq, rev, v)


def _via_chain(start, ways, way_seq):
    """Junction path from graph node `start` through the via ways (in any
    member order), ending where the last way ends; None if they don't
    form a chain from `start`."""
    path = [start]
    left = list(ways)
    cur = start
    while left:
        for wid in left:
            seq = [n for n, _r in way_seq[wid][0]]
            if seq[0] == cur:
                path.extend(seq[1:])
                cur = seq[-1]
            elif seq[-1] == cur:
                path.extend(reversed(seq[:-1]))
                cur = seq[0]
            else:
                continue
            left.remove(wid)
            break
        else:
            return None
    return path


def resolve(collector, way_seq) -> tuple[list, Counter]:
    """Node-path records [(flags, [nodes...])] and counts of what was
    dropped. `way_seq[way_id] = ([(node_idx, osm_ref) junctions in way
    order], oneway)` for every member way Pass 2 kept."""
    from_osm = {}
    for seq, _ow in way_seq.values():
        for n, r in seq:
            from_osm[r] = n
    records = {}
    dropped = Counter(collector.skipped)
    for kinds, frm, via_node, via_ways, to in collector.raw:
        flags_for = []
        for kind in ("no", "only"):
            f = (CAR if kinds["car"] == kind else 0) | (BIKE if kinds["bike"] == kind else 0)
            if f:
                flags_for.append(f | (ONLY if kind == "only" else 0))
        if any(w not in way_seq for w in frm + to + via_ways):
            dropped["member way not in the graph"] += 1
            continue
        paths = []
        unplaced = 0       # from/to pairs this relation cannot place
        for fw in frm:
            for tw in to:
                fseq, fow = way_seq[fw]
                tseq, tow = way_seq[tw]
                if via_node is not None:
                    v = from_osm.get(via_node)
                    if v is None:
                        unplaced += 1
                        continue
                    if fw == tw and fow == 0 and _arrive_from(fseq, 0, v) is None:
                        # no_u_turn on a two-way way through v: either side.
                        pos = [i for i, (n, _r) in enumerate(fseq) if n == v]
                        if len(pos) != 1 or not 0 < pos[0] < len(fseq) - 1:
                            unplaced += 1
                            continue
                        i = pos[0]
                        for side in (fseq[i - 1][0], fseq[i + 1][0]):
                            paths.append([side, v, side])
                        continue
                    u = _arrive_from(fseq, fow, v)
                    w = _leave_to(tseq, tow, v)
                    if u is None or w is None or (fw == tw and u != w):
                        # (the same way as from and to is a U-turn; on a
                        # one-way it would read as "straight on")
                        unplaced += 1
                        continue
                    paths.append([u, v, w])
                else:
                    # Via ways: the from-way meets the chain at one end.
                    ends = set()
                    for wid in via_ways:
                        s = way_seq[wid][0]
                        ends.update((s[0][0], s[-1][0]))
                    fnodes = {n for n, _r in fseq}
                    starts = [a for a in ends if a in fnodes]
                    chain = None
                    for a in starts:
                        c = _via_chain(a, via_ways, way_seq)
                        if c is not None and c[-1] in {n for n, _r in tseq}:
                            chain = c
                            break
                    if chain is None:
                        unplaced += 1
                        continue
                    u = _arrive_from(fseq, fow, chain[0])
                    w = _leave_to(tseq, tow, chain[-1])
                    if u is None or w is None:
                        unplaced += 1
                        continue
                    paths.append([u] + chain + [w])
        if not paths:
            dropped["cannot place on the graph"] += 1
            continue
        if unplaced:
            dropped["from/to pairs of a placed relation"] += unplaced
        for p in paths:
            if len(set(p[1:-1])) != len(p) - 2:
                dropped["via path revisits a node"] += 1
                continue
            for f in flags_for:
                # Kept apart by kind: a car ban and a bike only_* on one
                # path must not merge into an only_* for cars.
                key = (tuple(p), bool(f & ONLY))
                records[key] = records.get(key, 0) | (f & (CAR | BIKE))
    # ONLY records with the same prefix and different targets ban each
    # other's targets: for each mode they share, that mode drops both.
    for bit in (CAR, BIKE):
        by_prefix = {}
        for (p, only), modes in records.items():
            if only and modes & bit:
                by_prefix.setdefault(p[:-1], []).append(p)
        for ps in by_prefix.values():
            if len(ps) > 1:
                for p in ps:
                    records[(p, True)] &= ~bit
                dropped["conflicting only_* at one junction"] += len(ps)
    out = [((ONLY if only else 0) | modes, list(p))
           for (p, only), modes in sorted(records.items()) if modes]
    return out, dropped


def pack(records) -> bytes:
    """SZTR bytes for [(flags, path)] (empty bytes for none)."""
    if not records:
        return b""
    offs = [0]
    pool = []
    for _f, p in records:
        pool.extend(p)
        offs.append(len(pool))
    flags = bytes(f for f, _p in records)
    flags += b"\0" * (-len(flags) % 4)
    return (SZTR_MAGIC + struct.pack("<3I", SZTR_VERSION, len(records), len(pool))
            + np.asarray(offs, dtype="<u4").tobytes() + flags
            + np.asarray(pool, dtype="<u4").tobytes())


def unpack(buf, off: int = 0) -> list:
    """[(flags, [nodes])] from SZTR bytes at `off`; [] if there are none."""
    if len(buf) < off + 16 or bytes(buf[off:off + 4]) != SZTR_MAGIC:
        return []
    version, r, p = struct.unpack_from("<3I", buf, off + 4)
    if version != SZTR_VERSION:
        return []
    o = off + 16
    offs = np.frombuffer(buf, dtype="<u4", count=r + 1, offset=o).tolist()
    o += (r + 1) * 4
    flags = bytes(buf[o:o + r])
    o += r + (-r % 4)
    pool = np.frombuffer(buf, dtype="<u4", count=p, offset=o).tolist()
    return [(flags[i], pool[offs[i]:offs[i + 1]]) for i in range(r)]


def sidecar_path(graph_path):
    from pathlib import Path
    return Path(graph_path).with_name("routing-restrictions.bin")


def read_sidecar(graph_path) -> list:
    p = sidecar_path(graph_path)
    try:
        return unpack(p.read_bytes())
    except FileNotFoundError:
        return []
