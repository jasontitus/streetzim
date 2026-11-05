"""The "Back to map" bar of a bundled Wikipedia article in the map's
language (cloud/wiki_articles.py: back_to_map_bar, back_bar_script).

The build writes the bar in English and, right after it, a script stored
next to that Wikipedia's articles (sz-back.js at the map's root, or a
pack's ~w/<xx>/sz-back.js) that rewords it from map-config.json
"language", since one page serves every language version of a map. The
script says what web/drive/sw.js says for the same language, and the
service worker still finds the English bar to reword in the PWA."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cloud import wiki_articles as wa

ROOT = Path(__file__).resolve().parent.parent
SW = (ROOT / "web" / "drive" / "sw.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _sw_object(name: str) -> str:
    m = re.search(r"const " + name + r" = (\{.*?\});", SW, re.S)
    assert m, name
    return m.group(1)


def test_the_table_is_the_service_workers():
    js = ("const W = " + _sw_object("BACK_TO_MAP") + ", R = " + _sw_object("BACK_TO_MAP_RTL")
          + ", F = " + _sw_object("BACK_TO_MAP_FALLBACK")
          + ";\nconsole.log(JSON.stringify({w: W, r: Object.keys(R).sort(), f: F}));")
    if NODE is None:
        pytest.skip("node not installed")
    got = json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                                    check=True, timeout=60).stdout)
    assert got["w"] == wa.BACK_TO_MAP
    assert got["r"] == sorted(wa.BACK_TO_MAP_RTL)
    assert got["f"] == wa.BACK_TO_MAP_FALLBACK
    tables = sorted(p.stem for p in (ROOT / "resources/viewer/i18n").glob("*.json") if p.stem != "en")
    assert sorted(wa.BACK_TO_MAP) == tables


@pytest.mark.parametrize("title,depth,script,index", [
    ("Alpha", 0, "../sz-back.js", "../index.html"),
    ("AC/DC", 0, "../../sz-back.js", "../../index.html"),
    ("Alpha", 2, "../sz-back.js", "../../../index.html"),       # a pack: its own copy
    ("AC/DC", 2, "../../sz-back.js", "../../../../index.html"),
])
def test_the_bar_loads_the_script_beside_its_wikipedia(title, depth, script, index):
    bar = wa.back_to_map_bar(title, depth)
    assert bar.startswith(f'<nav class="sz-back"><a href="{index}" ')
    assert bar.endswith(f'&#8592; Back to map</a></nav><script src="{script}"></script>')
    # What the PWA's service worker looks for to reword it (sw.js withBackToMap).
    sw = re.search(r"html\.replace\(/(\(<nav class=\"sz-back\"><a .*?)/,", SW).group(1)
    assert re.search(sw.replace("<\\/a>", "</a>"), bar), sw


RUN = r"""
const [js, src, cfg] = [process.argv[1], process.argv[2], process.argv[3]];
const fetched = [];
const a = { textContent: '← Back to map', children: [],
            appendChild(c) { this.children.push(c); } };
const nav = { className: 'sz-back', querySelector: (q) => (q === 'a' ? a : null) };
globalThis.document = {
  currentScript: { src, previousElementSibling: nav },
  createElement: (t) => ({ tag: t, attrs: {}, textContent: '',
                           setAttribute(k, v) { this.attrs[k] = v; } }),
};
globalThis.fetch = (u) => { fetched.push(u); return Promise.resolve(cfg === '404'
  ? { ok: false, json: () => Promise.reject(new Error('404')) }
  : { ok: true, json: () => Promise.resolve(JSON.parse(cfg)) }); };
(0, eval)(js);
setTimeout(() => console.log(JSON.stringify({ fetched, text: a.textContent,
  span: a.children.map((c) => [c.tag, c.attrs.dir, c.textContent]) })), 50);
"""


def _run(src: str, cfg: str) -> dict:
    if NODE is None:
        pytest.skip("node not installed")
    r = subprocess.run([NODE, "-e", RUN, wa.back_bar_script().decode(), src, cfg],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("src,root", [
    ("http://127.0.0.1:8080/content/osm-x/sz-back.js", "http://127.0.0.1:8080/content/osm-x/"),
    ("http://127.0.0.1:8080/content/osm-x/~w/hi/sz-back.js", "http://127.0.0.1:8080/content/osm-x/"),
    ("http://127.0.0.1:8080/content/osm-x/%7Ew/zh-yue/sz-back.js", "http://127.0.0.1:8080/content/osm-x/"),
    ("zim://3f2a/sz-back.js?x=1", "zim://3f2a/"),
    ("https://streetzim.web.app/drive/viewer/~w/bn/sz-back.js", "https://streetzim.web.app/drive/viewer/"),
])
def test_the_script_reads_the_maps_config_at_its_root(src, root):
    got = _run(src, json.dumps({"language": "hi"}))
    assert got["fetched"] == [root + "map-config.json"]
    assert got["text"] == "← " + wa.BACK_TO_MAP["hi"]


def test_the_script_says_what_the_service_worker_says():
    if NODE is None:
        pytest.skip("node not installed")
    sw = subprocess.run([NODE, "-e", "const BACK_TO_MAP = " + _sw_object("BACK_TO_MAP")
                         + ", BACK_TO_MAP_RTL = " + _sw_object("BACK_TO_MAP_RTL")
                         + ", BACK_TO_MAP_FALLBACK = " + _sw_object("BACK_TO_MAP_FALLBACK")
                         + ", BACK_TO_MAP_EN = '&#8592; Back to map';\n"
                         + re.search(r"function backToMapText\(lang\) \{.*?\n\}", SW, re.S).group(0)
                         + "\nconsole.log(JSON.stringify(process.argv.slice(1).map(backToMapText)));",
                         "hi", "AR", "gsw", "de-CH", "en", "qu", ""],
                        capture_output=True, text=True, check=True, timeout=60)
    want = json.loads(sw.stdout)
    for lang, w in zip(["hi", "AR", "gsw", "de-CH", "en", "qu", ""], want):
        got = _run("http://h/c/sz-back.js", json.dumps({"language": lang}))
        if w == "&#8592; Back to map":                      # English, or no table: as built
            assert got == {"fetched": ["http://h/c/map-config.json"],
                           "text": "← Back to map", "span": []}, lang
        elif w.startswith('<span dir="rtl">&#8594; '):
            words = w[len('<span dir="rtl">&#8594; '):-len("</span>")]
            assert got["text"] == "" and got["span"] == [["span", "rtl", "→ " + words]], lang
        else:
            assert got["text"] == "← " + w[len("&#8592; "):] and got["span"] == [], lang


@pytest.mark.parametrize("cfg", ["404", "{}", '{"language": 7}', "[]"])
def test_without_a_language_the_bar_stays_english(cfg):
    assert _run("http://h/c/sz-back.js", cfg)["text"] == "← Back to map"
