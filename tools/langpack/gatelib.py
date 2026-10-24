"""Shared gate checks for recipe2 menu and make_site (review S1, S2)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

_md5_cache: dict[tuple, str] = {}


def work_dir() -> str:
    """Where gate verdicts, gated recipes and the chunk-hash cache live:
    $LANGPACK_WORK, else ./langpack-work (docs/language-pack-pipeline.md)."""
    return os.path.abspath(os.environ.get("LANGPACK_WORK") or "langpack-work")


def md5_file(path: str) -> str:
    st = os.stat(path)
    key = (os.path.realpath(path), st.st_size, st.st_mtime_ns)
    if key not in _md5_cache:
        h = hashlib.md5(usedforsecurity=False)
        with open(path, "rb") as fh:
            while b := fh.read(16 << 20):
                h.update(b)
        _md5_cache[key] = h.hexdigest()
    return _md5_cache[key]


def pack_gate_ok(path: str) -> tuple[bool, str]:
    """A pack is usable only with a passing pack_gate.py verdict for exactly these bytes."""
    g = path + ".gate.json"
    if not os.path.exists(g):
        return False, "no pack gate (run pack_gate.py)"
    d = json.load(open(g))
    if not d.get("ok"):
        return False, f"pack gate failed: {d.get('problems', [])[:2]}"
    if d.get("pack_md5") != md5_file(path):
        return False, "pack changed since its gate"
    return True, ""


def verdict_path(base_path: str, lang: str) -> str:
    tag = os.path.basename(os.path.realpath(base_path))[:-4]
    return os.path.join(work_dir(), "verdicts", tag, f"{lang}.json")


def entry_verdict_ok(base_path: str, lang: str, recipe: dict) -> tuple[bool, str]:
    """An entry may be published only with an all-PASS run_gates verdict for this exact recipe."""
    p = verdict_path(base_path, lang)
    if not os.path.exists(p):
        return False, "no gate verdict"
    v = json.load(open(p))
    if v.get("recipe_md5") != recipe["md5"] or v.get("recipe_uuid") != recipe["uuid"] or v.get("recipe_size") != recipe["size"]:
        return False, "verdict is for another recipe"
    if not v.get("all_pass"):
        return False, "gates failed: " + ",".join(k for k, ok in v.get("checks", {}).items() if not ok)
    return True, ""


def zimcheck_version(zc: str) -> str:
    """First line of `zimcheck --version`: 'zim-tools 3.8.0' for the official one."""
    try:
        r = subprocess.run([zc, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"not runnable: {e}"
    return ((r.stdout + r.stderr).strip().splitlines() or [""])[0].strip()


_OVERALL = re.compile(r"(\[INFO\] )?Overall Test Status: Pass")


def zimcheck_log_passed(text: str, rc: int | None) -> bool:
    """zimcheck's verdict in its output: the LAST line holding "Overall Test
    Status:" is exactly the Pass line (an earlier line may quote a link that
    reads like one), and its exit code is 0. rc None: a log kept without
    its exit code, judged by the line alone."""
    lines = [ln.strip() for ln in text.splitlines() if "Overall Test Status:" in ln]
    return bool(lines) and _OVERALL.fullmatch(lines[-1]) is not None and rc in (None, 0)
