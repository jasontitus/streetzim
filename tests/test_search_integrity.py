"""Search still works, and coordinates still point at the right place.

Written after 2026-09-25, when search-record coordinates were rounded from
full float64 repr to 5 dp (~1.1 m) to cut the search payload. That is a
lossy change to the field routing and the map pin both read, so it needs a
test that fails if the rounding is ever loosened past what a map user would
notice -- and one that fails if search stops returning anything at all,
which no gate in the pipeline actually checked.

Runs against a real ZIM. Set STREETZIM_TEST_ZIM, or it picks the newest
dated build on the host. Skips cleanly when there is none, so CI on a
machine without archives is quiet rather than red.
"""
import json
import math
import unicodedata
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
libzim_reader = pytest.importorskip("libzim.reader")


def _pick_zim():
    env = os.environ.get("STREETZIM_TEST_ZIM")
    if env:
        return Path(env)
    dated = sorted(ROOT.glob("osm-*-20??-??-??.zim"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return dated[0] if dated else None


ZIM = _pick_zim()
pytestmark = pytest.mark.skipif(ZIM is None, reason="no dated ZIM on this host")


@pytest.fixture(scope="module")
def archive():
    return libzim_reader.Archive(str(ZIM))


@pytest.fixture(scope="module")
def sample_records(archive):
    """A few thousand real search records from this ZIM."""
    # Spread across the whole archive, not the first few chunks: chunks are
    # keyed by name prefix, so taking the head samples only digits and
    # punctuation and misses every accented place name in the region.
    paths = [archive._get_entry_by_id(i).path for i in range(archive.all_entry_count)]
    chunks = [p for p in paths
              if p.startswith("search-data/") and not p.endswith("manifest.json")]
    if not chunks:
        return []
    step = max(1, len(chunks) // 60)
    recs = []
    for path in chunks[::step]:
        try:
            recs.extend(json.loads(bytes(archive.get_entry_by_path(path).get_item().content)))
        except Exception:                                    # noqa: BLE001
            continue
        if len(recs) > 20000:
            break
    return recs


def test_the_zim_has_search_data_at_all(sample_records):
    assert sample_records, f"{ZIM.name} yielded no search records"


def test_every_record_has_a_name_and_a_position(sample_records):
    bad = [r for r in sample_records
           if not r.get("n") or not isinstance(r.get("a"), (int, float))
           or not isinstance(r.get("o"), (int, float))]
    assert not bad[:5] and not bad, f"{len(bad)} records unusable, e.g. {bad[:2]}"


def test_coordinates_are_on_earth(sample_records):
    off = [r for r in sample_records
           if not (-90 <= r["a"] <= 90) or not (-180 <= r["o"] <= 180)]
    assert not off, f"off-planet coordinates: {off[:3]}"


def test_coordinate_precision_is_metre_scale_not_coarser(sample_records):
    """The rounding must stay fine enough to place a pin on the right building.

    5 dp is ~1.1 m at the equator. If someone drops it to 3 dp to save more
    bytes, a cafe moves ~110 m -- across the street and round the corner --
    and this test says so before it ships.
    """
    worst = 0
    for r in sample_records:
        for v in (r["a"], r["o"]):
            s = f"{v!r}"
            if "." in s and "e" not in s:
                worst = max(worst, len(s.split(".", 1)[1]))
    assert worst >= 4, (
        f"coordinates carry at most {worst} decimals (~{10 ** (5 - worst) * 1.1:.0f} m); "
        "too coarse to place a pin accurately")


def test_rounding_did_not_collapse_distinct_places(sample_records):
    """Rounding must not merge neighbouring places into one point.

    Two cafes 3 m apart should stay two points. If a future change coarsens
    the grid, distinct records start sharing coordinates and 'Nearby' and the
    map pins degrade silently.
    """
    named = [r for r in sample_records if r.get("n")]
    seen = {}
    collisions = 0
    for r in named:
        key = (round(r["a"], 5), round(r["o"], 5))
        if key in seen and seen[key] != r["n"]:
            collisions += 1
        seen.setdefault(key, r["n"])
    # Genuine co-located POIs exist (shops in one mall), so allow a slice.
    assert collisions / max(1, len(named)) < 0.05, (
        f"{collisions}/{len(named)} differently-named places share a rounded point")


def test_full_text_search_returns_hits(archive):
    """The Xapian index answers, and answers about THIS region."""
    search = pytest.importorskip("libzim.search")
    if not archive.has_fulltext_index:
        pytest.skip("no fulltext index in this ZIM")
    searcher = search.Searcher(archive)
    hits = searcher.search(search.Query().set_query("park")).getEstimatedMatches()
    assert hits > 0, "'park' returned nothing; the fulltext index is not answering"


def test_search_index_holds_no_viewer_chrome(archive):
    """UI text must not pollute results (the lawzim IndexData problem).

    April 2026 builds indexed viewer chrome and carried a 29.5 MB index
    against today's 15.5 MB. Current builds index a curated place list.
    """
    search = pytest.importorskip("libzim.search")
    if not archive.has_fulltext_index:
        pytest.skip("no fulltext index in this ZIM")
    searcher = search.Searcher(archive)
    for phrase in ('"Food & Drink"', "maplibre", '"Data Sources"'):
        n = searcher.search(search.Query().set_query(phrase)).getEstimatedMatches()
        assert n == 0, f"UI phrase {phrase} matched {n} documents"


def test_diacritics_fold(archive, sample_records):
    """An unaccented query must find an accented name.

    Verified on iceland 2026-09-25: Reykjavik/Reykjavík/reykjavik all return
    3, Abaejara/Ábæjará both return 13. Diacritic folding and case folding
    work; see the next test for what does not.
    """
    search = pytest.importorskip("libzim.search")
    if not archive.has_fulltext_index:
        pytest.skip("no fulltext index in this ZIM")

    def strip_marks(n):
        return "".join(c for c in unicodedata.normalize("NFKD", n)
                       if not unicodedata.combining(c))

    LIGATURES = "æÆøØþÞðÐßłŁ"
    searcher = search.Searcher(archive)

    def hits(q):
        return searcher.search(search.Query().set_query(q)).getEstimatedMatches()

    # The fulltext index covers a SUBSET of the search records (xapianbuilder
    # is fed "N docs of M total"), so most names return 0 in either form.
    # Only a name that is actually indexed can say anything about folding.
    pair = None
    for r in sample_records:
        n = (r.get("n") or "").strip()
        if " " in n or len(n) <= 4 or any(c in LIGATURES for c in n):
            continue
        folded = strip_marks(n)
        if folded == n or not folded.isascii():
            continue
        if hits(n) > 0:
            pair = (n, folded)
            break
    if not pair:
        pytest.skip("no indexed diacritic-only place name in the sample")
    assert hits(pair[1]) > 0, (
        f"{pair[0]!r} is findable but {pair[1]!r} is not; diacritics are not folding")


@pytest.mark.xfail(reason="known gap: the index folds diacritics but does not "
                          "expand ligatures/letters (ae, th, d, o)",
                   strict=False)
def test_letter_expansions_are_searchable(archive, sample_records):
    """Known gap, recorded so it flips green when fixed.

    Measured on iceland 2026-09-25:
        'Thingvellir' -> 0     while 'Þingvellir' -> 267
        'Abaejara'    -> 0     while 'Ábæjará'    -> 13
    A reader on an English keyboard cannot type þ, ð or æ, so those places
    are unreachable by search. Affects iceland, the faroes and the nordics
    (o for ø) most. Fixing it belongs in the index builder -- index both the
    original and an expanded form -- not in the viewer.
    """
    search = pytest.importorskip("libzim.search")
    if not archive.has_fulltext_index:
        pytest.skip("no fulltext index in this ZIM")
    EXPANSIONS = (("æ", "ae"), ("Æ", "AE"), ("ø", "o"), ("Ø", "O"),
                  ("þ", "th"), ("Þ", "TH"), ("ð", "d"), ("Ð", "D"))
    pair = None
    for r in sample_records:
        n = (r.get("n") or "").strip()
        if " " in n or len(n) <= 4:
            continue
        out = n
        for a, b in EXPANSIONS:
            out = out.replace(a, b)
        out = "".join(c for c in unicodedata.normalize("NFKD", out)
                      if not unicodedata.combining(c))
        if out != n and out.isascii():
            searcher0 = search.Searcher(archive)
            if searcher0.search(search.Query().set_query(n)).getEstimatedMatches() > 0:
                pair = (n, out)
                break
    if not pair:
        pytest.skip("no expandable place name in the sample")
    searcher = search.Searcher(archive)
    assert searcher.search(search.Query().set_query(pair[1])).getEstimatedMatches() > 0, (
        f"{pair[0]!r} is not findable as {pair[1]!r}")


def test_manifest_agrees_with_the_chunks_on_disk(archive):
    """A manifest naming a chunk the ZIM lacks makes search 404 mid-query."""
    try:
        man = json.loads(bytes(
            archive.get_entry_by_path("search-data/manifest.json").get_item().content))
    except Exception:                                        # noqa: BLE001
        pytest.skip("no search manifest")
    chunks = man.get("chunks") or {}
    names = list(chunks) if isinstance(chunks, dict) else list(chunks)
    missing = [c for c in names[:200]
               if not archive.has_entry_by_path(f"search-data/{c}.json")]
    assert not missing, f"manifest names chunks that are absent: {missing[:5]}"
