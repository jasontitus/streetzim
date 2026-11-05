"""The builder page's region list (web/build/) names each map by its title
alone, a map with search with a photo too; the note on the picked region
says whether it has photo search."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.langpack.conftest import ROOT
from tests.langpack.test_builder_empty import SCRIPT

NEED = ("NODE", "CHROME_PATH", "PLAYWRIGHT_CORE")
if not all(os.environ.get(k) and os.path.exists(os.environ[k]) for k in NEED):
    pytest.skip(f"the browser stack: set {', '.join(NEED)} (the build host's settings: docs/language-pack-pipeline.md)", allow_module_level=True)


def _swap(s: str, old: str, new: str) -> str:
    assert s.count(old) == 1, old
    return s.replace(old, new)


# the empty-page test's script, opening /build/<argv[4]> and reporting the region list and the note
LIST = SCRIPT
LIST = _swap(LIST, "/build/`);", "/build/${process.argv[4] || ''}`);")
LIST = _swap(LIST, "document.querySelector('#notice').textContent.length > 0",
             "document.querySelector('#baseInfo').textContent.length > 0")
LIST = _swap(LIST, "notice: await pg.$eval('#notice', (e) => e.textContent)",
             "options: await pg.$$eval('#region option', (o) => o.map((e) => [e.value, e.textContent]))")
LIST = _swap(LIST, "oneclick: await pg.$eval('#oneclick', (e) => e.disabled)",
             "note: await pg.$eval('#baseInfo', (e) => e.textContent)")


def _region(rid: str, title: str, photo: bool) -> dict:
    return {"id": rid, "title": title, "photo_search": photo,
            "base": {"file": f"osm-{rid}.zim", "size": 1000000, "download": f"https://example.invalid/osm-{rid}.zim"}}


def _page(tmp_path: Path, query: str) -> dict:
    rec = tmp_path / "recipes"
    rec.mkdir(exist_ok=True)
    (rec / "index.json").write_text(json.dumps({"regions": [_region("kyiv", "Kyiv", True), _region("athens", "Athens", False)]}))
    script = tmp_path / "list.mjs"
    script.write_text(LIST)
    r = subprocess.run([os.environ["NODE"], str(script), str(ROOT / "web"), str(rec), query], capture_output=True,
                       text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("query, rid, photo", [("", "athens", False), ("?region=kyiv", "kyiv", True)])
def test_the_region_list_names_each_map_by_its_title(tmp_path: Path, query: str, rid: str, photo: bool) -> None:
    out = _page(tmp_path, query)
    assert out["options"] == [["athens", "Athens"], ["kyiv", "Kyiv"]], out
    assert f"osm-{rid}.zim" in out["note"], out
    assert ("search with a photo" in out["note"]) is photo, out
