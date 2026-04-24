"""Pure-Python SZRG parser used by the routing differential test suite.

Mirrors the viewer parser in resources/viewer/index.html:parseRoutingGraphBinary.
Supports v2 / v3 / v4 (inline geoms) and v5 (split: main + SZGM companion
``routing-graph-geoms.bin``).

The parser does NOT reimplement the JS heap tie-breaking. Two graph versions
fed into the same Python A* will produce identical routes if their data bytes
are identical; that's the invariant we verify.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np


NO_GEOM_V2 = 0xFFFFFF
NO_GEOM_V3_V4 = 0xFFFFFFFF


@dataclass
class SZRG:
    version: int
    num_nodes: int
    num_edges: int
    num_geoms: int
    num_names: int
    # nodes_scaled[i*2] = lat_e7, [i*2+1] = lon_e7
    nodes_scaled: np.ndarray        # int32, shape (num_nodes*2,)
    adj_offsets: np.ndarray         # uint32, shape (num_nodes+1,)
    edges: np.ndarray               # uint32, shape (num_edges*stride,)
    geom_offsets: np.ndarray        # uint32, shape (num_geoms+1,) — empty array if geoms not loaded
    geom_blob: bytes
    name_offsets: np.ndarray        # uint32, shape (num_names+1,)
    names_blob: bytes
    edge_stride: int
    has_geoms: bool = True          # False on v5 main-only; attach via attach_geoms()

    def edge_target(self, i: int) -> int:
        return int(self.edges[i * self.edge_stride])

    def edge_dist_m(self, i: int) -> float:
        if self.version == 2:
            return int(self.edges[i * self.edge_stride + 1]) / 10.0
        return (int(self.edges[i * self.edge_stride + 1]) & 0xFFFFFF) / 10.0

    def edge_speed(self, i: int) -> int:
        if self.version == 2:
            return int(self.edges[i * self.edge_stride + 2]) >> 24
        return int(self.edges[i * self.edge_stride + 1]) >> 24

    def edge_geom_idx(self, i: int) -> int:
        if self.version == 2:
            return int(self.edges[i * self.edge_stride + 2]) & 0xFFFFFF
        return int(self.edges[i * self.edge_stride + 2])

    def edge_name_idx(self, i: int) -> int:
        return int(self.edges[i * self.edge_stride + 3])

    def edge_class_access(self, i: int) -> int:
        if self.version not in (4, 5):
            return 0
        return int(self.edges[i * self.edge_stride + 4])

    @property
    def no_geom(self) -> int:
        return NO_GEOM_V2 if self.version == 2 else NO_GEOM_V3_V4

    def get_name(self, name_idx: int) -> str:
        if name_idx <= 0 or name_idx >= self.num_names:
            return ""
        start = int(self.name_offsets[name_idx])
        end = int(self.name_offsets[name_idx + 1])
        if end == start:
            return ""
        return self.names_blob[start:end].decode("utf-8", errors="replace")

    def attach_geoms(self, geoms_buf: bytes) -> None:
        """Attach the v5 SZGM companion blob so decoders see geom data."""
        g_offsets, g_blob, g_num_geoms = parse_szgm_bytes(geoms_buf)
        if g_num_geoms != self.num_geoms:
            raise ValueError(
                f"SZGM numGeoms ({g_num_geoms}) does not match SZRG numGeoms "
                f"({self.num_geoms})"
            )
        self.geom_offsets = g_offsets
        self.geom_blob = g_blob
        self.has_geoms = True


def parse_szrg_bytes(buf: bytes) -> SZRG:
    if buf[:4] != b"SZRG":
        raise ValueError("Not a SZRG graph (bad magic)")
    version, num_nodes, num_edges, num_geoms, geom_bytes_total, num_names, names_bytes = \
        struct.unpack_from("<7I", buf, 4)
    if version not in (2, 3, 4, 5):
        raise ValueError(f"Unsupported SZRG version: {version}")
    # v4 and v5 both include the class_access u32 per edge.
    edge_stride = 5 if version in (4, 5) else 4

    off = 32
    nodes_scaled = np.frombuffer(buf, dtype="<i4", count=num_nodes * 2, offset=off)
    off += num_nodes * 2 * 4
    adj_offsets = np.frombuffer(buf, dtype="<u4", count=num_nodes + 1, offset=off)
    off += (num_nodes + 1) * 4
    edges = np.frombuffer(buf, dtype="<u4", count=num_edges * edge_stride, offset=off)
    off += num_edges * edge_stride * 4

    has_geoms = True
    if version == 5:
        # v5 hoists geom_offsets + geom_blob into the SZGM companion file.
        # Header reports numGeoms (so edge geom_idx values stay meaningful)
        # but geomBytes is 0 here; load via attach_geoms() before any
        # geom-dependent fingerprint field (``g`` in the corpus record).
        geom_offsets = np.empty(0, dtype="<u4")
        geom_blob = b""
        has_geoms = False
    else:
        geom_offsets = np.frombuffer(buf, dtype="<u4", count=num_geoms + 1, offset=off)
        off += (num_geoms + 1) * 4
        geom_blob = bytes(buf[off:off + geom_bytes_total])
        off += geom_bytes_total

    name_offsets = np.frombuffer(buf, dtype="<u4", count=num_names + 1, offset=off)
    off += (num_names + 1) * 4
    names_blob = bytes(buf[off:off + names_bytes])

    return SZRG(
        version=version,
        num_nodes=num_nodes,
        num_edges=num_edges,
        num_geoms=num_geoms,
        num_names=num_names,
        nodes_scaled=nodes_scaled,
        adj_offsets=adj_offsets,
        edges=edges,
        geom_offsets=geom_offsets,
        geom_blob=geom_blob,
        name_offsets=name_offsets,
        names_blob=names_blob,
        edge_stride=edge_stride,
        has_geoms=has_geoms,
    )


def parse_szgm_bytes(buf: bytes) -> tuple[np.ndarray, bytes, int]:
    """Parse the SZGM (Streetzim Graph-Geoms) v5 companion blob.

    Returns (geom_offsets, geom_blob, num_geoms). Keep this parser in sync
    with the viewer's ``parseRoutingGeoms()`` counterpart.
    """
    if buf[:4] != b"SZGM":
        raise ValueError("Not a SZGM geoms blob (bad magic)")
    version, num_geoms, geom_bytes_total = struct.unpack_from("<3I", buf, 4)
    if version != 1:
        raise ValueError(f"Unsupported SZGM version: {version}")
    off = 16
    geom_offsets = np.frombuffer(buf, dtype="<u4", count=num_geoms + 1, offset=off)
    off += (num_geoms + 1) * 4
    geom_blob = bytes(buf[off:off + geom_bytes_total])
    return geom_offsets, geom_blob, num_geoms


def _fetch_chunked_blob(arc, manifest_path: str, chunk_path_prefix: str) -> bytes:
    """Reassemble a chunked entry from a ZIM.

    Checks: manifest schema, declared total bytes, sha256 of concatenation.
    Anything torn → ValueError (surfaces torn uploads loudly rather than
    letting them corrupt routing silently).
    """
    import hashlib
    import json as _json
    manifest_entry = arc.get_entry_by_path(manifest_path)
    manifest = _json.loads(bytes(manifest_entry.get_item().content))
    expected_total = manifest["total_bytes"]
    expected_sha = manifest.get("sha256")
    out = bytearray()
    for ch in manifest["chunks"]:
        # ``path`` is relative to the manifest directory.
        base = manifest_path.rsplit("/", 1)[0] if "/" in manifest_path else ""
        fullpath = f"{base}/{ch['path']}" if base else ch["path"]
        entry = arc.get_entry_by_path(fullpath)
        data = bytes(entry.get_item().content)
        if len(data) != ch["bytes"]:
            raise ValueError(
                f"chunk {ch['path']} size mismatch: "
                f"manifest {ch['bytes']} vs entry {len(data)}"
            )
        out.extend(data)
    if len(out) != expected_total:
        raise ValueError(
            f"chunked blob total bytes {len(out)} != manifest "
            f"{expected_total}"
        )
    if expected_sha:
        got = hashlib.sha256(out).hexdigest()
        if got != expected_sha:
            raise ValueError(
                f"chunked blob sha256 mismatch: expected {expected_sha} "
                f"got {got}"
            )
    return bytes(out)


def _try_load_blob(arc, primary_path: str,
                   chunk_manifest_path: str,
                   chunk_path_prefix: str) -> bytes | None:
    """Try primary path first (single entry). Fall back to the chunk
    manifest if the primary doesn't exist.

    Returns None only if NEITHER entry is findable in the archive. If the
    primary isn't present but the manifest IS, any reassembly failure
    propagates (torn chunk, wrong sha, ...) so callers see the real
    problem instead of a generic "not found".
    """
    try:
        entry = arc.get_entry_by_path(primary_path)
        return bytes(entry.get_item().content)
    except Exception:
        pass
    try:
        arc.get_entry_by_path(chunk_manifest_path)
    except Exception:
        return None
    # Manifest exists — any error from here is a real reassembly problem.
    return _fetch_chunked_blob(arc, chunk_manifest_path, chunk_path_prefix)


def load_from_zim(zim_path: str | Path) -> SZRG:
    """Load routing graph from ZIM. Auto-detects:
      * v4/v5 via SZRG header version field
      * chunked v4/v5 via ``routing-data/graph-chunk-manifest.json``
      * v5 geoms companion (inline or chunked via graph-geoms-chunk-manifest.json)

    Spatial-chunked ZIMs (``routing-data/graph-cells-index.bin`` +
    per-cell SZRC entries) don't fit the monolithic SZRG contract — use
    ``tests.szrg_spatial.load_spatial_from_zim`` for those.
    """
    from libzim.reader import Archive
    arc = Archive(str(zim_path))

    main_bytes = _try_load_blob(
        arc,
        "routing-data/graph.bin",
        "routing-data/graph-chunk-manifest.json",
        "routing-data/graph-chunk",
    )
    if main_bytes is None:
        # Surface a more specific hint if the ZIM is actually
        # spatial-split. The old inner bare-except swallowed the
        # ValueError we were trying to raise when cells-index existed,
        # so callers got the generic FileNotFoundError and missed the
        # signal to use load_spatial_from_zim.
        spatial_present = False
        try:
            arc.get_entry_by_path("routing-data/graph-cells-index.bin")
            spatial_present = True
        except Exception:
            spatial_present = False
        if spatial_present:
            raise ValueError(
                f"{zim_path} is spatial-chunked — use "
                f"tests.szrg_spatial.load_spatial_from_zim()"
            )
        raise FileNotFoundError(
            f"{zim_path} has no routing-data/graph.bin or chunked variant"
        )
    g = parse_szrg_bytes(main_bytes)

    if g.version == 5 and not g.has_geoms:
        geoms_bytes = _try_load_blob(
            arc,
            "routing-data/graph-geoms.bin",
            "routing-data/graph-geoms-chunk-manifest.json",
            "routing-data/graph-geoms-chunk",
        )
        if geoms_bytes is not None:
            g.attach_geoms(geoms_bytes)
        else:
            import warnings
            warnings.warn(
                f"v5 SZRG without graph-geoms.bin companion (chunked or inline)"
            )
    return g


def load_from_file(path: str | Path, geoms_path: str | Path | None = None) -> SZRG:
    """Load SZRG from a raw file. Optionally attach a SZGM companion.

    When ``geoms_path`` is None and the main file is v5, the caller gets back
    a geom-less graph — fine for A* which doesn't read geoms, but no
    geom_sequence field in the route fingerprint.
    """
    with open(path, "rb") as fh:
        g = parse_szrg_bytes(fh.read())
    if g.version == 5 and geoms_path is not None:
        with open(geoms_path, "rb") as gh:
            g.attach_geoms(gh.read())
    elif g.version == 5:
        # Auto-discover companion beside the main file (common case: both
        # written by ``extract_routing_graph(split_graph=True)``).
        guess = Path(path).with_name(Path(path).stem.replace(
            "graph", "graph-geoms") + ".bin")
        if guess.is_file():
            with open(guess, "rb") as gh:
                g.attach_geoms(gh.read())
    return g
