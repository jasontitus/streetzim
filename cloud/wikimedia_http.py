"""Polite JSON GETs against the Wikimedia APIs (Wikipedia, Wikidata).

Shared by cloud/wiki_articles.py (`action=parse`) and
cloud/wikidata_titles.py (`wbgetentities`). It exists because both used a
fixed 1/2/4 s backoff that ignored `Retry-After`, and the article fetcher
then cached a 429 that outlived its retries as a permanent "no article"
(docs/head-to-head-dc.md, "Follow-up: real article text": a cold D.C.
build lost 177 real articles that way).

What a caller gets:
- `get_json` returns the parsed body, raises `urllib.error.HTTPError` for
  a definitive HTTP answer (404 and other non-transient 4xx), and raises
  `TransientError` when a 429, 408, 5xx, timeout, connection or truncated
  body error outlives the retries. A caller must never cache a
  `TransientError` as a miss: the next build asks again.
- Retries honour `Retry-After` (delta-seconds or HTTP-date); one longer
  than `max_wait` stops the run (a stopping `TransientError`) rather than
  retrying before the server allows it. Without one, retries back off
  exponentially with jitter, at least 5 s after a 429 or 503.
- A `Pacer` keeps a polite gap between requests (from the end of one
  response to the next request), widens it after each 429, maxlag or 5xx
  with Retry-After and eases back towards the base gap as requests
  succeed; it can also hold a cap on the request rate and a longer pause
  after a slow answer.
- Requests ask for gzip and decompress it (Wikimedia's robot policy).

User-Agent: Wikimedia's policy (meta.wikimedia.org/wiki/User-Agent_policy)
asks for a descriptive agent with a way to reach its operator. The default
names the project and its public issue tracker; an operator can add their
own address with STREETZIM_WIKI_CONTACT (never committed to the repo).
"""
from __future__ import annotations

import datetime
import email.utils
import gzip
import http.client
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from typing import Any

PROJECT_URL = "https://github.com/jasontitus/streetzim"
CONTACT_URL = PROJECT_URL + "/issues"
CONTACT_ENV = "STREETZIM_WIKI_CONTACT"
REQUIRE_ENV = "STREETZIM_REQUIRE_WIKI"
BUDGET_ENV = "STREETZIM_WIKI_WAIT_BUDGET"
DEFAULT_WAIT_BUDGET = 900.0   # seconds of rate-limit waiting per Pacer (per step)
GAP_ENV = "STREETZIM_WIKI_GAP"
MAX_PER_MIN_ENV = "STREETZIM_WIKI_MAX_PER_MIN"
DEFAULT_GAP = 0.1             # seconds from one response to the next request
DEFAULT_MAX_PER_MIN = 120.0   # request starts a minute (polite_pacer)
SLOW_AFTER = 1.0              # an answer slower than this ...
SLOW_GAP = 5.0                # ... is followed by at least this pause
# A 429 or 503 without Retry-After waits at least this long before its
# retry, never more than max_wait (Wikimedia APIs/Rate limits: "clients
# should wait at least five seconds").
MIN_THROTTLE_WAIT = 5.0

# HTTP statuses worth retrying.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Statuses from api.php that mean the client itself is refused or pointed
# at the wrong place (a blocked User-Agent, a proxy's 404): every further
# request would get the same, so a run stops asking. A MediaWiki-API-Error
# header naming a page-level code overrides this (the caller checks).
STOP_STATUSES = frozenset({400, 401, 403, 404, 405, 410})
# `error` codes in an HTTP 200 body that mean "slow down", not an answer.
THROTTLE_CODES = frozenset({"maxlag", "ratelimited"})


def user_agent(tool: str, version: str = "1.1") -> str:
    """`streetzim-<tool>/<version> (<issues URL>[; <contact>]) python-urllib/X.Y`."""
    contact = os.environ.get(CONTACT_ENV, "").strip()
    reach = CONTACT_URL + (f"; {contact}" if contact else "")
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"streetzim-{tool}/{version} ({reach}) python-urllib/{py}"


def require_complete() -> bool:
    """STREETZIM_REQUIRE_WIKI=1: fail rather than ship a partial Wikipedia set."""
    return os.environ.get(REQUIRE_ENV) == "1"


def env_number(name: str, default: float) -> float:
    """A non-negative number from the environment, else `default`."""
    try:
        v = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if v >= 0 else default


class TransientError(Exception):
    """A failure that says nothing about the page: rate limit, 5xx, network.

    `stop` marks one that every further request would repeat (a refused
    client, a spent wait budget): the run should stop asking."""

    def __init__(self, reason: str, status: int | None = None, *,
                 throttled: bool = False, stop: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.throttled = throttled or status == 429
        self.stop = stop

    @property
    def rate_limited(self) -> bool:
        return self.throttled


def api_error_code(e: urllib.error.HTTPError) -> str:
    """The MediaWiki-API-Error header of an HTTP error, or ""."""
    headers = e.headers
    return (headers.get("MediaWiki-API-Error") or "").strip() if headers else ""


def stop_error(e: urllib.error.HTTPError) -> TransientError:
    """TransientError for a non-transient HTTP status: `stop` when the
    status says the client is refused (STOP_STATUSES)."""
    return TransientError(f"HTTP {e.code}", e.code, stop=e.code in STOP_STATUSES)


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a `Retry-After` header, or None when absent or
    unparseable. Accepts delta-seconds ("120") and an HTTP-date; a date in
    the past means 0. Never raises."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if re.fullmatch(r"[0-9]+", v):
        return float(min(int(v), 10 ** 9))
    try:
        when = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:  # "-0000": an HTTP-date is GMT anyway
        when = when.replace(tzinfo=datetime.timezone.utc)
    t = time.time() if now is None else now
    return max(0.0, when.timestamp() - t)


def backoff_delay(attempt: int, retry_after: float | None, *, base: float = 2.0,
                  max_wait: float = 120.0,
                  rng: Callable[[], float] | None = None) -> float:
    """Wait before retry number `attempt` (0-based): the server's
    `Retry-After` when it gave one, else exponential backoff with jitter
    (between half and all of base * 2**attempt). Never more than max_wait."""
    rng = rng or random.random
    if retry_after is not None:
        # A little jitter on top so parallel builds do not return in step.
        return min(max_wait, retry_after + rng() * min(1.0, base))
    exp = base * (2 ** attempt)
    return min(max_wait, exp / 2 + rng() * exp / 2)


class Pacer:
    """Serial requests with a polite gap, widened by rate limits, and a
    per-run budget for waiting on them.

    The gap runs from the end of one response to the start of the next
    request, so a slow API is paced by its own response time (Wikimedia's
    API etiquette asks for serial requests, not a fixed rate). `interval`
    is the base gap. Each 429, maxlag/ratelimited answer or 5xx with a
    Retry-After doubles the current gap (to at least the server's
    `Retry-After`, capped at `max_interval`); each success eases it 10%
    back towards the base.

    Two floors keep a fast API polite without a fixed sleep:
    - `min_period`: at least this long from the start of one request to
      the start of the next, so a run of fast responses stays under a
      per-minute limit (Wikimedia's is 200 requests a minute for an
      unauthenticated client with a descriptive User-Agent);
    - `slow_after`/`slow_gap`: a response that took longer than
      `slow_after` seconds is followed by a gap of at least `slow_gap`
      (the robot policy's "if your request takes more than 1 second to
      serve, please wait 5 seconds before making another request").
    Both default to off.

    `budget` (seconds; default STREETZIM_WIKI_WAIT_BUDGET, else 15 min)
    bounds the waiting this Pacer's loop (one build step) spends on
    failures: retry backoff and any gap beyond the base. Once spent,
    `get_json` raises a stopping TransientError instead of sleeping, so a
    hard throttle costs a step minutes, not hours. The floors are etiquette, not failures, and are
    not charged to it: only the part of a wait beyond them is.

    Not thread-safe: use one Pacer per serial request loop (the floors
    assume nothing else is asking at the same time)."""

    def __init__(self, interval: float, max_interval: float = 30.0,
                 budget: float | None = None, *, min_period: float = 0.0,
                 slow_after: float | None = None, slow_gap: float = 0.0) -> None:
        self.base = max(0.0, interval)
        self.current = self.base
        self.max_interval = max(max_interval, self.base)
        self.min_period = max(0.0, min_period)
        self.slow_after = slow_after
        self.slow_gap = max(0.0, slow_gap)
        self.budget = env_number(BUDGET_ENV, DEFAULT_WAIT_BUDGET) if budget is None else budget
        self.spent = 0.0
        self._start: float | None = None   # when the last request went out
        self._last: float | None = None    # when its response came back
        self._slow = False                 # it took longer than slow_after

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.budget

    def can_wait(self, seconds: float) -> bool:
        return self.spent + seconds <= self.budget

    def charge(self, seconds: float) -> None:
        self.spent += max(0.0, seconds)

    def wait(self) -> None:
        """Sleep until the next request may start, then mark its start."""
        now = time.monotonic()
        floor_due = gap_due = now
        if self._last is not None:
            polite = max(self.base, self.slow_gap if self._slow else 0.0)
            floor_due = max(floor_due, self._last + polite)
            gap_due = self._last + self.current
        if self._start is not None:
            floor_due = max(floor_due, self._start + self.min_period)
        delay = max(floor_due, gap_due) - now
        if delay > 0:
            # Only the widening a rate limit caused, beyond the floors that
            # apply anyway, counts against the budget.
            self.charge(min(delay, max(0.0, gap_due - floor_due)))
            time.sleep(delay)
        self._start = time.monotonic()

    def done(self) -> None:
        """A response (or failure) came back: the gap starts now."""
        self._last = time.monotonic()
        self._slow = (self.slow_after is not None and self._start is not None
                      and self._last - self._start > self.slow_after)

    def rate_limited(self, retry_after: float | None = None) -> None:
        widened = max(self.current * 2, self.base, 1.0, retry_after or 0.0)
        self.current = min(self.max_interval, widened)

    def succeeded(self) -> None:
        self.current = max(self.base, self.current * 0.9)


def polite_pacer(gap: float | None = None, max_per_min: float | None = None,
                 budget: float | None = None) -> Pacer:
    """The Pacer for Wikimedia's Action API (docs/zimfarm.md, "Wikimedia
    API etiquette"): serial, `gap` seconds from each response to the next
    request (default STREETZIM_WIKI_GAP, else 0.1), at most `max_per_min`
    request starts a minute (default STREETZIM_WIKI_MAX_PER_MIN, else 120;
    0 means no cap), and 5 s after an answer that took over 1 s.

    The cap is per Pacer, so it assumes one Wikimedia client per worker
    IP: two builds side by side from one IP each get the full rate."""
    if gap is None:
        gap = env_number(GAP_ENV, DEFAULT_GAP)
    if max_per_min is None:
        max_per_min = env_number(MAX_PER_MIN_ENV, DEFAULT_MAX_PER_MIN)
    return Pacer(gap, budget=budget,
                 min_period=60.0 / max_per_min if max_per_min > 0 else 0.0,
                 slow_after=SLOW_AFTER, slow_gap=SLOW_GAP)


def _body_throttle(data: Any) -> str:
    """The code of a maxlag/ratelimited `error` body, else ""."""
    if isinstance(data, dict):
        err = data.get("error")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(err, dict):
            code = str(err.get("code", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            if code in THROTTLE_CODES:
                return code
    return ""


def get_json(url: str, *, user_agent: str, pacer: Pacer | None = None,
             retries: int = 5, timeout: float = 60.0, base: float = 2.0,
             max_wait: float = 120.0, accept: str = "application/json",
             log: Callable[[str], None] | None = None,
             throttle_retries: int | None = None) -> Any:
    """GET `url` and parse its JSON body, retrying transient failures
    (including a maxlag/ratelimited `error` body, with its Retry-After).

    Raises `urllib.error.HTTPError` for a non-transient HTTP status and
    `TransientError` once the `retries` attempts, or the pacer's wait
    budget, are spent.

    `throttle_retries` (opt-in; default `retries`): attempts in all while
    the answers are "slow down" (a 429, maxlag or ratelimited). Wikidata's
    maxlag means its replicas lag, which lasts minutes, not the ~30 s five
    tries cover; a caller with a pacer can pass a large number and let the
    pacer's wait budget bound the waiting. Any other failure still ends
    the call once `retries` attempts in all have been made."""
    # gzip: the robot policy's "Always request content with an
    # Accept-Encoding: gzip HTTP header".
    req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                               "Accept": accept,
                                               "Accept-Encoding": "gzip"})
    retries = max(1, retries)
    throttle_cap = retries if throttle_retries is None else max(retries, throttle_retries)
    if pacer is not None and pacer.exhausted:
        raise TransientError(f"wait budget of {pacer.budget:.0f}s spent", stop=True)
    last: TransientError | None = None
    attempt = 0
    while True:
        if pacer is not None:
            pacer.wait()
        retry_after: float | None = None
        ra_header: str | None = None
        try:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read()
                    hdrs = getattr(resp, "headers", None)
                    ra_header = hdrs.get("Retry-After") if hdrs is not None else None
                    encoding = (hdrs.get("Content-Encoding") or "") if hdrs is not None else ""
                if encoding.strip().lower() == "gzip":
                    body = gzip.decompress(body)
                data = json.loads(body)
            finally:
                if pacer is not None:
                    pacer.done()
            code = _body_throttle(data)
            if not code:
                if pacer is not None:
                    pacer.succeeded()
                return data
            retry_after = parse_retry_after(ra_header)
            last = TransientError(f"API error {code}", throttled=True)
        except urllib.error.HTTPError as e:
            if e.code not in TRANSIENT_STATUSES:
                raise
            headers = e.headers
            retry_after = parse_retry_after(headers.get("Retry-After") if headers else None)
            last = TransientError(f"HTTP {e.code}", e.code)
        except (urllib.error.URLError, TimeoutError, OSError, EOFError,
                zlib.error, http.client.HTTPException, ValueError) as e:
            # URLError: DNS/refused; OSError: resets, a bad gzip header;
            # EOFError/zlib.error: a truncated or corrupt gzip body;
            # HTTPException: IncompleteRead; ValueError: a non-JSON body.
            last = TransientError(f"{type(e).__name__}: {e}")
        # 429/maxlag say "slow down". A 5xx with a Retry-After says the
        # backend is overloaded (Wikimedia's 503) and widens the gap too; a
        # bare 5xx is only retried with backoff, and a run of them stops
        # the run (the callers' give-up streak).
        overloaded = (last.status or 0) >= 500 and retry_after is not None
        if pacer is not None and (last.throttled or overloaded):
            pacer.rate_limited(retry_after)
        if retry_after is not None and retry_after > max_wait:
            # Retrying sooner than the server allows would be refused
            # again (and is rude); waiting that long stalls the build.
            # Stop asking; the next build tries again.
            if pacer is not None:
                pacer.charge(pacer.budget)
            raise TransientError(
                f"{last.reason}; Retry-After {retry_after:.0f}s is over {max_wait:.0f}s",
                last.status, throttled=last.throttled, stop=True)
        attempt += 1
        if attempt >= (throttle_cap if last.throttled else retries):
            break
        # The exponent is capped: backoff_delay caps the wait anyway,
        # and 2 ** attempt must stay a float for a long throttle.
        delay = backoff_delay(min(attempt - 1, 16), retry_after, base=base,
                              max_wait=max_wait)
        if retry_after is None and (last.throttled or last.status == 503):
            delay = max(delay, min(MIN_THROTTLE_WAIT, max_wait))
        if pacer is not None:
            if not pacer.can_wait(delay):
                pacer.charge(pacer.budget)   # spent: later calls stop at once
                raise TransientError(
                    f"{last.reason}; wait budget of {pacer.budget:.0f}s spent",
                    last.status, throttled=last.throttled, stop=True)
            pacer.charge(delay)
        if log is not None:
            log(f"    {last.reason}; retrying in {delay:.1f}s"
                + (f" (Retry-After {retry_after:.0f}s)" if retry_after is not None else ""))
        time.sleep(delay)
    assert last is not None
    raise last
