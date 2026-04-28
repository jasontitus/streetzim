"""ManifestCreator — duck-compatible drop-in for libzim's Creator that
writes a JSONL manifest and shells out to `streetzim-pack` (Rust binary
backed by zimru) to emit the actual ZIM.

The libzim API surface used by `create_osm_zim.py` is small:

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
(used by repackage_zim.py). MapItem-shaped objects are introspected
via attributes set in the existing inner class:

    _path, _title, _mimetype, _is_front, _compress, _data, _file_path

For in-memory bytes (`_data`), we write them to a per-build staging
directory and reference them from the manifest by absolute path —
keeping the Rust side uniform (every item is file-backed in the
emitted JSONL).

Per-item compress is supported: zimru routes items into separate
clusters by effective compression, so a single ZIM can mix compressed
and raw clusters (use case: streetzim's >500 MB routing chunks that
need raw clusters for PWA fzstd compatibility while tiles/HTML stay
zstd-compressed).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


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


# Resolved once per process. Override with STREETZIM_PACK_BIN if the
# binary lives somewhere unusual (CI runners, vendored release builds).
def _resolve_pack_binary() -> str:
    explicit = os.environ.get("STREETZIM_PACK_BIN")
    if explicit:
        return explicit
    here = Path(__file__).resolve().parent
    repo = here.parent  # streetzim/
    for build in ("release", "debug"):
        cand = repo / "rust" / "streetzim-pack" / "target" / build / "streetzim-pack"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise RuntimeError(
        "streetzim-pack binary not found. Build it with "
        "`cd rust/streetzim-pack && cargo build --release` "
        "or set STREETZIM_PACK_BIN to its absolute path."
    )


class ManifestCreator:
    """Captures every libzim Creator call as a JSONL record. Spawns
    `streetzim-pack` at __exit__."""

    def __init__(
        self,
        output_path: str,
        *,
        compression: str = "zstd",
        compression_level: int | None = None,
        cluster_strategy: str = "by_mime",
        max_in_flight_bytes: int | None = None,
        keep_stage: bool = False,
        verbose: bool = False,
    ) -> None:
        self._output_path = str(output_path)
        self._stage_dir = Path(self._output_path + ".pack-stage")
        self._stage_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._stage_dir / "manifest.jsonl"
        self._mf = self._manifest_path.open("w", encoding="utf-8")
        self._n_inline = 0
        self._closed = False
        self._keep_stage = keep_stage
        self._verbose = verbose
        # Initial config record. Cluster size + main_path are filled in
        # by config_clustersize / set_mainpath; we buffer the dict and
        # write it at __enter__ so callers can configure freely first.
        self._config: dict[str, Any] = {
            "kind": "config",
            "compression": compression,
            "cluster_strategy": cluster_strategy,
        }
        if compression_level is not None:
            self._config["compression_level"] = compression_level
        if max_in_flight_bytes is not None:
            self._config["max_in_flight_bytes"] = max_in_flight_bytes

    # ---- libzim Creator config surface (mostly no-ops) ---------------

    def config_indexing(self, enabled: bool, lang: str) -> None:
        # zimru does not yet implement xapian fulltext indexing.
        # The flag is recorded for future use; for now the index is
        # absent and the PWA falls back to its in-ZIM JSON search.
        self._config["_indexing_requested"] = bool(enabled)
        self._config["_indexing_lang"] = lang

    def config_clustersize(self, bytes_size: int) -> None:
        self._config["cluster_size_target"] = int(bytes_size)

    def config_nbworkers(self, n: int) -> None:
        # zimru picks workers from rayon::current_num_threads(); the
        # value is recorded for parity but ignored by the packer.
        self._config["_nbworkers_requested"] = int(n)

    def set_mainpath(self, path: str) -> None:
        self._config["main_path"] = str(path)

    # ---- context manager: writes config + opens for items ------------

    def __enter__(self) -> "ManifestCreator":
        self._write_record(self._config)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._closed:
            return False
        self._closed = True
        self._mf.close()
        if exc_type is not None:
            # Bubble up the original error; leave the stage dir for
            # post-mortem unless the caller asked us not to.
            return False
        self._run_packer()
        if not self._keep_stage:
            shutil.rmtree(self._stage_dir, ignore_errors=True)
        return False

    # ---- libzim Creator item surface ---------------------------------

    def add_metadata(self, name: str, value: Any) -> None:
        self._write_record(self._metadata_record(name, value, mimetype=None))

    def add_metadata_with_mimetype(self, name: str, mimetype: str, value: Any) -> None:
        self._write_record(self._metadata_record(name, value, mimetype=mimetype))

    def add_illustration(self, side: int, png_bytes: bytes) -> None:
        path = self._stage_inline_bytes(f"illustration-{side}.png", png_bytes)
        self._write_record(
            {"kind": "illustration", "size": int(side), "file": str(path)}
        )

    def add_redirection(self, path: str, title: str, target: str) -> None:
        self._write_record(
            {"kind": "redirect", "path": str(path), "title": str(title or ""), "target": str(target)}
        )

    def add_item(self, item: Any) -> None:
        rec = self._item_record(item)
        self._write_record(rec)

    # ---- helpers -----------------------------------------------------

    def _metadata_record(
        self, name: str, value: Any, mimetype: str | None
    ) -> dict[str, Any]:
        rec: dict[str, Any] = {"kind": "metadata", "name": str(name)}
        if mimetype is not None:
            rec["mimetype"] = str(mimetype)
        if isinstance(value, (bytes, bytearray)):
            stage = self._stage_inline_bytes(f"meta-{name}", bytes(value))
            rec["file"] = str(stage)
        else:
            rec["value"] = value if isinstance(value, str) else str(value)
        return rec

    def _item_record(self, item: Any) -> dict[str, Any]:
        path = item._path  # noqa: SLF001 — duck-typed MapItem
        title = getattr(item, "_title", "") or ""
        mime = item._mimetype  # noqa: SLF001
        is_front = bool(getattr(item, "_is_front", False))
        compress = bool(getattr(item, "_compress", True))

        rec: dict[str, Any] = {
            "kind": "item",
            "path": str(path),
            "title": str(title),
            "mime": str(mime),
        }
        if is_front:
            rec["front"] = True
        if compress is False:
            # Per-item override — zimru routes this item to its own
            # uncompressed cluster regardless of the build's default.
            rec["compress"] = False

        file_path = getattr(item, "_file_path", None)
        if file_path:
            size = os.path.getsize(file_path)
            rec["file"] = str(file_path)
            # Stream items > ~64 MiB through zimru's chunked path so
            # peak RSS doesn't track item size.
            if size >= 64 * 1024 * 1024:
                rec["streaming"] = True
                rec["size"] = size
        else:
            data = getattr(item, "_data", None)
            if data is None:
                raise ValueError(
                    f"add_item({path!r}): item has neither _file_path nor _data"
                )
            data = bytes(data)
            # Inline small text-ish items directly in the manifest as a
            # `content` string. Saves the per-entry file-stage syscall
            # (open/write/close/stat/open-again-from-Rust/read/close)
            # which on a 17 K-entry build like silicon-valley costs
            # ~100 s of wall time. Threshold of 256 KiB keeps the
            # JSONL line-length sane and matches the size profile of
            # chip-bucket / search-data sub-chunks (most are < 50 KB).
            # Binary items (PBF, AVIF, WebP, PNG) are NOT inlined —
            # they're not valid UTF-8, and JSON-encoding the bytes
            # would balloon the manifest.
            if (len(data) <= _INLINE_TEXT_LIMIT
                    and mime in _INLINE_TEXT_MIMES):
                try:
                    rec["content"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    # Mime says text but bytes aren't UTF-8 — fall
                    # back to file-stage rather than risk lossy
                    # encoding.
                    stage = self._stage_inline_bytes(
                        self._safe_stage_name(path), data)
                    rec["file"] = str(stage)
            else:
                stage = self._stage_inline_bytes(
                    self._safe_stage_name(path), data)
                rec["file"] = str(stage)
        return rec

    def _safe_stage_name(self, path: str) -> str:
        return path.replace("/", "__").replace("\\", "__")

    def _stage_inline_bytes(self, hint: str, data: bytes) -> Path:
        # Unique-but-deterministic name based on a 4-digit counter +
        # caller hint. Keeps stage dir browsable for debugging.
        self._n_inline += 1
        name = f"{self._n_inline:06d}-{hint}"
        # Guard against huge or path-busting names.
        name = name[:200]
        out = self._stage_dir / name
        out.write_bytes(data)
        return out

    def _write_record(self, rec: dict[str, Any]) -> None:
        self._mf.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        self._mf.write("\n")

    def _run_packer(self) -> None:
        binary = _resolve_pack_binary()
        cmd = [binary, str(self._manifest_path), self._output_path]
        if self._verbose:
            cmd.append("--verbose")
            print(f"  streetzim-pack: {' '.join(cmd)}", flush=True)
        started = time.time()
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"streetzim-pack failed (exit {e.returncode}). "
                f"Manifest preserved at {self._manifest_path} for inspection."
            ) from e
        if self._verbose:
            print(
                f"  streetzim-pack done in {time.time() - started:.1f}s — {self._output_path}",
                flush=True,
            )


def iter_records(manifest_path: str) -> Iterable[dict[str, Any]]:
    """Read a manifest back as an iterator of records — for tests and
    diff tools."""
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            yield json.loads(line)


if __name__ == "__main__":
    # Tiny self-test: write a one-item manifest and run the packer.
    if len(sys.argv) != 2:
        print("usage: manifest_writer.py <output.zim>", file=sys.stderr)
        sys.exit(2)
    out = sys.argv[1]

    class _FakeItem:
        def __init__(self, path, title, mimetype, data, is_front=False, compress=True):
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
