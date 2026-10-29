"""web/generate.py and a map with today's viewer (tools/langpack/
fresh_viewer.py, docs/fresh-viewer.md): the card's Download is the build
worker's file only while build/recipes/<region>/fresh.json was made from
the very file archive.org serves (name and MD5), its index is published
beside it and its URL is the worker's; archive.org's file is the fallback.
Anything else: the card is as before."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "https://streetzim-build-proxy.example.workers.dev"
UUID = "34353f40-ff08-5e39-8495-a0a3c5bcd50d"
MD5 = "495f1645933ca9b53461f9b8bb5e56b9"
LIVE = "osm-bahrain-2026-10-28.zim"
FRESH = "osm-bahrain-2026-10-28.v5590ad0.zim"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_fresh", ROOT / "web" / "generate.py")
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["generate.py"]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


def publish(d: Path, **over) -> dict:
    fr = {"format": "streetzim-fresh-viewer/1", "region": "bahrain", "site_region": "bahrain-viewer", "lang": "en", "viewer": "5590ad0",
          "base": {"item": "streetzim-bahrain", "file": LIVE, "size": 47218674, "md5": MD5},
          "file": FRESH, "uuid": UUID, "webseed": f"{ORIGIN}/merged/bahrain-viewer/en/{UUID}/{FRESH}"}
    fr.update(over)
    (d / "bahrain-viewer").mkdir(parents=True, exist_ok=True)
    (d / "bahrain-viewer" / "fresh.json").write_text(json.dumps(fr))
    (d / "bahrain-viewer" / "en.ws.json").write_text("{}")
    return fr


def test_the_fresh_file_while_it_was_made_from_the_live_one(gen, tmp_path):
    fr = publish(tmp_path)
    got = gen.fresh_viewer("bahrain", LIVE, MD5.upper(), recipes_dir=str(tmp_path), origin=ORIGIN)
    assert got == {"url": fr["webseed"], "file": FRESH, "viewer": "5590ad0"}
    # a newer map is live (or the same name re-uploaded): archive.org's file again
    assert gen.fresh_viewer("bahrain", "osm-bahrain-2026-11-02.zim", MD5, recipes_dir=str(tmp_path), origin=ORIGIN) is None
    assert gen.fresh_viewer("bahrain", LIVE, "f" * 32, recipes_dir=str(tmp_path), origin=ORIGIN) is None
    assert gen.fresh_viewer("bahrain", LIVE, None, recipes_dir=str(tmp_path), origin=ORIGIN) is None
    assert gen.fresh_viewer("iceland", LIVE, MD5, recipes_dir=str(tmp_path), origin=ORIGIN) is None


@pytest.mark.parametrize("over", [
    {"webseed": f"https://elsewhere.example/merged/bahrain-viewer/en/{UUID}/{FRESH}"},  # not the worker
    {"webseed": f"{ORIGIN}/merged/iceland-viewer/en/{UUID}/{FRESH}"},                  # another region's
    {"webseed": f"{ORIGIN}/merged/bahrain/de/{UUID}/{FRESH}"},                         # a language version's
    {"webseed": f"{ORIGIN}/merged/bahrain-viewer/en/{UUID}/other.zim"},                # another file
    {"webseed": f"{ORIGIN}/merged/bahrain-viewer/en/{UUID}/{FRESH}?x=1"},
    {"format": "streetzim-fresh-viewer/0"}, {"region": "iceland"},
])
def test_anything_else_is_no_fresh_file(gen, tmp_path, over):
    publish(tmp_path, **over)
    assert gen.fresh_viewer("bahrain", LIVE, MD5, recipes_dir=str(tmp_path), origin=ORIGIN) is None


def test_no_index_beside_it_no_fresh_file(gen, tmp_path):
    publish(tmp_path)
    (tmp_path / "bahrain-viewer" / "en.ws.json").unlink()
    assert gen.fresh_viewer("bahrain", LIVE, MD5, recipes_dir=str(tmp_path), origin=ORIGIN) is None


def test_the_site_names_the_worker(gen):
    assert gen.merged_origin().startswith("https://") and "workers.dev" in gen.merged_origin()


def test_the_card_downloads_the_fresh_file_with_archive_org_as_fallback(gen):
    region = {"id": "bahrain", "title": "Bahrain", "zim_file": LIVE, "description": "Bahrain.", "tier": "country"}
    url = f"{ORIGIN}/merged/bahrain-viewer/en/{UUID}/{FRESH}"
    html = gen.render_live_card(region, "45 MB", fresh={"url": url, "file": FRESH, "viewer": "5590ad0"})
    assert f'href="{url}" data-fallback="https://archive.org/download/streetzim-bahrain/{LIVE}"' in html
    assert 'data-track="download"' in html
    plain = gen.render_live_card(region, "45 MB")
    assert f'href="https://archive.org/download/streetzim-bahrain/{LIVE}" data-track="download"' in plain
    assert "data-fallback" not in plain


def test_the_page_falls_back_to_archive_org():
    page = (ROOT / "web" / "template.html").read_text(encoding="utf-8")
    assert "a[data-fallback]" in page and "method: 'HEAD'" in page and "data-fallback" in page
