"""compose_verdicts.py, make_site.py and site_check.py on the small ZIMs:
gate outputs -> verdicts -> builder-page site files -> the pre-deploy check
(against archive.org-style item metadata in a cache directory). make_site
publishes only all-PASS entries over gated packs; site_check fails every
doctored copy of the site or of the verdicts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import tool  # noqa: E402

LANGS = "de,fr,th,en"
TITLES = json.dumps({"de": "Testland DE", "fr": "Testland FR", "th": "Testland TH", "en": "Testland"})
DL, PX = "https://dl.example", "https://proxy.example"


def _gate_outputs(out: Path, menu_jsonl: str, langs: list[str], fail: dict[str, str] | None = None) -> None:
    """What run_gates.sh leaves for compose_verdicts.py, every check passing
    unless `fail` names one (lang -> check)."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "menu.jsonl").write_text(menu_jsonl)
    files = {"same": "PASS\n", "verify.log": "SUMMARY 22/22 PASS\n",
             "zimcheck.log": "[INFO] Overall Test Status: Pass\n", "route.log": "route OK 1.2 km\n",
             "kiwix.json": '{"pass": true}\n', "viewer.json": '{"pass": true}\n', "ui.json": '{"pass": true}\n',
             "smoke.log": "12 passed, 0 failed\n"}
    for L in langs:
        for k, v in files.items():
            (out / f"{L}.{k}").write_text(v)
    for k in ("smoke.log", "viewer.json", "ui.json"):
        (out / f"base.{k}").write_text(files[k])
    bad = {"same": "FAIL\n", "verify.log": "SUMMARY 21/22 PASS\n", "zimcheck.log": "Overall Test Status: Fail\n",
           "route.log": "no route\n", "kiwix.json": '{"pass": false}\n', "viewer.json": '{"pass": false}\n',
           "ui.json": '{"pass": false}\n', "smoke.log": "11 passed, 1 failed\n"}
    for L, k in (fail or {}).items():
        (out / f"{L}.{k}").write_text(bad[k])


@pytest.fixture(scope="module")
def site(lp: SimpleNamespace, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    d = tmp_path_factory.mktemp("site")
    work = d / "work"
    env = {"LANGPACK_WORK": str(work)}
    packs = d / "packs"
    packs.mkdir()
    zc = lpzims.fake_zimcheck(d / "zc", "clean", None)
    for p in lp.packs.values():
        shutil.copy(p, packs / p.name)
        r = tool("pack_gate.py", packs / p.name, "--base", lp.base, env={**env, "PACK_GATE_ZIMCHECK": zc})
        assert r.returncode == 0, r.stdout + r.stderr
    tag = lp.base.name[:-4]
    r = tool("recipe2.py", "menu", lp.base, "--packs-dir", packs, "--langs", LANGS, "--region-langs", "en",
             "--region", "testland", "--titles", TITLES, "--language-meta", "map",
             "--out-dir", work / "recipes" / tag, env=env)
    assert r.returncode == 0, r.stderr
    gates = d / "gates"
    _gate_outputs(gates, r.stdout, LANGS.split(","))
    r = tool("compose_verdicts.py", gates, lp.base, LANGS, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    out = d / "site"
    r = tool("make_site.py", lp.base, "--packs-dir", packs, "--region", "testland", "--region-title", "Testland",
             "--region-langs", "en", "--langs", LANGS, "--titles", TITLES, "--language-meta", "map",
             "--url-template", f"{DL}/testland-packs/{{file}}", "--url-override", f"fr={DL}/testland-fr/{{file}}",
             "--base-download", f"{DL}/testland-base/{lp.base.name}",
             "--base-url", f"{PX}/testland-base/{lp.base.name}", "-o", out / "recipes", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    (out / "region-base-url.json").write_text(json.dumps({"url": f"{PX}/testland-base/{lp.base.name}"}))
    ia = d / "ia"
    ia.mkdir()
    items = {"testland-packs": [packs / lp.packs[x].name for x in ("de", "th")],
             "testland-fr": [packs / lp.packs["fr"].name], "testland-base": [lp.base]}
    for item, files in items.items():
        meta = {"metadata": {"identifier": item},
                "files": [{"name": f.name, "size": str(f.stat().st_size), "md5": lpzims.md5_of(f)} for f in files]}
        (ia / f"ia-{item}.json").write_text(json.dumps(meta))
    return SimpleNamespace(dir=d, work=work, env=env, packs=packs, gates=gates, out=out, ia=ia, tag=tag,
                           base=lp.base)


def _check(s: SimpleNamespace, site_dir: Path, verdicts: Path | None = None) -> tuple[int, list[str]]:
    args = [site_dir, "--base", s.base, "--packs-dir", s.packs, "--ia-cache", s.ia]
    if verdicts:
        args += ["--verdicts", verdicts]
    r = tool("site_check.py", *args, env=s.env)
    return r.returncode, [ln for ln in r.stdout.splitlines() if ln.startswith(("FAIL", "SUMMARY"))]


def test_verdicts_and_site(site: SimpleNamespace, lp: SimpleNamespace) -> None:
    v = json.loads((site.work / "verdicts" / site.tag / "de.json").read_text())
    assert v["all_pass"] and v["recipe_md5"] == json.loads((site.out / "recipes" / "de.json").read_text())["md5"]
    idx = json.loads((site.out / "recipes" / "index.json").read_text())
    menu = idx["regions"][0]["menu"]
    assert [m["lang"] for m in menu] == LANGS.split(",")
    assert json.loads((site.out / "recipes" / "refused.json").read_text()) == []
    assert menu[0]["gate_verdict"] == f"verdicts/{site.tag}/de.json"
    fr = json.loads((site.out / "recipes" / "fr.json").read_text())
    assert fr["packs"]["fr"]["url"] == f"{DL}/testland-fr/{lp.packs['fr'].name}"
    assert next(m for m in menu if m["lang"] == "en")["download_bytes"] == 0


def test_site_check_passes(site: SimpleNamespace) -> None:
    rc, lines = _check(site, site.out)
    assert rc == 0, lines
    assert lines[-1].endswith("-> OK")


def test_failed_check_means_no_verdict_pass(site: SimpleNamespace, lp: SimpleNamespace, tmp_path: Path) -> None:
    gates = tmp_path / "gates"
    _gate_outputs(gates, (site.gates / "menu.jsonl").read_text(), LANGS.split(","), fail={"th": "ui.json"})
    env = {"LANGPACK_WORK": str(tmp_path / "work")}
    shutil.copytree(site.work / "recipes", tmp_path / "work" / "recipes")
    r = tool("compose_verdicts.py", gates, lp.base, LANGS, env=env)
    assert r.returncode == 0
    vd = tmp_path / "work" / "verdicts" / site.tag
    th = json.loads((vd / "th.json").read_text())
    assert th["all_pass"] is False and th["checks"]["ui"] is False
    assert json.loads((vd / "de.json").read_text())["all_pass"] is True
    # make_site then refuses th and keeps the others
    out = tmp_path / "site"
    r = tool("make_site.py", lp.base, "--packs-dir", site.packs, "--region", "testland", "--region-title", "Testland",
             "--region-langs", "en", "--langs", LANGS, "--titles", TITLES, "--language-meta", "map",
             "--url-template", f"{DL}/testland-packs/{{file}}", "--base-download", f"{DL}/b/{lp.base.name}",
             "-o", out / "recipes", env=env)
    assert r.returncode == 0, r.stderr
    refused = json.loads((out / "recipes" / "refused.json").read_text())
    assert [x["lang"] for x in refused] == ["th"] and "ui" in refused[0]["why"]
    assert not (out / "recipes" / "th.json").exists()


def test_make_site_skips_an_ungated_pack(site: SimpleNamespace, lp: SimpleNamespace, tmp_path: Path) -> None:
    packs = tmp_path / "packs"
    shutil.copytree(site.packs, packs)
    g = packs / f"{lp.packs['th'].name}.gate.json"
    d = json.loads(g.read_text())
    d["ok"] = False
    g.write_text(json.dumps(d))
    out = tmp_path / "site"
    r = tool("make_site.py", lp.base, "--packs-dir", packs, "--region", "testland", "--region-title", "Testland",
             "--region-langs", "en", "--langs", "th", "--titles", TITLES, "--language-meta", "map",
             "--url-template", f"{DL}/p/{{file}}", "--base-download", f"{DL}/b/{lp.base.name}",
             "-o", out / "recipes", env=site.env)
    refused = json.loads((out / "recipes" / "refused.json").read_text())
    assert r.returncode == 0
    assert any(x.get("pack") == lp.packs["th"].name for x in refused)
    assert any(x.get("lang") == "th" and "missing packs" in x["why"] for x in refused)


def _jedit(path: Path, fn) -> None:  # noqa: ANN001
    d = json.loads(path.read_text())
    fn(d)
    path.write_text(json.dumps(d, ensure_ascii=False))


def _base_item(d: dict) -> None:
    b = d["regions"][0]["base"] if "regions" in d else d["base"]
    b["download"] = b["download"].replace("/testland-base/", "/testland-packs/")


def _inline(d: dict) -> None:
    seg = next(x for x in d["segments"] if x["src"] == "inline" and x["len"] > 8)
    seg["data"] = ("A" if seg["data"][0] != "A" else "B") + seg["data"][1:]


MUTANTS = {
    "pack_sha": ("recipes/fr.json", lambda d: d["packs"]["fr"]["sha256"].__setitem__(0, "0" * 64)),
    "pack_item": ("recipes/de.json", lambda d: d["packs"]["de"].__setitem__(
        "url", d["packs"]["de"]["url"].replace("/testland-packs/", "/testland-fr/"))),
    "index_md5": ("recipes/index.json", lambda d: d["regions"][0]["menu"][1].__setitem__("md5", "0" * 32)),
    "region_json": ("recipes/region.json", lambda d: d["base"].__setitem__("size", 1)),
    "base_item": ("recipes/index.json+region.json", _base_item),
    "base_uuid": ("recipes/th.json", lambda d: d["base"].__setitem__("uuid", "0" * 32)),
    "inline": ("recipes/th.json", _inline),
    "verdict_fail": ("verdicts/fr.json", lambda d: (d.__setitem__("all_pass", False),
                                                    d["checks"].__setitem__("ui", False))),
    "verdict_other": ("verdicts/th.json", lambda d: d.__setitem__("recipe_md5", "0" * 32)),
    "base_url_side": ("region-base-url.json", lambda d: d.__setitem__("url", "https://example.invalid/x.zim")),
    "refused": ("recipes/refused.json", None),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_site_check_kills(name: str, site: SimpleNamespace, tmp_path: Path) -> None:
    s, v = tmp_path / "site", tmp_path / "verdicts"
    shutil.copytree(site.out, s)
    shutil.copytree(site.work / "verdicts" / site.tag, v)
    where, fn = MUTANTS[name]
    if name == "refused":
        (s / where).write_text(json.dumps([{"lang": "fr", "why": "test"}]))
    elif where == "recipes/index.json+region.json":
        _jedit(s / "recipes" / "index.json", fn)
        _jedit(s / "recipes" / "region.json", fn)
    elif where.startswith("verdicts/"):
        _jedit(v / where.split("/", 1)[1], fn)
    else:
        _jedit(s / where, fn)
    rc, lines = _check(site, s, v)
    assert rc != 0 and any(ln.startswith("FAIL") for ln in lines), lines
