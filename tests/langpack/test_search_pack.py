"""Search packs (index_pack.py): Kiwix's search must find a merged file's
pack articles and the base's own pages. libzim answers suggestions from
X/title/xapian and full text from X/fulltext/xapian; a search pack carries
both, built by libzim from every page of the merged file, and the merger
puts them in place of the base's."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack.conftest import tool  # noqa: E402
from tools.langpack import index_pack  # noqa: E402


def _suggest(path: Path, q: str) -> list[str]:
    from libzim.reader import Archive
    from libzim.suggestion import SuggestionSearcher
    return list(SuggestionSearcher(Archive(path)).suggest(q).getResults(0, 20))


def _fulltext(path: Path, q: str) -> list[str]:
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    return list(Searcher(Archive(path)).search(Query().set_query(q)).getResults(0, 20))


@pytest.fixture(scope="module")
def sp(lp: SimpleNamespace, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    d = tmp_path_factory.mktemp("searchpack")
    packs = d / "packs"
    packs.mkdir()
    for p in lp.packs.values():
        shutil.copy(p, packs / p.name)
    de = packs / lp.packs["de"].name
    out = packs / "osm_testland_search-de_2026-10-13_x.zim"
    side = index_pack.build(str(lp.base), [str(de)], "de", "testland", str(out), d / "cache", log=lambda m: None)
    merged = d / "m-de.zim"
    r = tool("merge_zim.py", lp.base, de, "--index", out, "-o", merged, "--lang", "de", "--region", "testland")
    assert r.returncode == 0, r.stdout + r.stderr
    return SimpleNamespace(d=d, packs=packs, de=de, out=out, side=side, merged=merged, base=lp.base)


def test_kiwix_finds_pack_articles_and_base_pages(sp: SimpleNamespace) -> None:
    assert sp.side["gate"]["ok"], sp.side["gate"]
    # suggestions (Kiwix's search box): both sources, case and accents folded
    assert "~w/de/wiki-article/Alpha_Stadt" in _suggest(sp.merged, "alpha stadt")
    assert "~w/de/wiki-article/AC/DC_Platz" in _suggest(sp.merged, "AC/DC")
    assert "wiki-article/Alpha_Town" in _suggest(sp.merged, "Alpha Town")
    # full text (kiwix-serve /search)
    assert "~w/de/wiki-article/Beta_Gipfel" in _fulltext(sp.merged, "Gipfel")
    assert "wiki-article/Gamma_Lake" in _fulltext(sp.merged, "Gamma")
    r = tool("search_gate.py", sp.merged, sp.base, "--lang", "de")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0 and out["pass"] and out["suggest_found"] == out["titles"] == 3, out


def test_the_text_is_folded_like_libzims_queries() -> None:
    """python-libzim keeps accents in index-data terms while libzim's reader
    folds every query: the search pack folds the text itself (Greek full text
    found nothing before: 'Παρθενώνας' 0 hits on Greece el)."""
    assert index_pack.page_text("<p>Ο Παρθενώνας, Αθηνάς — Zürich</p>".encode()) == "Ο Παρθενωνας, Αθηνας — Zurich"
    assert index_pack.page_text(b"<head><title>x</title></head><script>var a</script><nav>menu</nav><p>body</p>") == "body"


def test_the_merge_carries_the_search_packs_indexes(sp: SimpleNamespace, tmp_path: Path) -> None:
    r = tool("verify_merge.py", sp.merged, sp.base, sp.de, "--index", sp.out, "--lang", "de", "--region", "testland",
             "--queries", "Alpha Town")
    assert r.returncode == 0 and "the search pack's Xapian databases replace the base's" in r.stdout, r.stdout[-3000:]
    # the recipe rebuilds the same bytes, the search pack being one more source
    from tools.langpack import recipe2
    recipe, plan, _ = recipe2.build_recipe(str(sp.base), [str(sp.de)], "de", region="testland",
                                           index_path=str(sp.out))
    assert recipe["packs"]["search"]["kind"] == "search" and recipe["packs"]["search"]["file"] == sp.out.name
    rebuilt = tmp_path / "r.zim"
    recipe2.materialize(recipe, str(sp.base), recipe2.plan_inputs(plan), str(rebuilt))
    assert rebuilt.read_bytes() == sp.merged.read_bytes()


def test_a_search_pack_counts_only_for_its_base_and_exact_packs(sp: SimpleNamespace, lp: SimpleNamespace,
                                                                tmp_path: Path) -> None:
    from tools.langpack.recipe2 import find_search_pack
    assert find_search_pack(str(sp.packs), str(sp.base), [str(sp.de)]) == str(sp.out)
    assert find_search_pack(str(sp.packs), str(sp.base), [str(sp.packs / lp.packs["fr"].name)]) is None
    assert find_search_pack(str(sp.packs), str(sp.base), [str(sp.de), str(sp.packs / lp.packs["fr"].name)]) is None
    # a pack that changed since the search pack was built
    d2 = tmp_path / "p"
    shutil.copytree(sp.packs, d2)
    de2 = d2 / sp.de.name
    de2.write_bytes(de2.read_bytes()[:-1] + b"\x00")
    assert find_search_pack(str(d2), str(sp.base), [str(de2)]) is None
    # a search pack whose own bytes changed since its gate
    d3 = tmp_path / "q"
    shutil.copytree(sp.packs, d3)
    o3 = d3 / sp.out.name
    o3.write_bytes(o3.read_bytes()[:-1] + b"\x00")
    assert find_search_pack(str(d3), str(sp.base), [str(d3 / sp.de.name)]) is None
    # the merger refuses a search pack built for another base
    side = json.loads((sp.out.parent / (sp.out.name + ".json")).read_text())
    d4 = tmp_path / "r"
    shutil.copytree(sp.packs, d4)
    (d4 / (sp.out.name + ".json")).write_text(json.dumps(dict(side, base_uuid="0" * 32)))
    r = tool("merge_zim.py", sp.base, d4 / sp.de.name, "--index", d4 / sp.out.name, "-o", tmp_path / "x.zim",
             "--lang", "de", "--region", "testland")
    assert r.returncode != 0 and "built for base" in r.stderr


def test_search_packs_one_per_pack_set(lp: SimpleNamespace, tmp_path: Path) -> None:
    """Entries with the same packs share one search pack; an entry without
    packs needs none; a rerun builds nothing again."""
    from tests.langpack import lpzims
    packs = tmp_path / "packs"
    packs.mkdir()
    zc = lpzims.fake_zimcheck(tmp_path / "zc", "clean", None)
    for x in ("de", "fr"):
        shutil.copy(lp.packs[x], packs / lp.packs[x].name)
        assert tool("pack_gate.py", packs / lp.packs[x].name, "--base", lp.base,
                    env={"PACK_GATE_ZIMCHECK": zc}).returncode == 0
    args = [lp.base, packs, "--region", "testland", "--langs", "de,fr,th,en", "--region-langs", "fr",
            "--date", "2026-10-22", "--cache", tmp_path / "cache"]
    r = tool("search_packs.py", *args)
    rows = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]   # libzim prints too
    assert r.returncode == 0, r.stderr[-2000:]
    # de: de+fr; fr, th (no th pack) and en: fr alone
    assert sorted((tuple(x["packs"]), tuple(x["entries"])) for x in rows) == [
        (("de", "fr"), ("de",)), (("fr",), ("fr", "th", "en"))], rows
    assert all(x["built"] and x["gate_ok"] for x in rows)
    assert {x["index_lang"] for x in rows} == {"de", "en"}      # the visitor's language, else the base's
    r2 = tool("search_packs.py", *args)
    assert [json.loads(x)["built"] for x in r2.stdout.splitlines() if x.startswith("{")] == [False, False]
