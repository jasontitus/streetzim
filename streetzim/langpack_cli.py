"""`streetzim-langpack`: a StreetZim region's language packs, search packs and
recipes, built the way a Zimfarm task builds them (docs/zimfarm-langpack.md).

    streetzim-langpack --mode pack --base-url URL --language de [--wikipedia-zim-url URL]
    streetzim-langpack --mode menu --base-url URL --pack-url URL [--pack-url URL ...]

(`streetzim-langpack pack ...` and `streetzim-langpack menu ...` mean the
same: Zimfarm runs one command with flags, so the mode is a flag.)

pack: one Wikipedia pack (~w/<xx>/, streetzim/wiki_pack.py) for one base and
  one language, read from that language's Kiwix Wikipedia ZIM (default: the
  newest wikipedia_<xx>_all_maxi in Kiwix's catalog, checked against the
  SHA-256 of its .meta4), its network inputs fetched first
  (tools/langpack/prefetch.py: the SQL dumps, Wikidata text), then built
  offline under the production time limit, accepted by
  tools/langpack/builder_ok.py and gated by pack_gate.py (the official
  zimcheck). Only a pack that passes reaches --output.
menu: a base and its packs (each gated again here) -> the menu's languages
  (tools/langpack/region_meta.py, "has a pack" for "has a Wikipedia ZIM"),
  its search packs (search_packs.py), each version's recipe (recipe2.py
  menu), each version merged and checked (recipe == merge byte for byte,
  verify_merge.py, zimcheck -A, Kiwix search over pack-only titles:
  search_gate.py), the recipes and their index (make_site.py), checked
  again as a whole (site_check.py --no-archive). Published: the search
  packs a version uses (ZIMs), and recipes/ (index.json, <L>.json, the
  verdicts and pack gates).

Every ZIM it publishes is named by its content (the first 12 hex digits of
its SHA-256 end the name), and a name already in --output is never given
other bytes: a recipe made earlier keeps building from the files it names.

The browser checks of StreetZim's own pipeline (the viewer, the interface,
the smoke test, kiwix-serve) need Chromium and kiwix-serve, which the image
does not have; a version is published here on the checks above
(docs/zimfarm-langpack.md, "What is checked"). Needs a checkout or the
Docker image: tools/langpack/ is not in the wheel.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from streetzim.paths import REPO_ROOT

PROG = "streetzim-langpack"
USER_AGENT = "streetzim-langpack (https://github.com/jasontitus/streetzim)"
MODES = ("pack", "menu")
IMAGE_MODES = ("none", "lead", "all")
DEFAULT_IMAGES = "lead"                 # what StreetZim's own packs carry
LANGPACK = REPO_ROOT / "tools" / "langpack"
BUILD_WIKI_PACK = REPO_ROOT / "tools" / "build_wiki_pack.py"
# A tool run from elsewhere than LANGPACK (tests put stand-ins here).
TOOL_OVERRIDES: dict[str, Path] = {}
OPDS_ENTRIES = "https://library.kiwix.org/catalog/v2/entries"
KIWIX_WIKIPEDIA = "https://download.kiwix.org/zim/wikipedia/"
KIWIX_ORIGIN = "https://download.kiwix.org"
KIWIX_HOSTS = ("download.kiwix.org", "lb.download.kiwix.org")
GET_LIMIT = 4 << 20                     # bytes: a .meta4, an OPDS feed, a listing
# Production's limits: the pack builder 3 h per 3 GB of base, at least 3 h;
# pack_gate.py 1 h; each merge, verify, zimcheck and search check 2 h.
BUILD_HOURS_PER_3GB = 3
GATE_TIMEOUT = 3600
STEP_TIMEOUT = 7200
LANG_RE = re.compile(r"^[a-z]{2,3}$")
LANGS_PATTERN = r"^[a-z]{2,3}(,[a-z]{2,3})*$"
REGION_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
ZIM_MAGIC = b"ZIM\x04"
HASH_HEX = 12                           # hex digits of the SHA-256 in a published name
# osm_<region>_wiki-<xx>_..., as --mode pack names its packs
PACK_NAME = re.compile(r"osm_([a-z0-9][a-z0-9-]*)_wiki-([a-z]{2,3})_[A-Za-z0-9._-]+\.zim")
# a base: Zimfarm's osm_<lang>_<region>_<YYYY-MM>.zim, or StreetZim's osm-<region>-<YYYY-MM-DD>.zim
BASE_NAMES = (re.compile(r"osm_[a-z]{2,3}_([a-z0-9][a-z0-9-]*)_\d{4}-\d{2}(?:-\d{2})?\.zim"),
              re.compile(r"osm-([a-z0-9][a-z0-9-]*?)-\d{4}-\d{2}-\d{2}\.zim"))
# a search pack's size, for the disk check before they exist: a share of
# the base (its pages are indexed again: Switzerland 0.21 of 2.28 GB) and
# of the version's packs
SEARCH_BASE_SHARE = 0.15
SEARCH_PACKS_SHARE = 0.5
DISK_MARGIN = 1 << 30

# Extra keys for offliner-definition-langpack.json, by flag
# (tools/offliner_definition_langpack.py). offliner False: not on Zimfarm.
ZIMFARM: dict[str, dict[str, Any]] = {
    "mode": {"title": "Mode", "required": True},
    "base_url": {"title": "Base ZIM URL", "type": "url"},
    "region": {"title": "Region id", "pattern": REGION_PATTERN},
    "jobs": {"title": "Processes", "min": 1},
    "language": {"title": "Wikipedia language", "pattern": LANG_RE.pattern},
    "wikipedia_zim_url": {"title": "Wikipedia ZIM URL", "type": "url"},
    "images": {"title": "Wikipedia images"},
    "no_wikidata_text": {"title": "No Wikidata text"},
    "pack_url": {"title": "Pack URLs", "type": "list-of-url"},
    "languages": {"title": "Menu languages", "pattern": LANGS_PATTERN},
    "url_template": {"title": "Part URL template"},
    "output": {"pattern": r"^/output$"},
    "stats_filename": {"pattern": r"^/output/task_progress\.json$"},
    "overwrite": {"offliner": False},
    "tmp": {"offliner": False},
    "dl": {"offliner": False},
    "zimcheck": {"offliner": False},
    "keep_temp": {"offliner": False},
}
# Zimfarm's form shows (and keeps) these only with their mode (a string-enum
# choice's "dependents": the other mode's are cleared when a recipe is saved).
MODE_FLAGS = {"pack": ["language", "wikipedia_zim_url", "images", "no_wikidata_text"],
              "menu": ["pack_url", "languages", "url_template"]}
MODE_TITLES = {"pack": "Pack: one Wikipedia pack", "menu": "Menu: search packs and recipes"}


class StepError(Exception):
    """A step failed; the message says which and why."""


def positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {n}")
    return n


def version() -> str:
    from streetzim.__about__ import __version__
    return __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="A StreetZim region's Wikipedia packs (--mode pack), or its search "
                    "packs and recipes (--mode menu), as Zimfarm runs them "
                    "(docs/zimfarm-langpack.md).")
    p.add_argument("--mode", required=True, choices=list(MODES),
                   help="pack: one Wikipedia pack for one base and language. menu: the "
                        "base and all its packs -> the search packs, every language "
                        "version merged and checked, and the recipes")
    p.add_argument("--base-url", required=True,
                   help="The region's multilingual base ZIM (streetzim --multilingual), "
                        "the very file the packs and recipes are for. A URL; on the "
                        "command line also a local file, read where it is")
    p.add_argument("--region",
                   help="Region id the parts are named after (osm_<region>_wiki-<xx>). "
                        "Default: from the packs' names, else the base's Name "
                        "(osm_mul_monaco: monaco); it must agree with both")
    p.add_argument("--jobs", type=positive_int, default=4,
                   help="Processes: articles read and cleaned at a time (pack), search "
                        "packs built at a time (menu). Default: 4")
    pk = p.add_argument_group("pack mode")
    pk.add_argument("--language",
                    help="Pack: the Wikipedia language, as Kiwix names its Wikipedia ZIMs "
                         "(de, fr, ja, ...)")
    pk.add_argument("--wikipedia-zim-url",
                    help="Pack: that language's Kiwix Wikipedia ZIM (a full one: every "
                         "article of the region is looked up in it). Default: the newest "
                         "wikipedia_<xx>_all_maxi in Kiwix's catalog (library.kiwix.org), "
                         "else on download.kiwix.org. A URL (download.kiwix.org or a "
                         "mirror, a path with /zim/, no query) is checked against the "
                         "SHA-256 in the .meta4 of the same path on download.kiwix.org, "
                         "and fails without one; on the command line also a local file, "
                         "read where it is")
    pk.add_argument("--images", choices=list(IMAGE_MODES),
                    help="Pack: pictures per article: none, the lead one (default, as "
                         "StreetZim's own packs), or all (up to 12)")
    pk.add_argument("--no-wikidata-text", action="store_true",
                    help="Pack: leave out the place names and descriptions in the "
                         "language from Wikidata (wikidata-text.json)")
    mn = p.add_argument_group("menu mode")
    mn.add_argument("--pack-url", action="append", default=[],
                    help="Menu: a Wikipedia pack of this base (from --mode pack, named "
                         "osm_<region>_wiki-<xx>_...zim); one per language, repeated. "
                         "Each is gated again before use")
    mn.add_argument("--languages",
                    help="Menu: the map languages to offer, comma-separated. Default: "
                         "StreetZim's core set (the 21 label languages and English). "
                         "The base's region languages are always offered; a language "
                         "is offered only with its pack (or in the base)")
    mn.add_argument("--url-template",
                    help="Menu: each part's URL in the recipes, {file} its file name "
                         "(https://download.kiwix.org/zim/maps/{file}). Default: {file}, "
                         "relative to wherever the recipes are published")
    p.add_argument("--output", default=os.environ.get("STREETZIM_OUTPUT", "output"),
                   help="Output folder. Default: ./output")
    p.add_argument("--stats-filename", help="Path to store the progress JSON file to")
    p.add_argument("--overwrite", action="store_true",
                   help="Replace an earlier recipes/ folder (a published ZIM is never "
                        "replaced by other bytes)")
    p.add_argument("--tmp", help="Folder for temporary files, outside --output. Default: "
                                 "a private folder, removed at the end")
    p.add_argument("--dl", help="Folder for downloads (the base, the packs, the Wikipedia "
                                "ZIM), outside --output, reused between runs. Default: "
                                "in the temporary folder, removed at the end")
    p.add_argument("--zimcheck", metavar="BIN",
                   help="The official zimcheck (zim-tools). Default: $PACK_GATE_ZIMCHECK, "
                        "else zimcheck on PATH")
    p.add_argument("--keep-temp", action="store_true",
                   help="Keep the temporary folder (merged files, logs)")
    p.add_argument("--version", action="version", version=f"%(prog)s {version()}")
    return p


def local_name(src: str) -> str:
    """The file name a URL or a path names."""
    if is_url(src):
        return urllib.parse.unquote(urllib.parse.urlsplit(src).path.rstrip("/").rsplit("/", 1)[-1])
    path = urllib.request.url2pathname(urllib.parse.urlsplit(src).path) if src.startswith("file://") else src
    return os.path.basename(path)


def base_name_region(src: str) -> str | None:
    """The region a base's file name gives, when it follows a known naming."""
    name = local_name(src)
    for rx in BASE_NAMES:
        m = rx.fullmatch(name)
        if m:
            return m.group(1)
    return None


def kiwix_url_problem(url: str) -> str | None:
    """Why a Wikipedia ZIM URL cannot be checked against Kiwix's SHA-256."""
    u = urllib.parse.urlsplit(url)
    if u.query or u.fragment:
        return "carries a query or fragment"
    if "/zim/" not in u.path:
        return "has no /zim/ path whose .meta4 download.kiwix.org gives"
    return None


def check_args(args: argparse.Namespace) -> list[str]:
    """What is wrong with the flags for the mode (empty: nothing). Zimfarm
    cannot make a flag required for one mode only, so this does; it also
    finds the region the names agree on (args.region)."""
    errs: list[str] = []
    pack_only = [f for f, v in (("--language", args.language),
                                ("--wikipedia-zim-url", args.wikipedia_zim_url),
                                ("--images", args.images),
                                ("--no-wikidata-text", args.no_wikidata_text)) if v]
    menu_only = [f for f, v in (("--pack-url", args.pack_url), ("--languages", args.languages),
                                ("--url-template", args.url_template)) if v]
    if args.region and not re.fullmatch(REGION_PATTERN, args.region):
        errs.append(f"--region {args.region!r} is not a region id (lower case, digits, -)")
    regions: dict[str, str] = {}               # region -> the name that gives it
    if args.region:
        regions[args.region] = "--region"
    base_region = base_name_region(args.base_url)
    if base_region:
        regions.setdefault(base_region, f"the base {local_name(args.base_url)}")
    if is_url(args.base_url) and urllib.parse.urlsplit(args.base_url).hostname in KIWIX_HOSTS:
        why = kiwix_url_problem(args.base_url)
        if why:
            errs.append(f"--base-url {args.base_url}: {why}")
    if args.mode == "pack":
        if menu_only:
            errs.append(f"{', '.join(menu_only)}: for --mode menu")
        if not args.language:
            errs.append("--mode pack needs --language")
        elif not LANG_RE.fullmatch(args.language):
            errs.append(f"--language {args.language!r} is not a language code (de, fr, ...)")
        else:
            from streetzim.languages import pack_iso639_3
            try:
                pack_iso639_3(args.language)
            except ValueError as e:
                errs.append(str(e))
        if args.wikipedia_zim_url and is_url(args.wikipedia_zim_url):
            why = kiwix_url_problem(args.wikipedia_zim_url)
            if why:
                errs.append(f"--wikipedia-zim-url {args.wikipedia_zim_url}: {why}")
    else:
        if pack_only:
            errs.append(f"{', '.join(pack_only)}: for --mode pack")
        if not args.pack_url:
            errs.append("--mode menu needs at least one --pack-url")
        by_lang: dict[str, str] = {}
        for src in args.pack_url:
            name = local_name(src)
            m = PACK_NAME.fullmatch(name)
            if not m:
                errs.append(f"--pack-url {src}: not a pack name (osm_<region>_wiki-<xx>_...zim)")
                continue
            region, lang = m.groups()
            if lang in by_lang:
                errs.append(f"two packs for {lang}: {by_lang[lang]} and {name} (one --pack-url "
                            "per language)")
            by_lang[lang] = name
            regions.setdefault(region, name)
            if is_url(src) and urllib.parse.urlsplit(src).hostname in KIWIX_HOSTS:
                why = kiwix_url_problem(src)
                if why:
                    errs.append(f"--pack-url {src}: {why}")
        if args.languages and not re.fullmatch(LANGS_PATTERN, args.languages):
            errs.append(f"--languages {args.languages!r} is not comma-separated codes")
        if args.url_template is not None:
            try:
                ok = "{file}" in args.url_template and \
                    args.url_template.format(file="x") == args.url_template.replace("{file}", "x")
            except (KeyError, IndexError, ValueError):
                ok = False
            if not ok:
                errs.append(f"--url-template {args.url_template!r} needs {{file}} and no other braces")
    if len(regions) > 1:
        errs.append("the region ids disagree: " + "; ".join(f"{r} ({w})" for r, w in regions.items()))
    elif regions:
        args.region = next(iter(regions))
    for flag in ("tmp", "dl"):
        folder = getattr(args, flag)
        if folder and Path(os.path.abspath(folder)).is_relative_to(Path(os.path.abspath(args.output))):
            errs.append(f"--{flag} {folder} is inside --output: a ZIM being made there could "
                        "be taken for an output")
    return errs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in MODES:                  # streetzim-langpack pack ...
        argv = [f"--mode={argv[0]}", *argv[1:]]
    parser = build_parser()
    args = parser.parse_args(argv)
    errs = check_args(args)
    if errs:
        parser.error("; ".join(errs))
    args.images = args.images or DEFAULT_IMAGES
    args.url_template = args.url_template or "{file}"
    return args


# ----------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(msg, flush=True)


def _tools() -> None:
    """tools.langpack importable (a checkout, or the image's /app)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def tool(name: str) -> Path:
    if name in TOOL_OVERRIDES:
        return TOOL_OVERRIDES[name]
    return BUILD_WIKI_PACK if name == "build_wiki_pack.py" else LANGPACK / name


class Progress:
    """--stats-filename: {"done", "total"} per step (streetzim.progress)."""

    def __init__(self, path: str | None, total: int) -> None:
        from streetzim.progress import StatsFile
        self.file = StatsFile(Path(path).resolve()) if path else None
        self.total, self.n = total, 0
        if self.file:
            self.file.write(0, total)

    def step(self, title: str) -> None:
        self.n += 1
        log(f"[{self.n}/{self.total}] {title}")
        if self.file:
            self.file.write(self.n - 1, self.total)

    def grow(self, extra: int) -> None:
        self.total += extra

    def finish(self) -> None:
        if self.file:
            self.file.write(self.total, self.total)


_CHILD: list[subprocess.Popen[bytes]] = []


def descendants(pid: int) -> list[int]:
    """Every process under pid, from /proc's parent links (a process may
    have left pid's process group: GNU timeout starts its own)."""
    kids: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for d in names:
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", "rb") as f:
                stat = f.read()
            ppid = int(stat[stat.rindex(b")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    out: list[int] = []
    todo = [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in out:
                out.append(c)
                todo.append(c)
    return out


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_tree(pid: int) -> None:
    """pid and every process under it: stopped first, so that none starts
    another while the tree is read, then killed; and pid's process group."""
    seen: list[int] = []
    for _ in range(3):
        new = [p for p in [pid, *descendants(pid)] if p not in seen]
        if not new:
            break
        for p in new:
            _signal(p, signal.SIGSTOP)
        seen += new
    for p in seen:
        _signal(p, signal.SIGKILL)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_children() -> None:
    for proc in list(_CHILD):
        kill_tree(proc.pid)


def run(cmd: list[str], logfile: Path, *, timeout: float, env: dict[str, str] | None = None,
        echo: bool = True) -> int:
    """cmd in a process group of its own, its output into `logfile` (and on
    stdout when `echo`); at `timeout` (rc 124), or when this command stops,
    it and every process under it are killed, and when it ends its group
    is."""
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "wb") as fh:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
        _CHILD.append(proc)
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            kill_tree(proc.pid)
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            out: IO[bytes] | None = proc.stdout
            assert out is not None
            for line in out:
                fh.write(line)
                fh.flush()                     # the log can be read while it runs
                if echo:
                    sys.stdout.write("    " + line.decode("utf-8", "replace"))
                    sys.stdout.flush()
            rc = proc.wait()
        finally:
            timer.cancel()
            _CHILD.remove(proc)
            if proc.poll() is None:            # stopped mid-run (a signal, an error)
                kill_tree(proc.pid)
                proc.wait()
            try:                               # nothing left in its group outlives it
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return 124 if timed_out.is_set() else rc


def tail(path: Path, n: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def json_lines(path: Path) -> list[dict[str, Any]]:
    """The lines of `path` that are JSON objects."""
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        if line.lstrip().startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)                  # pyright: ignore[reportUnknownArgumentType]
    return out


def last_json(path: Path) -> dict[str, Any]:
    """The last line of `path` that is a JSON object, else {}."""
    rows = json_lines(path)
    return rows[-1] if rows else {}


def py(script: Path, *args: str) -> list[str]:
    return [sys.executable, str(script), *args]


def tool_env(work: Path, zimcheck: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(LANGPACK_WORK=str(work / "lp"), PACK_GATE_ZIMCHECK=zimcheck,
               ZIMCHECK_BIN=zimcheck, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    return env


def zimcheck_version(zc: str) -> str:
    _tools()
    from tools.langpack.gatelib import zimcheck_version as zv
    return zv(zc)


def zimcheck_passed(text: str, rc: int, version: str) -> bool:
    """zim-tools' zimcheck (no other counts) exited 0 and its last "Overall
    Test Status:" line is the Pass line (tools/langpack/gatelib.py)."""
    _tools()
    from tools.langpack.gatelib import zimcheck_log_passed
    return version.startswith("zim-tools ") and zimcheck_log_passed(text, rc)


def verify_summary(text: str) -> tuple[bool, str | None]:
    """verify_merge.py's verdict: its "SUMMARY n/n PASS" line, every check passed."""
    m = re.search(r"^SUMMARY (\d+)/(\d+) PASS$", text, re.M)
    return bool(m and m.group(1) == m.group(2) and int(m.group(2)) > 0), (m.group(0) if m else None)


def is_url(src: str) -> bool:
    return urllib.parse.urlsplit(src).scheme in ("http", "https")


def url_file_name(url: str) -> str:
    """The published file name a URL names (its last path segment)."""
    name = local_name(url)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.zim", name):
        raise StepError(f"{url}: does not end in a ZIM file name")
    return name


def check_zim_head(head: bytes) -> None:
    if head[:4] != ZIM_MAGIC:
        raise ValueError("not a ZIM file")


def _get(url: str, timeout: float = 60, limit: int = GET_LIMIT) -> bytes:
    """A small document (a .meta4, an OPDS feed, a listing), at most `limit` bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(limit + 1)
    if len(body) > limit:
        raise OSError(f"{url}: over {limit:,} bytes")
    return body


def meta4_url(url: str) -> str:
    """The .meta4 of a Kiwix file on download.kiwix.org, from the URL's
    path from /zim/ on (a mirror's or download.kiwix.org's own)."""
    u = urllib.parse.urlsplit(url)
    why = kiwix_url_problem(url)
    if why:
        raise StepError(f"{url} {why}: no SHA-256 to check it against")
    return KIWIX_ORIGIN + u.path[u.path.index("/zim/"):] + ".meta4"


def meta4_sha256(text: str) -> str | None:
    """The whole file's SHA-256 in a Metalink 4 (.meta4) document."""
    m = re.search(r'<hash type="sha-256">([0-9a-f]{64})</hash>', text)
    return m.group(1) if m else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 24):
            h.update(b)
    return h.hexdigest()


def fetch_input(src: str, dest_dir: Path, *, verify: bool = False, reuse: bool = False,
                get: Callable[[str], bytes] = _get) -> Path:
    """A URL downloaded into dest_dir (resumed; zimscraperlib's session where
    installed: streetzim/download.py), or a local file (a path or file://
    URL) used where it is. Either must be a ZIM. `verify` (or a Kiwix host):
    the download is checked against the SHA-256 in download.kiwix.org's
    .meta4 for its path, and fails without one. A file already in dest_dir
    is used only with `reuse` (an explicit --dl)."""
    if is_url(src):
        from streetzim import download
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / url_file_name(src)
        kiwix = verify or urllib.parse.urlsplit(src).hostname in KIWIX_HOSTS
        if not reuse:
            for stale in (dest, dest.with_name(dest.name + ".sha256-ok")):
                stale.unlink(missing_ok=True)
        try:
            download.fetch_resumable(src, dest, user_agent=USER_AGENT, check_head=check_zim_head,
                                     trust_preseeded=False)
        except (OSError, ValueError) as e:
            raise StepError(f"{src}: {e}") from e
        if kiwix:
            verify_kiwix(src, dest, get)
        return dest
    path = Path(urllib.request.url2pathname(urllib.parse.urlsplit(src).path)
                if src.startswith("file://") else src)
    if not path.is_file():
        raise StepError(f"{src}: no such file")
    with open(path, "rb") as f:
        try:
            check_zim_head(f.read(4))
        except ValueError as e:
            raise StepError(f"{src}: {e}") from e
    return path.resolve()


def verify_kiwix(url: str, dest: Path, get: Callable[[str], bytes]) -> None:
    """dest against the SHA-256 in the .meta4 that download.kiwix.org gives
    for its path, whichever mirror served the bytes (once per download: the
    result is kept beside it, keyed to its size and mtime)."""
    st = dest.stat()
    ok_file = dest.with_name(dest.name + ".sha256-ok")
    stamp = f"{st.st_size} {st.st_mtime_ns}"
    if ok_file.exists() and ok_file.read_text().strip() == stamp:
        return
    m4 = meta4_url(url)
    try:
        want = meta4_sha256(get(m4).decode("utf-8", "replace"))
    except OSError as e:
        raise StepError(f"{m4}: {e}") from e
    if not want:
        raise StepError(f"{m4} gives no SHA-256: {dest.name} cannot be checked")
    log(f"  checking {dest.name} against Kiwix's SHA-256")
    got = sha256_file(dest)
    if got != want:
        dest.unlink(missing_ok=True)
        raise StepError(f"{url}: SHA-256 {got} is not Kiwix's {want}; deleted, run again")
    ok_file.write_text(stamp)


def opds_maxi_url(xml: bytes, code: str) -> str | None:
    """The ZIM URL of the wikipedia_<code>_all maxi entry in an OPDS v2
    acquisition feed (its open-access link names the .meta4)."""
    import xml.etree.ElementTree as ET
    atom = "{http://www.w3.org/2005/Atom}"
    root = ET.fromstring(xml)
    for e in root.iter(f"{atom}entry"):
        if (e.findtext(f"{atom}name") == f"wikipedia_{code}_all"
                and e.findtext(f"{atom}flavour") == "maxi"):
            for link in e.iter(f"{atom}link"):
                href = link.get("href") or ""
                if link.get("rel") == "http://opds-spec.org/acquisition/open-access" \
                        and href.endswith(".zim.meta4"):
                    return href[:-len(".meta4")]
    return None


def listing_maxi_url(html: str, code: str, base: str = KIWIX_WIKIPEDIA) -> str | None:
    """The newest wikipedia_<code>_all_maxi_<YYYY-MM>.zim in a directory listing."""
    names = set(re.findall(rf'href="(wikipedia_{re.escape(code)}_all_maxi_\d{{4}}-\d{{2}}\.zim)"', html))
    return base + max(names) if names else None


def resolve_wikipedia_zim(lang: str, *, get: Callable[[str], bytes] = _get) -> str:
    """The newest full Wikipedia ZIM with pictures (maxi) in `lang`: from
    Kiwix's OPDS catalog, else from download.kiwix.org's listing."""
    from streetzim.languages import kiwix_zim_code
    code = kiwix_zim_code(lang)
    why = []
    try:
        url = opds_maxi_url(get(f"{OPDS_ENTRIES}?name=wikipedia_{code}_all&count=20"), code)
        if url:
            return url
        why.append("no maxi entry in Kiwix's catalog")
    except Exception as e:  # noqa: BLE001 - any failure: try the listing
        why.append(f"Kiwix's catalog: {e}")
    try:
        url = listing_maxi_url(get(KIWIX_WIKIPEDIA).decode("utf-8", "replace"), code)
        if url:
            return url
        why.append(f"none on {KIWIX_WIKIPEDIA}")
    except Exception as e:  # noqa: BLE001
        why.append(f"{KIWIX_WIKIPEDIA}: {e}")
    raise StepError(f"no wikipedia_{code}_all_maxi ZIM found ({'; '.join(why)}); "
                    "give --wikipedia-zim-url")


def base_info(base: Path) -> dict[str, Any]:
    """Name, UUID, size and map-config of a base."""
    _tools()
    from tools.langpack.merge_zim import KEY_MAPCFG, blob_of
    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(str(base))
    name = (blob_of(z, (ord("M"), b"Name")) or b"").decode("utf-8", "replace")
    try:
        cfg = json.loads(blob_of(z, KEY_MAPCFG) or b"{}")
    except ValueError:
        cfg = {}
    return {"name": name, "uuid": z.header.uuid.hex(), "size": base.stat().st_size,
            "cfg": cfg if isinstance(cfg, dict) else {}}


def region_of(name: str) -> str | None:
    """The region id in a StreetZim Name (osm_mul_monaco: monaco)."""
    m = re.fullmatch(r"osm_[a-z]{2,3}_([a-z0-9][a-z0-9-]*)", name)
    return m.group(1) if m else None


def the_region(args: argparse.Namespace, info: dict[str, Any]) -> str:
    """The region: the names' (args.region), which must be the base's Name's."""
    named = region_of(info["name"])
    region = args.region or named
    if not region:
        raise StepError(f"the base's Name {info['name']!r} gives no region id: give --region")
    if named and named != region:
        raise StepError(f"the base's Name {info['name']!r} is region {named}, not {region}")
    return region


def content_name(path: Path, stem: str) -> str:
    """stem + "-" + the first HASH_HEX hex digits of path's SHA-256 + ".zim"."""
    return f"{stem}-{sha256_file(path)[:HASH_HEX]}.zim"


def same_bytes(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(1 << 24), fb.read(1 << 24)
            if x != y:
                return False
            if not x:
                return True


def publish(src: Path, dest: Path, *, replace: bool = False) -> bool:
    """src into dest's folder through a hidden name that does not end in
    .zim (Zimfarm's uploader takes a ZIM by its name), renamed when
    complete. A dest that holds the same bytes is kept (False); one with
    other bytes is replaced only with `replace` (sidecars), never for a
    ZIM. True when written."""
    if dest.exists():
        if same_bytes(src, dest):
            return False
        if not replace:
            raise StepError(f"{dest} exists with other bytes: not replaced (a recipe may name it)")
    part = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        shutil.copyfile(src, part)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return True


def build_timeout(base_bytes: int) -> int:
    """tools/langpack/build_packs.sh's: 3 h per started 3 GB of base, at least 3 h."""
    return 3600 * BUILD_HOURS_PER_3GB * max(1, -(-base_bytes // 3_000_000_000))


def prefetch_ok(rc: int, logfile: Path) -> tuple[bool, str]:
    """prefetch.py's result: (go on, what to say). Exit 1 with a summary that
    lists no failed title map is incomplete Wikidata text, which the pack's
    statistics say; anything else is a failure, with its cause."""
    rows = json_lines(logfile)
    summary = next((r for r in reversed(rows) if "base_uuid" in r), None)
    causes = [f"{r.get('lang')}: {r['titles_error']}" for r in rows if r.get("titles_error")]
    if rc == 0 and summary is not None:
        return True, "complete"
    if rc == 1 and summary is not None and not summary.get("failed") and not causes \
            and isinstance(summary.get("text_incomplete"), list):
        return True, f"Wikidata text incomplete for {','.join(summary['text_incomplete'])}"
    return False, "; ".join(causes) or f"rc={rc}:\n{tail(logfile)}"


# --------------------------------------------------------------------- pack

def run_pack(args: argparse.Namespace, work: Path, dl: Path, cache: Path, out_dir: Path, zc: str,
             progress: Progress) -> int:
    lang = args.language
    env = tool_env(work, zc)
    wcache = cache / "wiki_pack_cache"
    progress.step("Fetching the base")
    base = fetch_input(args.base_url, dl / "base", reuse=args.reuse)
    info = base_info(base)
    region = the_region(args, info)
    log(f"  base {base.name}: {info['name']}, UUID {info['uuid']}, region {region}")

    progress.step("Fetching the Wikipedia ZIM")
    src_url = args.wikipedia_zim_url or resolve_wikipedia_zim(lang)
    log(f"  {src_url}")
    source = fetch_input(src_url, dl / "wikipedia", verify=True, reuse=args.reuse)

    progress.step("Fetching the SQL dumps and Wikidata text")
    plog = work / "logs" / "prefetch.log"
    rc = run(py(tool("prefetch.py"), str(base), "--langs", lang, "--cache", str(wcache)),
             plog, timeout=STEP_TIMEOUT * 3, env=env)
    ok, why = prefetch_ok(rc, plog)
    if not ok:
        raise StepError(f"prefetch.py failed: {why}")
    log(f"  prefetch: {why}")

    progress.step(f"Building the {lang} pack")
    stage = work / "staging"
    stage.mkdir(parents=True, exist_ok=True)
    built = stage / f"{lang}.zim"
    stats = stage / f"{lang}.stats.json"
    marker = stage / f".run-{lang}"
    stats.unlink(missing_ok=True)
    marker.touch()
    cmd = py(tool("build_wiki_pack.py"), str(base), "--lang", lang, "--source", str(source),
             "--region", region, "--images", args.images, "--offline", "--cache", str(wcache),
             "--jobs", str(args.jobs), "--workers", "2", "-o", str(built), "--stats", str(stats),
             "--zimcheck", zc)
    if args.no_wikidata_text:
        cmd.append("--no-wikidata-text")
    t0 = time.time()
    brc = run(cmd, work / "logs" / "build.log", timeout=build_timeout(info["size"]), env=env)
    _tools()
    from tools.langpack.builder_ok import decide
    ok, why = decide(brc, str(stats), str(built), str(marker))
    log(f"  builder rc={brc} in {time.time() - t0:.0f} s: {'PROCEED' if ok else 'REJECT'} {why}")
    if not ok:
        raise StepError(f"the pack builder's result is refused ({why}):\n{tail(work / 'logs' / 'build.log')}")
    stem = f"osm_{region}_wiki-{lang}_{datetime.date.today().isoformat()}_{info['uuid'][:8]}"
    staged = stage / content_name(built, stem)
    os.replace(built, staged)

    progress.step("Gating the pack (pack_gate.py, zimcheck)")
    grc = run(py(tool("pack_gate.py"), str(staged), "--base", str(base)),
              work / "logs" / "pack_gate.log", timeout=GATE_TIMEOUT, env=env)
    if grc != 0:
        raise StepError(f"pack_gate.py FAIL (rc={grc}):\n{tail(work / 'logs' / 'pack_gate.log')}")
    gate = staged.with_name(staged.name + ".gate.json")
    if not gate.exists():
        raise StepError(f"pack_gate.py wrote no {gate.name}")

    progress.step("Publishing")
    final = out_dir / staged.name
    wrote = publish(staged, final)
    for side, suffix in ((gate, ".gate.json"), (stats, ".stats.json")):
        publish(side, final.with_name(final.name + suffix), replace=True)
    st = json.loads(stats.read_text())
    log(f"{PROG}: {'wrote' if wrote else 'already there, same bytes:'} {final} "
        f"({final.stat().st_size:,} B, {st.get('articles', '?')} articles); gate PASS")
    return 0


# --------------------------------------------------------------------- menu

def link_into(src: Path, folder: Path) -> Path:
    """src under its own name in folder (a symlink to a file used in place):
    pack_gate.py writes <pack>.gate.json beside the name it is given."""
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / src.name
    if dest.exists() or dest.is_symlink():
        raise StepError(f"two packs named {src.name}")
    dest.symlink_to(src)
    return dest


TITLE_WAIT_BUDGET = 120     # seconds of Wikidata throttling the titles may wait (best effort)


def title_fetcher() -> Any:
    """wiki_pack.api_fetcher with a short wait budget: Wikidata's maxlag can
    hold a request for the 15 minutes a pack's text may wait, and the
    region's name in each language only sets the versions' Titles."""
    from cloud.wikimedia_http import BUDGET_ENV
    from streetzim import wiki_pack
    before = os.environ.get(BUDGET_ENV)
    os.environ[BUDGET_ENV] = str(TITLE_WAIT_BUDGET)
    try:
        return wiki_pack.api_fetcher(log)
    finally:
        if before is None:
            os.environ.pop(BUDGET_ENV, None)
        else:
            os.environ[BUDGET_ENV] = before


def plan_menu(base: Path, avail: dict[str, str], core: list[str], region: str,
              cache: Path, *, online: bool = True) -> dict[str, Any]:
    """region_meta.meta with "has a gated pack" for "has a Wikipedia ZIM";
    the titles from Wikidata (best effort: else the base's Title)."""
    _tools()
    from tools.langpack import region_meta
    from tools.langpack.merge_zim import KEY_MAPCFG, blob_of
    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(str(base))
    cfg = json.loads(blob_of(z, KEY_MAPCFG) or b"{}")
    qid = region_meta.region_qid(z, str(cfg.get("name") or base.stem), region)
    if qid and online:
        own = [x for x in (cfg.get("regionLanguages") or []) if isinstance(x, str) and LANG_RE.fullmatch(x)]
        want = [str(x) for x in dict.fromkeys([*core, *own, *avail])]
        try:
            from streetzim import wiki_pack
            wiki_pack.wikidata_text([qid], "en", cache, fetch=title_fetcher(), langs=want, log=log)
        except Exception as e:  # noqa: BLE001 - titles fall back to the base's
            log(f"  Wikidata labels for {qid}: {e} (titles fall back to the base's)")
    tier = region_meta.registry_tier(region)
    out = region_meta.meta(str(base), "", core, cache, region_meta.REGION_PACKS_MAX, tier, region,
                           has=lambda lg: lg in avail)
    for d in out["dropped"]:
        d["why"] = d["why"].replace("Wikipedia ZIM in WIKI_SRC", "gated pack given")
    out["tier"] = tier
    return out


def file_md5(path: str) -> str:
    _tools()
    from tools.langpack.gatelib import md5_file
    return md5_file(path)


def free_bytes(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def need_disk(folder: Path, need: int, what: str) -> None:
    free = free_bytes(folder)
    if free < need + DISK_MARGIN:
        raise StepError(f"{folder} has {free / 1e9:.1f} GB free; {what} needs about "
                        f"{(need + DISK_MARGIN) / 1e9:.1f} GB")


def name_by_content(packs_dir: Path) -> list[str]:
    """Every search pack in packs_dir renamed to its content name, with its
    sidecar and gate (search_packs.py names them by date and base); recipes
    made after this name the new files."""
    renamed = []
    for p in sorted(packs_dir.glob("*_search-*.zim")):
        if re.search(rf"-[0-9a-f]{{{HASH_HEX}}}\.zim$", p.name):
            continue
        new = packs_dir / content_name(p, p.name[:-4])
        os.replace(p, new)
        for ext in (".json", ".gate.json"):
            side = p.with_name(p.name + ext)
            if side.exists():
                os.replace(side, new.with_name(new.name + ext))
        g = new.with_name(new.name + ".gate.json")
        if g.exists():
            d = json.loads(g.read_text())
            if isinstance(d, dict) and "pack" in d:
                d["pack"] = new.name
                g.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        renamed.append(new.name)
    return renamed


def gate_entry(row: dict[str, Any], *, base: Path, rl: list[str], region: str, title: str | None,
               query: str, work: Path, zc: str, zver: str, env: dict[str, str],
               keep: bool) -> dict[str, Any]:
    """run_gates.sh's entry checks that need neither a browser nor
    kiwix-serve: merged by merge_zim.py, the recipe materialized == that file,
    verify_merge.py, zimcheck -A, search_gate.py. Writes the entry's verdict
    where make_site.py and site_check.py read it, and returns it."""
    _tools()
    from tools.langpack import gatelib
    from tools.langpack.merge_zim import SEARCH, open_pack
    from tools.langpack.recipe2 import materialize
    L = row["L"]
    tag = base.name[:-4]
    recipe = json.loads((work / "lp" / "recipes" / tag / f"{L}.json").read_text())
    files: list[str] = list(row["pack_files"])
    idx: str | None = row.get("index_file")
    logs = work / "logs" / "gates"
    merged = work / "merged" / f"m-{tag}-{L}.zim"
    merged.parent.mkdir(parents=True, exist_ok=True)
    # the merged file and its copy from the recipe, each about base + packs
    inputs = info_size(base) + sum(info_size(Path(f)) for f in [*files, *([idx] if idx else [])])
    need_disk(merged.parent, 2 * inputs, f"checking the {L} version")
    ia = ["--index", idx] if idx else []
    ta = ["--title", title] if title else []
    common = ["--lang", L, "--region-langs", ",".join(rl), "--region", region, *ta,
              "--language-meta", "map"]
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    rc = run(py(tool("merge_zim.py"), str(base), *files, *ia, "-o", str(merged), *common),
             logs / f"{L}.merge.log", timeout=STEP_TIMEOUT, env=env, echo=False)
    if rc != 0 or not merged.exists():
        log(f"  {L}: merge_zim.py rc={rc}\n{tail(logs / f'{L}.merge.log', 8)}")
        checks.update(recipe_eq_merge=False, verify=False, zimcheck=False, search=False)
    else:
        packs = {open_pack(p).lang: p for p in files}
        if idx:
            packs[SEARCH] = idx
        twin = merged.with_name(merged.name + ".fromrecipe")
        try:
            materialize(recipe, str(base), packs, str(twin))
            checks["recipe_eq_merge"] = same_bytes(twin, merged)
        except Exception as e:  # noqa: BLE001 - recorded as the check's failure
            evidence["recipe_eq_merge"] = f"{type(e).__name__}: {e}"
            checks["recipe_eq_merge"] = False
        finally:
            twin.unlink(missing_ok=True)
        run(py(tool("verify_merge.py"), str(merged), str(base), *files, *ia, *common,
               "--queries", query), logs / f"{L}.verify.log", timeout=STEP_TIMEOUT, env=env, echo=False)
        checks["verify"], evidence["verify"] = verify_summary(
            (logs / f"{L}.verify.log").read_text(errors="replace"))
        zlog = logs / f"{L}.zimcheck.log"
        # single-threaded, as run_gates.sh (zim-tools 3.8.0 stalled with --threads=4)
        zrc = run([zc, "-A", "--threads=1", str(merged)], zlog, timeout=STEP_TIMEOUT, echo=False)
        checks["zimcheck"] = zimcheck_passed(zlog.read_text(errors="replace"), zrc, zver)
        evidence["zimcheck_rc"] = zrc
        run(py(tool("search_gate.py"), str(merged), str(base), "--lang", L),
            logs / f"{L}.search.json", timeout=STEP_TIMEOUT, env=env, echo=False)
        sj = last_json(logs / f"{L}.search.json")
        checks["search"] = sj.get("pass") is True
        evidence["search"] = {k: sj.get(k) for k in ("lang", "titles", "suggest_found",
                                                     "fulltext_found", "why")}
        if not keep:
            merged.unlink(missing_ok=True)
    verdict = {"tag": tag, "lang": L, "base_md5": file_md5(str(base)),
               "recipe_md5": recipe["md5"], "recipe_uuid": recipe["uuid"],
               "recipe_size": recipe["size"], "recipe_file_md5": recipe.get("file_md5"),
               "fingerprint": recipe.get("fingerprint"),
               "packs": {os.path.basename(f): file_md5(f) for f in [*files, *([idx] if idx else [])]},
               "zimcheck_version": zver, "checks": checks,
               "all_pass": bool(checks) and all(checks.values()), "advisory": [],
               # StreetZim's own gates run these too (run_gates.sh); here they cannot
               "not_run": ["route", "kiwix", "viewer", "ui", "smoke", "base_smoke",
                           "base_viewer", "base_ui"],
               "gates": f"{PROG} {version()}",
               "composed": datetime.datetime.now().isoformat(timespec="seconds"),
               "evidence": evidence}
    vp = Path(gatelib.verdict_path(str(base), L))
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text(json.dumps(verdict, ensure_ascii=False, indent=1))
    return verdict


def info_size(path: Path) -> int:
    return os.stat(path).st_size


def search_pack_sets(langs: list[str], rl: list[str], base_wiki: str | None,
                     avail: dict[str, str]) -> list[list[str]]:
    """The packs of each search pack search_packs.py builds: one per version
    that carries packs and lacks none, indexed in the version's own language
    (recipe2.search_lang), so two versions share one only with the same packs
    and the same language."""
    _tools()
    from tools.langpack.recipe2 import menu_packs, search_lang, wanted_packs
    keys: dict[tuple[tuple[str, ...], str], list[str]] = {}
    for L in langs:
        packs = menu_packs(rl, base_wiki, L, avail)
        if packs and not wanted_packs(rl, base_wiki, L) - set(packs):
            keys.setdefault((tuple(packs), search_lang(L)), packs)
    return list(keys.values())


def search_disk_need(base_size: int, sets: list[list[int]]) -> int:
    """The search packs' estimate (they do not exist yet; one per entry of
    `sets`, the pack sizes of each) plus the largest version's merged file
    and its copy: base + packs + its search pack, twice."""
    est = [int(SEARCH_BASE_SHARE * base_size + SEARCH_PACKS_SHARE * sum(s)) for s in sets]
    biggest = max((base_size + sum(s) + e for s, e in zip(sets, est)), default=base_size)
    return sum(est) + 2 * biggest


def run_menu(args: argparse.Namespace, work: Path, dl: Path, cache: Path, out_dir: Path, zc: str,
             progress: Progress) -> int:
    env = tool_env(work, zc)
    os.environ["LANGPACK_WORK"] = env["LANGPACK_WORK"]      # gatelib, in this process
    wcache = cache / "wiki_pack_cache"
    zver = zimcheck_version(zc)
    progress.step("Fetching the base and the packs")
    base = fetch_input(args.base_url, dl / "base", reuse=args.reuse)
    info = base_info(base)
    region = the_region(args, info)
    packs_dir = work / "packs"
    given: list[Path] = []
    _tools()
    from tools.langpack.merge_zim import open_pack
    for url in args.pack_url:
        p = link_into(fetch_input(url, dl / "packs", reuse=args.reuse), packs_dir)
        m = PACK_NAME.fullmatch(p.name)
        try:
            lang = open_pack(str(p)).lang
        except Exception as e:  # noqa: BLE001 - not a pack at all
            raise StepError(f"{url}: not a Wikipedia pack ({e})") from e
        if not m or m.group(2) != lang:
            raise StepError(f"{url}: its name says {m.group(2) if m else '?'}, its articles are {lang}")
        given.append(p)

    progress.step("Gating the packs (pack_gate.py, zimcheck)")
    gates: dict[str, dict[str, Any]] = {}
    for p in given:
        rc = run(py(tool("pack_gate.py"), str(p), "--base", str(base)),
                 work / "logs" / f"{p.name}.gate.log", timeout=GATE_TIMEOUT, env=env, echo=False)
        g = last_json(work / "logs" / f"{p.name}.gate.log")
        gates[p.name] = {"ok": rc == 0, "checks": g.get("checks")}
        log(f"  {p.name}: {'PASS' if rc == 0 else 'FAIL'} {g.get('checks', '')}")
    from tools.langpack.recipe2 import available_packs
    avail = available_packs(str(packs_dir), str(base), region)
    if not avail:
        raise StepError("no pack passed its gate for this base")

    progress.step("Planning the menu")
    core = [x for x in (args.languages or "").split(",") if x]
    if not core:
        from tools.langpack.region_meta import CORE_LANGS
        core = list(CORE_LANGS)
    plan = plan_menu(base, avail, core, region, wcache)
    langs: list[str] = plan["langs"]
    rl: list[str] = plan["region_langs"]
    titles: dict[str, str] = plan["titles"]
    log(f"  menu {','.join(langs)}; region languages {','.join(rl) or '-'}; "
        f"packs {','.join(sorted(avail))}; tier {plan['tier']}")
    for d in plan["dropped"]:
        log(f"  left out {d['lang']}: {d['why']}")
    if not langs:
        raise StepError("no language has everything its version needs")
    unused = sorted(set(avail) - set(plan["pack_langs"]))
    if unused:
        log(f"  packs no version needs: {','.join(unused)}")
    sets = search_pack_sets(langs, rl, plan.get("base_wiki"), avail)
    log(f"  {len(sets)} search packs to build (one per version that carries packs)")
    need_disk(work, search_disk_need(info["size"], [[info_size(Path(avail[x])) for x in s] for s in sets]),
              "the search packs and the largest version's checks")

    progress.step("Building the search packs (search_packs.py)")
    rc = run(py(tool("search_packs.py"), str(base), str(packs_dir), "--region", region,
                "--langs", ",".join(langs), "--region-langs", ",".join(rl), "--cache", str(wcache),
                "--jobs", str(args.jobs), "--work-dir", str(work / "search-build")),
             work / "logs" / "search_packs.log", timeout=STEP_TIMEOUT * 6, env=env)
    if rc != 0:
        log(f"  search_packs.py rc={rc}: a search pack failed its gate; its versions will fail")
    for name in name_by_content(packs_dir):
        log(f"  search pack {name}")

    tag = base.name[:-4]
    rdir = work / "lp" / "recipes" / tag
    menu_log = work / "logs" / "menu.jsonl"
    progress.step("Making the recipes (recipe2.py menu)")
    rc = run(py(tool("recipe2.py"), "menu", str(base), "--packs-dir", str(packs_dir),
                "--langs", ",".join(langs), "--region-langs", ",".join(rl), "--region", region,
                "--titles", json.dumps(titles, ensure_ascii=False), "--language-meta", "map",
                "--out-dir", str(rdir)), menu_log, timeout=STEP_TIMEOUT, env=env, echo=False)
    rows = [r for r in json_lines(menu_log) if "L" in r]
    if rc != 0 or not rows:
        raise StepError(f"recipe2.py menu rc={rc}:\n{tail(menu_log)}")
    progress.grow(len(rows))

    verdicts: dict[str, dict[str, Any]] = {}
    query = str(plan.get("region_title") or region)
    for row in rows:
        progress.step(f"Checking the {row['L']} version (merge, recipe, verify, zimcheck, search)")
        t0 = time.time()
        v = gate_entry(row, base=base, rl=rl, region=region, title=titles.get(row["L"]),
                       query=query, work=work, zc=zc, zver=zver, env=env, keep=args.keep_temp)
        verdicts[row["L"]] = v
        log(f"  {row['L']}: {'PASS' if v['all_pass'] else 'FAIL'} "
            + " ".join(f"{k}={'ok' if ok else 'FAIL'}" for k, ok in v["checks"].items())
            + f" ({time.time() - t0:.0f} s)")

    progress.step("Writing the recipes (make_site.py) and checking them (site_check.py)")
    site = work / "site"
    recipes = site / "recipes"
    base_download = args.base_url if is_url(args.base_url) else args.url_template.format(file=base.name)
    rc = run(py(tool("make_site.py"), str(base), "--packs-dir", str(packs_dir),
                "--region", region, "--region-title", str(plan["region_title"]),
                "--region-langs", ",".join(rl), "--langs", ",".join(langs),
                "--titles", json.dumps(titles, ensure_ascii=False), "--language-meta", "map",
                "--url-template", args.url_template, "--recipe-prefix", "recipes/",
                "--base-download", base_download, "-o", str(recipes)),
             work / "logs" / "make_site.log", timeout=STEP_TIMEOUT, env=env, echo=False)
    summary = last_json(work / "logs" / "make_site.log")
    if rc != 0 or "menu" not in summary:
        raise StepError(f"make_site.py rc={rc}:\n{tail(work / 'logs' / 'make_site.log')}")
    published: list[str] = summary["menu"]
    if not published:
        raise StepError(f"no version passed its checks: {summary.get('refused')}")
    rc = run(py(tool("site_check.py"), str(site), "--base", str(base), "--packs-dir",
                str(packs_dir), "--no-archive"), work / "logs" / "site_check.log",
             timeout=STEP_TIMEOUT, env=env)
    if rc != 0:
        raise StepError(f"site_check.py FAIL:\n{tail(work / 'logs' / 'site_check.log')}")

    progress.step("Publishing")
    used: set[str] = set()
    for L in published:
        r = json.loads((recipes / f"{L}.json").read_text())
        used.update(p["file"] for p in r["packs"].values() if p.get("kind") == "search")
    out_recipes = out_dir / "recipes"
    clash = [str(out_dir / f) for f in sorted(used)
             if (out_dir / f).exists() and not same_bytes(packs_dir / f, out_dir / f)]
    if clash:
        raise StepError(f"{', '.join(clash)} exist(s) with other bytes: not replaced")
    if out_recipes.exists() and not args.overwrite:
        raise StepError(f"{out_recipes} exists (use --overwrite)")
    need_disk(out_dir, sum(info_size(packs_dir / f) for f in used)
              + sum(info_size(x) for x in recipes.rglob("*") if x.is_file()), "publishing")
    staging = out_dir / f".recipes.{os.getpid()}.part"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(recipes, staging)
    for L in published:                       # the evidence beside the recipes
        vp = work / "lp" / "verdicts" / tag / f"{L}.json"
        (staging / "verdicts" / tag).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(vp, staging / "verdicts" / tag / f"{L}.json")
    (staging / "gates").mkdir(exist_ok=True)
    for g in packs_dir.glob("*.gate.json"):
        shutil.copyfile(g, staging / "gates" / g.name)
    menu_doc = {"format": "streetzim-langpack-menu/1", "tool": f"{PROG} {version()}",
                "base": {"file": base.name, "uuid": info["uuid"], "md5": file_md5(str(base))},
                "region": region, "tier": plan["tier"], "languages": langs,
                "region_languages": rl, "published": published,
                "refused": summary.get("refused"), "dropped": plan["dropped"],
                "packs": {k: os.path.basename(v) for k, v in sorted(avail.items())},
                "pack_gates": gates, "search_packs": sorted(used),
                "checks": {L: v["checks"] for L, v in verdicts.items()}}
    (staging / "menu.json").write_text(json.dumps(menu_doc, ensure_ascii=False, indent=1))
    for f in sorted(used):
        publish(packs_dir / f, out_dir / f)
    shutil.rmtree(out_recipes, ignore_errors=True)
    os.replace(staging, out_recipes)
    log(f"{PROG}: {len(published)} versions ({','.join(published)}), {len(used)} search "
        f"packs and recipes/ in {out_dir}")
    refused = summary.get("refused") or []
    if refused:
        log(f"{PROG}: refused {json.dumps(refused, ensure_ascii=False)}")
    return 0


# --------------------------------------------------------------------- main

def _error(msg: object) -> int:
    print(f"{PROG}: error: {msg}", file=sys.stderr, flush=True)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not LANGPACK.is_dir() or not BUILD_WIKI_PACK.is_file():
        return _error(f"{LANGPACK} is missing: {PROG} runs from a StreetZim checkout or the "
                      "Docker image (tools/langpack/ is not in the wheel)")
    out_dir = Path(args.output).resolve()
    zc = args.zimcheck or os.environ.get("PACK_GATE_ZIMCHECK") or shutil.which("zimcheck") or "zimcheck"
    zver = zimcheck_version(zc)
    if not zver.startswith("zim-tools "):
        return _error(f"{zc} is not the official zimcheck (--version: {zver!r}); the gates "
                      "need zim-tools' (--zimcheck or $PACK_GATE_ZIMCHECK)")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.tmp:
            tmp = Path(args.tmp).resolve()
            tmp.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix=f"{PROG}-{args.mode}-", dir=tmp))
        else:                                  # private, removed at the end
            work = Path(tempfile.mkdtemp(prefix=f"{PROG}-{args.mode}-"))
        args.reuse = bool(args.dl)             # downloads are reused only in an explicit --dl
        dl = Path(args.dl).resolve() if args.dl else work / "dl"
        dl.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _error(e)
    # Caches that hold no ZIM (SQL dumps, title maps, Wikidata text, the
    # bases' scans): $STREETZIM_CACHE_DIR where set (the image: /output/cache),
    # else beside the downloads. The variables set here are put back after.
    saved = {k: os.environ.get(k) for k in ("STREETZIM_CACHE_DIR", "LANGPACK_WORK")}
    if not os.environ.get("STREETZIM_CACHE_DIR"):
        os.environ["STREETZIM_CACHE_DIR"] = str(dl / "cache")
    cache = Path(os.environ["STREETZIM_CACHE_DIR"]).resolve()
    # pack: 6 steps; menu: 7, plus one per version once they are known
    progress = Progress(args.stats_filename, 6 if args.mode == "pack" else 7)
    previous = None
    if threading.current_thread() is threading.main_thread():
        def stop(signum: int, frame: object) -> None:
            raise SystemExit(128 + signum)
        previous = signal.signal(signal.SIGTERM, stop)
    t0 = time.time()
    try:
        log(f"{PROG} {version()} --mode {args.mode}; zimcheck: {zver}; work {work}; "
            f"downloads {dl}; caches {cache}")
        rc = (run_pack if args.mode == "pack" else run_menu)(args, work, dl, cache, out_dir, zc,
                                                             progress)
        progress.finish()
        log(f"{PROG}: done in {time.time() - t0:.0f} s")
        return rc
    except StepError as e:
        return _error(e)
    finally:
        _kill_children()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if args.keep_temp:
            log(f"{PROG}: kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
