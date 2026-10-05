"""Routing-graph builder: OSM PBF -> SZRG graph, and fixed-size graph
chunking (moved verbatim from create_osm_zim.py, which re-exports these
names). The readers/writers of the formats live alongside in this package.

Memory (extract_routing_graph). China's cut (870 M nodes, 26 M highway
ways, 43.6 M junctions) thrashed in a 16 GiB container in Pass 2: a dict
of every junction id (about 100 bytes each, 4-5 GB), a dict of every
geometry's bytes for dedup (about 85 bytes per geometry), Pass 1's Python
set of way ends, and a node-location index of every node in the cut (14 GB
on disk, random reads into a page cache the anonymous memory had squeezed
to 3 GB). The builder now keeps:
  - for a large extract, only the highway ways and their nodes (`osmium
    tags-filter`, choose_highway_filter), so the location index holds the
    highway nodes alone, in memory or in a file by size (choose_node_index);
    a small extract is read whole, with a file index, as before (the
    filter's own ID sets cost 1.4-2.2 GB whatever the extract's size);
  - junction ids as a sorted int64 array, looked up with searchsorted;
  - each way's refs spilled to files in Pass 1, read back into one array;
  - geometries spilled to a file with a 64-bit hash and length each, and
    deduplicated afterwards (dedup_geoms; bytes compared, so a hash
    collision only costs time);
  - edges written to the file in blocks, in from-node order.
The output is byte-identical to the dict-based builder's."""
import os
import shutil
import subprocess
import tempfile
import time

from streetzim import area
# The builder's flushing, phase-timing print (see streetzim/common.py).
from streetzim.common import (
    print,
)

# Geometry offsets are uint32 byte offsets into the blob, so the blob
# stops growing near 2^32 (later edges get no geometry and render as
# straight lines between their nodes). Leaves ~64 KB headroom.
GEOM_BLOB_CAP = 0xFFFF0000

# The hash dedup_geoms groups geometry candidates by (a test swaps in a
# weak one to force collisions, which the byte comparison must resolve).
_geom_hash = hash

# Node-location index (libosmium's sparse arrays: a sorted vector of
# (id, location) pairs, 16 bytes per node). In memory it is a std::vector,
# which doubles as it grows, so it can briefly take three times that.
NODE_INDEX_MODES = ("auto", "memory", "file")
NODE_INDEX_ENTRY_BYTES = 16
NODE_INDEX_GROWTH = 3
# auto: a file once the in-memory peak would pass this...
NODE_INDEX_FILE_ABOVE_BYTES = 1 << 30
# ...or this share of the memory limit (cpus.memory_limit()).
NODE_INDEX_LIMIT_SHARE = 0.1


# The highway filter (osmium tags-filter) is a separate process whose ID
# sets span the planet's node-ID range: 1.4 GB for Monaco's 1 MB extract,
# 2.0 GB for Luxembourg's 47 MB, 2.2 GB for Alaska's 413 MB. Read whole,
# the extract instead costs a file index of every node in it, about this
# many bytes per byte of PBF (China: 13.9 GB for its 6.48 GB cut)...
HIGHWAY_FILTER_MODES = ("auto", "on", "off")
WHOLE_INDEX_PER_PBF_BYTE = 2.2
# ...so auto filters once that index would pass this (the Netherlands'
# 1.40 GB: an estimated 3.09 GB, measured 1.94 GB of mapped file, which is
# page cache, against the filter's 2.2 GB of anonymous memory; read whole)...
HIGHWAY_FILTER_ABOVE_BYTES = 3 << 30
# ...or this share of the memory limit (cpus.memory_limit()).
HIGHWAY_FILTER_LIMIT_SHARE = 0.25


def choose_highway_filter(mode, pbf_bytes, memory_limit):
    """(read only the highway ways and their nodes, why) for an extract of
    `pbf_bytes`. `mode` is auto, on or off
    (STREETZIM_ROUTING_HIGHWAY_FILTER)."""
    if mode not in HIGHWAY_FILTER_MODES:
        raise ValueError(f"STREETZIM_ROUTING_HIGHWAY_FILTER must be one of "
                         f"{', '.join(HIGHWAY_FILTER_MODES)}, not {mode!r}")
    if mode != "auto":
        return mode == "on", f"STREETZIM_ROUTING_HIGHWAY_FILTER={mode}"
    est = WHOLE_INDEX_PER_PBF_BYTE * pbf_bytes
    gb = est / 1e9
    if est > HIGHWAY_FILTER_ABOVE_BYTES:
        return True, (f"auto: a whole-extract node index of about {gb:.1f} GB, "
                      f"over {HIGHWAY_FILTER_ABOVE_BYTES >> 30} GiB")
    if memory_limit is not None and est > HIGHWAY_FILTER_LIMIT_SHARE * memory_limit:
        return True, (f"auto: a whole-extract node index of about {gb:.1f} GB, "
                      f"over {HIGHWAY_FILTER_LIMIT_SHARE:.0%} of the "
                      f"{memory_limit / 1e9:.1f} GB limit")
    return False, f"auto: a whole-extract node index of about {gb:.1f} GB"


def choose_node_index(mode, n_nodes, memory_limit):
    """(keep the node-location index in a file, why) for about `n_nodes`
    highway nodes. `mode` is auto, memory or file
    (STREETZIM_ROUTING_NODE_INDEX)."""
    if mode not in NODE_INDEX_MODES:
        raise ValueError(f"STREETZIM_ROUTING_NODE_INDEX must be one of "
                         f"{', '.join(NODE_INDEX_MODES)}, not {mode!r}")
    if mode != "auto":
        return mode == "file", f"STREETZIM_ROUTING_NODE_INDEX={mode}"
    peak = NODE_INDEX_GROWTH * NODE_INDEX_ENTRY_BYTES * n_nodes
    gb = peak / 1e9
    if peak > NODE_INDEX_FILE_ABOVE_BYTES:
        return True, (f"auto: {n_nodes} highway nodes, up to {gb:.2f} GB in "
                      f"memory, over {NODE_INDEX_FILE_ABOVE_BYTES >> 30} GiB")
    if memory_limit is not None and peak > NODE_INDEX_LIMIT_SHARE * memory_limit:
        return True, (f"auto: {n_nodes} highway nodes, up to {gb:.2f} GB in "
                      f"memory, over {NODE_INDEX_LIMIT_SHARE:.0%} of the "
                      f"{memory_limit / 1e9:.1f} GB limit")
    return False, f"auto: {n_nodes} highway nodes, up to {gb:.2f} GB in memory"


def dedup_geoms(lengths, hashes, seg_first, read, cap=None):
    """Deduplicate the geometry candidates Pass 2 spilled, exactly as the
    old in-pass dict did: a geometry's index is the order in which its
    bytes first appeared; and once the kept blob reaches `cap` bytes at the
    start of a road segment, that segment and every later one get none.

    lengths, hashes, seg_first: per candidate, its byte length, a hash of
    its bytes and whether it opens its road segment (a reverse geometry
    after a forward one of the same segment does not). read(k) returns
    candidate k's bytes; it is called only for candidates whose hash and
    length match an earlier one's, to confirm the match.

    Returns (geom_of, keep): geom_of[k] is candidate k's geometry index
    (0xFFFFFFFF for none) and keep[k] whether its bytes go in the blob.
    """
    import numpy as np
    if cap is None:
        cap = GEOM_BLOB_CAP
    lengths = np.asarray(lengths, dtype=np.int64)
    m = len(lengths)
    if m == 0:
        return np.empty(0, dtype=np.uint32), np.empty(0, dtype=bool)
    hashes = np.asarray(hashes, dtype=np.int64)
    # rep[k] = the first candidate with k's hash (stable sort keeps index
    # order within a hash).
    order = np.argsort(hashes, kind="stable")
    hs = hashes[order]
    first = np.zeros(m, dtype=np.int64)        # sorted position of the group start
    starts = np.flatnonzero(hs[1:] != hs[:-1]) + 1
    del hs
    first[starts] = starts
    np.maximum.accumulate(first, out=first)
    rep = np.empty(m, dtype=np.int64)
    rep[order] = order[first]
    del first, starts
    # Confirm: same length and the same bytes. A group whose members do not
    # all match its first (a hash collision) is resolved by its bytes.
    idx = np.arange(m, dtype=np.int64)
    dups = np.flatnonzero(rep != idx)
    bad = dups[lengths[dups] != lengths[rep[dups]]]
    same_len = dups[lengths[dups] == lengths[rep[dups]]]
    mismatch = []
    for s in range(0, len(same_len), 1 << 20):
        mismatch.extend(k for k, r in zip(same_len[s:s + (1 << 20)].tolist(),
                                          rep[same_len[s:s + (1 << 20)]].tolist())
                        if read(k) != read(r))
    if len(bad) or mismatch:
        colliding = np.unique(rep[np.concatenate(
            [bad, np.asarray(mismatch, dtype=np.int64)])])
        groups = np.isin(rep, colliding)
        members = np.flatnonzero(groups)
        seen = {}
        for k in members.tolist():           # index order
            key = (int(rep[k]), read(k))
            rep[k] = seen.setdefault(key, k)
        del groups, members, seen
    del order
    unique = rep == idx
    del idx
    # The cap, checked where each segment starts against the blob so far.
    ulen = np.where(unique, lengths, 0)
    before = np.cumsum(ulen) - ulen
    del ulen
    over = np.flatnonzero(np.asarray(seg_first, dtype=bool) & (before >= cap))
    cut = int(over[0]) if len(over) else m
    del before, over
    keep = unique
    keep[cut:] = False
    gi = np.cumsum(keep, dtype=np.int64) - 1
    geom_of = np.full(m, 0xFFFFFFFF, dtype=np.uint32)
    geom_of[:cut] = gi[rep[:cut]]
    return geom_of, keep


def _antimeridian_twins(osmium, pbf, excluded, idx="flex_mem"):
    """{ref: ref it joins} for highway way ends at longitude -180 that sit
    at the latitude of a highway way end at +180 (the same point, split in
    two by OSM at the antimeridian)."""
    ends = {1: {}, -1: {}}           # side -> lat_e7 -> smallest ref

    class _Ends(osmium.SimpleHandler):
        def way(self, w):
            hw = w.tags.get("highway")
            if not hw or hw in excluded or len(w.nodes) < 2:
                return
            for n in (w.nodes[0], w.nodes[-1]):
                if not n.location.valid():
                    continue
                lon_e7 = int(round(n.location.lon * 1e7))
                if abs(lon_e7) == 1_800_000_000:
                    lat_e7 = int(round(n.location.lat * 1e7))
                    side = ends[1 if lon_e7 > 0 else -1]
                    side[lat_e7] = min(n.ref, side.get(lat_e7, n.ref))

    _Ends().apply_file(pbf, locations=True, idx=idx)
    return {ref: ends[1][lat] for lat, ref in ends[-1].items()
            if lat in ends[1] and ends[1][lat] != ref}


class _Spill:
    """An int64 column appended in Python and kept in a file until read
    back whole: no Python ints, no second copy at concatenation."""

    def __init__(self, directory, name):
        import array
        fd, self.path = tempfile.mkstemp(dir=directory, prefix=f"routing-{name}-",
                                         suffix=".bin")
        self._f = os.fdopen(fd, "wb")
        self._buf = array.array("q")
        self.count = 0

    def extend(self, values):
        self._buf.extend(values)
        if len(self._buf) >= 1 << 22:
            self.flush()

    def append(self, value):
        self._buf.append(value)

    def flush(self):
        self.count += len(self._buf)
        self._buf.tofile(self._f)
        del self._buf[:]

    def read(self):
        import numpy as np
        self.flush()
        self._f.close()
        arr = np.fromfile(self.path, dtype=np.int64)
        self.remove()
        return arr

    def remove(self):
        if not self._f.closed:
            self._f.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


def _highway_pbf(source_pbf, output_dir):
    """The highway ways of `source_pbf` and the nodes they use, in a file
    of their own (osmium tags-filter), or None when choose_highway_filter
    says the extract is small enough to read whole, or without the osmium
    tool. The location index then holds the highway nodes alone: China's
    cut has 870 M nodes, a 14 GB index, its roads 333 M of them."""
    from streetzim import cpus
    use, why = choose_highway_filter(
        os.environ.get("STREETZIM_ROUTING_HIGHWAY_FILTER", "auto"),
        os.path.getsize(source_pbf), cpus.memory_limit())
    if not use:
        print(f"    Reading the whole extract ({why})")
        return None
    if shutil.which("osmium") is None:
        print("    osmium tool not found: reading the whole extract")
        return None
    fd, path = tempfile.mkstemp(dir=output_dir, prefix="routing-highways-",
                                suffix=".osm.pbf")
    os.close(fd)
    try:
        subprocess.run(["osmium", "tags-filter", source_pbf, "w/highway",
                        "-o", path, "--overwrite"], check=True)
    except BaseException:
        os.remove(path)
        raise
    print(f"    Highway ways and their nodes: "
          f"{os.path.getsize(path) / (1024 * 1024):.1f} MB ({why})")
    return path


def _node_index_dir(n_nodes, output_dir, mode=None):
    """Where the node-location index goes: None for memory, else the folder
    for its file. Prints why. `mode` overrides STREETZIM_ROUTING_NODE_INDEX."""
    from streetzim import cpus
    # A file index's folder: STREETZIM_NODE_LOC_DIR when it is a writable
    # folder, else the build's own temporary folder (output_dir). The
    # default, /data, was chosen as a fast NVMe scratch volume (an earlier
    # comment here); on the current build host it is a root-owned folder on
    # the root filesystem, not writable by the build user, and in the
    # Docker image it does not exist, so both use the build's folder.
    loc_dir = os.environ.get("STREETZIM_NODE_LOC_DIR", "/data")
    if not os.path.isdir(loc_dir) or not os.access(loc_dir, os.W_OK):
        loc_dir = output_dir
    # Reclaim scratch files an OOM-killed earlier run left behind (they
    # no longer share a fixed name), whichever index this run uses. A file
    # this old may still be in use by a long continent build, but that is
    # harmless: libosmium opened it when the pass started, and on
    # Linux/macOS an unlinked file stays readable and writable through open
    # descriptors and mappings.
    import glob
    for stale in glob.glob(os.path.join(loc_dir, "streetzim_node_loc_*.bin")):
        try:
            if time.time() - os.path.getmtime(stale) > 6 * 3600:
                os.remove(stale)
                print(f"    removed stale node-location scratch {stale}")
        except OSError:
            pass
    use_file, why = choose_node_index(
        mode or os.environ.get("STREETZIM_ROUTING_NODE_INDEX", "auto"),
        n_nodes, cpus.memory_limit())
    if not use_file:
        print(f"    Node locations in memory ({why})")
        return None
    # sparse_file_array: sorted (id, lon, lat) triples in a file, mapped.
    print(f"    Node locations in a file in {loc_dir} ({why})")
    return loc_dir


def _node_index(loc_dir, scratch):
    """A libosmium index spec for one pass: in memory, or in a new file in
    `loc_dir` (unique per run: a fixed name let a second build on the same
    host delete/rewrite the first build's index mid-pass), added to
    `scratch`."""
    if loc_dir is None:
        return "sparse_mem_array", None
    fd, path = tempfile.mkstemp(dir=loc_dir, prefix="streetzim_node_loc_",
                                suffix=".bin")
    os.close(fd)
    scratch.append(path)
    return f"sparse_file_array,{path}", path


def extract_routing_graph(pbf_path, output_dir, bbox=None, precut=False):
    """Extract road network from OSM PBF and build a compact routing graph.

    Streams through the (bbox-filtered) PBF with pyosmium in two passes:
      Pass 1 — collect highway-way node refs + endpoints to identify junctions
              (intersection/terminus nodes, the graph vertices).
      Pass 2 — re-scan ways, split each at junction nodes into edges, emit
              edges incrementally into arrays + a spilled geometry file.
    Both read only the highway ways and their nodes (_highway_pbf). Memory:
    the module docstring.

    Args:
        pbf_path: Source OSM PBF file
        output_dir: Where to write the bbox-filtered PBF (intermediate),
                    the scratch files and the final routing-graph.bin.
        bbox: Optional (minlon, minlat, maxlon, maxlat) to bbox-filter first.
              Critical for regional builds from a planet PBF.
        precut: pbf_path is already cut to bbox: skip the cut, keep bbox for
              the rest (the antimeridian stitching).

    Writes SZRG v4 (docs/formats.md). The SZRG v5 split layout (a separate
    SZGM geometry file) is no longer written: nothing in production used it,
    and large regions use the spatial layout (--spatial-chunk-scale), which
    already loads geometry per cell. Readers keep v5 support for any
    published file.

    Returns the path of routing-graph.bin, or None if no highways were found.
    """
    try:
        import osmium  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("pyosmium is required for routing extraction "
                           "(pip install osmium)") from exc

    print("  Extracting routing graph from OSM data...")

    # Step 0: Bbox-filter the PBF first so we never read ways outside the region.
    source_pbf = str(pbf_path)
    # precut: pbf_path is already the build's own cut to this box, so the
    # cut is skipped; bbox still decides the antimeridian stitching below.
    if bbox and not precut:
        minlon, minlat, maxlon, maxlat = bbox
        bbox_pbf = os.path.join(output_dir, "region.osm.pbf")
        print(f"    Extracting bbox {minlon},{minlat},{maxlon},{maxlat} from planet PBF...")
        subprocess.run([
            "osmium", "extract",
            *area.osmium_extract_args(bbox, output_dir),
            source_pbf, "-o", bbox_pbf, "--overwrite",
        ], check=True)
        size_mb = os.path.getsize(bbox_pbf) / (1024 * 1024)
        print(f"    Region PBF: {size_mb:.1f} MB")
        source_pbf = bbox_pbf

    scratch = []
    hw_pbf = _highway_pbf(source_pbf, output_dir)
    if hw_pbf:
        scratch.append(hw_pbf)
    try:
        return _extract(hw_pbf or source_pbf, output_dir, bbox, scratch,
                        highways_only=hw_pbf is not None)
    finally:
        for p in scratch:
            try:
                os.remove(p)
            except OSError:
                pass


def _extract(source_pbf, output_dir, bbox, scratch, highways_only=True):
    """extract_routing_graph from the highway extract on. Files it creates
    go in `scratch`, which the caller removes. Without highways_only the
    location index holds every node of `source_pbf`, so it is a file (as it
    always was before the filter), whatever the highway-node count says."""
    import math
    import array
    import mmap
    import numpy as np
    import struct
    import osmium

    # Highway classes excluded from routing (non-navigable)
    EXCLUDED = frozenset({
        "proposed", "construction", "raceway", "bus_guideway",
        "platform", "elevator", "razed", "abandoned",
    })
    # Speed estimates (km/h) by highway class for travel time
    SPEED = {
        "motorway": 100, "motorway_link": 60,
        "trunk": 80, "trunk_link": 50,
        "primary": 60, "primary_link": 40,
        "secondary": 50, "secondary_link": 35,
        "tertiary": 40, "tertiary_link": 30,
        "residential": 30, "living_street": 20,
        "unclassified": 40, "service": 20,
        "track": 15, "path": 5, "footway": 5,
        "cycleway": 15, "pedestrian": 5, "steps": 3,
    }
    DEFAULT_SPEED = 30

    # Highway classes cars may never use (class_access bit 9). Tracks stay
    # routable (rural last mile); service roads stay routable (driveways,
    # parking aisles are how you reach the destination).
    NO_MOTOR_HIGHWAY = frozenset({
        "footway", "path", "steps", "pedestrian", "cycleway", "bridleway",
        "corridor", "escape", "busway",
    })
    # `private`, `destination`, `customers`, `delivery` are ALLOWED (like
    # OSRM/Valhalla's restricted-access classes): they're exactly the
    # roads you must use to reach a destination inside a gated community
    # or campus. Only an outright `no` blocks. (We have no penalty
    # mechanism, so through-routing over a private road is possible; the
    # alternative — relocating the destination to the nearest public road
    # with no indication — is worse.)
    _ACCESS_DENY = ("no",)
    _ACCESS_ALLOW = ("yes", "designated", "permissive", "destination",
                     "customers", "delivery", "private")

    def _access_value(tags, *keys):
        """First RECOGNISED value among `keys` (an unrecognised value such
        as motorcar=agricultural must not shadow motor_vehicle=no)."""
        for k in keys:
            v = tags.get(k)
            if v in _ACCESS_ALLOW or v in _ACCESS_DENY:
                return v
        return None

    def _way_no_motor_vehicle(hw, tags):
        """True when motor vehicles may not use this way (OSM access
        hierarchy: motorcar/motor_vehicle override vehicle override
        access). An explicit motor_vehicle=yes re-opens a road that
        access=no closed (e.g. private roads tagged for through traffic)."""
        mv = _access_value(tags, "motorcar", "motor_vehicle")
        if mv is not None:
            return mv in _ACCESS_DENY
        veh = _access_value(tags, "vehicle")
        if veh is not None:
            return veh in _ACCESS_DENY
        acc = _access_value(tags, "access")
        if acc is not None:
            return acc in _ACCESS_DENY
        return hw in NO_MOTOR_HIGHWAY

    # ---- walk / bike (class_access bits 5, 6, 10-18; docs/formats.md) ----
    # STREETZIM_ROUTING_WALKBIKE=0 builds today's graph byte for byte: bits
    # 5/6 as the literal foot=no / bicycle=no, no new bits, no records for
    # travel against one-ways.
    walkbike = os.environ.get("STREETZIM_ROUTING_WALKBIKE", "1") != "0"
    _SIDEPATH_DENY = ("no", "use_sidepath")
    # Classes neither walked nor cycled, whatever the access tags say
    # beyond an explicit foot/bicycle=yes.
    _NO_FOOT_BIKE = frozenset({"motorway", "motorway_link", "busway"})
    # Classes whose one-way tag binds walkers too (OSM: oneway on a
    # footway is for pedestrians).
    _FOOT_CLASS = frozenset({"footway", "path", "pedestrian", "steps", "corridor"})
    # Bikes are pushed here unless cycling is allowed explicitly.
    _PUSH_CLASS = frozenset({"footway", "pedestrian", "corridor", "bridleway", "steps"})
    _RIDE_OK = ("yes", "designated", "permissive")
    _PAVED = frozenset({"paved", "asphalt", "concrete", "concrete:plates",
                        "concrete:lanes", "paving_stones", "sett", "chipseal",
                        "metal", "wood", "bricks"})
    _FIRM = frozenset({"compacted", "fine_gravel", "gravel", "pebblestone",
                       "unhewn_cobblestone", "cobblestone"})
    _ROUGH = frozenset({"dirt", "ground", "grass", "sand", "mud", "earth",
                        "unpaved", "rock", "woodchips", "grass_paver"})
    _CYCLE_SIDES = ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both")

    def _foot_or_bike_value(tags, *keys):
        for k in keys:
            v = tags.get(k)
            if v in _ACCESS_ALLOW or v in _SIDEPATH_DENY:
                return v
        return None

    def _foot_denied(hw, tags):
        """Walking is not allowed on this way (foot > access; motorways,
        busways and motorroad=yes never)."""
        v = _foot_or_bike_value(tags, "foot")
        if v is not None:
            return v in _SIDEPATH_DENY
        if hw in _NO_FOOT_BIKE or tags.get("motorroad") == "yes":
            return True
        v = _foot_or_bike_value(tags, "access")
        return v is not None and v in _SIDEPATH_DENY

    def _bike_denied(hw, tags):
        """Cycling (riding or pushing) is not allowed (bicycle > vehicle >
        access; motorways, busways and motorroad=yes never). Steps are
        allowed with the bike pushed (bit 12): a cyclist carries a bike
        up a stair rather than have no route."""
        v = _foot_or_bike_value(tags, "bicycle")
        if v is not None:
            return v in _SIDEPATH_DENY
        if hw in _NO_FOOT_BIKE or tags.get("motorroad") == "yes":
            return True
        v = _foot_or_bike_value(tags, "vehicle", "access")
        return v is not None and v in _SIDEPATH_DENY

    def _walkbike_bits(hw, tags):
        """Direction-independent walk/bike bits: 5, 6, 12-17."""
        bits = 0
        if _foot_denied(hw, tags):
            bits |= 0x20                                   # bit 5
        if _bike_denied(hw, tags):
            bits |= 0x40                                   # bit 6
        bv = tags.get("bicycle")
        if bv == "dismount" or (hw in _PUSH_CLASS and bv not in _RIDE_OK):
            bits |= 0x1000                                 # bit 12 push
        sides = [tags.get(k) for k in _CYCLE_SIDES]
        if any(v in ("lane", "shared_lane", "share_busway") for v in sides):
            bits |= 0x2000                                 # bit 13 lane
        if hw == "cycleway" or "track" in sides or bv == "designated":
            bits |= 0x4000                                 # bit 14 infra
        surf = tags.get("surface")
        grade = tags.get("tracktype")
        if surf in _PAVED:
            bits |= 1 << 15
        elif surf in _FIRM or grade in ("grade1", "grade2"):
            bits |= 2 << 15
        elif surf in _ROUGH or grade in ("grade3", "grade4", "grade5"):
            bits |= 3 << 15                                # bits 15-16
        if any(tags.get(k) in ("no", "none")
               for k in ("sidewalk", "sidewalk:both")):
            bits |= 0x20000                                # bit 17
        return bits

    def _bike_contraflow(tags):
        """Cycling against the one-way is allowed."""
        if tags.get("oneway:bicycle") == "no":
            return True
        if tags.get("cycleway") in ("opposite", "opposite_lane", "opposite_track"):
            return True
        return any(tags.get(k) in ("-1", "no")
                   for k in ("cycleway:left:oneway", "cycleway:right:oneway"))

    def _foot_bound_by_oneway(hw, tags):
        """The way's one-way applies to walkers too."""
        if tags.get("oneway:foot") == "no":
            return False
        return tags.get("oneway:foot") == "yes" or hw in _FOOT_CLASS

    # Road-class ordinal for the v4 routing-graph class_access u32
    # (bits 0..4). See docs/formats.md for the full bit layout. Unknown /
    # missing classes fall through to 0.
    CLASS_ORDINAL = {
        "motorway": 1, "motorway_link": 2,
        "trunk": 3, "trunk_link": 4,
        "primary": 5, "primary_link": 6,
        "secondary": 7, "secondary_link": 8,
        "tertiary": 9, "tertiary_link": 10,
        "residential": 11, "living_street": 12,
        "unclassified": 13, "service": 14,
        "track": 15, "path": 16, "footway": 17,
        "cycleway": 18, "pedestrian": 19, "steps": 20,
    }
    if walkbike:
        # No-motor classes that were 0 (they carry bit 9 in every graph).
        CLASS_ORDINAL.update({"bridleway": 21, "corridor": 22, "busway": 23,
                              "escape": 24})

    # Pass 1: Walk every highway way, record node refs. Junctions = nodes
    # appearing in 2+ ways OR at way endpoints. Interior refs and endpoint
    # refs are spilled to files (int64), read back after the pass and
    # sorted; the duplicates in the sorted interior refs are the 2+ ones.
    print("    Pass 1: scanning highway ways for junction nodes...")
    interior = _Spill(output_dir, "interior")
    scratch.append(interior.path)
    endpoints = _Spill(output_dir, "ends")
    scratch.append(endpoints.path)

    class _Pass1(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.way_count = 0
            self.hw_count = 0

        def way(self, w):
            self.way_count += 1
            hw = w.tags.get("highway")
            if not hw or hw in EXCLUDED:
                return
            refs = [n.ref for n in w.nodes]
            if len(refs) < 2:
                return
            endpoints.append(refs[0])
            endpoints.append(refs[-1])
            if len(refs) > 2:
                interior.extend(refs[1:-1])
            self.hw_count += 1
            if self.hw_count % 200000 == 0:
                endpoints.flush()
                print(f"\r    Pass 1: {self.hw_count} highway ways...",
                      end="", flush=True)

    p1 = _Pass1()
    try:
        p1.apply_file(source_pbf)
    except BaseException:
        interior.remove()
        endpoints.remove()
        raise
    print(f"\r    Pass 1: scanned {p1.hw_count} highway ways "
          f"(of {p1.way_count} total)                    ")

    if p1.hw_count == 0:
        interior.remove()
        endpoints.remove()
        print("    Warning: no highway features found, skipping routing graph")
        # None means "no routing": the caller skips the routing phase.
        return None

    # Interior refs that appear in 2+ ways: each one that equals its
    # predecessor in the sorted array (np.unique collapses the repeats).
    interior_arr = interior.read()
    interior_arr.sort()
    if len(interior_arr) > 1:
        dup = interior_arr[1:] == interior_arr[:-1]
        interior_junctions = np.unique(interior_arr[1:][dup])
        n_interior = len(interior_arr) - int(np.count_nonzero(dup))
        del dup
    else:
        interior_junctions = interior_arr.copy()[:0]
        n_interior = len(interior_arr)
    del interior_arr
    endpoint_arr = np.unique(endpoints.read())
    # At most this many distinct highway nodes (the ends may be interior
    # elsewhere too): what the location index will hold.
    n_highway_nodes = n_interior + len(endpoint_arr)
    junction_arr = np.unique(np.concatenate([interior_junctions, endpoint_arr]))
    del interior_junctions, endpoint_arr
    print(f"    Found {len(junction_arr)} junction nodes (graph vertices)")

    loc_dir = _node_index_dir(n_highway_nodes, output_dir,
                              mode=None if highways_only else "file")

    # Across the antimeridian, OSM splits a road at ±180: one way ends on a
    # node at 180.0, the next starts on a different node at -180.0, same
    # latitude. Both are way endpoints, so both are junctions; make them one
    # graph vertex so a route can cross. Only for an area that crosses:
    # every other graph is unchanged.
    stitched = {}
    if bbox and area.crosses(bbox):
        twins_spec, twins_file = _node_index(loc_dir, scratch)
        try:
            stitched = _antimeridian_twins(osmium, source_pbf, EXCLUDED,
                                           idx=twins_spec)
        finally:
            if twins_file:
                os.remove(twins_file)
        if stitched:
            junction_arr = junction_arr[~np.isin(junction_arr, list(stitched))]
            print(f"    Joined {len(stitched)} road(s) split at the antimeridian")

    # Junction ref -> graph index is its position in the sorted junction_arr
    # (np.searchsorted per way; a dict of 43 M junctions took 4-5 GB). A
    # twin joined at the antimeridian takes its partner's index.
    num_nodes = len(junction_arr)
    twin_idx = {}
    for twin, kept in stitched.items():
        k = int(np.searchsorted(junction_arr, kept))
        if k < num_nodes and junction_arr[k] == kept:
            twin_idx[twin] = k

    # Pass 2: stream ways again, this time with node locations. Split each
    # highway way at junctions and emit edges + geoms directly into arrays.
    print("    Pass 2: building edges + geometries...")

    R = 6371000.0
    def _hav(lat1, lon1, lat2, lon2):
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _zigzag32(n):
        return ((n << 1) ^ (n >> 31)) & 0xFFFFFFFF

    def _varint(v, out):
        while v >= 0x80:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        out.append(v & 0x7F)

    def _encode_geom(lons_e7, lats_e7):
        """A geom's varint encoding (absolute first point, then deltas)."""
        out = bytearray(struct.pack('<ii', lons_e7[0], lats_e7[0]))
        prev_lon = lons_e7[0]
        prev_lat = lats_e7[0]
        for k in range(1, len(lons_e7)):
            dlon = lons_e7[k] - prev_lon
            # A way straddling the antimeridian has a ~3.6e9 delta, which
            # does not fit the 32-bit zigzag (it wrapped to +145° and shifted
            # every later point by 429°). Take the short way round instead:
            # decoders simply continue the running longitude past ±180°,
            # which MapLibre renders correctly (unwrapped coordinates).
            if dlon > 1_800_000_000:
                dlon -= 3_600_000_000
            elif dlon <= -1_800_000_000:
                dlon += 3_600_000_000
            _varint(_zigzag32(dlon), out)
            _varint(_zigzag32(lats_e7[k] - prev_lat), out)
            prev_lon += dlon
            prev_lat = lats_e7[k]
        return bytes(out)

    # Output buffers (using array.array for 4-byte primitives — much more
    # compact than Python lists of ints).
    # v3 edge layout (16 bytes/edge):
    #   target (u32), dist_speed (u32: dist_dm in low 24 bits + speed in high 8),
    #   geom_idx (u32 full; 0xFFFFFFFF = no geom), name_idx (u32)
    # v2 had geom_idx packed into only 24 bits which truncates at 16.78M geoms —
    # Japan has 19.87M, so ~16% of edges pointed at wrong geoms (Fukuoka-area
    # geometry grafted onto Kyoto-area edges etc.). v3 moves geom_idx to its
    # own full-width u32 field so continent-scale regions are correctly represented.
    edges_from = array.array('I')
    edges_to = array.array('I')
    edges_dist_speed = array.array('I')
    # Pass 2 stores the geometry CANDIDATE here; dedup_geoms maps it to the
    # geometry index (0xFFFFFFFF = no geom either way).
    edges_geom = array.array('I')
    edges_name = array.array('I')
    # v4 class_access u32 per edge — see docs/driving-mode-road-class-warnings.md
    # for the bit layout. We populate:
    #   bits 0..4 : road-class ordinal
    #   bit 5     : foot=no
    #   bit 6     : bicycle=no
    #   bit 7     : oneway=yes
    #   bit 8     : junction=roundabout / circular / mini_roundabout
    # bits 9..31 stay reserved so future access/maneuver flags can slot in.
    edges_class_access = array.array('I')

    # Node coordinates indexed by graph idx (populated lazily as we see them).
    node_coords = np.zeros((num_nodes, 2), dtype=np.int32)  # lat_e7, lon_e7
    # Explicit "seen" flags instead of using (0,0) as the unset sentinel —
    # a genuine node at Null Island is indistinguishable otherwise.
    node_has_coords = np.zeros(num_nodes, dtype=bool)

    # Geometry candidates: every edge's encoded interior points, appended to
    # a scratch file; dedup_geoms picks the distinct ones afterwards (and
    # applies GEOM_BLOB_CAP) from each one's length and hash.
    fd, geom_path = tempfile.mkstemp(dir=output_dir, prefix="routing-geoms-",
                                     suffix=".bin")
    scratch.append(geom_path)
    geom_file = os.fdopen(fd, "wb", buffering=8 << 20)
    geom_len = array.array('I')
    geom_hash = array.array('q')
    geom_seg_first = array.array('B')
    geom_bytes = [0]

    # The F821 noqas here and in _Pass2: these are closure arrays that this
    # function `del`s after the pass (to free them before the assembly);
    # ruff flags them only for that.
    def _candidate(blob, opens_segment):
        geom_file.write(blob)
        geom_len.append(len(blob))  # noqa: F821
        geom_hash.append(_geom_hash(blob))  # noqa: F821
        geom_seg_first.append(opens_segment)  # noqa: F821
        geom_bytes[0] += len(blob)
        return len(geom_len) - 1  # noqa: F821

    # Name table — deduped street-name strings.
    name_table = [""]
    name_map = {"": 0}
    last_junction = num_nodes - 1

    class _Pass2(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.hw_count = 0
            self.edge_count = 0

        def way(self, w):
            hw = w.tags.get("highway")
            if not hw or hw in EXCLUDED:
                return
            try:
                refs = []
                lats_e7 = []
                lons_e7 = []
                for n in w.nodes:
                    if not n.location.valid():
                        return
                    refs.append(n.ref)
                    lats_e7.append(int(round(n.location.lat * 1e7)))
                    lons_e7.append(int(round(n.location.lon * 1e7)))
            except osmium.InvalidLocationError:
                return
            if len(refs) < 2:
                return

            # One-way direction
            ow = w.tags.get("oneway", "")
            junction = (w.tags.get("junction") or "").strip()
            if ow in ("yes", "1", "true"):
                oneway = 1
            elif ow == "-1":
                oneway = -1
            elif ow in ("no", "0", "false"):
                oneway = 0
            elif junction in ("roundabout", "circular") or hw == "motorway":
                # OSM-implied one-ways. Without this a roundabout mapped per
                # convention (no explicit oneway tag — a large fraction) got
                # edges in both directions, so A* drove the short way round
                # against traffic and the HUD counted the wrong exit.
                oneway = 1
            else:
                oneway = 0
            speed = SPEED.get(hw, DEFAULT_SPEED)

            # v4 class_access u32 (bit layout: docs/formats.md).
            # Packed once per way — every edge derived from this way shares the
            # same class / access / roundabout state.
            class_ord = CLASS_ORDINAL.get(hw, 0) & 0x1F
            access_bits = 0
            if walkbike:
                wb_bits = _walkbike_bits(hw, w.tags)
                access_bits |= wb_bits
            else:
                if w.tags.get("foot") == "no":    access_bits |= 0x20  # bit 5
                if w.tags.get("bicycle") == "no": access_bits |= 0x40  # bit 6
            # bit 7 = "this edge is a one-way". Reversed (-1) ways only emit
            # the reverse edge, which is just as much a one-way for the HUD.
            if oneway != 0:                   access_bits |= 0x80  # bit 7
            # Roundabouts in OSM are implicitly oneway. Mark them in bit 8 so
            # the HUD can say "take roundabout" and render a curved arrow.
            # Includes mini_roundabout because the maneuver is the same from
            # a driver's perspective.
            if junction in ("roundabout", "circular", "mini_roundabout"):
                access_bits |= 0x100  # bit 8
            # bit 9 = no motor vehicles. The graph keeps footways, paths,
            # steps, cycleways and access-restricted roads (useful for
            # future foot/bike profiles and for snapping), but the driving
            # router must not use them: a 20 m staircase at 3 km/h beat any
            # road detour over ~200 m, so cars were routed down stairs and
            # through parks. Every consumer skips bit-9 edges for the car
            # profile (routing-worker.js, index.html, streetzim/routing/astar.py,
            # cloud/route_cli.py).
            if _way_no_motor_vehicle(hw, w.tags):
                access_bits |= 0x200  # bit 9
            class_access = class_ord | access_bits
            # Travel against this way's one-way, for walkers (unless the
            # one-way binds them) and for cyclists with a contraflow
            # exemption: a record a car never uses (speed 0, bits 9 + 10),
            # sharing the other direction's geometry reversed (bit 11).
            # Not on motorways. Bits 7/8 are left off so a walking HUD never
            # announces a roundabout exit.
            contra_ca = None
            if (walkbike and oneway != 0
                    and hw not in ("motorway", "motorway_link")):
                foot_ok = (not (wb_bits & 0x20)
                           and not _foot_bound_by_oneway(hw, w.tags))
                bike_ok = not (wb_bits & 0x40) and _bike_contraflow(w.tags)
                if foot_ok or bike_ok:
                    contra_ca = (class_ord | (wb_bits & 0x3F060)
                                 | 0x200 | 0x400 | 0x800)
                    if not foot_ok:
                        contra_ca |= 0x40000      # bit 18: not for walking
                    if not bike_ok:
                        contra_ca |= 0x1000       # bit 12: push the bike
            # One-way footways bind walkers in the way's direction only;
            # oneway:foot=yes on a two-way road forbids walking its reverse.
            rev_foot_deny = (walkbike and oneway == 0
                             and w.tags.get("oneway:foot") == "yes")
            rev_bike_push = (walkbike and oneway == 0
                             and w.tags.get("oneway:bicycle") == "yes")

            # Name label (same logic as before: prefer name, fall back to ref)
            name = (w.tags.get("name") or "").strip()
            refT = (w.tags.get("ref") or "").strip()
            if name and refT:
                label = f"{name} ({refT})"
            else:
                label = name or refT
            name_idx = name_map.get(label)
            if name_idx is None:
                name_idx = len(name_table)
                name_table.append(label)
                name_map[label] = name_idx

            # Graph index of each ref, and whether it is a junction.
            ref_arr = np.array(refs, dtype=np.int64)
            pos = np.searchsorted(junction_arr, ref_arr)  # noqa: F821
            np.minimum(pos, last_junction, out=pos)
            is_junction = (junction_arr[pos] == ref_arr).tolist()  # noqa: F821
            idx_of = pos.tolist()
            if twin_idx:
                for k, r in enumerate(refs):
                    t = twin_idx.get(r)
                    if t is not None:
                        is_junction[k] = True
                        idx_of[k] = t

            # Walk through refs, splitting at graph nodes (junctions).
            seg_start = 0
            n = len(refs)
            for i in range(1, n):
                if i != n - 1 and not is_junction[i]:
                    continue
                # Segment refs[seg_start:i+1] is between two graph nodes.
                a = seg_start
                b = i
                if b - a < 1:
                    seg_start = i
                    continue
                if not (is_junction[a] and is_junction[b]):
                    raise KeyError(refs[b] if is_junction[a] else refs[a])
                from_idx = idx_of[a]
                to_idx = idx_of[b]
                # Cache endpoint coordinates for EVERY junction we see,
                # including a→a loops: an isolated closed way (parking-lot
                # loop, park path loop) used to leave its only junction at
                # the (0,0) sentinel — a phantom node at Null Island.
                if not node_has_coords[from_idx]:  # noqa: F821
                    node_coords[from_idx, 0] = lats_e7[a]
                    node_coords[from_idx, 1] = lons_e7[a]
                    node_has_coords[from_idx] = True  # noqa: F821
                if not node_has_coords[to_idx]:  # noqa: F821
                    node_coords[to_idx, 0] = lats_e7[b]
                    node_coords[to_idx, 1] = lons_e7[b]
                    node_has_coords[to_idx] = True  # noqa: F821
                if from_idx != to_idx:
                    # Distance (haversine over all points in segment).
                    dist_m = 0.0
                    prev_lat = lats_e7[a] / 1e7
                    prev_lon = lons_e7[a] / 1e7
                    for j in range(a + 1, b + 1):
                        lat = lats_e7[j] / 1e7
                        lon = lons_e7[j] / 1e7
                        dist_m += _hav(prev_lat, prev_lon, lat, lon)
                        prev_lat = lat
                        prev_lon = lon
                    dist_dm = int(round(dist_m * 10))

                    # Geom: interior points only (endpoints are node vertices).
                    # The forward geom is only needed when a forward edge is
                    # emitted (oneway=-1 ways used to encode and orphan it).
                    # Reverse geom: a distinct encoding (deltas differ).
                    interior_len = b - a - 1
                    fgi = 0xFFFFFFFF
                    rgi = 0xFFFFFFFF
                    if oneway != -1 and interior_len > 0:
                        fgi = _candidate(_encode_geom(lons_e7[a + 1:b],
                                                      lats_e7[a + 1:b]), 1)
                    if oneway != 1 and interior_len > 0:
                        rgi = _candidate(_encode_geom(
                            list(reversed(lons_e7[a + 1:b])),
                            list(reversed(lats_e7[a + 1:b]))),
                            0 if oneway != -1 else 1)

                    # dist_dm truncates at 24 bits = 1677 km; real road edges
                    # don't come close, but clamp for safety.
                    dist_dm_packed = min(dist_dm, 0xFFFFFF)
                    dist_speed = ((speed & 0xFF) << 24) | dist_dm_packed
                    if oneway != -1:
                        edges_from.append(from_idx)  # noqa: F821
                        edges_to.append(to_idx)  # noqa: F821
                        edges_dist_speed.append(dist_speed)  # noqa: F821
                        edges_geom.append(fgi)  # noqa: F821
                        edges_name.append(name_idx)  # noqa: F821
                        edges_class_access.append(class_access)  # noqa: F821
                        self.edge_count += 1
                    if oneway != 1:
                        rev_ca = class_access
                        if rev_foot_deny:
                            rev_ca |= 0x40000  # bit 18
                        if rev_bike_push:
                            rev_ca |= 0x1000   # bit 12
                        edges_from.append(to_idx)  # noqa: F821
                        edges_to.append(from_idx)  # noqa: F821
                        edges_dist_speed.append(dist_speed)  # noqa: F821
                        edges_geom.append(rgi)  # noqa: F821
                        edges_name.append(name_idx)  # noqa: F821
                        edges_class_access.append(rev_ca)  # noqa: F821
                        self.edge_count += 1
                    if contra_ca is not None:
                        # Against the one-way: speed 0, the stored
                        # direction's geometry, reversed when drawn.
                        c_from, c_to, c_geom = ((to_idx, from_idx, fgi)
                                                if oneway == 1
                                                else (from_idx, to_idx, rgi))
                        edges_from.append(c_from)  # noqa: F821
                        edges_to.append(c_to)  # noqa: F821
                        edges_dist_speed.append(dist_dm_packed)  # noqa: F821
                        edges_geom.append(c_geom)  # noqa: F821
                        edges_name.append(name_idx)  # noqa: F821
                        edges_class_access.append(contra_ca)  # noqa: F821
                        self.edge_count += 1

                seg_start = i

            self.hw_count += 1
            if self.hw_count % 200000 == 0:
                print(f"\r    Pass 2: {self.hw_count} ways, "
                      f"{self.edge_count} edges, "
                      f"{len(geom_len)} geometry candidates, "  # noqa: F821
                      f"{geom_bytes[0] // (1024 * 1024)} MB...",
                      end="", flush=True)

    p2 = _Pass2()
    index_file = None
    loc_handler = None
    try:
        index_spec, index_file = _node_index(loc_dir, scratch)
        loc_handler = osmium.NodeLocationsForWays(osmium.index.create_map(index_spec))
        loc_handler.ignore_errors()
        osmium.apply(source_pbf, loc_handler, p2)
    finally:
        # Drop loc_handler (and its libosmium index) BEFORE the post-Pass-2
        # numpy work, so the kernel can release a file index's mapping:
        # without this its pages squat in RssFile even after os.remove(),
        # starving what follows of cache. Also runs on an exception so the
        # scratch file never outlives a failed pass.
        del loc_handler
        geom_file.close()
        if index_file:
            try:
                os.remove(index_file)
            except OSError:
                pass
    del junction_arr, node_has_coords
    print(f"\r    Pass 2: {p2.hw_count} ways, {p2.edge_count} edges, "
          f"{len(geom_len)} geometry candidates, "
          f"{geom_bytes[0] / (1024 * 1024):.1f} MB          ")

    # Distinct geometries, in first-seen order (what the dict did in-pass).
    geom_offsets_all = np.zeros(len(geom_len) + 1, dtype=np.int64)
    np.cumsum(np.frombuffer(geom_len, dtype=np.uint32), out=geom_offsets_all[1:])
    # Candidate bytes are read only to confirm a hash match (an empty
    # file cannot be mapped, and has no candidates to read).
    with open(geom_path, "rb") as gf:
        gm = (mmap.mmap(gf.fileno(), 0, access=mmap.ACCESS_READ)
              if geom_bytes[0] else None)
        try:
            def _read(k):
                assert gm is not None
                return gm[geom_offsets_all[k]:geom_offsets_all[k + 1]]
            geom_of, geom_keep = dedup_geoms(
                np.frombuffer(geom_len, dtype=np.uint32), geom_hash,
                geom_seg_first, _read, GEOM_BLOB_CAP)
        finally:
            if gm is not None:
                gm.close()
    del geom_hash, geom_seg_first
    kept_len = np.frombuffer(geom_len, dtype=np.uint32)[geom_keep]
    num_geoms = len(kept_len)
    geom_offsets_np = np.zeros(num_geoms + 1, dtype='<u4')
    np.cumsum(kept_len, out=geom_offsets_np[1:])
    geom_blob_len = int(kept_len.sum(dtype=np.int64))
    if geom_blob_len > 0xFFFFFFFF:
        # Only one segment straddling GEOM_BLOB_CAP with over 64 KB of
        # geometry could do this; the old builder failed here too.
        raise OverflowError(f"geometry blob of {geom_blob_len} bytes does "
                            f"not fit SZRG v4's u32 offsets")
    del kept_len, geom_len
    # Pad geom blob to 4-byte alignment (else the following Uint32Array view
    # of name_offsets lands at a non-aligned offset and the browser throws
    # RangeError — cost us hours with Baltics; keep this).
    geom_pad = (-geom_blob_len) % 4
    geom_bytes_total = geom_blob_len + geom_pad

    num_edges = len(edges_from)
    num_names = len(name_table)

    # adj_offsets[i] = first edge of from-node i (edges are written sorted
    # by from-node, stable).
    edges_from_np = np.frombuffer(edges_from, dtype=np.uint32)
    adj_offsets = np.zeros(num_nodes + 1, dtype='<u4')
    if num_edges > 0:
        adj_offsets[1:] = np.cumsum(np.bincount(edges_from_np,
                                                minlength=num_nodes))
    sort_order = np.argsort(edges_from_np, kind='stable')
    del edges_from_np, edges_from
    columns = [np.frombuffer(c, dtype=np.uint32) for c in
               (edges_to, edges_dist_speed, edges_geom, edges_name,
                edges_class_access)]
    class_access_col = columns[4]
    num_round = int(((class_access_col >> 8) & 1).sum())
    num_link = int(np.isin((class_access_col & 0x1F), [2, 4, 6, 8, 10]).sum())
    del class_access_col

    # Nodes array in (lat_e7, lon_e7) layout. node_coords is already shaped (N, 2).
    nodes_arr = node_coords.astype('<i4', copy=False)

    # Name table → UTF-8 blob + byte-offset index.
    name_blobs = [n.encode("utf-8") for n in name_table]
    names_bytes = sum(len(b) for b in name_blobs)
    name_offsets = np.empty(num_names + 1, dtype='<u4')
    cur = 0
    for i, b in enumerate(name_blobs):
        name_offsets[i] = cur
        cur += len(b)
    name_offsets[num_names] = cur

    # Serialize: SZRG v4, everything in one routing-graph.bin
    # (docs/formats.md; docs/mcpzim-contract.md).
    output_path = os.path.join(output_dir, "routing-graph.bin")
    block = 1 << 22
    with open(output_path, "wb") as f:
        f.write(b"SZRG")
        np.array([4, num_nodes, num_edges, num_geoms, geom_bytes_total,
                  num_names, names_bytes], dtype='<u4').tofile(f)
        nodes_arr.tofile(f)
        adj_offsets.tofile(f)
        # Edges in v4 layout (u32 stride = 5), in from-node order, a block
        # at a time (a whole (E, 5) copy beside the columns took 20 bytes
        # an edge more):
        #   (target, dist_speed, geom_idx, name_idx, class_access)
        # dist_speed  = (speed << 24) | dist_dm24
        # geom_idx    full u32; 0xFFFFFFFF = "no geometry"
        # class_access bit layout per docs/driving-mode-road-class-warnings.md
        for s in range(0, num_edges, block):
            sel = sort_order[s:s + block]
            out = np.empty((len(sel), 5), dtype='<u4')
            for c, col in enumerate(columns):
                out[:, c] = col[sel]
            g = out[:, 2]
            has = g != 0xFFFFFFFF
            g[has] = geom_of[g[has]]
            out.tofile(f)
        del sort_order, columns, edges_to, edges_dist_speed, edges_geom
        del edges_name, edges_class_access
        geom_offsets_np.tofile(f)
        # The kept candidates' bytes, in order, streamed from the scratch
        # file a block of candidates at a time.
        with open(geom_path, "rb") as gf:
            n_cand = len(geom_keep)
            for s in range(0, n_cand, block):
                e = min(s + block, n_cand)
                lo, hi = int(geom_offsets_all[s]), int(geom_offsets_all[e])
                gf.seek(lo)
                chunk = np.frombuffer(gf.read(hi - lo), dtype=np.uint8)
                mask = np.repeat(geom_keep[s:e],
                                 np.diff(geom_offsets_all[s:e + 1]))
                f.write(chunk[mask].tobytes())
        f.write(b"\0" * geom_pad)
        name_offsets.tofile(f)
        for b in name_blobs:
            f.write(b)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    # Class_access diagnostics — helps verify the writer populated flags
    # for regions that are expected to have lots of roundabouts or ramps.
    print(f"    Routing graph (v4 inline): {size_mb:.1f} MB "
          f"({num_nodes} nodes, {num_edges} edges, {num_geoms} geoms, "
          f"{geom_bytes_total / (1024*1024):.1f} MB geom blob, "
          f"{num_names} names, {names_bytes / 1024:.0f} KB name text, "
          f"{num_round} roundabout + {num_link} link edges)")
    return output_path


def chunk_graph_file(src_path: str, chunk_size_bytes: int,
                     out_prefix: str = "routing-graph-chunk") -> tuple[list[str], dict]:
    """Split ``src_path`` into N files of ``chunk_size_bytes`` each.

    Returns (chunk_paths, manifest_dict). The manifest mirrors the shape
    the viewer's ``loadChunkedGraph()`` expects:

        {"schema": 1, "total_bytes": <size>,
         "chunks": [{"path": "...", "bytes": N}, ...]}

    A dedicated manifest entry (rather than inferring from file listing)
    keeps the ordering deterministic for the reader. If concatenation of
    the chunks doesn't byte-match the source, the loader rejects them —
    that saves a lot of pain tracking down torn uploads.
    """
    import hashlib
    if chunk_size_bytes <= 0:
        raise ValueError("chunk_size_bytes must be positive")
    src_size = os.path.getsize(src_path)
    chunk_paths: list[str] = []
    entries: list[dict] = []
    src_dir = os.path.dirname(src_path) or "."

    with open(src_path, "rb") as src:
        idx = 0
        while True:
            chunk = src.read(chunk_size_bytes)
            if not chunk:
                break
            fname = f"{out_prefix}-{idx:04d}.bin"
            out_path = os.path.join(src_dir, fname)
            with open(out_path, "wb") as fh:
                fh.write(chunk)
            chunk_paths.append(out_path)
            entries.append({"path": fname, "bytes": len(chunk)})
            idx += 1

    # Sanity sha — the reader verifies this so torn uploads fail loud.
    h = hashlib.sha256()
    with open(src_path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)

    manifest = {
        "schema": 1,
        "total_bytes": src_size,
        "sha256": h.hexdigest(),
        "chunks": entries,
    }
    return chunk_paths, manifest
