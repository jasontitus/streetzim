"""The builder page (web/build/) with no recipes published yet: no
recipes/index.json at all (404) or one listing no region. The page says
"No maps yet" instead of "Could not load the list of maps (HTTP 404)"."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.langpack.conftest import ROOT

NEED = ("NODE", "CHROME_PATH", "PLAYWRIGHT_CORE")
if not all(os.environ.get(k) and os.path.exists(os.environ[k]) for k in NEED):
    pytest.skip(f"the browser stack: set {', '.join(NEED)} (the build host's settings: docs/language-pack-pipeline.md)", allow_module_level=True)

SCRIPT = r"""
import http from 'node:http'; import fs from 'node:fs'; import path from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_CORE);
const [WEB, RECIPES] = process.argv.slice(2);
if (process.env.BROWSER_LIBS) process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS;
const types = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.css': 'text/css' };
const srv = http.createServer((req, res) => {
  const p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  const f = p.startsWith('/build/recipes/') ? path.join(RECIPES, p.slice(15)) : path.join(WEB, p.endsWith('/') ? p + 'index.html' : p);
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': types[path.extname(f)] || 'application/octet-stream' }); res.end(fs.readFileSync(f));
});
await new Promise((r) => srv.listen(0, '127.0.0.1', r));
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true });
const pg = await browser.newPage();
await pg.goto(`http://127.0.0.1:${srv.address().port}/build/`);
await pg.waitForFunction(() => document.querySelector('#notice').textContent.length > 0, null, { timeout: 30000 }).catch(() => {});
console.log(JSON.stringify({ notice: await pg.$eval('#notice', (e) => e.textContent),
                             oneclick: await pg.$eval('#oneclick', (e) => e.disabled) }));
await Promise.race([browser.close(), new Promise((r) => setTimeout(r, 10000))]);
srv.close(); process.exit(0);
"""


@pytest.mark.parametrize("index", [None, {"regions": []}])
def test_no_recipes_yet_says_no_maps(index: dict | None, tmp_path: Path) -> None:
    rec = tmp_path / "recipes"
    rec.mkdir()
    if index is not None:
        (rec / "index.json").write_text(json.dumps(index))
    script = tmp_path / "empty.mjs"
    script.write_text(SCRIPT)
    r = subprocess.run([os.environ["NODE"], str(script), str(ROOT / "web"), str(rec)], capture_output=True, text=True,
                       timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["notice"].startswith("No maps yet"), out
    assert out["oneclick"] is True, out
