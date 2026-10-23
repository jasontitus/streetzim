"""'Preview in browser' for a map's language version: /drive/ streams the
merged file from the build worker's /merged/ URL and the viewer shows it in
that language (tools/langpack/drive_preview_e2e.mjs), against local servers
only: the worker on Node (build-proxy/serve-local.mjs), stand-ins for
archive.org and the site (webseed_e2e.ranged_server) and web/ served by
scripts/serve-web-local.py with a preview-config.js naming that worker.
No request leaves 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.langpack.conftest import ROOT
from tests.langpack.test_ship_packs import _run, ship  # noqa: F401
from tools.langpack.webseed_e2e import free_port, ranged_server

NEED = ("NODE", "CHROME_PATH", "PLAYWRIGHT_CORE")
if not all(os.environ.get(k) and os.path.exists(os.environ[k]) for k in NEED):
    pytest.skip(f"the browser stack: set {', '.join(NEED)} (ops/langpack/host-env.sh)", allow_module_level=True)

WS = "https://streetzim-build-proxy.example.workers.dev"


def _wait_port(port: int, secs: float = 20) -> None:
    t0 = time.time()
    while time.time() - t0 < secs:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"port {port} never came up")


@pytest.fixture
def preview(ship: SimpleNamespace, tmp_path: Path):  # noqa: F811
    r = _run(ship, LANGPACK_WEBSEED=WS)
    assert r.returncode == 0, r.stdout[-2000:]
    site = ship.work / "site" / "testland" / "recipes" / "testland"
    packs = ship.work / "packs" / ship.base.name[:-4]
    files = {f"streetzim-testland/{ship.base.name}": str(ship.base)}
    files |= {f"streetzim-testland-lang/{p.name}": str(p) for p in packs.glob("*.zim")}
    counter = {"download": 0, "node": 0, "site": 0}
    stand = ranged_server(files, str(site), "testland", counter)
    sport = stand.server_address[1]
    wport, wport_web = free_port(), free_port()
    wlog = tmp_path / "worker.log"
    worker = subprocess.Popen([os.environ["NODE"], str(ROOT / "build-proxy" / "serve-local.mjs"), str(wport)],
                              env={**os.environ, "SITE_ORIGIN": f"http://127.0.0.1:{sport}",
                                   "UPSTREAM": f"http://127.0.0.1:{sport}/download/", "SZ_LOG": "1", "SZ_STATS": "1"},
                              stdout=open(wlog, "w"), stderr=subprocess.STDOUT)
    web = tmp_path / "web"
    shutil.copytree(ROOT / "web" / "drive", web / "drive")
    (web / "drive" / "preview-config.js").write_text(
        "window.STREETZIM_PREVIEW_PROXY = '';\n"
        f"window.STREETZIM_MERGED_ORIGIN = 'http://127.0.0.1:{wport}';\n")
    server = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "serve-web-local.py"), str(wport_web), str(web)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(wport)
        _wait_port(wport_web)
        menu = json.loads((site / "region.json").read_text())["menu"]
        yield SimpleNamespace(site=f"http://127.0.0.1:{wport_web}", worker=f"http://127.0.0.1:{wport}",
                              wlog=wlog, menu={m["lang"]: m for m in menu}, counter=counter)
    finally:
        worker.terminate()
        server.terminate()
        stand.shutdown()
        worker.wait(10)
        server.wait(10)


def _local(p: SimpleNamespace, lang: str) -> str:
    """The entry's webseed URL with the local worker for its origin."""
    return p.worker + urllib.parse.urlparse(p.menu[lang]["webseed"]).path


def _e2e(p: SimpleNamespace, *args: str) -> dict:
    r = subprocess.run([os.environ["NODE"], str(ROOT / "tools" / "langpack" / "drive_preview_e2e.mjs"),
                        "--site", p.site, *args], capture_output=True, text=True, timeout=600)
    rows = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    assert rows, r.stdout[-2000:] + r.stderr[-2000:]
    return rows[-1]


def _log(p: SimpleNamespace) -> list[dict]:
    return [json.loads(x) for x in p.wlog.read_text().splitlines() if x.startswith('{"log"')]


def test_drive_streams_a_language_version_and_shows_its_language(preview: SimpleNamespace) -> None:
    url = _local(preview, "de")
    other = url.replace("127.0.0.1", "127.0.0.2", 1)                 # the same path on another origin
    res = _e2e(preview, "--zim", url, "--lang", "de",
               "--article", "~w/de/wiki-article/Alpha_Stadt", "--article-text", "Alpha Stadt",
               "--refuse", other, "--refuse", preview.worker + "/merged/x.zim", "--refuse", url + "?a=1",
               "--refuse", preview.worker + "/streetzim-testland/x.zim")      # the preview proxy's path shape
    assert res["pass"], res
    assert res["ui_lang"]["ui"] == "de" and res["map_lang"]["language"] == "de", res
    log = _log(preview)
    merged = [x for x in log if x["path"].startswith("/merged/testland/de/")]
    assert merged and all(x["status"] in (200, 204, 206) for x in merged), merged[:5]
    assert not [x for x in log if "/merged/x.zim" in x["path"] or x["path"].startswith("/streetzim-")], log[:10]


def test_a_refused_url_is_never_fetched(preview: SimpleNamespace) -> None:
    url = _local(preview, "fr")
    res = _e2e(preview, "--zim", url, "--lang", "fr",
               "--refuse", url.replace("/merged/", "/merged//"), "--refuse", url.replace(".zim", ".zim#x"),
               "--refuse", url.replace("http://", "http://u:p@"), "--refuse", url.replace("http://", "HTTP://"))
    assert res["checks"]["refused"], res["refused"]
    assert res["pass"], res
