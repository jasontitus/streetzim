"""cloud/wikimedia_http.py, and the no-cache-on-transient rules of
cloud/wiki_articles.py and cloud/wikidata_titles.py built on it.

Every HTTP call is mocked (urllib.request.urlopen) and time.sleep is
recorded, never slept; nothing here reaches Wikipedia or Wikidata.
"""
from __future__ import annotations

import email.message
import email.utils
import io
import json
import os
import urllib.error
import urllib.parse
from contextlib import contextmanager

import pytest

from cloud import wiki_articles as wa
from cloud import wikidata_titles as wt
from cloud import wikimedia_http as wm

ARTICLE = '<div class="mw-parser-output"><p>A real article body.</p></div>'
DISAMBIG = '<div class="mw-parser-output"><p><b>Aurora</b> may refer to:</p></div>'


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.test", code, "err", hdrs, None)


@contextmanager
def _body(payload):
    yield io.BytesIO(json.dumps(payload).encode("utf-8"))


class FakeAPI:
    """urlopen stand-in: `script` is consumed one answer per request (an
    exception to raise or a JSON payload); `default` answers after that."""

    def __init__(self, *script, default=None):
        self.script = list(script)
        self.default = default
        self.urls: list[str] = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        self.ua = req.get_header("User-agent")
        item = self.script.pop(0) if self.script else self.default
        if item is None:
            raise AssertionError(f"unexpected request {req.full_url}")
        if isinstance(item, BaseException):
            raise item
        return _body(item)


@pytest.fixture
def sleeps(monkeypatch):
    # A fake clock: a recorded sleep advances time.monotonic, as a real one would.
    got: list[float] = []
    now = [1000.0]

    def sleep(sec: float) -> None:
        got.append(sec)
        now[0] += sec
    monkeypatch.setattr(wm.time, "sleep", sleep)
    monkeypatch.setattr(wm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(wm.random, "random", lambda: 0.5)
    monkeypatch.delenv(wm.REQUIRE_ENV, raising=False)
    return got


def use(monkeypatch, api: FakeAPI) -> FakeAPI:
    monkeypatch.setattr(wm.urllib.request, "urlopen", api)
    return api


def parse_ok(html: str) -> dict:
    return {"parse": {"title": "T", "text": html}}


# ---- Retry-After, backoff, pacing, User-Agent ---------------------------

def test_retry_after_seconds_and_http_date():
    now = 1_700_000_000.0
    assert wm.parse_retry_after("120") == 120.0
    assert wm.parse_retry_after(" 7 ") == 7.0
    date = email.utils.formatdate(now + 30, usegmt=True)
    assert wm.parse_retry_after(date, now=now) == pytest.approx(30.0)
    past = email.utils.formatdate(now - 30, usegmt=True)
    assert wm.parse_retry_after(past, now=now) == 0.0
    for junk in (None, "", "soon", "-5", "1.5"):
        assert wm.parse_retry_after(junk) is None


def test_backoff_honours_retry_after_with_a_cap_else_jittered_exponential():
    half = lambda: 0.5  # noqa: E731
    assert wm.backoff_delay(0, 10.0, rng=half) == pytest.approx(10.5)
    assert wm.backoff_delay(3, 3600.0, max_wait=120, rng=half) == 120
    lo = [wm.backoff_delay(a, None, base=2, rng=lambda: 0.0) for a in range(4)]
    hi = [wm.backoff_delay(a, None, base=2, rng=lambda: 1.0) for a in range(4)]
    assert lo == [1, 2, 4, 8] and hi == [2, 4, 8, 16]
    assert wm.backoff_delay(10, None, base=2, max_wait=60, rng=half) == 60


def test_pacer_widens_on_rate_limit_and_eases_back():
    p = wm.Pacer(1.0, max_interval=30)
    p.rate_limited()
    assert p.current == 2.0
    p.rate_limited(retry_after=20)
    assert p.current == 20.0
    p.rate_limited(retry_after=500)
    assert p.current == 30.0              # capped
    for _ in range(100):
        p.succeeded()
    assert p.current == 1.0               # never below the polite base


def test_user_agent_names_a_contact(monkeypatch):
    monkeypatch.delenv(wm.CONTACT_ENV, raising=False)
    ua = wm.user_agent("wiki")
    assert ua.startswith("streetzim-wiki/") and wm.CONTACT_URL in ua
    monkeypatch.setenv(wm.CONTACT_ENV, "ops@example.org")
    assert "ops@example.org" in wm.user_agent("wiki")


def test_429_with_retry_after_waits_that_long(monkeypatch, sleeps):
    api = use(monkeypatch, FakeAPI(http_error(429, "17"), {"ok": 1}))
    assert wm.get_json("https://x.test/a", user_agent="ua") == {"ok": 1}
    assert len(api.urls) == 2
    assert sleeps == [pytest.approx(17.5)]     # Retry-After + jitter


def test_429_with_http_date_retry_after(monkeypatch, sleeps):
    when = email.utils.formatdate(wm.time.time() + 40, usegmt=True)
    use(monkeypatch, FakeAPI(http_error(429, when), {"ok": 1}))
    wm.get_json("https://x.test/a", user_agent="ua")
    assert 38 < sleeps[0] <= 41


def test_429_without_retry_after_backs_off_exponentially(monkeypatch, sleeps):
    use(monkeypatch, FakeAPI(http_error(429), http_error(429), http_error(503), {"ok": 1}))
    wm.get_json("https://x.test/a", user_agent="ua", base=2)
    assert sleeps == [1.5, 3.0, 6.0]


def test_exhausted_429_raises_transient(monkeypatch, sleeps):
    api = use(monkeypatch, FakeAPI(default=http_error(429, "5")))
    with pytest.raises(wm.TransientError) as ei:
        wm.get_json("https://x.test/a", user_agent="ua", retries=3)
    assert ei.value.rate_limited and len(api.urls) == 3 and len(sleeps) == 2


def test_404_is_an_answer_not_retried(monkeypatch, sleeps):
    api = use(monkeypatch, FakeAPI(http_error(404)))
    with pytest.raises(urllib.error.HTTPError):
        wm.get_json("https://x.test/a", user_agent="ua")
    assert len(api.urls) == 1 and sleeps == []


# ---- Wikipedia articles ---------------------------------------------------

def test_article_after_429_with_retry_after_is_cached(monkeypatch, sleeps, tmp_path):
    api = use(monkeypatch, FakeAPI(http_error(429, "9"), parse_ok(ARTICLE)))
    assert wa._fetch_online("Lincoln_Memorial", str(tmp_path), "ua") == ARTICLE
    assert sleeps == [pytest.approx(9.5)]
    html, _ = wa._cache_paths(str(tmp_path), "Lincoln_Memorial")
    assert open(html, encoding="utf-8").read() == ARTICLE
    assert len(api.urls) == 2


def test_article_429_that_exhausts_retries_is_not_cached_and_is_warned(
        monkeypatch, sleeps, tmp_path):
    use(monkeypatch, FakeAPI(parse_ok(ARTICLE), default=http_error(429, "3")))
    logs: list[str] = []
    stored: dict[str, bytes] = {}
    stats = wa.bundle_wiki_articles(
        ["en:Lincoln Memorial", "en:National Mall"],
        lambda p, t, m, c: stored.__setitem__(p, c),
        cache_dir=str(tmp_path), sleep=0, log=logs.append)
    assert stats["bundled"] == 1 and stats["unfetched"] == 1 and stats["rate_limited"] == 1
    assert set(stored) == {"wiki-article/Lincoln_Memorial"}
    assert stats["stored_titles"] == {"Lincoln_Memorial"}      # hasWikiArticles stays true
    # Nothing on disk says National Mall is missing.
    lincoln = wa._cache_paths(str(tmp_path), "Lincoln_Memorial")[0]
    assert os.listdir(tmp_path) == [os.path.basename(lincoln)]
    warn = [m for m in logs if "WARNING" in m]
    assert warn and "1 of 2 articles were NOT fetched" in warn[0]
    assert "1 rate-limited" in warn[0]


def test_require_wiki_fails_a_partial_run(monkeypatch, sleeps, tmp_path):
    use(monkeypatch, FakeAPI(default=http_error(429)))
    monkeypatch.setenv(wm.REQUIRE_ENV, "1")
    with pytest.raises(SystemExit, match=r"STREETZIM_REQUIRE_WIKI=1.*1 of 1"):
        wa.bundle_wiki_articles(["en:X"], lambda *a: None, cache_dir=str(tmp_path),
                                sleep=0, log=lambda *_: None)


@pytest.mark.parametrize("fault", [http_error(500), http_error(502), http_error(503),
                                   TimeoutError("timed out"),
                                   urllib.error.URLError("connection refused"),
                                   ConnectionResetError("reset"),
                                   {"error": {"code": "ratelimited"}},
                                   {"error": {"code": "internal_api_error_DBQueryError"}},
                                   http_error(403)])
def test_transient_failures_are_never_cached(monkeypatch, sleeps, tmp_path, fault):
    use(monkeypatch, FakeAPI(default=fault))
    with pytest.raises(wm.TransientError):
        wa._fetch_online("Some_Page", str(tmp_path), "ua")
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("answer,reason", [
    ({"error": {"code": "missingtitle", "info": "The page doesn't exist."}}, "missingtitle"),
    (parse_ok(""), "no-text"),
    (http_error(404), "http-404"),
])
def test_real_miss_is_cached_with_its_reason(monkeypatch, sleeps, tmp_path, answer, reason):
    use(monkeypatch, FakeAPI(answer))
    assert wa._fetch_online("No_Such_Page", str(tmp_path), "ua") is None
    _, miss = wa._cache_paths(str(tmp_path), "No_Such_Page")
    rec = json.load(open(miss, encoding="utf-8"))
    assert rec["reason"] == reason and rec["title"] == "No_Such_Page" and rec["checked"]
    use(monkeypatch, FakeAPI())      # a second lookup makes no request
    assert wa._fetch_online("No_Such_Page", str(tmp_path), "ua") is None


def test_disambiguation_is_cached_and_skipped_without_refetch(monkeypatch, sleeps, tmp_path):
    use(monkeypatch, FakeAPI(parse_ok(DISAMBIG)))
    stored: dict = {}
    for _ in range(2):              # the second build is served from the cache
        stats = wa.bundle_wiki_articles(["en:Aurora"], lambda *a: stored.setdefault(a[0], a),
                                        cache_dir=str(tmp_path), sleep=0,
                                        log=lambda *_: None)
        assert stats["disambiguation_skipped"] == 1 and stored == {}
        assert stats["unfetched"] == 0
        use(monkeypatch, FakeAPI())


def test_old_poisoned_cache_is_healed(monkeypatch, sleeps, tmp_path):
    # An old build left an empty .html for each of these: one really had no
    # page, one was a 429, one hits a 429 again now.
    d = str(tmp_path)
    for t in ("Gone_Page", "National_Academy_of_Sciences", "Still_Throttled"):
        open(wa._cache_paths(d, t)[0], "w").close()
    hit_html, hit_miss = wa._cache_paths(d, "Real_Hit")
    open(hit_html, "w", encoding="utf-8").write(ARTICLE)

    def answer(req, timeout=None):
        page = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)["page"][0]
        api.urls.append(page)
        if page == "Gone Page":
            return _body({"error": {"code": "missingtitle"}})
        if page == "National Academy of Sciences":
            return _body(parse_ok(ARTICLE))
        if page == "Still Throttled":
            raise http_error(429, "1")
        raise AssertionError(page)
    api = FakeAPI()
    monkeypatch.setattr(wm.urllib.request, "urlopen", answer)

    stats = wa.bundle_wiki_articles(
        ["en:Gone Page", "en:National Academy of Sciences", "en:Still Throttled",
         "en:Real Hit"], lambda *a: None, cache_dir=d, sleep=0, log=lambda *_: None)
    assert stats["bundled"] == 2 and stats["unfetched"] == 1
    assert "Real Hit" not in api.urls                 # hits are never re-checked
    gone_html, gone_miss = wa._cache_paths(d, "Gone_Page")
    assert os.path.exists(gone_miss) and not os.path.exists(gone_html)
    nas_html, _ = wa._cache_paths(d, "National_Academy_of_Sciences")
    assert open(nas_html, encoding="utf-8").read() == ARTICLE
    thr_html, thr_miss = wa._cache_paths(d, "Still_Throttled")
    assert os.path.getsize(thr_html) == 0 and not os.path.exists(thr_miss)  # asked again next time

    # Next build: only the still-unverified title is requested.
    api.urls.clear()
    wa.bundle_wiki_articles(
        ["en:Gone Page", "en:National Academy of Sciences", "en:Still Throttled",
         "en:Real Hit"], lambda *a: None, cache_dir=d, sleep=0, log=lambda *_: None)
    assert api.urls == ["Still Throttled"] * 5


def test_cache_hits_cost_no_delay(monkeypatch, sleeps, tmp_path):
    use(monkeypatch, FakeAPI(parse_ok(ARTICLE), parse_ok(ARTICLE)))
    titles = ["en:A", "en:B"]
    wa.bundle_wiki_articles(titles, lambda *a: None, cache_dir=str(tmp_path), sleep=5,
                            log=lambda *_: None)
    assert sum(sleeps) <= 5.01           # one polite gap between the two requests
    sleeps.clear()
    use(monkeypatch, FakeAPI())
    wa.bundle_wiki_articles(titles, lambda *a: None, cache_dir=str(tmp_path), sleep=5,
                            log=lambda *_: None)
    assert sleeps == []


def test_gives_up_after_a_long_streak_of_failures(monkeypatch, sleeps, tmp_path):
    api = use(monkeypatch, FakeAPI(default=http_error(503)))
    titles = [f"en:T{i}" for i in range(40)]
    stats = wa.bundle_wiki_articles(titles, lambda *a: None, cache_dir=str(tmp_path),
                                    sleep=0, log=lambda *_: None)
    assert stats["unfetched"] == 40 and stats["rate_limited"] == 0
    assert len(api.urls) == wa._GIVE_UP_AFTER * 5
    assert os.listdir(tmp_path) == []


def test_default_user_agent_is_sent(monkeypatch, sleeps, tmp_path):
    api = use(monkeypatch, FakeAPI(parse_ok(ARTICLE)))
    wa.bundle_wiki_articles(["en:A"], lambda *a: None, cache_dir=str(tmp_path), sleep=0,
                            log=lambda *_: None)
    assert wm.CONTACT_URL in api.ua


# ---- Wikidata titles --------------------------------------------------------

def entities(mapping: dict) -> dict:
    out = {}
    for q, title in mapping.items():
        if title is None:
            out[q] = {"id": q, "missing": ""}
        else:
            out[q] = {"id": q, "sitelinks": {"enwiki": {"title": title}} if title else {}}
    return {"entities": out}


def test_wikidata_429_with_retry_after_then_answer(monkeypatch, sleeps, tmp_path):
    cache = str(tmp_path / "t.json")
    use(monkeypatch, FakeAPI(http_error(429, "30"),
                             entities({"Q1": "One", "Q2": "", "Q3": None})))
    assert wt.resolve_qids(["Q1", "Q2", "Q3"], cache_path=cache, sleep=0) == {"Q1": "One"}
    assert sleeps == [pytest.approx(30.5)]
    assert json.load(open(cache)) == {"Q1": "One", "Q2": "", "Q3": ""}


def test_wikidata_exhausted_429_caches_nothing_and_warns(monkeypatch, sleeps, tmp_path,
                                                         capsys):
    cache = str(tmp_path / "t.json")
    qids = sorted(f"Q{i}" for i in range(1, 121))   # three batches, in request order
    first = entities({q: f"T{q}" for q in qids[:50]})
    use(monkeypatch, FakeAPI(first, default=http_error(429, "2")))
    out = wt.resolve_qids(qids, cache_path=cache, sleep=0)
    assert len(out) == 50
    assert set(json.load(open(cache))) == set(qids[:50])    # no "" for the rest
    err = capsys.readouterr().err
    assert "left 70 of 120 Q-IDs unresolved" in err and "HTTP 429" in err

    monkeypatch.setenv(wm.REQUIRE_ENV, "1")
    use(monkeypatch, FakeAPI(default=http_error(429)))
    with pytest.raises(SystemExit, match="STREETZIM_REQUIRE_WIKI=1"):
        wt.resolve_qids(qids, cache_path=cache, sleep=0)


def test_wikidata_5xx_then_answer(monkeypatch, sleeps):
    use(monkeypatch, FakeAPI(http_error(502), http_error(503), entities({"Q9": "Nine"})))
    assert wt.resolve_qids(["Q9"], sleep=0) == {"Q9": "Nine"}
    assert sleeps == [1.5, 3.0]


def test_wikidata_error_body_is_not_cached_as_misses(monkeypatch, sleeps, tmp_path, capsys):
    cache = str(tmp_path / "t.json")
    qids = sorted(f"Q{i}" for i in range(1, 61))    # two batches, in request order
    use(monkeypatch, FakeAPI({"error": {"code": "no-such-entity"}},
                             entities(dict.fromkeys(qids[50:], "T"))))
    out = wt.resolve_qids(qids, cache_path=cache, sleep=0)
    assert set(out) == set(qids[50:])
    assert set(json.load(open(cache))) == set(qids[50:])
    assert "refused a batch of 50" in capsys.readouterr().err


def test_wikidata_entity_absent_from_answer_is_not_a_miss(monkeypatch, sleeps, tmp_path):
    cache = str(tmp_path / "t.json")
    use(monkeypatch, FakeAPI(entities({"Q1": "One"})))
    wt.resolve_qids(["Q1", "Q2"], cache_path=cache, sleep=0)
    assert json.load(open(cache)) == {"Q1": "One"}


def test_wikidata_malformed_ids_are_not_requested(monkeypatch, sleeps):
    api = use(monkeypatch, FakeAPI(entities({"Q5": "Five"})))
    assert wt.resolve_qids(["Q5", "Q1;Q2", "Q05", "Qx"], sleep=0) == {"Q5": "Five"}
    assert "ids=Q5&" in api.urls[0]


def test_wikidata_poisoned_cache_is_healed(monkeypatch, sleeps, tmp_path, capsys):
    # A malformed id once spoiled its batch: every id in it cached as "".
    cache = str(tmp_path / "t.json")
    json.dump({"Q1;Q2": "", "Q5": "", "Q7": "Seven"}, open(cache, "w"))
    api = use(monkeypatch, FakeAPI(entities({"Q5": "Five"})))
    assert wt.resolve_qids(["Q5", "Q7"], cache_path=cache, sleep=0) == {
        "Q5": "Five", "Q7": "Seven"}
    assert len(api.urls) == 1 and "Q7" not in api.urls[0]      # hits kept
    assert json.load(open(cache)) == {"Q5": "Five", "Q7": "Seven"}
    assert "re-checking 2 cached misses" in capsys.readouterr().err
    # A clean cache is left alone: its misses are real.
    use(monkeypatch, FakeAPI())
    assert wt.resolve_qids(["Q5", "Q7"], cache_path=cache, sleep=0) == {
        "Q5": "Five", "Q7": "Seven"}
