"""Routing-graph builder: OSM PBF -> SZRG graph, and fixed-size graph
chunking (moved verbatim from create_osm_zim.py, which re-exports these
names). The readers/writers of the formats live alongside in this package."""
import os
import subprocess

from streetzim import area
# The builder's flushing, phase-timing print (see streetzim/common.py).
from streetzim.common import (
    print,
)


def _antimeridian_twins(osmium, pbf, excluded):
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

    _Ends().apply_file(pbf, locations=True)
    return {ref: ends[1][lat] for lat, ref in ends[-1].items()
            if lat in ends[1] and ends[1][lat] != ref}


def extract_routing_graph(pbf_path, output_dir, bbox=None, precut=False):
    """Extract road network from OSM PBF and build a compact routing graph.

    Streams through the (bbox-filtered) PBF with pyosmium in two passes:
      Pass 1 — collect highway-way node refs + endpoints to identify junctions
              (intersection/terminus nodes, the graph vertices).
      Pass 2 — re-scan ways, split each at junction nodes into edges, emit
              edges incrementally into arrays + a geom varint blob.

    The old implementation materialized all highway features in Python
    objects (~5 KB/feature), peaking at ~67 GB RAM for Japan and would
    need ~500 GB for Europe. Streaming + node-ref dedup + numpy/array.array
    storage keeps peak RAM well under 100 GB for any continent-scale bbox.

    Args:
        pbf_path: Source OSM PBF file
        output_dir: Where to write the bbox-filtered PBF (intermediate)
                    and the final routing-graph.bin.
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
    import math
    import array
    import numpy as np
    import struct

    try:
        import osmium
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

    # Pass 1: Walk every highway way, record node refs. Junctions = nodes
    # appearing in 2+ ways OR at way endpoints. Store interior refs in a
    # compact int64 array and endpoint refs in a set; after the pass, sort
    # the array to find the 2+ duplicates.
    print("    Pass 1: scanning highway ways for junction nodes...")

    class _Pass1(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.endpoints = set()
            self.interior_chunks = []   # list of numpy int64 arrays
            self._interior_buf = []
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
            self.endpoints.add(refs[0])
            self.endpoints.add(refs[-1])
            if len(refs) > 2:
                self._interior_buf.extend(refs[1:-1])
            self.hw_count += 1
            if self.hw_count % 200000 == 0:
                # Flush Python list into numpy (release Python-int overhead)
                if self._interior_buf:
                    self.interior_chunks.append(
                        np.fromiter(self._interior_buf, dtype=np.int64,
                                    count=len(self._interior_buf)))
                    self._interior_buf = []
                print(f"\r    Pass 1: {self.hw_count} highway ways...",
                      end="", flush=True)

        def finalize(self):
            if self._interior_buf:
                self.interior_chunks.append(
                    np.fromiter(self._interior_buf, dtype=np.int64,
                                count=len(self._interior_buf)))
                self._interior_buf = []

    p1 = _Pass1()
    p1.apply_file(source_pbf)
    p1.finalize()
    print(f"\r    Pass 1: scanned {p1.hw_count} highway ways "
          f"(of {p1.way_count} total)                    ")

    if p1.hw_count == 0:
        print("    Warning: no highway features found, skipping routing graph")
        # None means "no routing": the caller skips the routing phase.
        return None

    # Find interior refs that appear in 2+ ways.
    if p1.interior_chunks:
        interior_arr = np.concatenate(p1.interior_chunks)
        p1.interior_chunks = []  # free
    else:
        interior_arr = np.empty(0, dtype=np.int64)
    interior_arr.sort()
    # A ref is a "count>=2 junction" if it appears adjacent to an equal ref
    # in the sorted array. Mark either side of each equal-pair.
    if len(interior_arr) > 1:
        dup = interior_arr[:-1] == interior_arr[1:]
        mask = np.concatenate([dup, [False]]) | np.concatenate([[False], dup])
        interior_junctions = np.unique(interior_arr[mask])
    else:
        interior_junctions = np.empty(0, dtype=np.int64)
    del interior_arr
    endpoint_arr = np.fromiter(p1.endpoints, dtype=np.int64, count=len(p1.endpoints))
    junction_arr = np.unique(np.concatenate([interior_junctions, endpoint_arr]))
    del interior_junctions, endpoint_arr
    p1.endpoints = None
    print(f"    Found {len(junction_arr)} junction nodes (graph vertices)")

    # Across the antimeridian, OSM splits a road at ±180: one way ends on a
    # node at 180.0, the next starts on a different node at -180.0, same
    # latitude. Both are way endpoints, so both are junctions; make them one
    # graph vertex so a route can cross. Only for an area that crosses:
    # every other graph is unchanged.
    stitched = {}
    if bbox and area.crosses(bbox):
        stitched = _antimeridian_twins(osmium, source_pbf, EXCLUDED)
        if stitched:
            junction_arr = junction_arr[~np.isin(junction_arr, list(stitched))]
            print(f"    Joined {len(stitched)} road(s) split at the antimeridian")

    # Map junction ref -> graph index (0-based, sorted for determinism).
    # Dict lookup is hot in Pass 2 — Python dict is ~25 M lookups/s which is
    # fine for tens of millions of ways.
    ref_to_idx = {int(r): i for i, r in enumerate(junction_arr)}
    for twin, kept in stitched.items():
        if kept in ref_to_idx:
            ref_to_idx[twin] = ref_to_idx[kept]
    num_nodes = len(junction_arr)
    del junction_arr

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

    def _encode_geom(lons_e7, lats_e7, out):
        """Append a varint-encoded geom to `out`, return (start_byte, end_byte)."""
        start = len(out)
        out.extend(struct.pack('<ii', lons_e7[0], lats_e7[0]))
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
        return start, len(out)

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

    # Geom offsets are stored as uint32 byte offsets into the blob — v2 format
    # caps geom_blob at 2^32 bytes. For continent-scale extracts (Europe) the
    # naive blob can exceed 4 GB. When we detect we're close to the limit, we
    # stop growing the blob and fall back to geom_idx=-1 for subsequent edges
    # (they'll render as straight line-segments between their endpoint nodes).
    # That's a graceful degradation — routing still works, just with fewer
    # intermediate polyline points for very large regions.
    GEOM_BLOB_CAP = 0xFFFF0000  # leave ~64 KB headroom before 2^32

    # Node coordinates indexed by graph idx (populated lazily as we see them).
    node_coords = np.zeros((num_nodes, 2), dtype=np.int32)  # lat_e7, lon_e7
    # Explicit "seen" flags instead of using (0,0) as the unset sentinel —
    # a genuine node at Null Island is indistinguishable otherwise.
    node_has_coords = np.zeros(num_nodes, dtype=bool)

    # Geom dedup: hash geom bytes → geom index. Geom blob accumulates.
    geom_blob = bytearray()
    # geom_offsets[k] = byte offset of geom k's start; geom_offsets[k+1] = end.
    geom_offsets = array.array('I', [0])
    geom_map = {}

    # Name table — deduped street-name strings.
    name_table = [""]
    name_map = {"": 0}

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

            # Walk through refs, splitting at graph nodes (junctions).
            seg_start = 0
            n = len(refs)
            for i in range(1, n):
                if i != n - 1 and refs[i] not in ref_to_idx:
                    continue
                # Segment refs[seg_start:i+1] is between two graph nodes.
                a = seg_start
                b = i
                if b - a < 1:
                    seg_start = i
                    continue
                from_idx = ref_to_idx[refs[a]]
                to_idx = ref_to_idx[refs[b]]
                # Cache endpoint coordinates for EVERY junction we see,
                # including a→a loops: an isolated closed way (parking-lot
                # loop, park path loop) used to leave its only junction at
                # the (0,0) sentinel — a phantom node at Null Island.
                if not node_has_coords[from_idx]:
                    node_coords[from_idx, 0] = lats_e7[a]
                    node_coords[from_idx, 1] = lons_e7[a]
                    node_has_coords[from_idx] = True
                if not node_has_coords[to_idx]:
                    node_coords[to_idx, 0] = lats_e7[b]
                    node_coords[to_idx, 1] = lons_e7[b]
                    node_has_coords[to_idx] = True
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
                    # Skip encoding when near the uint32 blob-size cap —
                    # downstream typed arrays use 4-byte offsets and must fit.
                    # The forward geom is only needed when a forward edge is
                    # emitted (oneway=-1 ways used to encode and orphan it).
                    interior_len = b - a - 1
                    near_cap = len(geom_blob) >= GEOM_BLOB_CAP
                    fgi = -1
                    rgi = -1
                    if oneway != -1 and interior_len > 0 and not near_cap:
                        i_lons = lons_e7[a + 1:b]
                        i_lats = lats_e7[a + 1:b]
                        fstart, fend = _encode_geom(i_lons, i_lats, geom_blob)
                        key = bytes(geom_blob[fstart:fend])
                        existing_gi = geom_map.get(key)
                        if existing_gi is None:
                            fgi = len(geom_offsets) - 1
                            geom_offsets.append(fend)
                            geom_map[key] = fgi
                        else:
                            # Undo append: we already had this geom, trim blob.
                            del geom_blob[fstart:fend]
                            fgi = existing_gi

                    # Reverse geom (distinct encoding since deltas differ).
                    if oneway != 1 and interior_len > 0 and not near_cap:
                        r_lons = list(reversed(lons_e7[a + 1:b]))
                        r_lats = list(reversed(lats_e7[a + 1:b]))
                        rstart, rend = _encode_geom(r_lons, r_lats, geom_blob)
                        rkey = bytes(geom_blob[rstart:rend])
                        existing_rgi = geom_map.get(rkey)
                        if existing_rgi is None:
                            rgi = len(geom_offsets) - 1
                            geom_offsets.append(rend)
                            geom_map[rkey] = rgi
                        else:
                            del geom_blob[rstart:rend]
                            rgi = existing_rgi

                    # dist_dm truncates at 24 bits = 1677 km; real road edges
                    # don't come close, but clamp for safety.
                    dist_dm_packed = min(dist_dm, 0xFFFFFF)
                    dist_speed = ((speed & 0xFF) << 24) | dist_dm_packed
                    # The F821 noqa below: these are closure arrays from
                    # extract_routing_graph; ruff flags them only because the
                    # function `del`s them after this pass.
                    if oneway != -1:
                        edges_from.append(from_idx)  # noqa: F821
                        edges_to.append(to_idx)  # noqa: F821
                        edges_dist_speed.append(dist_speed)  # noqa: F821
                        edges_geom.append(0xFFFFFFFF if fgi < 0 else fgi)  # noqa: F821
                        edges_name.append(name_idx)  # noqa: F821
                        edges_class_access.append(class_access)  # noqa: F821
                        self.edge_count += 1
                    if oneway != 1:
                        edges_from.append(to_idx)  # noqa: F821
                        edges_to.append(from_idx)  # noqa: F821
                        edges_dist_speed.append(dist_speed)  # noqa: F821
                        edges_geom.append(0xFFFFFFFF if rgi < 0 else rgi)  # noqa: F821
                        edges_name.append(name_idx)  # noqa: F821
                        edges_class_access.append(class_access)  # noqa: F821
                        self.edge_count += 1

                seg_start = i

            self.hw_count += 1
            if self.hw_count % 200000 == 0:
                print(f"\r    Pass 2: {self.hw_count} ways, "
                      f"{self.edge_count} edges, "
                      f"{len(geom_offsets) - 1} geoms, "
                      f"{len(geom_blob) // (1024 * 1024)} MB geom blob...",
                      end="", flush=True)

    p2 = _Pass2()
    # File-backed sparse node location store on a fast (NVMe) volume.
    # We iterated through several map types:
    #   - default sparse_mem_array — OOM'd US Pass 2 three runs in a row.
    #   - dense_file_array on /storage HDD — OOM-safe but each node
    #     lookup was a random HDD seek (US Pass 2 didn't finish 200k of
    #     53M ways in 1.5 h).
    #   - dense_mmap_array — anonymous mmap committed ~96 GB virtual for
    #     planet-scale node ids, OOM-killed Europe Pass 2.
    #   - sparse_mem_map — hash-based; OOM-killed Europe Pass 2 too
    #     (~50 GB peak with libosmium overhead + Pass 1 state).
    # sparse_file_array is sorted (id, lon, lat) triples on disk —
    # ~16 GB for Europe's ~1B touched nodes, sequential writes during
    # indexing, mostly cached lookups during way iteration. Putting it
    # on /data (NVMe SSD, 370 GB free) makes random reads fast enough.
    # /data is the project's reserved fast-scratch volume (separate from
    # /storage HDD and the 79 GB / root); cleaned up at end of pass.
    NODE_LOC_DIR = os.environ.get("STREETZIM_NODE_LOC_DIR", "/data")
    if not os.path.isdir(NODE_LOC_DIR) or not os.access(NODE_LOC_DIR, os.W_OK):
        NODE_LOC_DIR = output_dir
    # Unique per run: a fixed name let a second build on the same host
    # delete/rewrite the first build's 16-60 GB index mid-pass.
    import tempfile as _tempfile
    # Another active continent build can use its index for much longer than
    # six hours. Age alone does not establish that a scratch file is stale;
    # each run must only remove the unique file that it created.
    _loc_fd, node_loc_path = _tempfile.mkstemp(
        dir=NODE_LOC_DIR, prefix="streetzim_node_loc_", suffix=".bin")
    os.close(_loc_fd)
    loc_handler = None
    try:
        loc_handler = osmium.NodeLocationsForWays(
            osmium.index.create_map(f"sparse_file_array,{node_loc_path}"))
        loc_handler.ignore_errors()
        osmium.apply(source_pbf, loc_handler, p2)
    finally:
        # Drop loc_handler (and its libosmium index) BEFORE the post-Pass-2
        # numpy work, so the kernel can release the ~60 GB sparse_file_array
        # mmap. Without this, the file pages squat in RssFile even after
        # os.remove(), starving the argsort/fancy-indexing ops that follow
        # of cache and forcing them to thrash through swap. Also runs on
        # an exception so the multi-GB scratch file never outlives a
        # failed pass.
        del loc_handler
        try:
            os.remove(node_loc_path)
        except OSError:
            pass
    print(f"\r    Pass 2: {p2.hw_count} ways, {p2.edge_count} edges, "
          f"{len(geom_offsets) - 1} geoms, "
          f"{len(geom_blob) / (1024 * 1024):.1f} MB geom blob          ")

    # Sort edges by from-node so adj_offsets is just a cumulative-count array.
    num_edges = len(edges_from)
    num_geoms = len(geom_offsets) - 1
    num_names = len(name_table)

    edges_from_np = np.frombuffer(edges_from, dtype=np.uint32)
    sort_order = np.argsort(edges_from_np, kind='stable')
    # Build final edges array in v4 layout (u32 stride = 5):
    #   (target, dist_speed, geom_idx, name_idx, class_access)
    # dist_speed  = (speed << 24) | dist_dm24
    # geom_idx    full u32; 0xFFFFFFFF = "no geometry"
    # class_access bit layout per docs/driving-mode-road-class-warnings.md
    edges_arr = np.empty((num_edges, 5), dtype='<u4')
    edges_arr[:, 0] = np.frombuffer(edges_to, dtype=np.uint32)[sort_order]
    edges_arr[:, 1] = np.frombuffer(edges_dist_speed, dtype=np.uint32)[sort_order]
    edges_arr[:, 2] = np.frombuffer(edges_geom, dtype=np.uint32)[sort_order]
    edges_arr[:, 3] = np.frombuffer(edges_name, dtype=np.uint32)[sort_order]
    edges_arr[:, 4] = np.frombuffer(edges_class_access, dtype=np.uint32)[sort_order]
    edges_from_sorted = edges_from_np[sort_order]
    del edges_from, edges_to, edges_dist_speed, edges_geom, edges_name, edges_class_access
    del edges_from_np, sort_order

    adj_offsets = np.zeros(num_nodes + 1, dtype='<u4')
    # Cumulative count of edges by from-node.
    if num_edges > 0:
        np.add.at(adj_offsets, edges_from_sorted.astype(np.int64) + 1, 1)
    np.cumsum(adj_offsets, out=adj_offsets)
    del edges_from_sorted

    # Nodes array in (lat_e7, lon_e7) layout. node_coords is already shaped (N, 2).
    nodes_arr = node_coords.astype('<i4', copy=False)

    # Geom offsets as numpy uint32; include the closing offset.
    geom_offsets_np = np.frombuffer(geom_offsets, dtype=np.uint32).astype('<u4', copy=False)

    # Pad geom blob to 4-byte alignment (else the following Uint32Array view
    # of name_offsets lands at a non-aligned offset and the browser throws
    # RangeError — cost us hours with Baltics; keep this).
    while len(geom_blob) % 4 != 0:
        geom_blob.append(0)
    geom_bytes_total = len(geom_blob)

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
    with open(output_path, "wb") as f:
        f.write(b"SZRG")
        np.array([4, num_nodes, num_edges, num_geoms, geom_bytes_total,
                  num_names, names_bytes], dtype='<u4').tofile(f)
        nodes_arr.tofile(f)
        adj_offsets.tofile(f)
        edges_arr.tofile(f)
        geom_offsets_np.tofile(f)
        # Binary files accept a buffer directly; bytes() would duplicate the
        # entire geometry blob here (up to ~4 GB) during serialization.
        f.write(geom_blob)
        name_offsets.tofile(f)
        for b in name_blobs:
            f.write(b)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    # Class_access diagnostics — helps verify the writer populated flags
    # for regions that are expected to have lots of roundabouts or ramps.
    class_access_col = edges_arr[:, 4]
    num_round = int(((class_access_col >> 8) & 1).sum())
    num_link = int(np.isin((class_access_col & 0x1F), [2, 4, 6, 8, 10]).sum())
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
