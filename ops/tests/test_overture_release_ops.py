"""The ops wrappers keep a pinned OVERTURE_RELEASE by default (a queue or
ship started by another script without it must stay on the round's cached
parquets), and resolve OVERTURE_RELEASE=latest once, before any
overture_cache/<theme>-<id>-<release>.parquet name is formed. The queue
records its release and --continue refuses a silent switch. Runs the
wrappers' real blocks against a stub python."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIN = "2026-08-19.0"

# script, variable, block start
WRAPPERS = {
    "queue": ("ops/build-refresh-queue.sh", "OVERTURE_RELEASE",
              'if [ "$OVERTURE_RELEASE" = latest ]; then'),
    "ship": ("ops/ship-region.sh", "OVERTURE_RELEASE",
             'if [ "$OVERTURE_RELEASE" = latest ]; then'),
    "cloud": ("ops/cloud/build_region.sh", "release",
              'if [ "$release" = latest ]; then'),
}


def _block(script: str, start: str, end: str = "\nfi\n") -> str:
    text = (ROOT / script).read_text()
    i = text.index(start)
    return text[i:text.index(end, i) + len(end)]


def _bash(tmp_path: Path, body: str, py_ok: bool = True):
    py = tmp_path / "venv312" / "bin" / "python3"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text('#!/bin/sh\necho "$*" >> "$STUB_LOG"\n' +
                  ('echo 2026-09-23.1\n' if py_ok else 'echo boom >&2; exit 1\n'))
    py.chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.unlink(missing_ok=True)
    r = subprocess.run(["bash", "-c", f'log() {{ echo "$*"; }}\nPY="{py}"\n{body}'],
                       cwd=tmp_path, capture_output=True, text=True,
                       env={**os.environ, "STUB_LOG": str(calls)})
    return r, (calls.read_text() if calls.exists() else "")


def _resolve(tmp_path, which, value, py_ok=True, dry=0, regate=0):
    script, var, start = WRAPPERS[which]
    body = (f"fail=0; id=r; DRY={dry}; REGATE={regate}; RELFILE={tmp_path}/q.release\n"
            f"{var}={value}\n{_block(script, start)}\necho \"RESULT=${var} fail=$fail\"\n")
    return _bash(tmp_path, body, py_ok)


@pytest.mark.parametrize("which", sorted(WRAPPERS))
def test_default_is_the_pinned_round_release(which):
    script, var, start = WRAPPERS[which]
    text = (ROOT / script).read_text()
    assert f'"${{OVERTURE_RELEASE:-{PIN}}}"' in text
    assert '="${OVERTURE_RELEASE:-latest}"' not in text
    # `latest` stays an opt-in, resolved before the first cache file name.
    assert text.index(start) < text.index(f"-${{{var}}}.parquet")


@pytest.mark.parametrize("which", sorted(WRAPPERS))
def test_opt_in_latest_resolves_once_for_both_themes(tmp_path, which):
    r, calls = _resolve(tmp_path, which, "latest")
    assert r.returncode == 0, r.stderr
    assert "RESULT=2026-09-23.1 fail=0" in r.stdout
    assert calls.splitlines() == ["download_overture_data.py addresses places --print-release"]


@pytest.mark.parametrize("which", sorted(WRAPPERS))
def test_pinned_release_is_kept(tmp_path, which):
    r, calls = _resolve(tmp_path, which, PIN)
    assert f"RESULT={PIN} fail=0" in r.stdout and calls == ""


@pytest.mark.parametrize("which", sorted(WRAPPERS))
def test_unresolvable_latest_stops(tmp_path, which):
    r, _ = _resolve(tmp_path, which, "latest", py_ok=False)
    if which == "queue":          # preflight collects failures, then exits
        assert "fail=1" in r.stdout
    else:
        assert r.returncode != 0 and "RESULT" not in r.stdout
    assert "OVERTURE_RELEASE=<release>" in r.stdout


@pytest.mark.parametrize("flag", ["dry", "regate"])
def test_queue_dry_run_and_regate_never_resolve_over_the_network(tmp_path, flag):
    kw = {flag: 1}
    cache = tmp_path / "overture_cache"
    cache.mkdir()
    for n in ("places-a-2026-08-19.0.parquet", "places-b-2026-09-23.2.parquet",
              "places-c-2026-09-23.10.parquet", "addresses-d-2027-01-01.0.parquet"):
        (cache / n).write_text("x")
    r, calls = _resolve(tmp_path, "queue", "latest", **kw)
    assert "RESULT=2026-09-23.10 fail=0" in r.stdout and calls == ""
    (tmp_path / "q.release").write_text("2026-09-23.2\n")          # recorded wins
    r, calls = _resolve(tmp_path, "queue", "latest", **kw)
    assert "RESULT=2026-09-23.2 fail=0" in r.stdout and calls == ""


def test_queue_dry_run_with_nothing_to_use_offline_fails(tmp_path):
    r, calls = _resolve(tmp_path, "queue", "latest", dry=1)
    assert "fail=1" in r.stdout and calls == ""


def _explicit(tmp_path, env_value):
    line = next(ln for ln in (ROOT / WRAPPERS["queue"][0]).read_text().splitlines()
                if ln.startswith("case \"${OVERTURE_RELEASE:-latest}\""))
    pre = "unset OVERTURE_RELEASE" if env_value is None else f"OVERTURE_RELEASE={env_value}"
    r, _ = _bash(tmp_path, f"{pre}\n{line}\necho OV=$OV_EXPLICIT\n")
    return r.stdout.strip()


def test_queue_knows_an_explicit_pin(tmp_path):
    assert _explicit(tmp_path, None) == "OV=0"
    assert _explicit(tmp_path, "latest") == "OV=0"
    assert _explicit(tmp_path, PIN) == "OV=1"


@pytest.mark.parametrize("recorded,current,cont,explicit,refused", [
    ("2026-08-19.0", "2026-09-23.1", 1, 0, True),     # latest moved mid-round
    ("2026-09-23.1", "2026-08-19.0", 1, 0, True),     # round was on latest, default pin now
    ("2026-08-19.0", "2026-09-23.1", 1, 1, False),    # switch on purpose
    ("2026-09-23.1", "2026-09-23.1", 1, 0, False),
    ("2026-08-19.0", "2026-09-23.1", 0, 0, False),    # not --continue: a new round
    ("", "2026-09-23.1", 1, 0, False),                # nothing recorded yet
])
def test_queue_continue_refuses_a_release_switch(tmp_path, recorded, current, cont, explicit,
                                                 refused):
    if recorded:
        (tmp_path / "q.release").write_text(recorded + "\n")
    block = _block(WRAPPERS["queue"][0], "REC_RELEASE=")
    body = (f"fail=0; CONTINUE={cont}; OV_EXPLICIT={explicit}; RELFILE={tmp_path}/q.release\n"
            f"OVERTURE_RELEASE={current}\n{block}\necho fail=$fail\n")
    r, _ = _bash(tmp_path, body)
    assert ("fail=1" in r.stdout) == refused, r.stdout
    if refused:
        assert f"OVERTURE_RELEASE={recorded}" in r.stdout


def test_queue_records_the_run_release():
    text = (ROOT / WRAPPERS["queue"][0]).read_text()
    assert 'RELFILE="${TSV%.tsv}.release"' in text
    write = '[ "$DRY" -eq 1 ] || echo "$OVERTURE_RELEASE" > "$RELFILE"'
    assert write in text
    # After preflight (resolution and the --continue check), before any build.
    assert text.index('preflight FAILED') < text.index(write) < text.index("=== refresh queue start")


@pytest.mark.parametrize("value,ok", [(None, False), ("", False), ("latest", False), (PIN, True)])
def test_build_region_fast_needs_a_resolved_release(tmp_path, value, ok):
    text = (ROOT / "ops/build-region-fast.sh").read_text()
    i = text.index('if [ -z "${OVERTURE_RELEASE:-}" ]; then')
    j = text.index("ADDR=", i)
    pre = "unset OVERTURE_RELEASE" if value is None else f"OVERTURE_RELEASE={value}"
    r = subprocess.run(["bash", "-c", f"set -u\n{pre}\n{text[i:j]}\necho built"],
                       capture_output=True, text=True)
    assert (r.returncode == 0 and "built" in r.stdout) == ok
    assert "2026-04-15.0" not in text


TRANSPORT = '--transport "${OVERTURE_TRANSPORT:-s3}"'


def _download_calls() -> list[tuple[str, str]]:
    """Every download_overture_data.py call in an ops shell script that
    downloads (not --print-release), with its continuation lines joined."""
    calls = []
    for path in sorted((ROOT / "ops").rglob("*.sh")):
        joined = path.read_text().replace("\\\n", " ")
        for line in joined.splitlines():
            if ("download_overture_data.py" in line and "--print-release" not in line
                    and not line.lstrip().startswith("#") and "--bbox" in line):
                calls.append((str(path.relative_to(ROOT)), line))
    return calls


def test_every_ops_download_keeps_the_s3_transport_by_default():
    calls = _download_calls()
    scripts = {s for s, _ in calls}
    assert {"ops/build-refresh-queue.sh", "ops/ship-region.sh", "ops/cloud/build_region.sh",
            "ops/cloud/rebuild_old_regions.sh"} <= scripts
    for script, line in calls:
        assert TRANSPORT in line, f"{script}: {line.strip()}"


@pytest.mark.parametrize("env,want", [({}, "s3"), ({"OVERTURE_TRANSPORT": "https"}, "https")])
def test_transport_reaches_the_downloader(tmp_path, env, want):
    """The ship-region download loop, run against a stub python."""
    text = (ROOT / "ops/ship-region.sh").read_text()
    i = text.index("for theme in addresses places; do")
    block = text[i:text.index("\ndone\n", i) + 6]
    py = tmp_path / "py"
    py.write_text('#!/bin/sh\necho "$*" >> "$STUB_LOG"\n'
                  'while [ $# -gt 0 ]; do [ "$1" = --out ] && echo x > "$2"; shift; done\n')
    py.chmod(0o755)
    (tmp_path / "overture_cache").mkdir()
    log = tmp_path / "calls.log"
    body = (f'log() {{ :; }}; bbox_stale() {{ return 1; }}; bbox_mark() {{ :; }}\n'
            f'PY="{py}"; ID=r; BBOX=1,2,3,4; LOG=/dev/null; OVERTURE_RELEASE={PIN}\n{block}')
    e = {k: v for k, v in os.environ.items() if k != "OVERTURE_TRANSPORT"}
    subprocess.run(["bash", "-c", body], cwd=tmp_path, check=True,
                   env={**e, **env, "STUB_LOG": str(log)})
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and all(f"--transport {want} " in ln for ln in lines)
