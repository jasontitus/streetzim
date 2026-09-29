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
- Retries honour `Retry-After` (delta-seconds or HTTP-date), capped at
  `max_wait`, and otherwise back off exponentially with jitter.
- A `Pacer` keeps a polite gap between requests, widens it after each 429
  and eases back towards the base gap as requests succeed.

User-Agent: Wikimedia's policy (meta.wikimedia.org/wiki/User-Agent_policy)
asks for a descriptive agent with a way to reach its operator. The default
names the project and its public issue tracker; an operator can add their
own address with STREETZIM_WIKI_CONTACT (never committed to the repo).
"""
from __future__ import annotations

import email.utils
import http.client
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

PROJECT_URL = "https://github.com/jasontitus/streetzim"
CONTACT_URL = PROJECT_URL + "/issues"
CONTACT_ENV = "STREETZIM_WIKI_CONTACT"
REQUIRE_ENV = "STREETZIM_REQUIRE_WIKI"

# HTTP statuses worth retrying; any other error status is an answer.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def user_agent(tool: str, version: str = "1.1") -> str:
    """`streetzim-<tool>/<version> (<issues URL>[; <contact>]) python-urllib/X.Y`."""
    contact = os.environ.get(CONTACT_ENV, "").strip()
    reach = CONTACT_URL + (f"; {contact}" if contact else "")
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"streetzim-{tool}/{version} ({reach}) python-urllib/{py}"


def require_complete() -> bool:
    """STREETZIM_REQUIRE_WIKI=1: fail rather than ship a partial Wikipedia set."""
    return os.environ.get(REQUIRE_ENV) == "1"


class TransientError(Exception):
    """A failure that says nothing about the page: rate limit, 5xx, network."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a `Retry-After` header, or None when absent or
    unparseable. Accepts delta-seconds ("120") and an HTTP-date; a date in
    the past means 0."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if v.isdigit():
        return float(v)
    try:
        when = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:  # an HTTP-date is always GMT
        return None
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
    """A minimum gap between requests to one API, widened by rate limits.

    `interval` is the polite base gap. Each 429 doubles the current gap (to
    at least the server's `Retry-After`, capped at `max_interval`); each
    success eases it 10% back towards the base."""

    def __init__(self, interval: float, max_interval: float = 30.0) -> None:
        self.base = max(0.0, interval)
        self.current = self.base
        self.max_interval = max(max_interval, self.base)
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None and self.current > 0:
            gap = self._last + self.current - time.monotonic()
            if gap > 0:
                time.sleep(gap)
        self._last = time.monotonic()

    def rate_limited(self, retry_after: float | None = None) -> None:
        widened = max(self.current * 2, self.base, 1.0, retry_after or 0.0)
        self.current = min(self.max_interval, widened)

    def succeeded(self) -> None:
        self.current = max(self.base, self.current * 0.9)


def get_json(url: str, *, user_agent: str, pacer: Pacer | None = None,
             retries: int = 5, timeout: float = 60.0, base: float = 2.0,
             max_wait: float = 120.0, log: Callable[[str], None] | None = None) -> Any:
    """GET `url` and parse its JSON body, retrying transient failures.

    Raises `urllib.error.HTTPError` for a non-transient HTTP status and
    `TransientError` once the `retries` attempts are spent."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                               "Accept": "application/json"})
    retries = max(1, retries)
    last: TransientError | None = None
    for attempt in range(retries):
        if pacer is not None:
            pacer.wait()
        retry_after: float | None = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            if pacer is not None:
                pacer.succeeded()
            return data
        except urllib.error.HTTPError as e:
            if e.code not in TRANSIENT_STATUSES:
                raise
            headers = e.headers
            retry_after = parse_retry_after(headers.get("Retry-After") if headers else None)
            last = TransientError(f"HTTP {e.code}", e.code)
            if e.code == 429 and pacer is not None:
                pacer.rate_limited(retry_after)
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException, ValueError) as e:
            # URLError: DNS/refused; OSError: resets; HTTPException:
            # IncompleteRead; ValueError: a truncated or non-JSON body.
            last = TransientError(f"{type(e).__name__}: {e}")
        if attempt < retries - 1:
            delay = backoff_delay(attempt, retry_after, base=base, max_wait=max_wait)
            if log is not None:
                log(f"    {last.reason}; retrying in {delay:.1f}s"
                    + (f" (Retry-After {retry_after:.0f}s)" if retry_after is not None else ""))
            time.sleep(delay)
    assert last is not None
    raise last
