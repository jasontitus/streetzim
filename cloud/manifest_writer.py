"""ManifestCreator — duck-compatible drop-in for libzim's Creator that
writes a JSONL manifest and runs the Python `streetzim.pack` writer.

The libzim API surface used by `streetzim/zim_writer.py` is small:

    Creator(str(output_path))
    creator.config_indexing(True, "en")
    creator.config_clustersize(cluster_size)
    creator.config_nbworkers(num_workers)
    creator.set_mainpath("index.html")
    with creator:
        creator.add_metadata(name, value)
        creator.add_illustration(side, png_bytes)
        creator.add_item(MapItem(...))

ManifestCreator implements the same surface plus `add_redirection`
(used by repackage_zim.py).

Body-encoding strategy:

  - Inline UTF-8 (`content`) for text mimes ≤ 256 KiB. Cheapest path;
    no encoding cost.
  - Inline base64 (`body_b64`) for everything else that fits in
    memory. The 33 % size tax buys us no per-item `open()` syscalls
    at consume time — at Japan-scale (3.2 M items) the previous
    per-body file-stage path spent ~320 s in `open()` syscalls alone.
  - On-disk path (`file`) is reserved for streaming-mode items
    (>= 64 MiB; avoids large base64 and JSON transport allocations).
    Both raw and compressed file bodies stream through bounded chunks.

Per-item compress is supported: the packer routes items into separate
clusters by effective compression, so a single ZIM can mix compressed
and raw clusters (use case: streetzim's >500 MB routing chunks that
need raw clusters for PWA fzstd compatibility while tiles/HTML stay
zstd-compressed).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import Any


# Mime types we'll inline directly in the manifest as `content` strings.
# Skipping the per-entry file-stage syscall is the single biggest win on
# small-item-heavy builds (silicon-valley has 17 K entries, mostly
# small JSON). Anything not in this set (PBF tiles, AVIF satellite,
# WebP terrain, PNG icons) gets file-staged as before — those are
# binary, JSON-stringification would lose data.
_INLINE_TEXT_MIMES = frozenset({
    "application/json",
    "application/javascript",
    "text/javascript",
    "application/xml",
    "text/html",
    "text/plain",
    "text/css",
    "text/csv",
    "image/svg+xml",
})

# Cap inline payloads at 256 KiB so the JSONL line-length stays sane
# and Python's json.dumps doesn't blow memory on a freak record. Items
# above this fall back to file-stage even when the mime suggests text.
_INLINE_TEXT_LIMIT = 256 * 1024

# Bodies at or above this size use a file recipe rather than base64.
# The Python packer streams raw and compressed bodies in bounded chunks. Base64-inlining a 1 GB routing
# chunk would briefly hold ~1.4 GB of UTF-8 string + the source bytes
# in Python's memory. 64 MiB is the same threshold add_item used
# pre-base64 to switch on `streaming`, so the break-even point is
# unchanged.
_STREAMING_THRESHOLD = 64 * 1024 * 1024


def _encode_body_b64(data: bytes) -> str:
    """Encode binary body for inline JSONL transport. Pure ASCII out,
    so JSON needs no escape characters and the 1.33× inflation is
    the only cost. Decode happens once in the packer per record."""
    return base64.b64encode(data).decode("ascii")


# Where a built Rust streetzim-pack is looked for, after STREETZIM_PACK_BIN.
def _rust_build_candidates() -> list[Path]:
    repo = Path(__file__).resolve().parent.parent
    return [repo / "rust" / "streetzim-pack" / "target" / build / "streetzim-pack"
            for build in ("release", "debug")]


# Override with STREETZIM_PACK_BIN if the binary lives somewhere unusual
# (CI runners, vendored release builds).
def resolve_pack_binary() -> str:
    """Resolve a built Rust streetzim-pack: STREETZIM_PACK_BIN, else
    rust/streetzim-pack/target/{release,debug}/streetzim-pack."""
    explicit = os.environ.get("STREETZIM_PACK_BIN")
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError(
                f"STREETZIM_PACK_BIN is not an executable file: {explicit!r}. "
                "Build streetzim-pack and provide its executable path.")
        return str(candidate.resolve())
    candidates = _rust_build_candidates()
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise RuntimeError(
        "--zim-builder rust needs a built Rust streetzim-pack, and none was found "
        f"(looked for STREETZIM_PACK_BIN, {', '.join(str(c) for c in candidates)}). "
        "Build it with `cd rust/streetzim-pack && cargo build --release` "
        "(see docs/zim-builder-rust.md), set STREETZIM_PACK_BIN to its path, "
        "or use --zim-builder manifest for the Python packer."
    )


_resolve_pack_binary = resolve_pack_binary  # compatibility for existing callers


PACK_BUILDERS = ("manifest", "rust")


def resolve_pack_command(builder: str = "manifest") -> list[str]:
    """The packer command for a ``--zim-builder`` choice.

    ``manifest``: this interpreter's ``streetzim.pack`` (the Python packer),
    unless STREETZIM_PACK_BIN names an executable override.
    ``rust``: a built Rust streetzim-pack (resolve_pack_binary); raises
    RuntimeError when there is none rather than running Python instead.
    """
    if builder not in PACK_BUILDERS:
        raise ValueError(f"unknown packer backend: {builder!r}")
    if builder == "rust" or os.environ.get("STREETZIM_PACK_BIN"):
        return [resolve_pack_binary()]
    from importlib.util import find_spec
    if find_spec("streetzim.pack") is None or find_spec("zstandard") is None:
        raise RuntimeError("install streetzim and its zstandard dependency before packing")
    return [sys.executable, "-m", "streetzim.pack"]


_MANIFEST_ZSTD_THREADS = 4

# On an interrupted build the packer gets SIGTERM (the Python packer then
# removes its scratch and exits) and SIGKILL only if it outlives this grace.
_TERM_GRACE_S = 5.0

# Set to 1 to keep a failed attempt's stage (manifest, staged bodies, partial
# archive) for inspection. Otherwise a failed attempt removes it.
KEEP_STAGE_ENV = "STREETZIM_KEEP_PACK_STAGE"


def _keep_failed_stage_requested() -> bool:
    return os.environ.get(KEEP_STAGE_ENV, "") == "1"


def _proc_vm_hwm_kb(pid: int | str) -> int | None:
    """VmHWM (kB) from /proc/<pid>/status; None where /proc is unavailable."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _proc_cmdline(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0")[:-1]]


def _manifest_zstd_enabled() -> bool:
    if os.environ.get("STREETZIM_MANIFEST_ZSTD", "1") == "0":
        return False
    try:
        import zstandard  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        return False
    return True


class ManifestCreator:
    """Captures every libzim Creator call as a JSONL record. Spawns
    `streetzim-pack` at __exit__."""

    def __init__(
        self,
        output_path: str,
        *,
        compression: str = "zstd",
        compression_level: int | None = None,
        cluster_strategy: str = "single",
        # Empirically: ``by_mime`` puts search-data/*.json into a
        # different cluster from index/wikidata/etc., so a viewer
        # fan-out fetch of 256 chunks ends up spread across more
        # clusters → more random-access seeks per chunk → ~25-50%
        # higher wall-clock for typeahead-style workloads. Measured
        # on California 2026-05-07: by_mime hit the 15 s smoke
        # timeout while libzim's `single` (default) finished in <8 s
        # for the same byte-identical chunk content. ``single`` packs
        # everything in one cluster (post-zstd) and trades a slight
        # write-time hit for the random-access win at read time —
        # the right default for streetzim ZIMs whose primary load
        # pattern IS random-access typeahead lookup.
        max_in_flight_bytes: int | None = None,
        keep_stage: bool = False,
        verbose: bool = False,
        builder: str = "manifest",
    ) -> None:
        if builder not in PACK_BUILDERS:
            raise ValueError(f"unknown packer backend: {builder!r}")
        self._builder = builder
        self._output_path = str(output_path)
        # A unique stage contains the manifest and large in-memory bodies.
        # Concurrent attempts cannot truncate each other's recovery material.
        output = Path(self._output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        self._stage_dir = Path(tempfile.mkdtemp(
            prefix=output.name + ".pack-stage-", dir=output.parent))
        self._staged_bodies = 0
        self._workers: int | None = None
        # Write the manifest zstd-compressed when python-zstandard is present
        # (STREETZIM_MANIFEST_ZSTD=0 forces plain). Measured on brazil's 93 GB
        # manifest: zstd -3 shrinks the base64 tile section 1.87x and the JSON
        # search-data section — 72% of the bytes — 5.22x, so ~93 GB -> ~25 GB.
        # That is a footprint win, not a speed win: generation is bound by one
        # Python core (profiled at 100% CPU, 2 MB/s written), and compression
        # runs on zstd's own worker threads outside the GIL. What it buys is
        # 70 GB less sequential I/O on the HDD the tile reads share, and a
        # manifest small enough to stage on NVMe under the reserve.
        # streetzim-pack detects zstd by magic, so either form packs.
        self._zstd = _manifest_zstd_enabled()
        self._manifest_path = self._stage_dir / (
            "manifest.jsonl.zst" if self._zstd else "manifest.jsonl")
        try:
            if self._zstd:
                import io
                import zstandard
                _raw = self._manifest_path.open("wb")
                try:
                    _zw = zstandard.ZstdCompressor(
                        level=3, threads=_MANIFEST_ZSTD_THREADS).stream_writer(_raw, closefd=True)
                    try:
                        self._mf = io.TextIOWrapper(_zw, encoding="utf-8", newline="\n")
                    except BaseException:
                        _raw.close()
                        raise
                except BaseException:
                    _raw.close()
                    raise
            else:
                self._mf = self._manifest_path.open("w", encoding="utf-8")
        except BaseException:
            shutil.rmtree(self._stage_dir, ignore_errors=True)
            raise
        self._entered = False
        # keep_stage keeps the stage after success as well; the environment
        # flag keeps only a failed attempt's stage.
        self._keep_failed_stage = keep_stage or _keep_failed_stage_requested()
        self._closed = False
        self._keep_stage = keep_stage
        self._verbose = verbose
        # Initial config record. Cluster size + main_path are filled in
        # by config_clustersize / set_mainpath; we buffer the dict and
        # write it at __enter__ so callers can configure freely first.
        # Default cluster_size_target = 8 MiB. Measured 25 % faster
        # AND 2 % smaller output than libzim's 2 MiB default at zstd-22
        # on silicon-valley (137 s → 119 s wall, 289 MB → 283 MB
        # output) — bigger clusters give zstd a richer dictionary and
        # amortize per-cluster overhead. 32 MiB is marginally faster
        # (116 s) but doubles peak in-flight bytes; 8 MiB is the
        # sweet spot. Callers can still override with
        # config_clustersize().
        self._config: dict[str, Any] = {
            "kind": "config",
            "compression": compression,
            "cluster_strategy": cluster_strategy,
            "cluster_size_target": 8 * 1024 * 1024,
        }
        if compression_level is not None:
            self._config["compression_level"] = compression_level
        if max_in_flight_bytes is not None:
            self._config["max_in_flight_bytes"] = max_in_flight_bytes

    # ---- libzim Creator config surface (mostly no-ops) ---------------

    def _configurable(self) -> None:
        if self._entered or self._closed:
            raise RuntimeError("configure the manifest creator before entering it")

    def config_indexing(self, enabled: bool, lang: str) -> None:
        self._configurable()
        if enabled:
            raise ValueError(
                "Manifest ZIM writer cannot run libzim's Xapian indexer; "
                "choose --xapian=builder (external indexes) or "
                "--xapian=none (in-viewer search only).")
        self._config["_indexing_requested"] = bool(enabled)
        self._config["_indexing_lang"] = lang

    def config_clustersize(self, bytes_size: int) -> None:
        self._configurable()
        self._config["cluster_size_target"] = int(bytes_size)

    def config_nbworkers(self, n: int) -> None:
        if n <= 0:
            raise ValueError("compression workers must be positive")
        self._configurable()
        self._workers = int(n)
        self._config["_nbworkers_requested"] = int(n)

    def set_mainpath(self, path: str) -> None:
        self._configurable()
        self._config["main_path"] = str(path)

    # ---- context manager: writes config + opens for items ------------

    def __enter__(self) -> ManifestCreator:
        self._configurable()
        self._entered = True
        try:
            self._write_record(self._config)
        except BaseException:
            self._closed = True
            try:
                self._mf.close()
            except BaseException:
                print("Manifest flush also failed", file=sys.stderr)
            self._discard_failed_stage()
            raise
        return self

    def _discard_failed_stage(self) -> None:
        """Remove a failed attempt's stage, unless asked to keep it."""
        if self._keep_failed_stage:
            print(f"  Pack stage kept for inspection: {self._stage_dir}", file=sys.stderr)
        else:
            shutil.rmtree(self._stage_dir, ignore_errors=True)

    def _stage_note(self) -> str:
        if self._keep_failed_stage:
            return f"Manifest preserved at {self._manifest_path} for inspection."
        return (f"The pack stage was removed; set {KEEP_STAGE_ENV}=1 to keep the "
                "manifest of a failed attempt.")

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> bool:
        if self._closed:
            return False
        self._closed = True
        try:
            self._mf.close()
        except BaseException:
            if exc_type is None:
                self._discard_failed_stage()
                raise
            # A failed compressor flush must not replace the producer failure.
            print("Manifest flush also failed", file=sys.stderr)
        if exc_type is not None:
            # Bubble up the original error. The stage is removed unless
            # keep_stage or STREETZIM_KEEP_PACK_STAGE=1 asks to keep it.
            self._discard_failed_stage()
            return False
        try:
            self._run_packer()
        except BaseException:
            self._discard_failed_stage()
            raise
        if not self._keep_stage:
            shutil.rmtree(self._stage_dir, ignore_errors=True)
        return False

    # ---- libzim Creator item surface ---------------------------------

    def add_metadata(self, name: str, value: Any) -> None:
        self._write_record(self._metadata_record(name, value, mimetype=None))

    def add_metadata_with_mimetype(self, name: str, mimetype: str, value: Any) -> None:
        self._write_record(self._metadata_record(name, value, mimetype=mimetype))

    def add_illustration(self, side: int, png_bytes: bytes) -> None:
        # Illustration PNGs are tiny (typical 48 px favicon ~ a few KB)
        # — always inline.
        self._write_record(
            {
                "kind": "illustration",
                "size": int(side),
                "body_b64": _encode_body_b64(bytes(png_bytes)),
            }
        )

    def add_redirection(self, path: str, title: str, target: str,
                        hints: dict[Any, Any] | None = None) -> None:
        """libzim's signature: a redirect is a front article (listed in
        listing/titleOrdered/v1) only with a true FRONT_ARTICLE hint."""
        rec: dict[str, Any] = {"kind": "redirect", "path": str(path),
                               "title": str(title or ""), "target": str(target)}
        if any(getattr(key, "name", key) == "FRONT_ARTICLE" and value
               for key, value in (hints or {}).items()):
            rec["front"] = True
        self._write_record(rec)

    def add_item(self, item: Any) -> None:
        rec = self._item_record(item)
        self._write_record(rec)

    def cluster_break(self, cluster_size_target: int | None = None) -> None:
        """Close the cluster being filled so the next item starts a new one,
        optionally changing the cluster size target from here on.

        Meant to be emitted by the builder between zoom levels of the tile
        components (--tile-order zoom-hilbert, designed in
        docs/zim-variants.md but not yet ported: nothing calls this on
        main) so a zoom never shares a
        cluster with its neighbours and cloud/derive_zim.py can drop or copy
        it whole. The Python packer flushes every active bucket and applies
        the optional target to subsequent items. An explicit legacy Rust
        executable honours the flush only with its ``cluster_break`` Cargo
        feature and zimru's flush/target patch; otherwise it warns once.
        Older packers reject this record, so producers need a compatible
        executable override when selecting one.
        """
        rec: dict[str, Any] = {"kind": "cluster_break"}
        if cluster_size_target is not None:
            rec["cluster_size_target"] = int(cluster_size_target)
        self._write_record(rec)

    # ---- helpers -----------------------------------------------------

    def _metadata_record(
        self, name: str, value: Any, mimetype: str | None
    ) -> dict[str, Any]:
        rec: dict[str, Any] = {"kind": "metadata", "name": str(name)}
        if mimetype is not None:
            rec["mimetype"] = str(mimetype)
        if isinstance(value, (bytes, bytearray)):
            rec["body_b64"] = _encode_body_b64(bytes(value))
        else:
            rec["value"] = value if isinstance(value, str) else str(value)
        return rec

    def _item_record(self, item: Any) -> dict[str, Any]:
        path = item._path  # noqa: SLF001 — duck-typed MapItem
        title = getattr(item, "_title", "") or ""
        mime = item._mimetype  # noqa: SLF001
        is_front = getattr(item, "_is_front", None)
        compress = bool(getattr(item, "_compress", True))
        namespace = getattr(item, "_namespace", None)

        rec: dict[str, Any] = {
            "kind": "item",
            "path": str(path),
            "title": str(title),
            "mime": str(mime),
        }
        # The FRONT_ARTICLE hint. Without a front field the packer applies
        # libzim's default (text/html items are front articles), so an HTML
        # application page such as places.html records an explicit false.
        if is_front or (is_front is not None and str(mime).startswith("text/html")):
            rec["front"] = bool(is_front)
        if compress is False:
            # Per-item override — the packer routes this item to its own
            # uncompressed cluster regardless of the build's default.
            rec["compress"] = False
        if namespace is not None:
            # Caller is placing the item into a non-default namespace
            # (typical: 'X' for Xapian indexes at X/fulltext/xapian and
            # X/title/xapian). the manifest writer honors the
            # namespace byte; pass it through so the packer can route
            # the entry to the right namespace dirent table.
            if isinstance(namespace, str):
                if len(namespace) != 1:
                    raise ValueError(
                        f"namespace must be a single character: {namespace!r}"
                    )
                rec["namespace"] = ord(namespace)
            else:
                rec["namespace"] = int(namespace)

        file_path = getattr(item, "_file_path", None)
        if file_path:
            size = os.path.getsize(file_path)
            if size >= _STREAMING_THRESHOLD:
                # Multi-MiB-to-multi-GB items (routing graph chunks,
                # large PBF blobs) — let the packer read them in its own
                # 4 MiB chunks at pack time (raw bodies avoid whole-file buffering).
                rec["file"] = str(Path(file_path).resolve())
                rec["streaming"] = True
                rec["size"] = size
            else:
                # Sub-streaming-threshold disk-backed item — read once
                # and inline as base64. Saves a per-item `open()` on
                # the Rust side (the whole point of the body_b64
                # path); the file is typically already hot in the
                # page cache because Python just wrote it.
                with open(file_path, "rb") as f:
                    rec["body_b64"] = _encode_body_b64(f.read())
        else:
            data = getattr(item, "_data", None)
            if data is None:
                raise ValueError(
                    f"add_item({path!r}): item has neither _file_path nor _data"
                )
            data = bytes(data)
            if len(data) >= _STREAMING_THRESHOLD:
                # Existing in-memory content still belongs to the caller, but
                # staging avoids two base64 copies plus a huge JSON string.
                # Keep the body with its manifest for retries after a failure.
                self._staged_bodies += 1
                body_path = self._stage_dir / f"body-{self._staged_bodies:08d}.bin"
                with body_path.open("wb") as body:
                    body.write(data)
                rec.update(file=str(body_path.resolve()), streaming=True,
                           size=len(data))
                return rec
            # Inline small text-ish items as a `content` string. UTF-8
            # round-trips through JSON without the 33 % base64 tax;
            # for HTML/JS/CSS/JSON that mostly lives in this branch
            # the savings are real on the manifest size side.
            # Threshold of 256 KiB keeps any single JSONL line sane.
            inlined_text = False
            if (len(data) <= _INLINE_TEXT_LIMIT
                    and mime in _INLINE_TEXT_MIMES):
                try:
                    rec["content"] = data.decode("utf-8")
                    inlined_text = True
                except UnicodeDecodeError:
                    # Mime says text but bytes aren't UTF-8 — fall
                    # through to body_b64 to avoid lossy encoding.
                    pass
            if not inlined_text:
                rec["body_b64"] = _encode_body_b64(data)
        return rec

    def _write_record(self, rec: dict[str, Any]) -> None:
        if not self._entered or self._closed:
            raise RuntimeError("add records inside the manifest creator context")
        self._mf.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        self._mf.write("\n")

    def _run_packer(self) -> None:
        command = (resolve_pack_command() if self._builder == "manifest"
                   else resolve_pack_command(self._builder))
        candidate = self._stage_dir / "packed.zim"
        stats_path = self._stage_dir / "pack-stats.json"
        cmd = command + [str(self._manifest_path), str(candidate)]
        if self._workers is not None:
            cmd.extend(["--threads", str(self._workers)])
        _rayon = str(self._workers) if self._workers is not None else os.environ.get("RAYON_NUM_THREADS")
        if self._verbose:
            cmd.append("--verbose")
            print(f"  streetzim-pack: {' '.join(cmd)}", flush=True)
        manifest_size = os.path.getsize(self._manifest_path)
        started = time.time()
        _peak_kb = [0]
        try:
            # Peak RSS is the packer's own. The Python packer reads its VmHWM
            # from /proc/self/status as it finishes and writes it to
            # STREETZIM_PACK_STATS_FILE; that figure is used when present.
            # wait4()'s ru_maxrss is not: on Linux it carries the parent's RSS
            # at fork, so a large builder inflated it. For an executable that
            # writes no stats, /proc/<pid>/status is polled, ignoring samples
            # taken before the child has exec'd the packer (its command line
            # is still the parent's), whose VmHWM is the parent's as well.
            watch_stop = threading.Event()

            def _watch_hwm(pid: int, out: list[int]) -> None:
                execed = False
                while not watch_stop.is_set():
                    if not execed:
                        current = _proc_cmdline(pid)
                        if current is None:
                            return          # process gone or no /proc
                        # A shebang script's argv[0] becomes its interpreter,
                        # so compare the arguments that follow it.
                        execed = (len(current) >= len(cmd)
                                  and current[len(current) - len(cmd) + 1:] == cmd[1:])
                    if execed:
                        value = _proc_vm_hwm_kb(pid)
                        if value is None:
                            return          # process gone
                        out[0] = max(out[0], value)
                    watch_stop.wait(0.25)

            child_env = os.environ.copy()
            child_env["STREETZIM_PACK_STATS_FILE"] = str(stats_path)
            if len(command) > 1:
                # Absolute script invocations from another cwd need to resolve
                # the same checkout/package as this parent. Keep the cwd so
                # relative input paths retain their meaning.
                package_root = str(Path(__file__).resolve().parent.parent)
                prior = child_env.get("PYTHONPATH")
                child_env["PYTHONPATH"] = package_root + (os.pathsep + prior if prior else "")
            _proc = subprocess.Popen(cmd, env=child_env)
            _t: threading.Thread | None = None
            try:
                _t = threading.Thread(target=_watch_hwm, args=(_proc.pid, _peak_kb),
                                      daemon=True)
                _t.start()
                _rc = _proc.wait()
            except BaseException:
                # Interrupted (or the monitor failed): ask the packer to stop
                # so it removes its scratch, then kill it after a short grace.
                # A bare Popen would otherwise leave it running on its own.
                _proc.terminate()
                try:
                    _proc.wait(timeout=_TERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    _proc.kill()
                    _proc.wait()
                raise
            finally:
                watch_stop.set()
                if _t is not None and _t.ident is not None:
                    _t.join(timeout=1.0)
            reported = self._reported_peak_kb(stats_path)
            if reported is not None:
                _peak_kb[0] = reported
            if _rc != 0:
                raise subprocess.CalledProcessError(_rc, cmd)
            if not candidate.is_file() or candidate.stat().st_size == 0:
                raise RuntimeError(f"streetzim-pack returned success without an archive. "
                                   f"{self._stage_note()}")
            os.replace(candidate, self._output_path)
            if _peak_kb[0]:
                _peak = (f"{_peak_kb[0] / 1048576:.1f} GB" if _peak_kb[0] >= 1048576
                         else f"{_peak_kb[0] / 1024:.0f} MB")
                print(f"    streetzim-pack peak RSS {_peak}"
                      + (f" (workers={_rayon})" if _rayon else " (automatic workers)"),
                      flush=True)
        except subprocess.CalledProcessError as e:
            if e.returncode == -9:
                raise RuntimeError(
                    f"streetzim-pack was KILLED (SIGKILL), possibly by a memory "
                    f"limit or external termination. If memory was exhausted, "
                    f"lower --zim-workers or RAYON_NUM_THREADS "
                    f"(currently {_rayon or 'all cores'}). {self._stage_note()}"
                ) from e
            raise RuntimeError(
                f"streetzim-pack failed (exit {e.returncode}). {self._stage_note()}"
            ) from e
        elapsed = time.time() - started
        out_size = (os.path.getsize(self._output_path)
                    if os.path.isfile(self._output_path) else 0)
        if self._verbose:
            print(
                f"  streetzim-pack done in {elapsed:.1f}s — "
                f"{self._output_path} ({out_size/1e9:.2f} GB)",
                flush=True,
            )
        # Surface the packer wall-clock as a sub-phase under whatever
        # top-level phase the caller is currently in (usually the
        # `[N/total] Creating ZIM file` phase). Keeps the optimization
        # target — libzim Creator vs. Python manifest packer — in
        # the build summary so before/after comparisons are concrete.
        #
        # Inter-module note: when create_osm_zim runs as __main__, its
        # module name is '__main__', not 'create_osm_zim'. A bare
        # `from create_osm_zim import PHASE_TIMER` would import the
        # file again as a fresh module with a fresh-and-empty timer.
        # Walk sys.modules and pick whichever copy has a PHASE_TIMER —
        # they share file path so it's the same instance whichever
        # module name we ended up under.
        # PHASE_TIMER now lives in streetzim.common (one instance, whatever
        # imported the builder); the sys.modules walk stays as a fallback for
        # an older checkout on sys.path.
        timer = None
        try:
            from streetzim.common import PHASE_TIMER as timer
        except ImportError:
            for mod_name in ("__main__", "create_osm_zim"):
                mod = sys.modules.get(mod_name)
                if mod is not None and hasattr(mod, "PHASE_TIMER"):
                    timer = mod.PHASE_TIMER
                    break
        if timer is not None:
            try:
                timer.record_subphase(
                    "zim-pack: streetzim-pack (Rust)" if self._builder == "rust"
                    else "zim-pack: streetzim-pack (override)" if len(command) == 1
                    else "zim-pack: streetzim-pack (Python)",
                    elapsed,
                    note=f"manifest {manifest_size/1e6:.0f} MB"
                         f"{' (zstd)' if self._zstd else ''} → ZIM {out_size/1e9:.2f} GB",
                )
                timer.record_metric(
                    # On-disk bytes. Compressed manifests are ~3.5x smaller, so
                    # label them rather than let them read as a 70% drop against
                    # older builds' plain figures.
                    "zim-pack: manifest size (zstd)" if self._zstd
                    else "zim-pack: manifest size",
                    f"{manifest_size/1e6:.0f}", "MB",
                )
                timer.record_metric(
                    "zim-pack: output ZIM size", f"{out_size/1e6:.0f}", "MB",
                )
                if _peak_kb[0]:
                    timer.record_metric("zim-pack: process peak RSS", f"{_peak_kb[0] * 1024 / 1e6:.1f}", "MB")
            except Exception:
                pass

    @staticmethod
    def _reported_peak_kb(stats_path: Path) -> int | None:
        """The packer's own peak RSS (kB) from its stats file, if it wrote one."""
        try:
            value = json.loads(stats_path.read_text())["peak_rss_bytes"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if type(value) is not int or value <= 0:
            return None
        return value // 1024


def iter_records(manifest_path: str) -> Iterable[dict[str, Any]]:
    """Read a manifest back as an iterator of records — for tests and
    diff tools. Accepts plain or zstd-compressed manifests."""
    from streetzim.pack import iter_manifest
    for _, record in iter_manifest(Path(manifest_path)):
        yield record


if __name__ == "__main__":
    # Tiny self-test: write a one-item manifest and run the packer.
    if len(sys.argv) != 2:
        print("usage: manifest_writer.py <output.zim>", file=sys.stderr)
        sys.exit(2)
    out = sys.argv[1]

    class _FakeItem:
        def __init__(self, path: str, title: str, mimetype: str, data: bytes,
                     is_front: bool = False, compress: bool = True) -> None:
            self._path = path
            self._title = title
            self._mimetype = mimetype
            self._data = data
            self._file_path = None
            self._is_front = is_front
            self._compress = compress

    c = ManifestCreator(out, verbose=True)
    c.config_indexing(False, "en")
    c.config_clustersize(2 * 1024 * 1024)
    c.set_mainpath("index.html")
    with c:
        c.add_metadata("Title", "manifest_writer self-test")
        c.add_metadata("Description", "smoke")
        c.add_metadata("Language", "eng")
        c.add_metadata("Date", time.strftime("%Y-%m-%d"))
        c.add_metadata("Creator", "streetzim")
        c.add_metadata("Publisher", "streetzim")
        c.add_metadata("Name", "selftest")
        c.add_metadata("Tags", "test")
        c.add_metadata("Flavour", "test")
        c.add_metadata("Scraper", "streetzim/0.1")
        c.add_item(
            _FakeItem("index.html", "Home", "text/html", b"<h1>hi</h1>", is_front=True)
        )
    print(f"wrote {out}")
