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
import unicodedata
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
libzim_reader = pytest.importorskip("libzim.reader")


# Entry enumeration is linear in the archive, so a continent-scale ZIM turns
# this module into a 20-minute disk hog -- and the newest dated build is often
# exactly that, still being uploaded. Prefer the newest build that is small
# enough to scan quickly; fall back to the newest of any size.
_MAX_TEST_ZIM_BYTES = int(os.environ.get("STREETZIM_TEST_ZIM_MAX_GB", "3")) * 1000 ** 3


def _pick_zim():
    env = os.environ.get("STREETZIM_TEST_ZIM")
    if env:
        return Path(env)
    dated = sorted(ROOT.glob("osm-*-20??-??-??.zim"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not dated:
        return None
    for candidate in dated:
        if candidate.stat().st_size <= _MAX_TEST_ZIM_BYTES:
            return candidate
    return dated[0]


ZIM = _pick_zim()
pytestmark = pytest.mark.skipif(ZIM is None, reason="no dated ZIM on this host")


def _candidate_zims(limit=6):
    """Small dated builds, newest first — for tests that need the right region.

    Diacritic folding can only be observed on a ZIM whose index actually holds
    an accented name, and the default pick is whatever built most recently. On
    2026-09-27 that was switzerland-light, so both folding tests skipped and
    the suite reported green while checking nothing. Widening the search to a
    handful of builds makes them run on any host that has an Icelandic or
    Nordic region lying around.
    """
    env = os.environ.get("STREETZIM_TEST_ZIM")
    if env:
        return [Path(env)]
    small = [z for z in sorted(ROOT.glob("osm-*-20??-??-??.zim"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
             if z.stat().st_size <= _MAX_TEST_ZIM_BYTES]
    # Newest first, but wide enough to reach a region with the script a given
    # test needs: the eight newest builds on this host held no æ/ø/þ at all,
    # so a narrow list turned the ligature check into a permanent skip.
    return small[:limit]


# Regions whose place names actually use æ, ø, þ or ð. Scanning every small
# build to find one costs minutes on a host that is also running a build; the
# script lives in a known handful of regions, so ask them directly.
LIGATURE_REGIONS = ("iceland", "nordics", "faroes", "baltics", "poland",
                    "britain-ireland", "europe")


def _ligature_zims(limit=2):
    env = os.environ.get("STREETZIM_TEST_ZIM")
    if env:
        return [Path(env)]
    out = []
    for z in sorted(ROOT.glob("osm-*-20??-??-??.zim"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        if z.stat().st_size > _MAX_TEST_ZIM_BYTES:
            continue
        if any(f"osm-{r}-" in z.name for r in LIGATURE_REGIONS):
            out.append(z)
        if len(out) >= limit:
            break
    return out

def _first_id_at_or_after(archive, prefix):
    """Lowest entry id whose path sorts at or after `prefix`.

    libzim stores entries sorted by path, which is what get_entry_by_path's
    own lookup relies on, so a bisect over ids needs no enumeration.
    """
    lo, hi = 0, archive.all_entry_count
    while lo < hi:
        mid = (lo + hi) // 2
        if archive._get_entry_by_id(mid).path < prefix:
            lo = mid + 1
        else:
            hi = mid
    return lo


@pytest.fixture(scope="module")
def archive():
    return libzim_reader.Archive(str(ZIM))


@pytest.fixture(scope="module")
def sample_records(archive):
    """A few thousand real search records from this ZIM."""
    # Two constraints at once. The sample must SPREAD across the whole
    # search-data range -- chunks are keyed by name prefix, so the head holds
    # only digits and punctuation and would miss every accented place name --
    # and it must not enumerate the archive, which is tens of millions of
    # entries on a continent build. Entry ids are in sorted path order
    # (verified: 4,004 strided reads of a real ZIM, zero inversions), so
    # binary-search the "search-data/" block and stride inside it.
    lo = _first_id_at_or_after(archive, "search-data/")
    hi = _first_id_at_or_after(archive, "search-data0")   # '0' is the next char after '/'
    chunks = [archive._get_entry_by_id(i).path for i in range(lo, hi)]
    chunks = [c for c in chunks if not c.endswith("manifest.json")]
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


def test_diacritics_fold():
    """An unaccented query must find an accented name.

    Verified on iceland 2026-09-25: Reykjavik/Reykjavík/reykjavik all return
    3, Abaejara/Ábæjará both return 13. Diacritic folding and case folding
    work; see the next test for what does not.

    Walks several small builds rather than only the newest, because the
    newest may be a region with no accents at all -- and a skip that looks
    like a pass is how this check went unobserved.
    """
    search = pytest.importorskip("libzim.search")
    checked = []
    for path in _candidate_zims():
        arc = libzim_reader.Archive(str(path))
        if not arc.has_fulltext_index:
            continue
        pair = _find_folding_pair(arc, search, _records_from(arc))
        if not pair:
            continue
        checked.append(path.name)
        searcher = search.Searcher(arc)
        hits = searcher.search(search.Query().set_query(pair[1])).getEstimatedMatches()
        assert hits > 0, (
            f"{pair[0]!r} is findable in {path.name} but {pair[1]!r} is not; "
            "diacritics are not folding")
        return
    pytest.skip(f"no indexed diacritic-only place name in {len(_candidate_zims())} builds")


def _strip_marks(name):
    return "".join(c for c in unicodedata.normalize("NFKD", name)
                   if not unicodedata.combining(c))


# Letters libzim does not fold to ASCII; a name containing one tells us nothing
# about diacritic folding, so they are excluded from that test's candidates.
LIGATURES = "æÆøØþÞðÐßłŁ"


def _records_from(arc):
    lo = _first_id_at_or_after(arc, "search-data/")
    hi = _first_id_at_or_after(arc, "search-data0")
    chunks = [arc._get_entry_by_id(i).path for i in range(lo, hi)
              if not arc._get_entry_by_id(i).path.endswith("manifest.json")]
    if not chunks:
        return []
    step = max(1, len(chunks) // 60)
    recs = []
    for path in chunks[::step]:
        try:
            recs.extend(json.loads(bytes(arc.get_entry_by_path(path).get_item().content)))
        except Exception:                                    # noqa: BLE001
            continue
        if len(recs) > 20000:
            break
    return recs


def _find_folding_pair(arc, search, records):
    """A single-word indexed name whose only ASCII difference is its accents."""
    searcher = search.Searcher(arc)
    for r in records:
        name = (r.get("n") or "").strip()
        if " " in name or len(name) <= 4 or any(c in LIGATURES for c in name):
            continue
        folded = _strip_marks(name)
        if folded == name or not folded.isascii():
            continue
        if searcher.search(search.Query().set_query(name)).getEstimatedMatches() > 0:
            return name, folded
    return None


@pytest.mark.xfail(reason="known gap: the index folds diacritics but does not "
                          "expand ligatures/letters (ae, th, d, o)",
                   strict=False)
def test_letter_expansions_are_searchable():
    """Known gap, recorded so it flips green when fixed.

    Measured on iceland 2026-09-25:
        'Thingvellir' -> 0     while 'Þingvellir' -> 267
        'Abaejara'    -> 0     while 'Ábæjará'    -> 13
    A reader on an English keyboard cannot type þ, ð or æ, so those places
    are unreachable by search. Affects iceland, the faroes and the nordics
    (o for ø) most. Fixing it belongs in the index builder -- index both the
    original and an expanded form -- not in the viewer.

    Walks the same candidate builds as the folding test: pinned to the newest
    build alone this only ever skipped, which records nothing.
    """
    search = pytest.importorskip("libzim.search")
    EXPANSIONS = (("æ", "ae"), ("Æ", "AE"), ("ø", "o"), ("Ø", "O"),
                  ("þ", "th"), ("Þ", "TH"), ("ð", "d"), ("Ð", "D"))
    for path in _ligature_zims():
        arc = libzim_reader.Archive(str(path))
        if not arc.has_fulltext_index:
            continue
        searcher = search.Searcher(arc)
        # Bounded: a ligature name, if the region has any, turns up early in a
        # 20k-record sample, and an unbounded walk across eight builds runs for
        # minutes against the disk a build is already using.
        for r in _records_from(arc)[:6000]:
            name = (r.get("n") or "").strip()
            if " " in name or len(name) <= 4:
                continue
            # Must actually contain a letter that needs EXPANDING. Without this
            # the test accepted any accented name, stripped its marks and
            # duplicated the folding test above -- it xpassed on
            # 'Adandjro-akodé' -> 'Adandjro-akode', which says nothing about æ.
            if not any(a in name for a, _ in EXPANSIONS):
                continue
            out = name
            for a, b in EXPANSIONS:
                out = out.replace(a, b)
            out = _strip_marks(out)
            if out == name or not out.isascii():
                continue
            if searcher.search(search.Query().set_query(name)).getEstimatedMatches() == 0:
                continue
            assert searcher.search(
                search.Query().set_query(out)).getEstimatedMatches() > 0, (
                f"{name!r} is not findable as {out!r} in {path.name}")
            return
    pytest.skip(f"no expandable place name in {[p.name for p in _ligature_zims()]}")


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
