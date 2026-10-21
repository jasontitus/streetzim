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
import http.server, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
books = [a for a in sys.argv[1:] if a.endswith(".zim")]
xml = "<feed>" + "".join(f"<entry><title>x</title><link href=\"/content/{b.rsplit('/', 1)[-1][:-4].lower()}\"/></entry>"
                         for b in books) + "</feed>"
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/catalog/v2/entries"):
            body = xml.encode(); self.send_response(200); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

NODE_STUB = r'''#!/bin/bash
# stand-in for node: the gate scripts must exist where run_gates.sh says
[ -f "$1" ] || { echo "no such script: $1" >&2; exit 1; }
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
                                  "route": {"src": "10.0,20.0", "dst": "10.1,20.1"}}))
    work = d / "work"
    env = {**os.environ, "LANGPACK_WORK": str(work), "PYTHON": sys.executable, "KIWIX_SERVE": str(ks),
           "NODE": str(node), "PACK_GATE_ZIMCHECK": zc, "LANGUAGE_META": "map",
           "TITLES": json.dumps({"de": "Testland DE", "th": "Testland TH"}), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([str(TOOLS / "run_gates.sh"), str(lp.base), str(packs), "de,th,en", "en", "testland",
                        str(region), str(_free_port())], capture_output=True, text=True, timeout=1200, env=env)
    return SimpleNamespace(r=r, work=work, tag=lp.base.name[:-4], env=env, packs=packs, region=region, base=lp.base)


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
    assert v["zimcheck_version"] == "zim-tools 3.8.0"
    title = {"de": "Testland DE", "th": "Testland TH"}.get(lang)
    merged = json.loads((out / f"{lang}.merge.json").read_text())
    assert merged["metadata"]["Title"] == (title or "OSM - Testland")
    assert (gates.work / "merged" / f"m-{gates.tag}-{lang}.zim").exists()
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
