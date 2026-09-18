"""Live download cards get a Preview button only when the online preview
is switched on — i.e. when web/drive/preview-config.js names the range
proxy the /drive/ picker streams archive.org ZIMs through
(docs/online-preview.md). Without the proxy the button would open a page
that can only report "not configured".
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["gen"] = gen
spec.loader.exec_module(gen)

REGION = {"id": "washington-dc", "title": "Washington, D.C.",
          "zim_file": "osm-washington-dc-2026-09-08.zim",
          "description": "The capital.", "tier": "local"}


def test_proxy_url_is_read_from_the_picker_config(tmp_path):
    cfg = tmp_path / "preview-config.js"
    cfg.write_text("// comment\nwindow.STREETZIM_PREVIEW_PROXY = 'https://p.example.workers.dev/';\n")
    assert gen.preview_proxy_url(cfg) == "https://p.example.workers.dev/"
    cfg.write_text('window.STREETZIM_PREVIEW_PROXY = "";\n')
    assert gen.preview_proxy_url(cfg) == ""
    assert gen.preview_proxy_url(tmp_path / "missing.js") == ""


def test_committed_config_parses():
    # Whatever value is committed, the parser must understand the file.
    assert isinstance(gen.preview_proxy_url(), str)


def test_card_without_preview_has_no_button():
    html = gen.render_live_card(REGION, "216 MB")
    assert 'data-track="preview"' not in html
    assert "/drive/?zim=" not in html


def test_preview_button_opens_the_picker_on_the_download_url():
    html = gen.render_live_card(REGION, "216 MB", preview=True)
    assert ('href="/drive/?zim=https://archive.org/download/'
            'streetzim-washington-dc/osm-washington-dc-2026-09-08.zim"') in html
    assert 'data-track="preview"' in html
    # Download stays the primary action; Preview sits right after it.
    assert html.index('data-track="download"') < html.index('data-track="preview"')
    assert html.index('data-track="preview"') < html.index('data-track="details"')


def test_preview_url_escapes_odd_filenames():
    region = {**REGION, "zim_file": "osm-a&b 2026.zim"}
    html = gen.render_live_card(region, "1 MB", preview=True)
    assert "/drive/?zim=https://archive.org/download/streetzim-washington-dc/osm-a%26b%202026.zim" in html
