"""compose_verdicts.py, make_site.py and site_check.py on the small ZIMs:
gate outputs -> verdicts -> builder-page site files -> the pre-deploy check
(against archive.org-style item metadata in a cache directory). make_site
publishes only all-PASS entries over gated packs; site_check fails every
doctored copy of the site or of the verdicts."""
from __future__ import annotations

import json
import os
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


def _gate_outputs(out: Path, menu_jsonl: str, langs: list[str], fail: dict[str, str] | None = None,
                  zimcheck_version: str | None = "zim-tools 3.8.0", base: Path | None = None) -> None:
    """What run_gates.sh leaves for compose_verdicts.py, every check passing
    unless `fail` names one (lang -> check)."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "menu.jsonl").write_text(menu_jsonl)
    if zimcheck_version is not None:
        (out / "zimcheck.version").write_text(zimcheck_version + "\n")
    files = {"same": "PASS\n", "verify.log": "SUMMARY 22/22 PASS\n",
             "zimcheck.log": "[INFO] Overall Test Status: Pass\n", "route.log": "route OK 1.2 km\n",
             "kiwix.json": '{"pass": true}\n', "search.json": '{"pass": true}\n',
             "viewer.json": '{"pass": true}\n', "ui.json": '{"pass": true}\n',
             "smoke.log": "12 passed, 0 failed\n"}
    recipes = {m["L"]: json.load(open(m["recipe"])) for m in map(json.loads, menu_jsonl.splitlines()) if "recipe" in m}
    for L in langs:
        for k, v in files.items():
            (out / f"{L}.{k}").write_text(v)
        if L in recipes:            # run_gates.sh's stamp: these logs are for this recipe
            (out / f"{L}.stamp").write_text(f"{recipes[L]['md5']} {recipes[L]['uuid']}\n")
    for k in ("smoke.log", "viewer.json", "ui.json"):
        (out / f"base.{k}").write_text(files[k])
    if base is not None:
        st = os.stat(base)
        (out / "base.identity").write_text(f"{st.st_size} {int(st.st_mtime)}\n")
    bad = {"same": "FAIL\n", "verify.log": "SUMMARY 21/22 PASS\n", "zimcheck.log": "Overall Test Status: Fail\n",
           "route.log": "no route\n", "kiwix.json": '{"pass": false}\n', "search.json": '{"pass": false}\n',
           "viewer.json": '{"pass": false}\n',
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
    _gate_outputs(gates, r.stdout, LANGS.split(","), base=lp.base)
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
    _gate_outputs(gates, (site.gates / "menu.jsonl").read_text(), LANGS.split(","), fail={"th": "ui.json"}, base=lp.base)
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


@pytest.mark.parametrize("case", ["log_fail", "zimru", "no_version"])
def test_zimcheck_must_pass_and_be_official(case: str, site: SimpleNamespace, lp: SimpleNamespace,
                                            tmp_path: Path) -> None:
    gates = tmp_path / "gates"
    menu = (site.gates / "menu.jsonl").read_text()
    if case == "log_fail":
        _gate_outputs(gates, menu, LANGS.split(","), fail={"de": "zimcheck.log"}, base=lp.base)
    elif case == "zimru":
        _gate_outputs(gates, menu, LANGS.split(","), zimcheck_version="zimcheck (zimru) 0.1.0", base=lp.base)
    else:
        _gate_outputs(gates, menu, LANGS.split(","), zimcheck_version=None, base=lp.base)
    env = {"LANGPACK_WORK": str(tmp_path / "work")}
    shutil.copytree(site.work / "recipes", tmp_path / "work" / "recipes")
    assert tool("compose_verdicts.py", gates, lp.base, LANGS, env=env).returncode == 0
    v = json.loads((tmp_path / "work" / "verdicts" / site.tag / "de.json").read_text())
    assert v["checks"]["zimcheck"] is False and v["all_pass"] is False
    assert v["zimcheck_version"] == {"log_fail": "zim-tools 3.8.0", "zimru": "zimcheck (zimru) 0.1.0",
                                     "no_version": ""}[case]


def test_the_verdict_must_name_this_recipe(site: SimpleNamespace, lp: SimpleNamespace, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """entry_verdict_ok (make_site's gate) compares the recipe's md5, uuid and
    size with the verdict's; any difference refuses the entry."""
    from tools.langpack.gatelib import entry_verdict_ok
    monkeypatch.setenv("LANGPACK_WORK", str(site.work))
    recipe = json.loads((site.out / "recipes" / "de.json").read_text())
    assert entry_verdict_ok(str(lp.base), "de", recipe) == (True, "")
    for k, v in (("md5", "0" * 32), ("uuid", "00000000-0000-5000-8000-000000000000"), ("size", recipe["size"] + 1)):
        ok, why = entry_verdict_ok(str(lp.base), "de", dict(recipe, **{k: v}))
        assert not ok and why == "verdict is for another recipe", k
    # through make_site: a verdict for another recipe refuses the entry
    work = tmp_path / "work"
    shutil.copytree(site.work, work)
    vp = work / "verdicts" / site.tag / "de.json"
    v = json.loads(vp.read_text())
    v["recipe_uuid"] = "00000000-0000-5000-8000-000000000000"
    vp.write_text(json.dumps(v))
    out = tmp_path / "site"
    r = tool("make_site.py", lp.base, "--packs-dir", site.packs, "--region", "testland", "--region-title", "Testland",
             "--region-langs", "en", "--langs", "de,th", "--titles", TITLES, "--language-meta", "map",
             "--url-template", f"{DL}/p/{{file}}", "--base-download", f"{DL}/b/{lp.base.name}",
             "-o", out / "recipes", env={"LANGPACK_WORK": str(work)})
    assert r.returncode == 0, r.stderr
    refused = json.loads((out / "recipes" / "refused.json").read_text())
    assert refused == [{"lang": "de", "why": "verdict is for another recipe"}]
    assert [m["lang"] for m in json.loads((out / "recipes" / "index.json").read_text())["regions"][0]["menu"]] == ["th"]


@pytest.mark.parametrize("mode,th_passes", [(None, False), ("report", True)])
def test_kiwix_search_is_required_unless_reported(site: SimpleNamespace, lp: SimpleNamespace, tmp_path: Path,
                                                 mode: str | None, th_passes: bool) -> None:
    """search_gate.py's FAIL (Kiwix's search cannot find the pack articles)
    fails the entry; LANGPACK_SEARCH_GATE=report records it as advisory."""
    gates = tmp_path / "gates"
    _gate_outputs(gates, (site.gates / "menu.jsonl").read_text(), LANGS.split(","), fail={"th": "search.json"}, base=lp.base)
    env = {"LANGPACK_WORK": str(tmp_path / "work"), **({"LANGPACK_SEARCH_GATE": mode} if mode else {})}
    shutil.copytree(site.work / "recipes", tmp_path / "work" / "recipes")
    assert tool("compose_verdicts.py", gates, lp.base, LANGS, env=env).returncode == 0
    th = json.loads((tmp_path / "work" / "verdicts" / site.tag / "th.json").read_text())
    assert th["checks"]["search"] is False and th["all_pass"] is th_passes
    assert th["advisory"] == (["search"] if mode else [])
