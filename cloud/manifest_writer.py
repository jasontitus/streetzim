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
(used by repackage_zim.py).

Body-encoding strategy:

  - Inline UTF-8 (`content`) for text mimes ≤ 256 KiB. Cheapest path;
    no encoding cost.
  - Inline base64 (`body_b64`) for everything else that fits in
    memory. The 33 % size tax buys us no per-item `open()` syscalls
    at consume time — at Japan-scale (3.2 M items) the previous
    per-body file-stage path spent ~320 s in `open()` syscalls alone.
  - On-disk path (`file`) is reserved for streaming-mode items
    (>= 64 MiB; zimru's chunked-encode path keeps RSS bounded by
    chunk size, not file size). Anything smaller goes inline.

Per-item compress is supported: zimru routes items into separate
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

# Bodies at or above this size are written through zimru's streaming
# path (Item with `streaming: true` + `file` reference) rather than
# base64-inlined. zimru's chunked encoder keeps peak RSS to one chunk
# (~4 MiB) regardless of file size; base64-inlining a 1 GB routing
# chunk would briefly hold ~1.4 GB of UTF-8 string + the source bytes
# in Python's memory. 64 MiB is the same threshold add_item used
# pre-base64 to switch on `streaming`, so the break-even point is
# unchanged.
_STREAMING_THRESHOLD = 64 * 1024 * 1024


def _encode_body_b64(data: bytes) -> str:
    """Encode binary body for inline JSONL transport. Pure ASCII out,
    so JSON needs no escape characters and the 1.33× inflation is
    the only cost. Decode happens once on the Rust side per record."""
    return base64.b64encode(data).decode("ascii")


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
        # Stage dir holds just the manifest.jsonl now — bodies are
        # base64-inlined. Kept as a directory rather than a bare
        # file so existing tooling/inspection scripts that look at
        # `<output>.pack-stage/` continue to find the manifest.
        self._stage_dir = Path(self._output_path + ".pack-stage")
        self._stage_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._stage_dir / "manifest.jsonl"
        self._mf = self._manifest_path.open("w", encoding="utf-8")
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
        # Illustration PNGs are tiny (typical 48 px favicon ~ a few KB)
        # — always inline.
        self._write_record(
            {
                "kind": "illustration",
                "size": int(side),
                "body_b64": _encode_body_b64(bytes(png_bytes)),
            }
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
            rec["body_b64"] = _encode_body_b64(bytes(value))
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
            if size >= _STREAMING_THRESHOLD:
                # Multi-MiB-to-multi-GB items (routing graph chunks,
                # large PBF blobs) — let zimru read them in its own
                # 4 MiB chunks at pack time so peak RSS stays bounded.
                rec["file"] = str(file_path)
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
