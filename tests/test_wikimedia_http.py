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


def http_error(code: int, retry_after: str | None = None,
               api_error: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    if api_error is not None:
        hdrs["MediaWiki-API-Error"] = api_error
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
    for env in (wm.REQUIRE_ENV, wm.GAP_ENV, wm.MAX_PER_MIN_ENV):
        monkeypatch.delenv(env, raising=False)
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
    for junk in (None, "", "soon", "-5", "1.5", "\u00b2", "\u0663", "Mon, 99 Foo 2026"):
        assert wm.parse_retry_after(junk) is None       # never raises
    assert wm.parse_retry_after("9" * 30) == 1e9          # no float overflow
    minus0 = email.utils.formatdate(now + 60, usegmt=True).replace("GMT", "-0000")
    assert wm.parse_retry_after(minus0, now=now) == pytest.approx(60.0)


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
    # At least 5 s after a 429 that names no time (Wikimedia's rate-limit
    # page); a 5xx takes the plain exponential backoff.
    use(monkeypatch, FakeAPI(http_error(429), http_error(429), http_error(503), {"ok": 1}))
    wm.get_json("https://x.test/a", user_agent="ua", base=2)
    assert sleeps == [5.0, 5.0, 6.0]


def test_exhausted_429_raises_transient(monkeypatch, sleeps):
    api = use(monkeypatch, FakeAPI(default=http_error(429, "5")))
    with pytest.raises(wm.TransientError) as ei:
        wm.get_json("https://x.test/a", user_agent="ua", retries=3)
    assert ei.value.rate_limited and len(api.urls) == 3 and len(sleeps) == 2


def test_non_ascii_retry_after_does_not_crash_a_build(monkeypatch, sleeps, tmp_path):
    use(monkeypatch, FakeAPI(http_error(429, "\u00b2"), parse_ok(ARTICLE)))
    stats = wa.bundle_wiki_articles(["en:A"], lambda *a: None, cache_dir=str(tmp_path),
                                    sleep=0, log=lambda *_: None)
    assert stats["bundled"] == 1 and sleeps == [5.0]       # as with no Retry-After


def test_wait_budget_bounds_a_hard_throttle(monkeypatch, sleeps, tmp_path):
    monkeypatch.setenv(wm.BUDGET_ENV, "300")
    api = use(monkeypatch, FakeAPI(default=http_error(429, "120")))
    logs: list[str] = []
    stats = wa.bundle_wiki_articles([f"en:X{i}" for i in range(30)], lambda *a: None,
                                    cache_dir=str(tmp_path), sleep=1, log=logs.append)
    assert stats["unfetched"] == 30 and stats["rate_limited"] >= 1
    assert sum(sleeps) <= 300 + 30 * 1.01          # the budget, plus base gaps at most
    assert len(api.urls) < 10
    assert any("wait budget of 300s spent" in m for m in logs)


def test_404_is_an_answer_not_retried(monkeypatch, sleeps):
    api = use(monkeypatch, FakeAPI(http_error(404)))
    with pytest.raises(urllib.error.HTTPError):
        wm.get_json("https://x.test/a", user_agent="ua")
    assert len(api.urls) == 1 and sleeps == []



# ---- article pacing: from the end of each response, no fixed sleep ------

class Clock:
    """A fake clock: sleeps are recorded, and both sleeps and the time a
    TimedAPI answer takes advance it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, sec: float) -> None:
        self.sleeps.append(sec)
        self.now += sec


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(wm.time, "sleep", c.sleep)
    monkeypatch.setattr(wm.time, "monotonic", lambda: c.now)
    monkeypatch.setattr(wm.random, "random", lambda: 0.5)
    for env in (wm.REQUIRE_ENV, wm.GAP_ENV, wm.MAX_PER_MIN_ENV):
        monkeypatch.delenv(env, raising=False)
    return c


class TimedAPI(FakeAPI):
    """FakeAPI whose n-th answer takes `took(n)` seconds on `clock`;
    records when each request started."""

    def __init__(self, clock: Clock, *script, default=None, took=lambda n: 0.2):
        super().__init__(*script, default=default)
        self.clock = clock
        self.took = took
        self.starts: list[float] = []

    def __call__(self, req, timeout=None):
        self.starts.append(self.clock.now)
        self.clock.now += self.took(len(self.starts) - 1)
        return super().__call__(req, timeout)


def test_no_fixed_sleep_after_a_response(monkeypatch, clock, tmp_path):
    # Answers taking 0.5 s: the only wait is the 0.1 s gap after each one,
    # not the old fixed 1 s.
    api = use(monkeypatch, TimedAPI(clock, default=parse_ok(ARTICLE), took=lambda n: 0.5))
    stats = wa.bundle_wiki_articles([f"en:T{i}" for i in range(6)], lambda *a: None,
                                    cache_dir=str(tmp_path), log=lambda *_: None)
    assert stats["bundled"] == 6
    assert clock.sleeps == [pytest.approx(wm.DEFAULT_GAP)] * 5
    assert all(b - a == pytest.approx(0.6) for a, b in zip(api.starts, api.starts[1:]))


def test_fast_responses_stay_under_the_per_minute_limit(monkeypatch, clock, tmp_path):
    # 0.05 s answers plus a 0.1 s gap would be 400 requests a minute; the
    # request-rate floor holds them to 120 (Wikimedia allows 200).
    api = use(monkeypatch, TimedAPI(clock, default=parse_ok(ARTICLE), took=lambda n: 0.05))
    wa.bundle_wiki_articles([f"en:T{i}" for i in range(20)], lambda *a: None,
                            cache_dir=str(tmp_path), log=lambda *_: None)
    spacing = [b - a for a, b in zip(api.starts, api.starts[1:])]
    assert min(spacing) >= 60 / wm.DEFAULT_MAX_PER_MIN - 1e-9
    assert max(spacing) == pytest.approx(60 / wm.DEFAULT_MAX_PER_MIN)
    # STREETZIM_WIKI_MAX_PER_MIN=0 lifts the cap: only the gap is left.
    monkeypatch.setenv(wm.MAX_PER_MIN_ENV, "0")
    clock.sleeps.clear()
    use(monkeypatch, TimedAPI(clock, default=parse_ok(ARTICLE), took=lambda n: 0.05))
    wa.bundle_wiki_articles([f"en:U{i}" for i in range(3)], lambda *a: None,
                            cache_dir=str(tmp_path), log=lambda *_: None)
    assert clock.sleeps == [pytest.approx(0.1)] * 2


def test_slow_answer_is_followed_by_a_longer_pause(monkeypatch, clock, tmp_path):
    # The robot policy: over 1 s to serve -> wait 5 s before the next.
    use(monkeypatch, TimedAPI(clock, default=parse_ok(ARTICLE),
                              took=lambda n: 1.5 if n == 1 else 0.45))
    wa.bundle_wiki_articles([f"en:T{i}" for i in range(4)], lambda *a: None,
                            cache_dir=str(tmp_path), log=lambda *_: None)
    assert clock.sleeps == [pytest.approx(0.1), pytest.approx(5.0), pytest.approx(0.1)]


def test_gap_widens_on_429_and_eases_back(monkeypatch, sleeps, tmp_path):
    use(monkeypatch, FakeAPI(http_error(429, "4"), default=parse_ok(ARTICLE)))
    stats = wa.bundle_wiki_articles([f"en:T{i}" for i in range(5)], lambda *a: None,
                                    cache_dir=str(tmp_path), sleep=0.1,
                                    log=lambda *_: None)
    assert stats["bundled"] == 5 and stats["unfetched"] == 0
    # Retry-After 4 s (+ jitter), then gaps of 4 s easing 10% per success.
    assert sleeps == [pytest.approx(4.5), pytest.approx(3.6), pytest.approx(3.24),
                      pytest.approx(2.916), pytest.approx(2.6244)]
    p = wm.polite_pacer(0.1, 0)
    p.rate_limited(4.0)
    for _ in range(100):
        p.succeeded()
    assert p.current == pytest.approx(0.1)    # all the way back to the base


@pytest.mark.parametrize("fault,widens", [(http_error(503, "7"), True),
                                          (http_error(502), False),
                                          ({"error": {"code": "maxlag"}}, True)])
def test_overload_widens_the_gap_a_bare_5xx_only_retries(monkeypatch, sleeps, fault,
                                                          widens):
    use(monkeypatch, FakeAPI(fault, {"ok": 1}))
    p = wm.Pacer(0.1)
    assert wm.get_json("https://x.test/a", user_agent="ua", pacer=p) == {"ok": 1}
    assert (p.current > 0.1) is widens


def test_gzip_is_asked_for_and_decoded(monkeypatch, sleeps):
    import gzip as _gzip

    class Resp(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    seen = {}

    def urlopen(req, timeout=None):
        seen["ae"] = req.get_header("Accept-encoding")
        return Resp(_gzip.compress(json.dumps(parse_ok(ARTICLE)).encode()))
    monkeypatch.setattr(wm.urllib.request, "urlopen", urlopen)
    assert wm.get_json("https://x.test/a", user_agent="ua") == parse_ok(ARTICLE)
    assert seen["ae"] == "gzip"
    # A truncated gzip body is a transient failure, never a crash.
    monkeypatch.setattr(wm.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp(_gzip.compress(b'{"a": 1}')[:-6]))
    with pytest.raises(wm.TransientError):
        wm.get_json("https://x.test/a", user_agent="ua", retries=1)

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
                                   ["unexpected"], {"batchcomplete": True},
                                   {"parse": "not an object"},
                                   http_error(400), http_error(404), http_error(410),
                                   http_error(401), http_error(403)])
def test_transient_failures_are_never_cached(monkeypatch, sleeps, tmp_path, fault):
    use(monkeypatch, FakeAPI(default=fault))
    with pytest.raises(wm.TransientError):
        wa._fetch_online("Some_Page", str(tmp_path), "ua")
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("answer,reason", [
    ({"error": {"code": "missingtitle", "info": "The page doesn't exist."}}, "missingtitle"),
    (parse_ok(""), "no-text"),
    (http_error(414), "http-414"),
    (http_error(404, api_error="missingtitle"), "missingtitle"),
    (http_error(400, api_error="invalidtitle"), "invalidtitle"),
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
    assert sleeps == [pytest.approx(5.0)]   # one polite gap between the two requests
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


def test_refused_client_stops_at_once_but_cache_hits_still_count(monkeypatch, sleeps,
                                                                 tmp_path):
    d = str(tmp_path)
    open(wa._cache_paths(d, "Cached_Hit")[0], "w", encoding="utf-8").write(ARTICLE)
    api = use(monkeypatch, FakeAPI(default=http_error(403)))
    stats = wa.bundle_wiki_articles(["en:A", "en:B", "en:Cached Hit", "en:C"],
                                    lambda *a: None, cache_dir=d, sleep=0,
                                    log=lambda *_: None)
    assert len(api.urls) == 1                          # 403: no retries, no more titles
    assert stats["stored_titles"] == {"Cached_Hit"} and stats["unfetched"] == 3
    assert os.listdir(d) == [os.path.basename(wa._cache_paths(d, "Cached_Hit")[0])]


def test_after_giving_up_cache_hits_are_still_bundled(monkeypatch, sleeps, tmp_path):
    d = str(tmp_path)
    open(wa._cache_paths(d, "Cached_Hit")[0], "w", encoding="utf-8").write(ARTICLE)
    use(monkeypatch, FakeAPI(default=http_error(503)))
    titles = [f"en:T{i:02d}" for i in range(wa._GIVE_UP_AFTER)] + ["en:Cached Hit"]
    stats = wa.bundle_wiki_articles(titles, lambda *a: None, cache_dir=d, sleep=0,
                                    log=lambda *_: None)
    assert stats["stored_titles"] == {"Cached_Hit"} and stats["bundled"] == 1
    assert stats["unfetched"] == wa._GIVE_UP_AFTER


def test_cache_hits_do_not_reset_the_failure_streak(monkeypatch, sleeps, tmp_path):
    d = str(tmp_path)
    titles = []
    for i in range(40):
        open(wa._cache_paths(d, f"H{i}")[0], "w", encoding="utf-8").write(ARTICLE)
        titles += [f"en:H{i}", f"en:M{i}"]
    api = use(monkeypatch, FakeAPI(default=http_error(503)))
    stats = wa.bundle_wiki_articles(titles, lambda *a: None, cache_dir=d, sleep=0,
                                    log=lambda *_: None)
    assert len(api.urls) == wa._GIVE_UP_AFTER * 5    # then it stops asking
    assert stats["bundled"] == 40 and stats["unfetched"] == 40


def test_legacy_recheck_is_capped_per_build(monkeypatch, sleeps, tmp_path):
    d = str(tmp_path)
    for t in ("L1", "L2", "L3"):
        open(wa._cache_paths(d, t)[0], "w").close()
    monkeypatch.setenv(wa.RECHECK_ENV, "1")
    api = use(monkeypatch, FakeAPI(parse_ok(ARTICLE)))
    logs: list[str] = []
    stats = wa.bundle_wiki_articles(["en:L1", "en:L2", "en:L3"], lambda *a: None,
                                    cache_dir=d, sleep=0, log=logs.append)
    assert len(api.urls) == 1 and stats["rechecked"] == 1 and stats["recheck_left"] == 2
    assert stats["unfetched"] == 0
    assert any("1 of 3 old empty markers to re-check" in m for m in logs)
    assert any("re-checked 1 old empty cache markers; 2 left" in m for m in logs)


def test_default_user_agent_is_sent(monkeypatch, sleeps, tmp_path):
    api = use(monkeypatch, FakeAPI(parse_ok(ARTICLE)))
    wa.bundle_wiki_articles(["en:A"], lambda *a: None, cache_dir=str(tmp_path), sleep=0,
                            log=lambda *_: None)
    assert wm.CONTACT_URL in api.ua


def test_redirect_only_titles_online(monkeypatch, sleeps, tmp_path):
    # One action=parse request per title tells whether it is a redirect
    # (`redirects` in the answer); the answer is cached, and so is the
    # target's text under its own title.
    d = str(tmp_path)
    aalten = {"parse": {"title": "Aalten", "text": ARTICLE}}
    dorp = {"parse": {"title": "Aalten", "text": ARTICLE,
                      "redirects": [{"from": "Aalten (dorp)", "to": "Aalten"}]}}
    pannenberg = {"parse": {"title": "Wolfhart Pannenberg", "text": ARTICLE,
                            "redirects": [{"from": "Pannenberg", "to": "Wolfhart Pannenberg"}]}}
    camp = {"parse": {"title": "De Camp", "text": ARTICLE}}
    api = use(monkeypatch, FakeAPI(aalten, dorp, pannenberg, camp,
                                   http_error(404, api_error="missingtitle")))
    stored = {}
    stats = wa.bundle_wiki_articles(
        ["en:Aalten"], lambda p, t, m, c: stored.__setitem__(p, t), cache_dir=d, sleep=0,
        log=lambda *_: None,
        redirect_only=["nl:Aalten (dorp)", "nl:Pannenberg", "nl:De Camp", "nl:Nergens"])
    assert stored == {"wiki-article/Aalten": "Aalten", "wiki-article/Aalten_(dorp)": "Aalten"}
    assert len(api.urls) == 5 and "redirects=1" in api.urls[1]
    assert (stats["redirects"], stats["redirects_skipped"], stats["unfetched"]) == (1, 3, 0)
    assert wa._cache_state("Wolfhart_Pannenberg", d)[0] == "hit"
    assert wa._redirect_cached("Pannenberg", d) == (True, "Wolfhart_Pannenberg")
    assert wa._redirect_cached("De_Camp", d) == (True, None)      # an article
    assert wa._redirect_cached("Nergens", d) == (True, None)      # no such page
    # The next build asks nothing.
    api = use(monkeypatch, FakeAPI())
    stored.clear()
    wa.bundle_wiki_articles(
        ["en:Aalten"], lambda p, t, m, c: stored.__setitem__(p, t), cache_dir=d, sleep=0,
        log=lambda *_: None,
        redirect_only=["nl:Aalten (dorp)", "nl:Pannenberg", "nl:De Camp", "nl:Nergens"])
    assert api.urls == [] and sorted(stored) == ["wiki-article/Aalten",
                                                 "wiki-article/Aalten_(dorp)"]


def test_redirect_only_title_unanswered_is_not_cached(monkeypatch, sleeps, tmp_path):
    d = str(tmp_path)
    use(monkeypatch, FakeAPI(parse_ok(ARTICLE), default=http_error(503)))
    stats = wa.bundle_wiki_articles(["en:A"], lambda *a: None, cache_dir=d, sleep=0,
                                    log=lambda *_: None, redirect_only=["nl:B"])
    assert stats["unfetched"] == 1 and stats["requested"] == 2
    assert wa._redirect_cached("B", d) == (False, None)


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
    assert sleeps == [1.5, 5.0]      # a 503 without Retry-After waits at least 5 s


def wikidata_api(bad: set[str] = frozenset(), log: list | None = None):
    """wbgetentities stand-in: refuses any batch holding a `bad` id."""
    def answer(req, timeout=None):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        ids = q["ids"][0].split("|")
        assert q["maxlag"] == ["5"]
        if log is not None:
            log.append(ids)
        if bad & set(ids):
            return _body({"error": {"code": "no-such-entity", "id": sorted(bad & set(ids))[0]}})
        return _body(entities({i: f"T{i}" for i in ids}))
    return answer


def test_wikidata_refused_batch_is_halved_around_the_bad_id(monkeypatch, sleeps,
                                                             tmp_path, capsys):
    cache = str(tmp_path / "t.json")
    qids = [f"Q{i}" for i in range(1, 61)]
    calls: list = []
    monkeypatch.setattr(wm.urllib.request, "urlopen", wikidata_api({"Q7"}, calls))
    out = wt.resolve_qids(qids, cache_path=cache, sleep=0)
    assert set(out) == set(qids) - {"Q7"}
    saved = json.load(open(cache))
    assert saved["Q7"] == wt._REFUSED and len(saved) == 60
    assert len(calls) <= 2 + 2 * 6                  # halving costs O(log n) requests
    assert "refused 1 ids on their own (e.g. Q7)" in capsys.readouterr().err
    calls.clear()
    misses: set = set()
    wt.resolve_qids(qids, cache_path=cache, sleep=0, misses=misses)
    assert calls == []                               # the next build asks nothing
    assert misses == set()                           # and Q7 is still not a "no article"


def test_wikidata_bad_id_at_the_head_of_a_batch_is_isolated(monkeypatch, sleeps, tmp_path,
                                                            capsys):
    # Refused whole, the first batch is halved 50, 25, 12, 6, 3, 1 with the
    # bad id always in the first half: six refusals in a row, which used to
    # stop the run before the id was isolated (nothing resolved at all).
    cache = str(tmp_path / "t.json")
    qids = [f"Q{i}" for i in range(1, 101)]          # "Q1" sorts first
    calls: list = []
    monkeypatch.setattr(wm.urllib.request, "urlopen", wikidata_api({"Q1"}, calls))
    misses: set = set()
    out = wt.resolve_qids(qids, cache_path=cache, sleep=0, misses=misses)
    assert calls[0][0] == "Q1" and len(calls[0]) == 50
    assert set(out) == set(qids) - {"Q1"}
    assert misses == set()
    assert json.load(open(cache))["Q1"] == wt._REFUSED
    assert "WARNING" not in capsys.readouterr().err


def test_wikidata_written_off_id_is_never_a_miss(monkeypatch, sleeps, tmp_path):
    # An id refused on its own may have been refused for something else
    # (a refusal of a tiny batch); it must not become "no English article",
    # which drops a non-English tag from bundling for good.
    cache = str(tmp_path / "t.json")
    monkeypatch.setattr(wm.urllib.request, "urlopen", wikidata_api({"Q5"}))
    misses: set = set()
    assert wt.resolve_qids(["Q4", "Q5", "Q6"], cache_path=cache, sleep=0,
                           misses=misses) == {"Q4": "TQ4", "Q6": "TQ6"}
    assert misses == set()
    # Read back from the cache on a later build: still not a miss, not a
    # title, and not asked again.
    use(monkeypatch, FakeAPI())
    misses = set()
    assert wt.resolve_qids(["Q5"], cache_path=cache, sleep=0, misses=misses) == {}
    assert misses == set()
    # An older cache wrote "" for it: that stays a miss (it cannot be told
    # from a real one), as before.
    json.dump({"Q5": ""}, open(cache, "w"))
    wt.resolve_qids(["Q5"], cache_path=cache, sleep=0, misses=misses)
    assert misses == {"Q5"}


def test_wikidata_unanswered_ids_are_not_misses(monkeypatch, sleeps, tmp_path, capsys):
    # The first batch is answered (Q10: no article, Q11: no such item);
    # the second meets 503 after 503 and the run stops. Only the answered
    # no-article ids are misses: "Wikidata could not answer" never is.
    cache = str(tmp_path / "t.json")
    qids = sorted(f"Q{i}" for i in range(10, 80))    # two batches
    first = entities({q: f"T{q}" for q in qids[:50]} | {"Q10": "", "Q11": None})
    use(monkeypatch, FakeAPI(first, default=http_error(503)))
    misses: set = set()
    out = wt.resolve_qids(qids, cache_path=cache, sleep=0, misses=misses)
    assert misses == {"Q10", "Q11"}
    assert len(out) == 48 and not set(out) & set(qids[50:])
    assert "HTTP 503" in capsys.readouterr().err
    # The same ids on the next build, from the cache, while it stays down.
    misses = set()
    wt.resolve_qids(qids, cache_path=cache, sleep=0, misses=misses)
    assert misses == {"Q10", "Q11"}


def test_wikidata_offline_map_reports_no_misses(monkeypatch):
    # An offline map's gaps say nothing either way (a partial dump).
    api = use(monkeypatch, FakeAPI())
    misses: set = set()
    assert wt.resolve_qids(["Q1", "Q2"], offline_map={"Q1": "One", "Q2": ""},
                           misses=misses) == {"Q1": "One"}
    assert misses == set() and api.urls == []


def test_wikidata_refusing_everything_stops_and_caches_nothing(monkeypatch, sleeps,
                                                               tmp_path, capsys):
    cache = str(tmp_path / "t.json")
    api = use(monkeypatch, FakeAPI(default={"error": {"code": "mustbeposted"}}))
    out = wt.resolve_qids([f"Q{i}" for i in range(1, 200)], cache_path=cache, sleep=0)
    assert out == {} and json.load(open(cache)) == {}
    # 50, 25, 12, 6, 3, then three ids refused on their own (1, 2, 1, 1).
    assert len(api.urls) == 9
    err = capsys.readouterr().err
    assert "left 199 of 199 Q-IDs unresolved" in err
    assert f"{wt._MAX_WRITTEN_OFF_IN_A_ROW} ids in a row refused" in err


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_wikidata_refused_client_stops_at_once(monkeypatch, sleeps, tmp_path, capsys, code):
    cache = str(tmp_path / "t.json")
    api = use(monkeypatch, FakeAPI(default=http_error(code)))
    wt.resolve_qids([f"Q{i}" for i in range(1, 5001)], cache_path=cache, sleep=0)
    assert len(api.urls) == 1 and json.load(open(cache)) == {}
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1 and f"HTTP {code}" in err


def test_wikidata_maxlag_is_waited_out_then_answered(monkeypatch, sleeps, tmp_path):
    cache = str(tmp_path / "t.json")

    @contextmanager
    def lagged():
        resp = io.BytesIO(json.dumps({"error": {"code": "maxlag", "lag": 7}}).encode())
        resp.headers = email.message.Message()   # type: ignore[attr-defined]
        resp.headers["Retry-After"] = "5"        # type: ignore[attr-defined]
        yield resp
    script = [lagged, lambda: _body(entities({"Q1": "One"}))]
    monkeypatch.setattr(wm.urllib.request, "urlopen",
                        lambda req, timeout=None: script.pop(0)())
    assert wt.resolve_qids(["Q1"], cache_path=cache, sleep=0) == {"Q1": "One"}
    assert sleeps and sleeps[0] == pytest.approx(5.5)
    assert json.load(open(cache)) == {"Q1": "One"}


@contextmanager
def _lagged(retry_after: str = "5"):
    """A maxlag answer as Wikidata sends it: HTTP 200, Retry-After set."""
    resp = io.BytesIO(json.dumps({"error": {"code": "maxlag", "lag": 7}}).encode())
    resp.headers = email.message.Message()   # type: ignore[attr-defined]
    resp.headers["Retry-After"] = retry_after  # type: ignore[attr-defined]
    yield resp


def test_wikidata_waits_out_a_replication_lag_episode(monkeypatch, sleeps, tmp_path, capsys):
    # Two minutes of maxlag (Retry-After 5, the gap widening to 30 s): the
    # build used to give up after five tries and resolve nothing
    # ("stopped at Q-ID 0 (TransientError: API error maxlag)").
    cache = str(tmp_path / "t.json")
    script = [_lagged] * 8 + [lambda: _body(entities({"Q1": "One", "Q2": ""}))]
    urls: list = []

    def answer(req, timeout=None):
        urls.append(req.full_url)
        return script.pop(0)()
    monkeypatch.setattr(wm.urllib.request, "urlopen", answer)
    misses: set = set()
    assert wt.resolve_qids(["Q1", "Q2"], cache_path=cache, sleep=0, misses=misses) == {
        "Q1": "One"}
    assert len(urls) == 9 and misses == {"Q2"}
    assert 120 < sum(sleeps) < wm.DEFAULT_WAIT_BUDGET
    # Never sooner than Retry-After: each retry waits at least 5 s.
    assert sum(1 for s in sleeps if s >= 5) >= 8
    assert "WARNING" not in capsys.readouterr().err


@pytest.mark.parametrize("code", ["maxlag", "ratelimited"])
def test_wikidata_throttle_that_outlasts_the_wait_budget_stops_and_caches_nothing(
        monkeypatch, sleeps, tmp_path, capsys, code):
    cache = str(tmp_path / "t.json")
    qids = sorted(f"Q{i}" for i in range(1, 121))
    api = use(monkeypatch, FakeAPI(entities(dict.fromkeys(qids[:50], "T")),
                                   default={"error": {"code": code}}))
    out = wt.resolve_qids(qids, cache_path=cache, sleep=0)
    assert len(out) == 50 and set(json.load(open(cache))) == set(qids[:50])
    # Retried past get_json's usual five tries, until the wait budget ran
    # out; then the run stops (one batch, never the next).
    assert 1 + 5 < len(api.urls) < 1 + 60
    assert qids[50] in urllib.parse.unquote(api.urls[-1])
    assert sum(sleeps) <= wm.DEFAULT_WAIT_BUDGET + 60
    err = capsys.readouterr().err
    assert f"API error {code}" in err and "wait budget of 900s spent" in err


def test_throttle_retries_is_opt_in_and_only_for_throttles(monkeypatch, sleeps):
    # Other callers keep five tries for a maxlag answer ...
    api = use(monkeypatch, FakeAPI(default={"error": {"code": "maxlag"}}))
    with pytest.raises(wm.TransientError):
        wm.get_json("https://x.test/a", user_agent="ua")
    assert len(api.urls) == 5
    # ... and a 5xx keeps its five tries even when throttles get more.
    api = use(monkeypatch, FakeAPI(default=http_error(502)))
    with pytest.raises(wm.TransientError, match="HTTP 502"):
        wm.get_json("https://x.test/a", user_agent="ua", throttle_retries=100)
    assert len(api.urls) == 5
    # A long throttle without a pacer is bounded by the count, and the
    # backoff stays a capped float however many tries it takes.
    api = use(monkeypatch, FakeAPI(default=http_error(429)))
    with pytest.raises(wm.TransientError, match="HTTP 429"):
        wm.get_json("https://x.test/a", user_agent="ua", throttle_retries=40)
    assert len(api.urls) == 40 and max(sleeps) <= 120


def test_wikidata_readonly_body_is_transient(monkeypatch, sleeps, tmp_path):
    cache = str(tmp_path / "t.json")
    use(monkeypatch, FakeAPI(default={"error": {"code": "readonly"}}))
    assert wt.resolve_qids(["Q1"], cache_path=cache, sleep=0) == {}
    assert json.load(open(cache)) == {}


def test_wikidata_pacing_is_serial_with_a_small_gap(monkeypatch, sleeps):
    qids = [f"Q{i}" for i in range(1, 151)]          # three requests
    monkeypatch.setattr(wm.urllib.request, "urlopen", wikidata_api())
    # Instant answers: the per-minute cap (120, one start per 0.5 s) paces them.
    wt.resolve_qids(qids)
    assert sleeps == [pytest.approx(60 / wm.DEFAULT_MAX_PER_MIN)] * 2
    # Without the cap only the small gap after each answer is left.
    monkeypatch.setenv(wm.MAX_PER_MIN_ENV, "0")
    sleeps.clear()
    wt.resolve_qids(qids)
    assert sleeps == [pytest.approx(0.1)] * 2


def test_wikidata_entity_absent_from_answer_is_not_a_miss(monkeypatch, sleeps, tmp_path):
    cache = str(tmp_path / "t.json")
    use(monkeypatch, FakeAPI(entities({"Q1": "One"})))
    wt.resolve_qids(["Q1", "Q2"], cache_path=cache, sleep=0)
    assert json.load(open(cache)) == {"Q1": "One"}


def test_wikidata_malformed_ids_are_not_requested(monkeypatch, sleeps):
    api = use(monkeypatch, FakeAPI(entities({"Q5": "Five"})))
    assert wt.resolve_qids(["Q5", "Q1;Q2", "Q05", "Qx", "Q" + "9" * 11],
                           sleep=0) == {"Q5": "Five"}
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
    assert "dropping 2 cached misses to re-check them once" in capsys.readouterr().err
    # A clean cache is left alone: its misses are real.
    use(monkeypatch, FakeAPI())
    assert wt.resolve_qids(["Q5", "Q7"], cache_path=cache, sleep=0) == {
        "Q5": "Five", "Q7": "Seven"}


# ---- wikidata_cache.py SPARQL ------------------------------------------------

def test_sparql_honours_retry_after_and_sends_a_contact(monkeypatch, sleeps):
    import wikidata_cache as wc
    api = use(monkeypatch, FakeAPI(http_error(429, "12"),
                                   {"results": {"bindings": [{"x": 1}]}}))
    assert wc._run_sparql("SELECT 1") == [{"x": 1}]
    assert sleeps == [pytest.approx(12.5)]
    assert wm.CONTACT_URL in api.ua


def test_sparql_exhausted_raises_for_the_caller_to_skip(monkeypatch, sleeps):
    import wikidata_cache as wc
    use(monkeypatch, FakeAPI(default=http_error(503)))
    with pytest.raises(wm.TransientError):
        wc._run_sparql("SELECT 1")


# ---- review follow-ups: cap, budget, long Retry-After, headers, extracts ----

def test_retries_count_toward_the_per_minute_cap(monkeypatch, clock):
    # A 502 retried after a short backoff still waits for the next slot.
    api = use(monkeypatch, TimedAPI(clock, http_error(502), {"ok": 1},
                                    took=lambda n: 0.05))
    p = wm.polite_pacer(0.1, 120)
    assert wm.get_json("https://x.test/a", user_agent="ua", pacer=p, base=0.1) == {"ok": 1}
    assert len(api.starts) == 2
    assert api.starts[1] - api.starts[0] == pytest.approx(0.5)


def test_floors_are_not_charged_to_the_wait_budget(monkeypatch, clock):
    p = wm.polite_pacer(0.1, 120, budget=100)
    for took in (0.05, 1.5, 0.05, 0.05):      # the cap, then a slow answer's 5 s
        p.wait()
        clock.now += took
        p.done()
    p.wait()
    assert sum(clock.sleeps) > 5 and p.spent == 0
    # A widened gap is charged only beyond the floor that applies anyway.
    p.rate_limited(retry_after=2.0)           # 2 s gap; after a slow answer the floor is 5 s
    clock.now += 1.5
    p.done()
    p.wait()
    assert clock.sleeps[-1] == pytest.approx(5.0) and p.spent == 0
    p.rate_limited(retry_after=8.0)           # an 8 s gap over the 0.5 s cap floor
    clock.now += 0.05                         # (due 0.45 s after this answer)
    p.done()
    p.wait()
    assert clock.sleeps[-1] == pytest.approx(8.0) and p.spent == pytest.approx(7.55)


def test_retry_after_beyond_max_wait_stops_instead_of_retrying_early(monkeypatch,
                                                                      sleeps, tmp_path):
    api = use(monkeypatch, FakeAPI(http_error(429, "600"), {"ok": 1}))
    with pytest.raises(wm.TransientError) as ei:
        wm.get_json("https://x.test/a", user_agent="ua", max_wait=120)
    assert ei.value.stop and ei.value.rate_limited and "600s" in ei.value.reason
    assert len(api.urls) == 1 and sleeps == []
    # A build stops requesting at once and caches nothing.
    api = use(monkeypatch, FakeAPI(default=http_error(429, "3600")))
    stats = wa.bundle_wiki_articles([f"en:T{i}" for i in range(5)], lambda *a: None,
                                    cache_dir=str(tmp_path), log=lambda *_: None)
    assert len(api.urls) == 1 and stats["unfetched"] == 5 and os.listdir(tmp_path) == []


def test_response_with_headers_but_no_content_encoding(monkeypatch, sleeps):
    @contextmanager
    def plain(req, timeout=None):
        resp = io.BytesIO(json.dumps({"ok": 1}).encode())
        resp.headers = email.message.Message()   # type: ignore[attr-defined]
        resp.headers["Content-Type"] = "application/json"  # type: ignore[attr-defined]
        yield resp
    monkeypatch.setattr(wm.urllib.request, "urlopen", plain)
    assert wm.get_json("https://x.test/a", user_agent="ua") == {"ok": 1}


def _extracts_answer(titles):
    return {"query": {"pages": [{"title": t, "extract": f"{t} is a place."}
                                for t in titles]}}


def test_extracts_honour_retry_after_and_stop_when_told(monkeypatch, sleeps, capsys):
    import wikidata_cache as wc
    entries = {f"Q{i}": {"wikipedia_title": f"Place_{i}"} for i in range(40)}
    api = use(monkeypatch, FakeAPI(http_error(429, "3"),
                                   _extracts_answer([f"Place {i}" for i in range(20)]),
                                   _extracts_answer([f"Place {i}" for i in range(20, 40)])))
    wc.fetch_wikipedia_extracts(entries)
    assert all(e.get("extract") for e in entries.values())
    assert len(api.urls) == 3 and sleeps[0] == pytest.approx(3.5)
    assert wm.CONTACT_URL in api.ua
    # A Retry-After beyond the retry cap stops the rest: one request, no retry.
    entries = {f"Q{i}": {"wikipedia_title": f"Place_{i}"} for i in range(100)}
    api = use(monkeypatch, FakeAPI(default=http_error(429, "900")))
    wc.fetch_wikipedia_extracts(entries)
    assert len(api.urls) == 1 and not any("extract" in e for e in entries.values())
    assert "not fetching the remaining 80 extracts" in capsys.readouterr().out
    # A 429 that outlives its retries skips that batch and goes on.
    entries = {f"Q{i}": {"wikipedia_title": f"Place_{i}"} for i in range(40)}
    api = use(monkeypatch, FakeAPI(*[http_error(429)] * 5,
                                   _extracts_answer([f"Place {i}" for i in range(20, 40)])))
    wc.fetch_wikipedia_extracts(entries)
    assert len(api.urls) == 6
    assert sum(1 for e in entries.values() if e.get("extract")) == 20


def test_throttle_floor_never_exceeds_max_wait_and_covers_503(monkeypatch, sleeps):
    use(monkeypatch, FakeAPI(http_error(429), {"ok": 1}))
    wm.get_json("https://x.test/a", user_agent="ua", max_wait=2)
    assert sleeps == [2]                       # the 5 s floor, capped at max_wait
    sleeps.clear()
    use(monkeypatch, FakeAPI(http_error(503), {"ok": 1}))
    wm.get_json("https://x.test/a", user_agent="ua")
    assert sleeps == [5.0]
    sleeps.clear()
    use(monkeypatch, FakeAPI(http_error(502), {"ok": 1}))
    wm.get_json("https://x.test/a", user_agent="ua")
    assert sleeps == [1.5]                     # other 5xx: plain backoff


def test_extracts_other_4xx_skips_the_batch_401_403_stop(monkeypatch, sleeps, capsys):
    import wikidata_cache as wc
    entries = {f"Q{i}": {"wikipedia_title": f"Place_{i}"} for i in range(40)}
    api = use(monkeypatch, FakeAPI(http_error(414),
                                   _extracts_answer([f"Place {i}" for i in range(20, 40)])))
    assert wc.fetch_wikipedia_extracts(entries) == 20       # the first batch stays pending
    assert len(api.urls) == 2
    assert sum(1 for e in entries.values() if e.get("extract")) == 20
    assert not any(e.get(wc.NO_EXTRACT) for e in entries.values())
    for code in (401, 403):
        entries = {f"Q{i}": {"wikipedia_title": f"Place_{i}"} for i in range(40)}
        api = use(monkeypatch, FakeAPI(default=http_error(code)))
        assert wc.fetch_wikipedia_extracts(entries) == 40
        assert len(api.urls) == 1


def test_unanswered_extracts_are_asked_again_real_misses_are_not(monkeypatch, sleeps,
                                                                  tmp_path):
    import wikidata_cache as wc
    qids = {f"Q{i}": {"name": f"Place {i}"} for i in (11, 12, 13)}
    monkeypatch.setattr(wc, "extract_qids_from_pbf", lambda *a, **k: qids)
    fetched: list = []

    def props(new_qids, cache_dir=None):
        fetched.append(list(new_qids))
        return {q: {"qid": q, "label": f"L{q}", "wikipedia_title": f"Place_{q[1:]}"}
                for q in new_qids}
    monkeypatch.setattr(wc, "fetch_wikidata_batch", props)
    d = str(tmp_path)

    # Build 1: the extracts API stops the run (Retry-After beyond the cap).
    use(monkeypatch, FakeAPI(default=http_error(429, "900")))
    wc.build_cache(pbf_path="x.pbf", cache_dir=d)
    cached = wc.load_cache(d)
    assert set(cached) == set(qids) and all(wc.extract_pending(e) for e in cached.values())

    # Build 2: no new Q-IDs, but the unanswered extracts are asked again.
    # Place 13 has no extract: that answer is recorded, not a pending one.
    api = use(monkeypatch, FakeAPI({"query": {"pages": [
        {"title": "Place 11", "extract": "Eleven is a place."},
        {"title": "Place 12", "extract": "Twelve is a place."},
        {"title": "Place 13", "missing": True}]}}))
    wc.build_cache(pbf_path="x.pbf", cache_dir=d)
    assert len(fetched) == 1 and len(api.urls) == 1
    cached = wc.load_cache(d)
    assert cached["Q11"]["extract"] == "Eleven is a place."
    assert cached["Q13"].get(wc.NO_EXTRACT) and not cached["Q13"].get("extract")
    assert not any(wc.extract_pending(e) for e in cached.values())

    # Build 3: nothing is asked again.
    api = use(monkeypatch, FakeAPI())
    wc.build_cache(pbf_path="x.pbf", cache_dir=d)
    assert api.urls == [] and len(fetched) == 1


def test_a_continued_extracts_answer_marks_no_misses(monkeypatch, sleeps):
    import wikidata_cache as wc
    entries = {"Q1": {"wikipedia_title": "A"}, "Q2": {"wikipedia_title": "B"}}
    use(monkeypatch, FakeAPI({"continue": {"excontinue": 1},
                              "query": {"pages": [{"title": "A", "extract": "A is."},
                                                  {"title": "B"}]}}))
    assert wc.fetch_wikipedia_extracts(entries) == 1
    assert entries["Q1"]["extract"] == "A is." and wc.extract_pending(entries["Q2"])
