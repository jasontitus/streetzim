"""run_gates.sh's plumbing on the small ZIMs, with stand-ins for kiwix-serve,
node (the browser gates) and zimcheck: every step runs from the right paths
and the verdicts say what the steps found. The real browser run is
test_real_data.py::test_run_gates_end_to_end."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import TOOLS, tool  # noqa: E402

for _cmd in ("bash", "curl", "ss", "setsid"):
    if not shutil.which(_cmd):
        pytest.skip(f"run_gates.sh needs {_cmd}", allow_module_level=True)

KIWIX_STUB = r'''
import http.server, json, os, signal, sys, time
port = int(sys.argv[sys.argv.index("--port") + 1])
addr = sys.argv[sys.argv.index("--address") + 1] if "--address" in sys.argv else "0.0.0.0"
books = [a for a in sys.argv[1:] if a.endswith(".zim")]
# what the disk held when this server started (the gates must stream: GATE_JOBS merged files at a time)
log = os.environ.get("KIWIX_STUB_LOG")
if log:
    merged = os.environ.get("LANGPACK_MERGED") or os.path.join(os.environ["LANGPACK_WORK"], "merged")
    with open(log, "a") as fh:
        fh.write(json.dumps({"books": [os.path.basename(b) for b in books], "address": addr, "port": port,
                             "merged_dir": sorted(os.listdir(merged)), "start": time.time()}) + "\n")
    def bye(*_a):
        with open(log + ".end", "a") as fh:
            fh.write(json.dumps({"port": port, "books": [os.path.basename(b) for b in books], "end": time.time()}) + "\n")
        os._exit(0)
    signal.signal(signal.SIGTERM, bye)
xml = "<feed>" + "".join(f"<entry><title>x</title><link href=\"/content/{b.rsplit('/', 1)[-1][:-4].lower()}\"/></entry>"
                         for b in books) + "</feed>"
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/catalog/v2/entries"):
            body = xml.encode(); self.send_response(200); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer((addr, port), H).serve_forever()
'''

NODE_STUB = r'''#!/bin/bash
# stand-in for node: the gate scripts must exist where run_gates.sh says
[ -f "$1" ] || { echo "no such script: $1" >&2; exit 1; }
case "$1" in *viewer_gate.mjs) [ -n "$NODE_STUB_LOG" ] && echo "EXPECT_PACKS=$EXPECT_PACKS $2" >> "$NODE_STUB_LOG"; sleep "${NODE_STUB_SLEEP:-0}" ;; esac
case "$1" in
  *viewer_gate.mjs|*ui_gate.mjs) echo '{"pass": true}' ;;
  *zim_viewer_smoke.mjs) echo "1 passed, 0 failed" ;;
  *) exit 1 ;;
esac
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def gates(lp: SimpleNamespace, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    d = tmp_path_factory.mktemp("rungates")
    packs = d / "packs"
    packs.mkdir()
    zc_pack = lpzims.fake_zimcheck(d / "zc", "clean", None)
    for p in lp.packs.values():
        shutil.copy(p, packs / p.name)
        r = tool("pack_gate.py", packs / p.name, "--base", lp.base, env={"PACK_GATE_ZIMCHECK": zc_pack})
        assert r.returncode == 0, r.stdout
    stubs = d / "stubs"
    stubs.mkdir()
    ks = stubs / "kiwix-serve"
    ks.write_text(f"#!{sys.executable}\n{KIWIX_STUB}")
    ks.chmod(0o755)
    node = stubs / "node"
    node.write_text(NODE_STUB)
    node.chmod(0o755)
    zc = lpzims.fake_zimcheck(d / "zc", "pass", "[INFO] Overall Test Status: Pass\n", rc=0)
    region = d / "region.json"
    region.write_text(json.dumps({"search": {"english": {"q": "Alpha Town", "expect": "Alpha Town"}}, "named_qids": "Q1",
                                  "route": {"src": "-10.0,-20.0", "dst": "-10.1,-20.1"}}))   # south of the equator
    work = d / "work"
    env = {**os.environ, "LANGPACK_WORK": str(work), "PYTHON": sys.executable, "KIWIX_SERVE": str(ks),
           "NODE": str(node), "PACK_GATE_ZIMCHECK": zc, "LANGUAGE_META": "map",
           "TITLES": json.dumps({"de": "Testland DE", "th": "Testland TH"}), "PYTHONDONTWRITEBYTECODE": "1",
           "LANGPACK_DISK_MARGIN_GB": "0"}
    served = d / "served.jsonl"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(lp.base), str(packs), "de,th,en", "en", "testland",
                        str(region), str(_free_port())], capture_output=True, text=True, timeout=1200,
                       env={**env, "KIWIX_STUB_LOG": str(served)})
    return SimpleNamespace(r=r, work=work, tag=lp.base.name[:-4], env=env, packs=packs, region=region, base=lp.base,
                           served=served, stubs=stubs, d=d)


@pytest.mark.parametrize("lang", ["de", "th", "en"])
def test_every_step_ran(lang: str, gates: SimpleNamespace) -> None:
    vp = gates.work / "verdicts" / gates.tag / f"{lang}.json"
    assert vp.exists(), gates.r.stdout[-3000:] + gates.r.stderr[-3000:]
    v = json.loads(vp.read_text())
    out = gates.work / "gates" / gates.tag
    ok = {k: v["checks"][k] for k in ("recipe_eq_merge", "verify", "zimcheck", "viewer", "ui", "smoke",
                                      "base_viewer", "base_ui", "base_smoke")}
    assert all(ok.values()), (ok, (out / f"{lang}.verify.log").read_text()[-2000:])
    # the stand-ins cannot route or answer kiwix-serve's search: those must FAIL, and so the verdict
    assert v["checks"]["route"] is False and v["checks"]["kiwix"] is False and v["all_pass"] is False
    # but route_cli.py took the endpoints: "-10.0,-20.0" after a bare --src read as an option
    # (East Polynesia: every entry failed its route check with a usage error)
    assert "usage:" not in (out / f"{lang}.route.log").read_text()
    # no search pack here (run_gates.sh does not build them): Kiwix cannot find the pack articles
    # (search_gate.py), a required check, so FAIL; the English entry of an English base carries
    # no pack, so nothing is missed
    assert v["checks"]["search"] is (lang == "en") and v["advisory"] == []
    if lang != "en":
        assert v["evidence"]["search"]["titles"] >= 1 and v["evidence"]["search"]["suggest_found"] == 0
    assert v["zimcheck_version"] == "zim-tools 3.8.0"
    title = {"de": "Testland DE", "th": "Testland TH"}.get(lang)
    merged = json.loads((out / f"{lang}.merge.json").read_text())
    assert merged["metadata"]["Title"] == (title or "OSM - Testland")
    # streamed: the merged file is gone once its checks ran
    assert not (gates.work / "merged" / f"m-{gates.tag}-{lang}.zim").exists()
    # the region file's search.english.q is what the checks search for
    assert "INFO  suggest 'Alpha Town'" in (out / f"{lang}.verify.log").read_text()


def test_refuses_a_zimcheck_that_is_not_the_official_one(gates: SimpleNamespace, tmp_path: Path) -> None:
    """zimru's zimcheck prints the same "Overall Test Status" line: run_gates.sh
    must not start with it."""
    zimru = lpzims.fake_zimcheck(tmp_path / "zc", "zimru", "[INFO] Overall Test Status: Pass\n", rc=0,
                                 version="zimcheck (zimru) 0.1.0")
    work = tmp_path / "work"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de", "en", "testland",
                        str(gates.region), str(_free_port())], capture_output=True, text=True, timeout=120,
                       env={**gates.env, "PACK_GATE_ZIMCHECK": zimru, "LANGPACK_WORK": str(work)})
    assert r.returncode == 1 and "not the official zimcheck" in r.stderr, r.stderr
    assert not (work / "verdicts").exists()


def test_zimcheck_passes_only_with_exit_0(gates: SimpleNamespace, tmp_path: Path) -> None:
    """run_gates.sh keeps zimcheck's exit code beside its log; a Pass line
    with a non-zero exit is no pass (compose_verdicts.py)."""
    out = gates.work / "gates" / gates.tag
    assert (out / "de.zimcheck.log.rc").read_text().strip() == "0"
    zc = lpzims.fake_zimcheck(tmp_path / "zc", "passrc1", "[INFO] Overall Test Status: Pass\n", rc=1)
    work = tmp_path / "work"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de", "en", "testland",
                        str(gates.region), str(_free_port())], capture_output=True, text=True, timeout=600,
                       env={**gates.env, "PACK_GATE_ZIMCHECK": zc, "LANGPACK_WORK": str(work)})
    assert r.returncode == 0, r.stderr[-2000:]
    assert (work / "gates" / gates.tag / "de.zimcheck.log.rc").read_text().strip() == "1"
    v = json.loads((work / "verdicts" / gates.tag / "de.json").read_text())
    assert v["checks"]["zimcheck"] is False and v["checks"]["recipe_eq_merge"] is True


def test_one_merged_file_at_a_time(gates: SimpleNamespace) -> None:
    """kiwix-serve serves the base alone, then each merged file alone, and the
    merged folder holds only the file being checked (and none for the base)."""
    rows = [json.loads(x) for x in gates.served.read_text().splitlines()]
    assert [r["books"] for r in rows] == [[gates.base.name]] + [[f"m-{gates.tag}-{L}.zim"] for L in ("de", "th", "en")], rows
    assert rows[0]["merged_dir"] == []
    for r in rows[1:]:
        assert r["merged_dir"] == r["books"], r
    assert sorted(os.listdir(gates.work / "merged")) == []
    # nothing is served beyond the host itself: kiwix-serve listens on 127.0.0.1 only
    assert {r["address"] for r in rows} == {"127.0.0.1"}, rows


def test_keep_merged_and_search_as_advisory(gates: SimpleNamespace, tmp_path: Path) -> None:
    work = tmp_path / "work"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de", "en", "testland",
                        str(gates.region), str(_free_port())], capture_output=True, text=True, timeout=600,
                       env={**gates.env, "LANGPACK_WORK": str(work), "KEEP_MERGED": "1",
                            "LANGPACK_SEARCH_GATE": "report"})
    assert r.returncode == 0, r.stderr[-2000:]
    assert (work / "merged" / f"m-{gates.tag}-de.zim").exists()
    v = json.loads((work / "verdicts" / gates.tag / "de.json").read_text())
    assert v["advisory"] == ["search"] and v["checks"]["search"] is False


def test_refuses_without_room_for_a_merged_file(gates: SimpleNamespace, tmp_path: Path) -> None:
    work = tmp_path / "work"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de", "en", "testland",
                        str(gates.region), str(_free_port())], capture_output=True, text=True, timeout=120,
                       env={**gates.env, "LANGPACK_WORK": str(work), "LANGPACK_DISK_MARGIN_GB": "1000000000"})
    assert r.returncode == 1 and "a merged file needs" in r.stderr, r.stderr
    assert not (work / "verdicts").exists() and not (work / "recipes").exists()


def _flaky_zimcheck(d: Path, first: str, then: str, killed: bool = False) -> str:
    """A zimcheck whose first -A run prints `first` (and then, `killed`, dies
    of SIGKILL) and every later one `then`."""
    d.mkdir(parents=True, exist_ok=True)
    n = d / "runs"
    script = d / "zc.py"
    script.write_text("\n".join([
        f"#!{sys.executable}", "import sys, os",
        "if '--version' in sys.argv: print('zim-tools 3.8.0'); sys.exit(0)",
        f"p = {str(n)!r}", "k = int(open(p).read()) if os.path.exists(p) else 0", "open(p, 'w').write(str(k + 1))",
        "with open(p + '.args', 'a') as fh: fh.write(' '.join(sys.argv[1:-1]) + '\\n')",
        f"sys.stdout.write({first!r} if k == 0 else {then!r})", "sys.stdout.flush()",
        f"k == 0 and {killed!r} and os.kill(os.getpid(), 9)", "sys.exit(0)"]) + "\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.parametrize("first,killed,then,runs,ok", [
    ("[INFO] Verifying...\n", True, "[INFO] Overall Test Status: Pass\n", 2, True),     # killed: run again
    # ended on its own without a verdict: the same run again would do the same (review LOW)
    ("[INFO] Verifying...\n", False, "[INFO] Overall Test Status: Pass\n", 1, False),
    ("[INFO] Overall Test Status: Fail\n", False, "[INFO] Overall Test Status: Pass\n", 1, False),  # a Fail stays
])
def test_zimcheck_without_a_verdict_runs_once_more(gates: SimpleNamespace, tmp_path: Path, first: str, killed: bool,
                                                   then: str, runs: int, ok: bool) -> None:
    zc = _flaky_zimcheck(tmp_path / "zc", first, then, killed)
    work = tmp_path / "work"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de", "en", "testland",
                        str(gates.region), str(_free_port())], capture_output=True, text=True, timeout=600,
                       env={**gates.env, "LANGPACK_WORK": str(work), "PACK_GATE_ZIMCHECK": zc})
    assert r.returncode == 0, r.stderr[-2000:]
    assert int((tmp_path / "zc" / "runs").read_text()) == runs
    args = (tmp_path / "zc" / "runs.args").read_text().splitlines()
    assert args[0] == "-A --threads=1" and (runs == 1 or args[1] == "-A --threads=1"), args
    v = json.loads((work / "verdicts" / gates.tag / "de.json").read_text())
    assert v["checks"]["zimcheck"] is ok


def test_resume_skips_what_passed_for_the_same_recipe(gates: SimpleNamespace, tmp_path: Path) -> None:
    """RESUME=1: an entry whose verdict passes for this recipe, and the base when
    its checks passed, are not run again; the stand-ins' route/kiwix FAIL makes
    every verdict fail, so a passing verdict is written by hand here."""
    work = tmp_path / "work"
    env = {**gates.env, "LANGPACK_WORK": str(work)}
    args = [str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de,th", "en", "testland",
            str(gates.region), str(_free_port())]
    r1 = subprocess.run(args, capture_output=True, text=True, timeout=600, env=env)
    assert r1.returncode == 0, r1.stderr[-2000:]
    vp = work / "verdicts" / gates.tag / "de.json"
    v = json.loads(vp.read_text())
    v["all_pass"] = True
    vp.write_text(json.dumps(v))
    served = tmp_path / "served.jsonl"
    r2 = subprocess.run(args, capture_output=True, text=True, timeout=600,
                        env={**env, "RESUME": "1", "KIWIX_STUB_LOG": str(served)})
    assert r2.returncode == 0, r2.stderr[-2000:]
    assert "de: its verdict passes for this recipe (RESUME=1)" in r2.stdout
    assert "base: its checks passed in the last run (RESUME=1)" in r2.stdout
    rows = [json.loads(x)["books"] for x in served.read_text().splitlines()]
    assert rows == [[f"m-{gates.tag}-th.zim"]], rows        # th failed (route/kiwix stand-ins): run again
    # th's verdict was composed again, de's logs were kept and composed the same way
    assert json.loads(vp.read_text())["checks"] == v["checks"]
    # a base patched in place (same name, new mtime) has its checks run again
    os.utime(gates.base, None)
    served.unlink()
    r3 = subprocess.run(args, capture_output=True, text=True, timeout=600,
                        env={**env, "RESUME": "1", "KIWIX_STUB_LOG": str(served)})
    assert r3.returncode == 0
    assert [json.loads(x)["books"] for x in served.read_text().splitlines()][0] == [gates.base.name]


DF_STUB = r'''#!/bin/bash
# plenty of room on the first call (run_gates' start check), none afterwards
n=$(cat "$DF_COUNT" 2>/dev/null || echo 0); echo $((n + 1)) > "$DF_COUNT"
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
if [ "$n" = 0 ]; then echo "x 999999999999 0 999999999999 0% /"; else echo "x 999999999999 0 0 100% /"; fi
'''


def test_a_disk_break_never_passes_old_logs_for_a_new_recipe(gates: SimpleNamespace, tmp_path: Path) -> None:
    """Review r3 H-1: a resumed run whose recipe changed (a new title) and that
    stops for disk space must not compose an all-PASS verdict for the new
    recipe out of the last run's logs."""
    work = tmp_path / "work"
    tag = gates.tag
    env = {**gates.env, "LANGPACK_WORK": str(work), "RESUME": "1", "TITLES": json.dumps({"de": "Testland DE"})}
    args = [str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de,th", "en", "testland",
            str(gates.region), str(_free_port())]
    assert subprocess.run(args, capture_output=True, text=True, timeout=600, env=env).returncode == 0
    out = work / "gates" / tag
    for L in ("de", "th"):                   # the stand-ins cannot route or answer kiwix: make the logs pass
        (out / f"{L}.route.log").write_text("route OK\n")
        (out / f"{L}.kiwix.json").write_text(json.dumps({"pass": True, "checks": {}}) + "\n")
        (out / f"{L}.search.json").write_text(json.dumps({"pass": True}) + "\n")
    old = json.loads((work / "recipes" / tag / "de.json").read_text())
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "df").write_text(DF_STUB)
    (stubs / "df").chmod(0o755)
    served = tmp_path / "served.jsonl"
    env2 = {**env, "TITLES": json.dumps({"de": "Testlandien"}), "PATH": f"{stubs}:{os.environ['PATH']}",
            "DF_COUNT": str(tmp_path / "df.count"), "KIWIX_STUB_LOG": str(served)}
    r2 = subprocess.run(args, capture_output=True, text=True, timeout=600, env=env2)
    assert r2.returncode == 0 and "de: not run" in r2.stderr, r2.stderr[-2000:]
    new = json.loads((work / "recipes" / tag / "de.json").read_text())
    assert new["md5"] != old["md5"]
    v = json.loads((work / "verdicts" / tag / "de.json").read_text())
    assert v["recipe_md5"] == new["md5"] and v["all_pass"] is False and v["checks"]["logs_for_recipe"] is False
    assert not list(out.glob("de.*")) and not list(out.glob("th.*"))    # languages not reached: no logs
    sys.path.insert(0, str(TOOLS.parent.parent))
    from tools.langpack import gatelib
    os.environ["LANGPACK_WORK"] = str(work)
    try:
        assert gatelib.entry_verdict_ok(str(gates.base), "de", new)[0] is False
    finally:
        os.environ.pop("LANGPACK_WORK", None)


def test_logs_with_another_stamp_fail(gates: SimpleNamespace, tmp_path: Path) -> None:
    """compose_verdicts.py: logs stamped for another recipe (or not stamped) fail."""
    work = tmp_path / "work"
    env = {**gates.env, "LANGPACK_WORK": str(work)}
    args = [str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de", "en", "testland",
            str(gates.region), str(_free_port())]
    assert subprocess.run(args, capture_output=True, text=True, timeout=600, env=env).returncode == 0
    out = work / "gates" / gates.tag
    v = json.loads((work / "verdicts" / gates.tag / "de.json").read_text())
    assert v["checks"]["logs_for_recipe"] is True and v["checks"]["base_logs_current"] is True
    (out / "de.stamp").write_text("0" * 32 + " 00000000-0000-0000-0000-000000000000\n")
    r = subprocess.run([sys.executable, str(TOOLS / "compose_verdicts.py"), str(out), str(gates.base), "de"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert json.loads((work / "verdicts" / gates.tag / "de.json").read_text())["checks"]["logs_for_recipe"] is False


def test_a_search_pack_makes_kiwix_find_the_pack(gates: SimpleNamespace, tmp_path: Path) -> None:
    """With the entry's search pack in the packs folder (index_pack.py), the
    merge carries it, Kiwix's search finds the pack articles (search PASS),
    and the viewer is still told to expect one Wikipedia pack, not two."""
    from tools.langpack import index_pack
    packs = tmp_path / "packs"
    shutil.copytree(gates.packs, packs)
    de = next(packs.glob("osm_testland_wiki-de_*.zim"))
    side = index_pack.build(str(gates.base), [str(de)], "de", "testland",
                            str(packs / "osm_testland_search-de_2026-10-13_x.zim"), tmp_path / "cache", log=lambda m: None)
    assert side["gate"]["ok"]
    work = tmp_path / "work"
    log = tmp_path / "node.log"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(gates.base), str(packs), "de", "en", "testland",
                        str(gates.region), str(_free_port())], capture_output=True, text=True, timeout=600,
                       env={**gates.env, "LANGPACK_WORK": str(work), "NODE_STUB_LOG": str(log)})
    assert r.returncode == 0, r.stderr[-2000:]
    v = json.loads((work / "verdicts" / gates.tag / "de.json").read_text())
    assert v["checks"]["search"] is True and v["checks"]["verify"] is True and v["checks"]["recipe_eq_merge"] is True, v
    assert set(v["packs"]) == {de.name, "osm_testland_search-de_2026-10-13_x.zim"}
    assert any(x.startswith("EXPECT_PACKS=1 ") and "m-" in x for x in log.read_text().splitlines()), log.read_text()


def _flip_a_non_slot_byte(base: Path) -> None:
    """One byte of an image blob (uncompressed, not a viewer slot) changed in
    place and the trailing MD5 recomputed: same name and UUID, other bytes."""
    import hashlib
    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(str(base))
    d = z.dirent_at(z.find(ord("C"), b"images/marker.webp"))
    off, n = z.raw_blob(d.cluster, d.blob)
    data = bytearray(base.read_bytes())
    data[off + n - 1] ^= 0xFF
    data[-16:] = hashlib.md5(bytes(data[:-16]), usedforsecurity=False).digest()
    base.write_bytes(bytes(data))


def _passed_by_hand(vp: Path) -> dict:
    """The stand-ins cannot route or answer kiwix-serve: passed by hand, as a real run would."""
    v = json.loads(vp.read_text())
    v["checks"].update(route=True, kiwix=True, search=True)
    v["all_pass"] = all(v["checks"].values())
    assert v["all_pass"], v["checks"]
    vp.write_text(json.dumps(v))
    return v


def test_a_viewer_patch_needs_only_a_light_gate(gates: SimpleNamespace, tmp_path: Path) -> None:
    """Plan review B2: a base patched in place (new viewer slots, nothing else)
    keeps its recipes' fingerprint, so a resumed run re-checks what the viewer
    can change (recipe = merge, verify, zimcheck, the browser checks) and
    carries route, kiwix-serve and Kiwix search from the last passing verdict
    for another base MD5; anything else changed means the full gate (B2-1, B2-2)."""
    from tests.langpack.test_viewer_slots import V2, patch
    d = tmp_path
    base = d / "bases" / "osm-testland-2026-10-13.zim"
    base.parent.mkdir()
    uuid = lpzims.write_base(base, slots=lpzims.VIEWER_V1)
    packs = d / "packs"
    packs.mkdir()
    de = packs / "osm_testland_wiki-de_2026-10-13.zim"
    lpzims.write_pack(de, "de", uuid)
    zc_pack = lpzims.fake_zimcheck(d / "zcp", "clean", None)
    assert tool("pack_gate.py", de, "--base", base, env={"PACK_GATE_ZIMCHECK": zc_pack}).returncode == 0
    zc = _flaky_zimcheck(d / "zc", "[INFO] Overall Test Status: Pass\n", "[INFO] Overall Test Status: Pass\n")
    runs = d / "zc" / "runs"
    work = d / "work"
    env = {**gates.env, "LANGPACK_WORK": str(work), "RESUME": "1", "PACK_GATE_ZIMCHECK": zc}
    args = [str(TOOLS / "run_gates.sh"), str(base), str(packs), "de", "en", "testland", str(gates.region),
            str(_free_port())]
    out = work / "gates" / "osm-testland-2026-10-13"

    def run(e: dict) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        return r

    run(env)
    vp = work / "verdicts" / "osm-testland-2026-10-13" / "de.json"
    v1 = _passed_by_hand(vp)
    assert v1["fingerprint"] and v1["base_md5"] and int(runs.read_text()) == 1
    patch(base, d / "patched.zim", V2, d)
    shutil.copy(d / "patched.zim", base)                      # in place: same name and UUID, new viewer
    r = run(env)
    assert "de: the base changed only in its viewer slots: light gate" in r.stdout, r.stdout
    assert int(runs.read_text()) == 2                         # zimcheck runs in a light gate too (B2-1)
    v2 = json.loads(vp.read_text())
    assert v2["recipe_md5"] != v1["recipe_md5"] and v2["fingerprint"] == v1["fingerprint"]
    assert v2["base_md5"] != v1["base_md5"] and v2["all_pass"] is True, v2["checks"]
    assert v2["evidence"]["light_gate"] == {"from_recipe_md5": v1["recipe_md5"], "carried": ["route", "kiwix", "search"]}
    assert v2["checks"]["recipe_eq_merge"] and v2["checks"]["verify"] and v2["checks"]["zimcheck"]
    # compose_verdicts checks the light gate again: the fingerprint, and a base MD5 that changed
    compose = [sys.executable, str(TOOLS / "compose_verdicts.py"), str(out), str(base), "de"]
    for change, carried in (({}, True), ({"fingerprint": "0" * 64}, False), ({"base_md5": v2["base_md5"]}, False)):
        (out / "de.light.json").write_text(json.dumps({**v1, "base_md5": "f" * 32, **change}))
        subprocess.run(compose, capture_output=True, text=True, env=env)
        assert json.loads(vp.read_text())["checks"]["route"] is carried, change
    # a passing verdict of this base MD5 and fingerprint for another recipe (a merger change
    # that escaped the fingerprint): no light gate (B2-1)
    vp.write_text(json.dumps({**v2, "recipe_md5": "0" * 32}))
    r = run(env)
    assert "light gate" not in r.stdout and int(runs.read_text()) == 3, r.stdout
    # another title: another fingerprint, the full gate again
    _passed_by_hand(vp)
    env_t = {**env, "TITLES": json.dumps({"de": "Testland neu"})}
    r = run(env_t)
    assert "light gate" not in r.stdout and int(runs.read_text()) == 4
    # the pack rebuilt with other bytes (same name, same articles): its chunk hashes are in the
    # fingerprint, so the full gate again, though the last verdict passed
    _passed_by_hand(vp)
    lpzims.write_pack(de, "de", uuid, title="Testland Wiki (de) 2")
    assert tool("pack_gate.py", de, "--base", base, env={"PACK_GATE_ZIMCHECK": zc_pack}).returncode == 0
    r = run(env_t)
    assert "light gate" not in r.stdout and int(runs.read_text()) == 5, r.stdout
    # a base byte outside the viewer slots changed in place (same name and UUID, new MD5):
    # the base's chunk hashes are in the fingerprint, so the full gate (B2-2)
    _passed_by_hand(vp)
    _flip_a_non_slot_byte(base)
    assert tool("pack_gate.py", de, "--base", base, env={"PACK_GATE_ZIMCHECK": zc_pack}).returncode == 0
    r = run(env_t)
    assert "light gate" not in r.stdout and int(runs.read_text()) == 6, r.stdout


def _servers(log: Path) -> list[dict]:
    """kiwix-serve's lifetimes: start (with what it served) and end, by port and books."""
    starts = [json.loads(x) for x in log.read_text().splitlines()]
    ends = {(e["port"], tuple(e["books"])): e["end"]
            for e in (json.loads(x) for x in Path(str(log) + ".end").read_text().splitlines())}
    for s in starts:
        s["end"] = ends[(s["port"], tuple(s["books"]))]
    return starts


def _most_at_once(spans: list[dict]) -> int:
    ev = sorted([(s["start"], 1) for s in spans] + [(s["end"], -1) for s in spans], key=lambda x: (x[0], x[1]))
    n = best = 0
    for _, d in ev:
        n += d
        best = max(best, n)
    return best


def test_entries_are_checked_side_by_side(gates: SimpleNamespace, tmp_path: Path) -> None:
    """GATE_JOBS=2: two entries at a time, each served alone by its own
    kiwix-serve on its own port; never more than two merged files on disk,
    in LANGPACK_MERGED; the verdicts are the serial run's."""
    work = tmp_path / "work"
    merged = tmp_path / "nvme-merged"
    served = tmp_path / "served.jsonl"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de,th,en", "en", "testland",
                        str(gates.region), str(_free_port())], capture_output=True, text=True, timeout=1200,
                       env={**gates.env, "LANGPACK_WORK": str(work), "GATE_JOBS": "2", "LANGPACK_MERGED": str(merged),
                            "KIWIX_STUB_LOG": str(served), "NODE_STUB_SLEEP": "3"})
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    rows = _servers(served)
    entries = [x for x in rows if x["books"] != [gates.base.name]]
    assert sorted(x["books"][0] for x in entries) == sorted(f"m-{gates.tag}-{L}.zim" for L in ("de", "th", "en"))
    assert all(len(x["books"]) == 1 for x in rows)
    assert _most_at_once(entries) == 2, entries                    # two at a time, never three
    for a in rows:                                              # servers alive together listen on different ports
        for b in rows:
            if a is not b and a["start"] < b["end"] and b["start"] < a["end"]:
                assert a["port"] != b["port"], (a, b)
    assert max(len([f for f in x["merged_dir"] if f.endswith(".zim")]) for x in entries) <= 2
    assert {x["address"] for x in rows} == {"127.0.0.1"}
    assert merged.is_dir() and not list(merged.iterdir())             # the merged files were there, and are gone
    assert not (work / "merged").exists() or not list((work / "merged").iterdir())
    out = work / "gates" / gates.tag
    for L in ("de", "th", "en"):
        v = json.loads((work / "verdicts" / gates.tag / f"{L}.json").read_text())
        w = json.loads((gates.work / "verdicts" / gates.tag / f"{L}.json").read_text())
        assert v["checks"] == w["checks"] and v["recipe_md5"] == w["recipe_md5"] and v["all_pass"] == w["all_pass"], L
        assert (out / f"{L}.catalog.xml").exists() and (out / f"{L}.kiwix-serve.log").exists()


DF_ONE_JOB = r'''#!/bin/bash
# room for one merged file (2 x the base) and no more
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "x 999999999999 0 $(( $(stat -L -c %s "$DF_BASE") / 1024 * 3 )) 0% /"
'''


def test_an_entry_waits_while_there_is_room_only_for_the_running_ones(gates: SimpleNamespace, tmp_path: Path) -> None:
    """GATE_JOBS=3 on a disk with room for one merged file: one entry at a
    time, every one of them checked (none is skipped for want of room)."""
    work = tmp_path / "work"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "df").write_text(DF_ONE_JOB)
    (stubs / "df").chmod(0o755)
    served = tmp_path / "served.jsonl"
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de,th,en", "en", "testland",
                        str(gates.region), str(_free_port())], capture_output=True, text=True, timeout=1200,
                       env={**gates.env, "LANGPACK_WORK": str(work), "GATE_JOBS": "3", "KIWIX_STUB_LOG": str(served),
                            "NODE_STUB_SLEEP": "2", "PATH": f"{stubs}:{os.environ['PATH']}", "DF_BASE": str(gates.base)})
    assert r.returncode == 0 and "not run" not in r.stderr, r.stdout[-2000:] + r.stderr[-2000:]
    entries = [x for x in _servers(served) if x["books"] != [gates.base.name]]
    assert len(entries) == 3 and _most_at_once(entries) == 1, entries
    for L in ("de", "th", "en"):
        assert json.loads((work / "verdicts" / gates.tag / f"{L}.json").read_text())["checks"]["verify"] is True


@pytest.mark.parametrize("how", ["group", "run_gates"])
def test_a_stopped_run_leaves_no_kiwix_serve(gates: SimpleNamespace, tmp_path: Path, how: str) -> None:
    """Stopped while two entries are in a browser check (a command under GNU
    timeout, in a process group of its own), by a signal to its whole
    process group (timeout's) or to run_gates.sh alone: it ends at once, not
    when the check would have, and each job stops its kiwix-serve (which
    runs in a session of its own)."""
    import signal
    import time
    served, viewer = tmp_path / "served.jsonl", tmp_path / "viewer.log"
    p = subprocess.Popen([str(TOOLS / "run_gates.sh"), str(gates.base), str(gates.packs), "de,th,en", "en", "testland",
                          str(gates.region), str(_free_port())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={**gates.env, "LANGPACK_WORK": str(tmp_path / "work"), "GATE_JOBS": "2",
                              "KIWIX_STUB_LOG": str(served), "NODE_STUB_LOG": str(viewer), "NODE_STUB_SLEEP": "60"},
                         start_new_session=True)
    try:
        t0 = time.time()
        while (not viewer.exists() or len(viewer.read_text().splitlines()) < 2) and time.time() - t0 < 300:
            time.sleep(0.2)
        assert len(viewer.read_text().splitlines()) >= 2 and len(served.read_text().splitlines()) >= 2
        t1 = time.time()
        if how == "group":
            os.killpg(p.pid, signal.SIGTERM)
        else:
            os.kill(p.pid, signal.SIGTERM)
        p.wait(timeout=20)
        assert time.time() - t1 < 20
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
    t0 = time.time()
    while time.time() - t0 < 30:
        ends = Path(str(served) + ".end")
        if ends.exists() and len(ends.read_text().splitlines()) == len(served.read_text().splitlines()):
            break
        time.sleep(0.2)
    starts = served.read_text().splitlines()
    assert len(Path(str(served) + ".end").read_text().splitlines()) == len(starts), starts
