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
    assert index_pack.page_text("<p>Ο Παρθενώνας, Αθηνάς — Zürich</p>".encode()) == "ο παρθενωνας, αθηνας — zurich"
    assert index_pack.page_text(b"<head><title>x</title></head><script>var a</script><nav>menu</nav><p>body</p>") == "body"


# Words whose marks libzim's folding takes off: Thai and Devanagari vowel
# signs (category M, combining class 0), accents, Arabic harakat, a dotted
# capital I, a final sigma.
FOLD_WORDS = ["สวัสดี", "เชียงใหม่", "दिल्ली", "मुंबई", "Μονεμβασιά", "Préfecture", "Hà Nội", "القَاهِرَة",
              "İstanbul", "ΟΔΟΣ", "Straße"]


def test_fold_is_libzims_own(tmp_path: Path) -> None:
    """Review S-2: fold() must equal libzim's zim::removeAccents ("Lower; NFD;
    [:M:] remove; NFC"). libzim's own HTML indexer folds a page's text so;
    a search pack's index data, folded by fold(), must give the same full
    text hits for every word (the old fold kept the Thai and Devanagari
    vowel signs: 'สวัสดี', 'दिल्ली' were never found)."""
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    from libzim.writer import Creator, Hint, IndexData, Item, StringProvider

    class Data(IndexData):
        def __init__(self, t: str, x: str):
            super().__init__()
            self.t, self.x = t, x

        def has_indexdata(self): return True
        def get_title(self): return self.t
        def get_content(self): return self.x
        def get_keywords(self): return ""
        def get_wordcount(self): return len(self.x.split())
        def get_geoposition(self): return None

    class Page(Item):                   # libzim's own indexer reads the HTML
        def __init__(self, p: str, html: str, idx=None):
            super().__init__()
            self.p, self.h, self.idx = p, html, idx

        def get_path(self): return self.p
        def get_title(self): return "t " + self.p
        def get_mimetype(self): return "text/html"
        def get_contentprovider(self): return StringProvider(self.h)
        def get_hints(self): return {Hint.FRONT_ARTICLE: True}

    class PackPage(Page):               # a search pack's: our index data
        def get_indexdata(self): return self.idx

    zims = {}
    for how in ("libzim", "fold"):
        zims[how] = tmp_path / f"{how}.zim"
        with Creator(zims[how]).config_indexing(True, "eng") as c:
            c.add_metadata("Title", "t")
            for i, w in enumerate(FOLD_WORDS):
                text = f"{w} filler words"
                html = f"<html><head><title>t{i}</title></head><body><p>{text}</p></body></html>"
                c.add_item(Page(f"p{i}", html) if how == "libzim" else
                           PackPage(f"p{i}", html, Data("", index_pack.fold(text))))
    for i, w in enumerate(FOLD_WORDS):
        got = {how: list(Searcher(Archive(z)).search(Query().set_query(w)).getResults(0, 5)) for how, z in zims.items()}
        assert got["libzim"] == [f"p{i}"] and got["fold"] == got["libzim"], (w, index_pack.fold(w), got)


@pytest.fixture(scope="module")
def sr(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """A base whose places have other names (front redirects), a de pack and
    a th (non-Latin) pack, their search pack and the merged file."""
    from tests.langpack import lpzims
    d = tmp_path_factory.mktemp("searchredir")
    base = d / "osm-testland-2026-10-13.zim"
    u = lpzims.write_base(base, front_redirects=[("wiki-article/Alpha_GR", "Άλφα Πόλη", "wiki-article/Alpha_Town"),
                                                 ("wiki-article/Beta_BG", "Бета Връх", "wiki-article/Beta_Peak")])
    packs = {x: d / f"osm_testland_wiki-{x}_2026-10-13_{u[:8]}.zim" for x in ("de", "th")}
    for x, p in packs.items():
        lpzims.write_pack(p, x, u)
    out = d / "osm_testland_search-de-th_2026-10-13_x.zim"
    side = index_pack.build(str(base), [str(p) for p in packs.values()], "de", "testland", str(out), d / "cache",
                            log=lambda m: None)
    merged = d / "m.zim"
    r = tool("merge_zim.py", base, *packs.values(), "--index", out, "-o", merged, "--lang", "th", "--region", "testland")
    assert r.returncode == 0, r.stdout + r.stderr
    return SimpleNamespace(d=d, base=base, packs=packs, out=out, side=side, merged=merged, uuid=u)


def test_kiwix_suggests_the_bases_other_names(sr: SimpleNamespace) -> None:
    """Review S-1: the base's front redirects (a place's other names) are in
    the search pack's title index, so Kiwix still suggests them in the
    merged file (Greece: 2,566 such; 20 of 20 sampled found by the base, 3
    by the merged file before)."""
    for q, path in (("Άλφα", "wiki-article/Alpha_GR"), ("αλφα πολη", "wiki-article/Alpha_GR"),
                    ("Бета", "wiki-article/Beta_BG")):
        assert path in _suggest(sr.base, q), q            # what the base's own index finds
        assert path in _suggest(sr.merged, q), q
    assert sr.side["redirects"] == 2 and sr.side["per_source"]["base"]["redirects"] == 2, sr.side
    assert sr.side["gate"]["ok"] and sr.side["gate"]["found"]["osm-testland-2026-10-13.zim (redirect)"]["suggest"]


def test_a_non_latin_pack_is_found_by_full_text(sr: SimpleNamespace) -> None:
    """Review S-2: the gate requires full text, and a Thai pack's article is
    found by it (the old fold kept its vowel sign; libzim folds it off the
    query)."""
    assert "~w/th/wiki-article/อัลฟา" in _fulltext(sr.merged, "อัลฟา")
    r = tool("search_gate.py", sr.merged, sr.base, "--lang", "th")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0 and out["pass"] and out["fulltext_found"] == out["titles"] == 1, out
    assert out["base"]["base_found"] == out["base"]["merged_found"] and out["base"]["redirects"] == 2, out


def test_a_title_libzim_cannot_index_is_not_required_by_full_text(sr: SimpleNamespace, tmp_path: Path) -> None:
    """Switzerland's th entry: "ท่าอากาศยานนานาชาติเจนีวา" (Geneva airport) is
    one 66-byte word folded, over Xapian's 64-byte word limit, so no libzim
    full-text index can hold it; the gate still requires it by suggestion
    and requires full text for the other titles."""
    from tests.langpack import lpzims
    th = tmp_path / sr.packs["th"].name
    lpzims.write_pack(th, "th", sr.uuid, articles=[("อัลฟา", "อัลฟา"),
                                                  ("ท่าอากาศยานนานาชาติเจนีวา", "ท่าอากาศยานนานาชาติเจนีวา")])
    out = tmp_path / "osm_testland_search-de-th_2026-10-13_v.zim"
    index_pack.build(str(sr.base), [str(sr.packs["de"]), str(th)], "th", "testland", str(out), None, log=lambda m: None)
    merged = tmp_path / "m.zim"
    assert tool("merge_zim.py", sr.base, sr.packs["de"], th, "--index", out, "-o", merged, "--lang", "th",
                "--region", "testland").returncode == 0
    r = tool("search_gate.py", merged, sr.base, "--lang", "th")
    g = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0 and g["pass"], g
    assert g["titles"] == g["suggest_found"] == 2 and g["fulltext_found"] == 1 and g["fulltext_unindexable"] == 1, g


def test_the_gate_requires_the_bases_redirects(sr: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    """Review S-1: a search pack without the base's redirects fails the
    search gate's base parity (the base suggests them, the merged file not)."""
    real = getattr(index_pack, "entries", None)
    if real is not None:
        monkeypatch.setattr(index_pack, "entries", lambda p: (r for r in real(p) if r[0] != "r"))
    out = tmp_path / "osm_testland_search-de-th_2026-10-13_y.zim"
    index_pack.build(str(sr.base), [str(p) for p in sr.packs.values()], "de", "testland", str(out), None,
                     log=lambda m: None)
    merged = tmp_path / "m.zim"
    assert tool("merge_zim.py", sr.base, *sr.packs.values(), "--index", out, "-o", merged, "--lang", "th",
                "--region", "testland").returncode == 0
    r = tool("search_gate.py", merged, sr.base, "--lang", "de")
    g = json.loads(r.stdout.strip().splitlines()[-1])
    assert g["suggest_found"] == g["fulltext_found"] == g["titles"] == 3, g      # the de pack's part passes
    assert r.returncode == 1 and not g["pass"], g
    assert {x["path"] for x in g["base"]["missed"]} == {"wiki-article/Alpha_GR", "wiki-article/Beta_BG"}, g


def test_the_gates_require_full_text(sr: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    """Review S-2: text folded the old way (combining marks only) keeps the
    Thai vowel sign, so full text misses the th article while suggestions
    find it: the search pack's own gate and the merged file's search gate
    fail it (the th entry passed with 2/12 by full text on Greece)."""
    import unicodedata
    monkeypatch.setattr(index_pack, "fold", lambda t: unicodedata.normalize("NFC", "".join(
        ch for ch in unicodedata.normalize("NFD", t) if not unicodedata.combining(ch))))
    out = tmp_path / "osm_testland_search-de-th_2026-10-13_w.zim"
    side = index_pack.build(str(sr.base), [str(p) for p in sr.packs.values()], "th", "testland", str(out), None,
                            log=lambda m: None)
    assert not side["gate"]["ok"] and any("full text" in x and "อัลฟา" in x for x in side["gate"]["problems"]), side["gate"]
    merged = tmp_path / "m.zim"
    assert tool("merge_zim.py", sr.base, *sr.packs.values(), "--index", out, "-o", merged, "--lang", "th",
                "--region", "testland").returncode == 0
    r = tool("search_gate.py", merged, sr.base, "--lang", "th")
    g = json.loads(r.stdout.strip().splitlines()[-1])
    assert g["suggest_found"] == g["titles"] == 1 and g["fulltext_found"] == 0, g
    assert g["base"]["merged_found"] == g["base"]["base_found"], g        # parity holds: full text alone fails it
    assert r.returncode == 1 and not g["pass"], g


def test_full_text_finds_a_title_alone(sr: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    """The index data's title is folded too, as libzim's own indexer folds
    it: with no page text at all, full text still finds the Thai article by
    its title."""
    monkeypatch.setattr(index_pack, "page_text", lambda raw: "")
    out = tmp_path / "osm_testland_search-th_2026-10-13_t.zim"
    index_pack.build(str(sr.base), [str(sr.packs["th"])], "th", "testland", str(out), None, log=lambda m: None)
    assert "~w/th/wiki-article/อัลฟา" in _fulltext(out, "อัลฟา")


def test_pages_stream_into_the_writer(sr: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    """Review S-3: rows go to libzim's writer as they are read; nothing holds
    every page's text (Greece: 497 MB peak before). Each add_item comes
    right after its row; a stale cache version is not read; the cache is
    written once complete."""
    import libzim.writer as lw
    made = [0]

    def counting(real):
        def rows(p):
            for r in real(p):
                made[0] += 1
                yield r
        return rows
    for name in ("entries", "pages"):              # the row reader (pages before S-3)
        if hasattr(index_pack, name):
            monkeypatch.setattr(index_pack, name, counting(getattr(index_pack, name)))
    seen: list[int] = []

    class Spy(lw.Creator):
        def add_item(self, item):
            seen.append(made[0])
            return super().add_item(item)
    monkeypatch.setattr(lw, "Creator", Spy)
    cache = tmp_path / "cache"
    (cache / "index-text").mkdir(parents=True)
    import gzip
    uuid = str(__import__("uuid").UUID(sr.uuid))
    with gzip.open(cache / "index-text" / f"{uuid}.jsonl.gz", "wt") as fh:      # an old version's cache
        fh.write(json.dumps(["wiki-article/Junk", "Junk", True, "junk"]) + "\n")
    out = tmp_path / "osm_testland_search-de_2026-10-13_z.zim"
    side = index_pack.build(str(sr.base), [str(sr.packs["de"])], "de", "testland", str(out), cache, log=lambda m: None)
    assert 1 <= seen[0] <= 2 and len(seen) == side["pages"], (seen, side["pages"])
    assert "wiki-article/Junk" not in _suggest(out, "Junk")
    assert (cache / "index-text" / f"{uuid}.v{index_pack.TEXT_VERSION}.jsonl.gz").exists()


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
    assert find_search_pack(str(sp.packs), str(sp.base), [str(sp.de)], "de") == str(sp.out)
    assert find_search_pack(str(sp.packs), str(sp.base), [str(sp.de)], "en") is None       # indexed in another language
    assert find_search_pack(str(sp.packs), str(sp.base), [str(sp.packs / lp.packs["fr"].name)], "de") is None
    assert find_search_pack(str(sp.packs), str(sp.base), [str(sp.de), str(sp.packs / lp.packs["fr"].name)], "de") is None
    # a pack that changed since the search pack was built
    d2 = tmp_path / "p"
    shutil.copytree(sp.packs, d2)
    de2 = d2 / sp.de.name
    de2.write_bytes(de2.read_bytes()[:-1] + b"\x00")
    assert find_search_pack(str(d2), str(sp.base), [str(de2)], "de") is None
    # a search pack whose own bytes changed since its gate
    d3 = tmp_path / "q"
    shutil.copytree(sp.packs, d3)
    o3 = d3 / sp.out.name
    o3.write_bytes(o3.read_bytes()[:-1] + b"\x00")
    assert find_search_pack(str(d3), str(sp.base), [str(d3 / sp.de.name)], "de") is None
    # the merger refuses a search pack built for another base
    side = json.loads((sp.out.parent / (sp.out.name + ".json")).read_text())
    d4 = tmp_path / "r"
    shutil.copytree(sp.packs, d4)
    (d4 / (sp.out.name + ".json")).write_text(json.dumps(dict(side, base_uuid="0" * 32)))
    r = tool("merge_zim.py", sp.base, d4 / sp.de.name, "--index", d4 / sp.out.name, "-o", tmp_path / "x.zim",
             "--lang", "de", "--region", "testland")
    assert r.returncode != 0 and "built for base" in r.stderr


def test_search_packs_one_per_pack_set(lp: SimpleNamespace, tmp_path: Path) -> None:
    """Each entry's search pack is indexed in its own language: entries with
    the same packs (fr, en) share none; an entry without packs needs none, nor
    one that lacks a pack it must carry (th: make_site.py refuses it); a rerun
    builds nothing again; a pack of the old scheme (one per pack set, indexed
    in the base's language) still serves the entry of its language."""
    from tests.langpack import lpzims
    packs = tmp_path / "packs"
    packs.mkdir()
    zc = lpzims.fake_zimcheck(tmp_path / "zc", "clean", None)
    for x in ("de", "fr"):
        shutil.copy(lp.packs[x], packs / lp.packs[x].name)
        assert tool("pack_gate.py", packs / lp.packs[x].name, "--base", lp.base,
                    env={"PACK_GATE_ZIMCHECK": zc}).returncode == 0
    # the old scheme's pack for fr alone: indexed in the base's language (English)
    old = packs / "osm_testland_search-fr_2026-10-21_x.zim"
    assert index_pack.build(str(lp.base), [str(packs / lp.packs["fr"].name)], "en", "testland", str(old),
                            tmp_path / "cache", log=lambda m: None)["gate"]["ok"]
    args = [lp.base, packs, "--region", "testland", "--langs", "de,fr,th,en", "--region-langs", "fr",
            "--date", "2026-10-22", "--cache", tmp_path / "cache"]
    r = tool("search_packs.py", *args)
    rows = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]   # libzim prints too
    assert r.returncode == 0, r.stderr[-2000:]
    # th has no th pack: none (make_site.py refuses it); de: de+fr; fr and en: fr alone, each in its
    # own language; en reuses the old pack
    u8 = lpzims.read_uuid(lp.base)[:8]
    assert rows[0] == {"packs": ["fr"], "entries": ["th"], "built": False, "missing": ["th"]}, rows[0]
    got = [(tuple(x["packs"]), tuple(x["entries"]), x["search_pack"], x["built"]) for x in rows[1:]]
    assert got == [(("de", "fr"), ("de",), f"osm_testland_search-de-fr_2026-10-22_{u8}.zim", True),
                   (("fr",), ("fr",), f"osm_testland_search-fr.fr_2026-10-22_{u8}.zim", True),
                   (("fr",), ("en",), old.name, False)], got
    assert not list(packs.glob("*search-fr.th*"))
    assert all(x["gate_ok"] and x["index_lang"] == x["entries"][0] for x in rows if x["built"])
    for x in rows[1:]:
        assert json.loads((packs / (x["search_pack"] + ".json")).read_text())["lang"] == x["entries"][0]
    r2 = tool("search_packs.py", *args)
    assert [json.loads(x)["built"] for x in r2.stdout.splitlines() if x.startswith("{")] == [False] * 4


def test_search_packs_side_by_side_in_a_scratch_folder(lp: SimpleNamespace, tmp_path: Path) -> None:
    """--jobs: the pack sets' builds run side by side, each in a process of its
    own; --work-dir (the NVMe scratch): built there, then moved into the packs
    folder with their sidecars and gates (nothing left behind); the rows are
    the serial run's."""
    import threading
    import time
    from tests.langpack import lpzims
    zc = lpzims.fake_zimcheck(tmp_path / "zc", "clean", None)
    runs = {}
    for name, jobs in (("serial", "1"), ("side", "3")):
        packs = tmp_path / name / "packs"
        packs.mkdir(parents=True)
        for x in ("de", "fr", "th"):
            shutil.copy(lp.packs[x], packs / lp.packs[x].name)
            assert tool("pack_gate.py", packs / lp.packs[x].name, "--base", lp.base,
                        env={"PACK_GATE_ZIMCHECK": zc}).returncode == 0
        work = tmp_path / name / "scratch"
        most, stop = [0], threading.Event()

        def watch(w: Path = work, m: list = most, s: threading.Event = stop) -> None:
            while not s.is_set():
                if w.exists():
                    m[0] = max(m[0], len({p.name.split(".zim")[0] for p in w.iterdir()}))   # builds there at once
                time.sleep(0.005)
        t = threading.Thread(target=watch)
        t.start()
        try:
            r = tool("search_packs.py", lp.base, packs, "--region", "testland", "--langs", "de,fr,th,en", "--region-langs", "",
                     "--date", "2026-10-22", "--cache", tmp_path / name / "cache", "--jobs", jobs, "--work-dir", work)
        finally:
            stop.set()
            t.join()
        assert r.returncode == 0, r.stderr[-2000:]
        rows = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        runs[name] = (rows, most[0], r.stderr)
        assert not list(work.iterdir())                         # moved out, nothing left
        for x in rows:
            out = packs / x["search_pack"]
            g = json.loads(Path(str(out) + ".gate.json").read_text())
            assert g["ok"] and g["pack_md5"] == lpzims.md5_of(out) and Path(str(out) + ".json").exists()
        assert not list(packs.glob(".*.part"))
    key = [(tuple(x["packs"]), tuple(x["entries"]), x["index_lang"], x["gate_ok"]) for x in runs["serial"][0]]
    assert key == [(("de",), ("de",), "de", True), (("fr",), ("fr",), "fr", True), (("th",), ("th",), "th", True)], key
    assert [(tuple(x["packs"]), tuple(x["entries"]), x["index_lang"], x["gate_ok"]) for x in runs["side"][0]] == key
    assert runs["serial"][1] == 1 and runs["side"][1] >= 2, runs     # one build at a time; then side by side
    assert runs["side"][2].count("base pages and redirects:") == 1, runs["side"][2]    # the base's pages read once
    assert "search packs: 3 to build, 3 at a time, in " in runs["side"][2]


def test_each_version_is_stemmed_in_its_own_language(lp: SimpleNamespace, tmp_path: Path) -> None:
    """A German speaker's version gets German stemming: with de a region
    language, the de and en versions carry the same packs, and their search
    packs are indexed in de and in en: "Kirchen" finds "Die Kirche von
    Testdorf" in the de version (libzim's German stemmer), not in the en one."""
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    from tests.langpack import lpzims
    packs = tmp_path / "packs"
    packs.mkdir()
    de = packs / "osm_testland_wiki-de_2026-10-13.zim"
    lpzims.write_pack(de, "de", lpzims.read_uuid(lp.base), articles=[("Kirche_Testdorf", "Die Kirche von Testdorf"),
                                                                      ("Bruecke_Testdorf", "Eine Brücke über den Fluss")])
    zc = lpzims.fake_zimcheck(tmp_path / "zc", "clean", None)
    assert tool("pack_gate.py", de, "--base", lp.base, env={"PACK_GATE_ZIMCHECK": zc}).returncode == 0
    r = tool("search_packs.py", lp.base, packs, "--region", "testland", "--langs", "de,en", "--region-langs", "de",
             "--date", "2026-10-23", "--cache", tmp_path / "cache")
    assert r.returncode == 0, r.stderr[-2000:]
    rows = {x["entries"][0]: x for x in (json.loads(y) for y in r.stdout.splitlines() if y.startswith("{"))}
    assert rows["de"]["packs"] == rows["en"]["packs"] == ["de"] and rows["de"]["search_pack"] != rows["en"]["search_pack"]

    def found(entry: str, q: str) -> list[str]:
        a = Archive(packs / rows[entry]["search_pack"])
        return list(Searcher(a).search(Query().set_query(q)).getResults(0, 10))
    assert "~w/de/wiki-article/Kirche_Testdorf" in found("de", "Kirchen")
    assert "~w/de/wiki-article/Bruecke_Testdorf" in found("de", "Brücken")
    assert "~w/de/wiki-article/Kirche_Testdorf" not in found("en", "Kirchen")
    assert "~w/de/wiki-article/Kirche_Testdorf" in found("en", "Kirche")       # the word itself, either way


def test_a_title_the_base_crowds_off_full_text_is_not_sampled(tmp_path: Path) -> None:
    """Iceland's is, en, tr and vi entries: the base has 1,000+ pages of
    "Vatnajökull National Park", so full text for "Vatnajökull" fills
    kiwix-serve's first page with them in any file and the pack article
    never shows there. The gate samples the next pack-only title instead,
    only for a title the merged file misses ("Zeta", crowded in the base but
    found, is sampled); a base that mentions a title only in its pages' text
    ("Epsilon"), or holds it only inside other words ("Hunting Lodge" for
    "Hunt"), crowds nothing; and a pack whose every title is crowded off
    fails."""
    from libzim.reader import Archive
    from libzim.search import Query, Searcher

    from tests.langpack import lpzims
    from tools.langpack.search_gate import base_crowds
    pages = [(f"search/delta-park-{i}.html", f"Delta Park · Deltagarður {i}", "Delta Park, a national park in Delta.")
             for i in range(30)]
    pages += [(f"search/page-{i}.html", f"Page {i}", "Near Epsilon, by Epsilon road.") for i in range(30)]
    pages += [(f"search/zeta-park-{i}.html", f"Zeta Park {i}", "A park.") for i in range(30)]
    pages += [(f"search/hunting-{i}.html", f"Hunting Lodge {i}", "A lodge for hunting.") for i in range(30)]
    base = tmp_path / "osm-testland-2026-10-13.zim"
    u = lpzims.write_base(base, pages=pages, fulltext=True)

    def merged(name: str, articles: list[tuple[str, str]], bodies: dict[str, str] | None = None) -> Path:
        pack = tmp_path / f"osm_testland_wiki-de_2026-10-13_{name}.zim"
        lpzims.write_pack(pack, "de", u, articles=articles, bodies=bodies)
        idx = tmp_path / f"osm_testland_search-de_2026-10-13_{name}.zim"
        index_pack.build(str(base), [str(pack)], "de", "testland", str(idx), None, log=lambda m: None)
        m = tmp_path / f"m-{name}.zim"
        r = tool("merge_zim.py", base, pack, "--index", idx, "-o", m, "--lang", "de", "--region", "testland")
        assert r.returncode == 0, r.stdout + r.stderr
        return m

    def gate(m: Path, *more: str) -> tuple[int, dict]:
        r = tool("search_gate.py", m, base, "--lang", "de", *more)
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])

    b = Archive(base)
    crowds = {t: base_crowds(b, Searcher(b), t) for t in ("Delta", "Zeta", "Epsilon", "Hunt")}
    assert crowds == {"Delta": True, "Zeta": True, "Epsilon": False, "Hunt": False}, crowds
    assert len(list(Searcher(b).search(Query().set_query("Hunt")).getResults(0, 25))) == 25    # by the English stem
    m = merged("a", [("Alpha_Stadt", "Alpha Stadt"), ("Beta_Gipfel", "Beta Gipfel"), ("Delta", "Delta"),
                     ("Epsilon", "Epsilon"), ("Hunt", "Hunt"), ("Zeta", "Zeta")], {"Zeta": " ".join(["Zeta"] * 20)})
    top = list(Searcher(Archive(m)).search(Query().set_query("Delta")).getResults(0, 25))
    assert len(top) == 25 and not any(p.startswith("~w/") for p in top), top      # crowded off in the merged file
    rc, g = gate(m)
    assert rc == 0 and g["pass"] and g["crowded"] == ["Delta"], g
    assert [r["title"] for r in g["rows"]] == ["Alpha Stadt", "Beta Gipfel", "Epsilon", "Hunt", "Zeta"], g
    assert g["fulltext_found"] == g["titles"] == 5, g
    rc, g = gate(m, "--n", "3")             # the spread is Alpha Stadt, Delta, Hunt: Beta Gipfel takes Delta's place
    assert rc == 0 and g["crowded"] == ["Delta"], g
    assert [r["title"] for r in g["rows"]] == ["Alpha Stadt", "Hunt", "Beta Gipfel"], g
    rc, g = gate(merged("b", [("Delta", "Delta")]))
    assert rc == 1 and not g["pass"] and g["titles"] == 0 and "crowded off" in g["why"], g
    rc, g = gate(tmp_path / "osm_testland_wiki-de_2026-10-13_b.zim")      # no full-text index: nothing set aside
    assert rc == 1 and g["crowded"] == [] and g["titles"] == 1 and g["fulltext_found"] == 0, g


def test_a_title_another_pack_crowds_off_full_text_is_not_sampled(tmp_path: Path) -> None:
    """Iberia's fr entry: every entry carries the Catalan pack, whose
    "Rectoria de ..." articles fill full text's first page for the French
    "Rectoria": set aside as one the base crowds off."""
    from libzim.reader import Archive
    from libzim.search import Query, Searcher

    from tests.langpack import lpzims
    base = tmp_path / "osm-testland-2026-10-13.zim"
    u = lpzims.write_base(base, fulltext=True)
    de = tmp_path / f"osm_testland_wiki-de_2026-10-13_{u[:8]}.zim"
    lpzims.write_pack(de, "de", u, articles=[("Alpha_Stadt", "Alpha Stadt"), ("Omega", "Omega"),
                                            ("Beta_Gipfel", "Beta Gipfel")])
    th = tmp_path / f"osm_testland_wiki-th_2026-10-13_{u[:8]}.zim"
    lpzims.write_pack(th, "th", u, articles=[(f"Omega_Haus_{i}", f"Omega Haus {i}") for i in range(30)],
                      bodies={f"Omega_Haus_{i}": "Omega Haus, an Omega house." for i in range(30)})
    idx = tmp_path / "osm_testland_search-de-th_2026-10-13_x.zim"
    index_pack.build(str(base), [str(de), str(th)], "de", "testland", str(idx), None, log=lambda m: None)
    m = tmp_path / "m.zim"
    r = tool("merge_zim.py", base, de, th, "--index", idx, "-o", m, "--lang", "de", "--region", "testland")
    assert r.returncode == 0, r.stdout + r.stderr
    top = list(Searcher(Archive(m)).search(Query().set_query("Omega")).getResults(0, 25))
    assert len(top) == 25 and not any(p.startswith("~w/de/") for p in top), top      # crowded off by th
    r = tool("search_gate.py", m, base, "--lang", "de")
    g = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0 and g["pass"] and g["crowded"] == ["Omega"], g
    assert [x["title"] for x in g["rows"]] == ["Alpha Stadt", "Beta Gipfel"], g
