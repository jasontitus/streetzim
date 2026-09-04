"""
url_cache_filter.py — helpers the build pipeline can use to consult
the URL liveness cache produced by `validate_overture_urls.py`.

Pure-Python (no aiohttp dependency) so create_osm_zim.py can import
this without pulling in extra packages on the build box.

Usage in the build:

    from cloud.url_cache_filter import (
        load_cache, decide_record_action, scrub_record_url,
    )

    cache = load_cache("url_validation_cache.json")
    for rec in records:
        action = decide_record_action(rec, cache,
                                      policy="drop-record")
        if action == "drop":
            continue                # skip the record entirely
        if action == "scrub":
            scrub_record_url(rec)   # delete `ws` (and friends), keep record
        emit(rec)

Two policies are supported (build picks via the `policy` arg):

  "scrub-only"    — only drop the offending URL field. The POI/place
                    record stays in the index. Conservative; users
                    just lose the broken link, nothing else.

  "drop-record"   — drop the whole record when its `ws` URL is
                    dead/parked. Rationale (per user 2026-05-10):
                    "a dead website may likely mean a dead business
                    so we might want to drop it entirely". Riskier —
                    a record without a ws field is unaffected
                    because there's nothing to validate, but if the
                    business legitimately took down their website
                    we silently drop a still-real POI. Mitigated by
                    only marking dead via:
                      (a) HEAD/GET in the dead range (4xx/5xx) or
                      (b) DNS/TLS/connection failure or
                      (c) redirect to a known parker host or
                      (d) parking phrase in body.

A record without a `ws` field is always kept.
A record with a `ws` field that's NOT in the cache (never crawled)
is always kept — we don't drop on absence of evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal


Policy = Literal["scrub-only", "drop-record"]


def load_cache(path: str | Path) -> dict[str, dict]:
    """Return the {url: result} entries map, or {} if missing."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def is_url_dead(url: str | None, cache: dict[str, dict]) -> bool:
    """True iff cache has an explicit alive=False for this URL."""
    if not url or not isinstance(url, str):
        return False
    rec = cache.get(url.strip())
    if not rec:
        return False
    if rec.get("alive") is not False:
        return False
    # STREETZIM_URL_DEAD_STATUSES=404,410,dns narrows "dead" to those
    # cache statuses (403/429/5xx/timeouts are mostly bot-blocked, live
    # sites). Unset keeps the historical any-alive=False rule. Mirrors
    # create_osm_zim._is_url_dead.
    raw = os.environ.get("STREETZIM_URL_DEAD_STATUSES", "").strip()
    if not raw:
        return True
    dead = {x.strip().lower() for x in raw.split(",") if x.strip()}
    return str(rec.get("status", "")).lower() in dead


def decide_record_action(
    rec: dict,
    cache: dict[str, dict],
    *,
    policy: Policy = "scrub-only",
) -> Literal["keep", "scrub", "drop"]:
    """Decide what to do with `rec` based on its `ws` URL.

    Returns:
      "keep"  — emit the record as-is.
      "scrub" — emit the record, but the caller must remove the
                `ws` field (and `soc` / `p` if you want to be
                aggressive about all dead links — see
                `scrub_record_url`).
      "drop"  — skip the record entirely.
    """
    ws = rec.get("ws")
    if not is_url_dead(ws, cache):
        return "keep"
    if policy == "drop-record":
        return "drop"
    return "scrub"


def scrub_record_url(rec: dict) -> None:
    """In-place: drop the dead `ws` from the record. Mutates."""
    rec.pop("ws", None)


def cache_summary(cache: dict[str, dict]) -> dict:
    """Aggregate counts for diagnostics."""
    total = len(cache)
    alive = sum(1 for e in cache.values() if e.get("alive"))
    by_status: dict[str, int] = {}
    for e in cache.values():
        s = str(e.get("status"))
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "total": total,
        "alive": alive,
        "dead": total - alive,
        "by_status": by_status,
    }
