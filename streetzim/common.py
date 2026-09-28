"""Shared plumbing for the builder (create_osm_zim.py and streetzim/*).

The phase-timing ``print`` wrapper and PHASE_TIMER, repo paths, download
URLs, download_file, parse_bbox and a few shared constants. Moved verbatim
out of create_osm_zim.py, which re-exports every name. Modules that print
build progress import ``print`` from here so lines flush immediately and
"[N/total] Title..." headers feed PHASE_TIMER.

SCRIPT_DIR / REPO_ROOT are the repository root (the directory holding
create_osm_zim.py), not this package.
"""
import os
import re
import subprocess
import urllib.request
from pathlib import Path


# Wrap print to auto-flush step/progress lines so monitoring never sees stale output.
# Also doubles as a phase-timer hook: lines that look like a phase header
# (``"[N/total] Title..."``) trigger PHASE_TIMER.start so we can emit a
# clean per-phase wall-clock summary at end-of-run without rewriting every
# phase header in the script.
_builtin_print = print


class _PhaseTimer:
    """Tracks (start, end) wall-clock for each numbered phase the
    script announces, plus the overall build window. ``start(name)``
    closes any previous open phase, ``stop()`` finalises the last
    one, and ``summary()`` prints a Markdown-style table — printed
    automatically at end-of-main and also written into the build log
    so post-mortems don't need to re-time anything.
    """

    def __init__(self) -> None:
        import time as _t
        self._t = _t
        self._t0 = _t.time()
        self._cur: tuple[str, float] | None = None
        self._records: list[tuple[str, float, float]] = []
        # Sub-phase + metric records, used to surface fine-grained
        # timing on the parts we're actively optimizing (Xapian build,
        # ZIM-pack subprocess, etc.) without disturbing top-level
        # phase boundaries. Each entry: (parent, name, duration_s, note).
        self._subphases: list[tuple[str, str, float, str]] = []
        # (parent, name, value_str, unit)
        self._metrics: list[tuple[str, str, str, str]] = []

    @property
    def t0(self) -> float:
        return self._t0

    @property
    def current(self) -> str:
        return self._cur[0] if self._cur else "<no phase>"

    def start(self, name: str) -> None:
        if self._cur is not None:
            self._close()
        now = self._t.time()
        self._cur = (name, now)

    def stop(self) -> None:
        self._close()

    def _close(self) -> None:
        if self._cur is None:
            return
        name, t_start = self._cur
        t_end = self._t.time()
        self._records.append((name, t_start, t_end))
        self._cur = None

    def record_subphase(self, name: str, duration_s: float, note: str = "") -> None:
        """Record a measured sub-step under the currently-running phase.
        Called from helpers (xapianbuilder, streetzim-pack invocation,
        per-pass loops) to attribute time to the things we're tuning."""
        self._subphases.append((self.current, name, float(duration_s), note))

    def subphase(self, name: str):
        """Context manager — wrap a block of code to measure its
        wall-clock and attribute it under the currently-running phase.

        Usage:
            with PHASE_TIMER.subphase("zim-pack: vector tiles") as sp:
                # ... do work
                sp.set_note(f"{n:,} tiles")
        """
        outer = self
        class _Ctx:
            def __init__(self):
                self._t0 = None
                self.note = ""
            def __enter__(self):
                import time as _t
                self._t0 = _t.time()
                return self
            def __exit__(self, exc_type, exc, tb):
                import time as _t
                elapsed = _t.time() - self._t0
                outer.record_subphase(name, elapsed, note=self.note)
                return False
            def set_note(self, note: str) -> None:
                self.note = str(note)
        return _Ctx()

    def record_metric(self, name: str, value: str, unit: str = "") -> None:
        """Record a non-time measurement (e.g. final glass-DB size,
        record count, peak bytes). Surfaced in the summary table."""
        self._metrics.append((self.current, name, str(value), unit))

    def summary(self) -> str:
        self._close()
        if not self._records:
            return ""
        end = max(t for _, _, t in self._records)
        total = end - self._t0
        rows = [("Phase", "Started", "Duration", "% of run")]
        for name, t_start, t_end in self._records:
            ts = self._t.strftime("%H:%M:%S", self._t.localtime(t_start))
            dur = t_end - t_start
            pct = 100 * dur / total if total > 0 else 0
            rows.append((name, ts, _fmt_phase_dur(dur), f"{pct:.1f}%"))
        rows.append(("TOTAL", self._t.strftime("%H:%M:%S", self._t.localtime(self._t0)),
                     _fmt_phase_dur(total), "100.0%"))
        widths = [max(len(r[i]) for r in rows) for i in range(4)]
        sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
        lines = [sep, _phase_row(rows[0], widths), sep]
        for r in rows[1:-1]:
            lines.append(_phase_row(r, widths))
        lines.append(sep)
        lines.append(_phase_row(rows[-1], widths))
        lines.append(sep)

        if self._subphases:
            lines.append("")
            lines.append("Sub-phase timing (optimization targets):")
            sub_rows = [("Parent phase", "Sub-phase", "Duration", "Note")]
            for parent, name, dur, note in self._subphases:
                sub_rows.append((parent, name, _fmt_phase_dur(dur), note))
            sw = [max(len(r[i]) for r in sub_rows) for i in range(4)]
            ssep = "+" + "+".join("-" * (w + 2) for w in sw) + "+"
            lines.append(ssep)
            lines.append("| " + " | ".join(f"{sub_rows[0][i]:<{sw[i]}}" for i in range(4)) + " |")
            lines.append(ssep)
            for r in sub_rows[1:]:
                lines.append("| " + " | ".join(f"{r[i]:<{sw[i]}}" for i in range(4)) + " |")
            lines.append(ssep)

        if self._metrics:
            lines.append("")
            lines.append("Build metrics:")
            mrows = [("Parent phase", "Metric", "Value", "Unit")]
            for parent, name, val, unit in self._metrics:
                mrows.append((parent, name, val, unit))
            mw = [max(len(r[i]) for r in mrows) for i in range(4)]
            msep = "+" + "+".join("-" * (w + 2) for w in mw) + "+"
            lines.append(msep)
            lines.append("| " + " | ".join(f"{mrows[0][i]:<{mw[i]}}" for i in range(4)) + " |")
            lines.append(msep)
            for r in mrows[1:]:
                lines.append("| " + " | ".join(f"{r[i]:<{mw[i]}}" for i in range(4)) + " |")
            lines.append(msep)

        return "\n".join(lines)


def _fmt_phase_dur(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _phase_row(cells, widths):
    return "| " + " | ".join(f"{cells[i]:<{widths[i]}}" for i in range(4)) + " |"


PHASE_TIMER = _PhaseTimer()


# Phase headers in this script use the pattern ``[<n>/<total>] Title``.
# Detecting them in the print wrapper means we don't have to thread a
# timer object through every helper call site.
import re as _re_phase

# Only http(s) URLs may become hrefs in detail pages — index data is not
# trusted (a javascript: value would run on tap). Same rule as places.html.
# Module-level so search_detail_html doesn't recompile it per record.
_HTTP_OK_RE = re.compile(r"^https?://", re.I)
_PHASE_RE = _re_phase.compile(r"^\s*\[(\d+)/(\d+)\]\s+(.+?)(\.{3,})?\s*$")


def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    if args and isinstance(args[0], str):
        first = args[0]
        m = _PHASE_RE.match(first)
        if m:
            phase_name = f"[{m.group(1)}/{m.group(2)}] {m.group(3).strip()}"
            PHASE_TIMER.start(phase_name)
            # Add a wall-clock prefix so live monitors can see when each
            # phase started without parsing the eventual summary table.
            import time as _t
            ts = _t.strftime("%H:%M:%S", _t.localtime())
            args = (f"[{ts}] {first}", *args[1:])
    _builtin_print(*args, **kwargs)


SCRIPT_DIR = Path(__file__).parent.parent.resolve()
# Where the heavy download caches live (satellite, DEM/terrain, Wikidata,
# Wikipedia articles): $STREETZIM_CACHE_DIR, else the repo root as always.
# The Docker image points it at the mounted /output volume so the caches
# survive `docker run --rm`.
CACHE_DIR = Path(os.environ.get("STREETZIM_CACHE_DIR") or SCRIPT_DIR)
RESOURCES_DIR = SCRIPT_DIR / "resources"
TILEMAKER_CONFIG = RESOURCES_DIR / "tilemaker" / "config-openmaptiles.json"
TILEMAKER_PROCESS = RESOURCES_DIR / "tilemaker" / "process-openmaptiles.lua"
VIEWER_DIR = RESOURCES_DIR / "viewer"
# Same as create_osm_zim.py's Path(__file__).resolve().parent.
REPO_ROOT = Path(__file__).resolve().parent.parent


def log_viewer_freshness():
    """Print viewer-HTML fingerprints at the top of every build.

    Reasoning: on 2026-04-22 I lost ~hours of builds because a git-
    worktree's `resources/viewer/index.html` was 2h stale relative to
    the main tree. Every ZIM built from that worktree baked the old
    viewer (no `ws` website rendering, stale Route-button code paths,
    etc.). Nothing warned about it until a user downloaded a ZIM and
    noticed the regression.
    Now every build logs the viewer files' size + mtime + first-512-
    byte SHA-1 prefix + the most recent git commit that touched them.
    If the file is older than the commit or missing expected strings,
    a loud WARNING prints so future-me catches it before packaging.
    """
    import hashlib
    import datetime as _dt
    print("  --- viewer HTML fingerprint ---")
    expected_markers = {
        "index.html": ["enrich.ws", "item.ws", "places-link"],
        "places.html": ["Search near", "near-input"],
    }
    worst_age_mtime = None
    warned = False
    for name in ("index.html", "places.html"):
        p = VIEWER_DIR / name
        if not p.exists():
            print(f"    {name}: MISSING at {p}")
            warned = True
            continue
        st = p.stat()
        mtime = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        sha = hashlib.sha1(p.read_bytes()[:512]).hexdigest()[:12]
        try:
            gitlog = subprocess.run(
                ["git", "-C", str(SCRIPT_DIR), "log", "-1",
                 "--format=%ai  %h  %s", "--", f"resources/viewer/{name}"],
                capture_output=True, text=True, timeout=5)
            last_commit = (gitlog.stdout or "").strip() or "(no git)"
        except Exception:
            last_commit = "(git unavailable)"
        print(f"    {name}: {st.st_size:>8d} B  mtime={mtime}  sha1={sha}")
        print(f"      last commit: {last_commit}")
        # Marker check — catches "somebody renamed the field, file on
        # disk still has the old name" regressions before ZIM packaging.
        body = p.read_text(errors="replace")
        missing = [m for m in expected_markers[name] if m not in body]
        if missing:
            print(f"    ⚠️  {name}: MISSING EXPECTED STRINGS {missing} — "
                  "viewer is probably stale. Packaging anyway, but the "
                  "resulting ZIM will miss features.")
            warned = True
    # index.html is built from resources/viewer/src/index/ (tools/build_viewer.py);
    # a part edited without rebuilding would silently ship the old viewer.
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "_build_viewer", SCRIPT_DIR / "tools" / "build_viewer.py")
        _bv = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_bv)
        if _bv.build() != (VIEWER_DIR / "index.html").read_bytes():
            print("    ⚠️  index.html does not match resources/viewer/src/index/ — "
                  "run: python tools/build_viewer.py (packaging the OLD index.html)")
            warned = True
    except (Exception, SystemExit) as _e:
        print(f"    (viewer parts check skipped: {_e})")
    if not warned:
        print("    viewer freshness OK")
    print()

# Geofabrik base URL for downloading OSM extracts
GEOFABRIK_BASE = "https://download.geofabrik.de"

# Sentinel-2 Cloudless satellite tile service (EOX, CC BY-NC-SA 4.0 for 2021 vintage)
SATELLITE_TILE_URL = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/default/g/{z}/{y}/{x}.jpg"

# Copernicus GLO-30 DEM tile URL (public S3, no auth)
COPERNICUS_DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)

# Copernicus GLO-90 DEM fallback — broader coverage than GLO-30 (includes
# Georgia, Armenia, Azerbaijan and other restricted-region countries).
# Used when GLO-30 returns 404. 90m resolution vs 30m but fine for hillshade.
COPERNICUS_DEM_URL_GLO90 = (
    "https://copernicus-dem-90m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)

# MapLibre GL JS version to bundle
MAPLIBRE_VERSION = "5.23.0"
MAPLIBRE_CDN = f"https://unpkg.com/maplibre-gl@{MAPLIBRE_VERSION}/dist"


def download_file(url, dest, desc=None):
    """Download a file with progress indication."""
    desc = desc or os.path.basename(dest)
    print(f"  Downloading {desc}...")
    print(f"    URL: {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "create_osm_zim/1.0"})
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        mb = downloaded / (1024 * 1024)
                        print(f"\r    {mb:.1f} MB ({pct}%)", end="", flush=True)
            print()
    except Exception as e:
        print(f"\n    Error downloading: {e}")
        raise


# Decimal places kept for search-record coordinates. 5 dp is ~1.1 m at the
# equator and less nearer the poles -- finer than any consumer GPS fix, and
# finer than the OSM geometry most of these come from.
_SEARCH_COORD_DP = int(os.environ.get("SEARCH_COORD_DP", "5") or 5)


def parse_bbox(bbox_str):
    """Parse a bbox string 'minlon,minlat,maxlon,maxlat' into a list of floats."""
    parts = [float(x.strip()) for x in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Invalid bbox format: {bbox_str}. Expected: minlon,minlat,maxlon,maxlat")
    return parts
