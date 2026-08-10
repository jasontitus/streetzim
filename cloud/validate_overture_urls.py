#!/usr/bin/env python3
"""
validate_overture_urls.py — async URL liveness crawler for the
Overture website fields (`ws`) we bake into search-data records.

Field testing surfaced that many POI websites in Overture are stale
(domain expired, returning 4xx, redirected to parking pages). The
viewer happily renders them as "🌐" links; users tap and get a dead
end. This tool builds a persistent cache of URL → liveness so the
build can drop dead links before they ship.

Two-step flow:
  1. Run this nightly/weekly against the latest URL set →
     updates `url_validation_cache.json` (or wherever
     --cache points).
  2. Build pipeline reads the cache; create_osm_zim.py drops the
     `ws` field on records whose URL is `alive=false`.

Cache schema (JSON):
  {
    "version": 1,
    "checked_at": "2026-05-10T12:34:56Z",
    "entries": {
      "https://example.com/foo": {
        "last_checked": "2026-05-10T12:34:56Z",
        "status": 200,           // HTTP code, or "timeout"/"dns"/"ssl"/"error"
        "alive": true,           // 2xx/3xx → alive
        "final_url": "https://example.com/foo/",  // after redirects
        "redirect_count": 1,
        "error": null            // only set when not alive
      },
      ...
    }
  }

Cache entries are reused if `last_checked` is within --max-age-days
(default 30). Older or missing entries get re-checked.

Inputs (one of):
  --zim PATH             extract unique `ws` URLs from a ZIM's
                         search-data
  --urls PATH            newline-delimited list of URLs

Output:
  --cache PATH           cache JSON to read+update (default
                         url_validation_cache.json next to script)

Tunables:
  --concurrency N        max in-flight HEAD requests (default 32)
  --per-host-concurrency N    cap per hostname (default 4 — be a
                              good neighbour to small sites)
  --timeout SECONDS      per-request timeout (default 12)
  --max-age-days N       skip URLs checked more recently than this
                         (default 30)
  --dry-run              print what would be checked, don't fetch
  --progress-every N     stdout progress line every N URLs (default 200)

Exit codes:
  0 — completed (cache updated, even if some URLs failed)
  1 — fatal error (couldn't load input or write cache)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:
    print(
        "ERROR: aiohttp not installed. Run: ./venv312/bin/pip install aiohttp",
        file=sys.stderr,
    )
    sys.exit(1)


# Browsers Overture POIs were probably scraped from sometimes block
# generic crawlers. Identify ourselves honestly but with a UA that
# resembles a real browser so casual bot blockers don't trip.
USER_AGENT = (
    "Mozilla/5.0 (compatible; StreetZim-LinkValidator/1.0; "
    "+https://github.com/jasontitus/streetzim)"
)

# Domain-squatter / parking-page hosts. URLs that 30x to one of
# these are "alive" by HTTP status but useless for the user — the
# business itself almost always doesn't exist anymore. Keep this
# list conservative; false positives drop real businesses.
#
# Most parking redirects are to a *.<parker>.com page; we match the
# bare apex too in case some shops use it directly.
PARKING_HOSTS = frozenset({
    "sedo.com", "sedoparking.com",
    "afternic.com",
    "hugedomains.com",
    "godaddy.com",                 # parked.godaddy.com / godaddy domain registry
    "bodis.com",
    "above.com",
    "namebright.com",
    "uniregistrymarket.link",
    "dan.com",
    "buydomains.com",
    "parkingcrew.net",
    "smartname.com",
    "ww16.parkingcrew.com",
    "ww17.parkingcrew.com",
    "domainagents.com",
    "namesilo.com",
    "domainpunch.com",
    "expireddomains.net",
    "domain-for-sale.org",
    "1and1.com",                   # legacy parking
    "ionos.com",                   # 1and1 successor parking templates
})

# Substrings in the FINAL hostname (after redirect) that strongly
# suggest a parking template even when the parent host isn't on the
# list above. Fuzzy but cheap; only catches obvious cases.
PARKING_HOST_FRAGMENTS = (
    "parking",       # parking.example.com / *.parkingcrew.com
    "domain-for-sale",
    "this-domain",
    "buy-this-domain",
    "domainmarket",
)

# Body-content tells when --check-parking-body is on. Many squatters
# return 200 with a templated "this domain is for sale" page hosted
# on the original domain. We download a small chunk and grep for
# these phrases.
PARKING_BODY_PHRASES = (
    "buy this domain",
    "this domain is for sale",
    "this domain may be for sale",
    "domain for sale",
    "domain is parked",
    "domain has expired",
    "make an offer for",
    "buy this premium domain",
    "the domain you are looking for is for sale",
)


def _looks_parked(final_url: str) -> bool:
    """True if the final-after-redirects hostname matches a known parker."""
    if not final_url:
        return False
    try:
        host = (urlparse(final_url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    if host in PARKING_HOSTS:
        return True
    # Match e.g. *.sedoparking.com → "sedoparking.com" suffix
    for parker in PARKING_HOSTS:
        if host == parker or host.endswith("." + parker):
            return True
    for frag in PARKING_HOST_FRAGMENTS:
        if frag in host:
            return True
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_recent(entry: dict, max_age_days: int) -> bool:
    """True if this cache entry was checked within max_age_days."""
    if not entry or "last_checked" not in entry:
        return False
    try:
        ts = datetime.fromisoformat(entry["last_checked"])
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age < max_age_days * 86400


def _extract_urls_from_zim(zim_path: Path) -> set[str]:
    """Walk the ZIM's search-data chunks, pull every `ws` field."""
    try:
        from libzim.reader import Archive
    except ImportError:
        print("ERROR: libzim not installed; run from venv312", file=sys.stderr)
        sys.exit(1)

    print(f"  reading {zim_path}…", file=sys.stderr)
    a = Archive(str(zim_path))
    manifest_entry = a.get_entry_by_path("search-data/manifest.json")
    manifest = json.loads(bytes(manifest_entry.get_item().content))

    urls: set[str] = set()
    chunks = list(manifest.get("chunks", {}).keys())
    n = len(chunks)
    for i, chunk in enumerate(chunks):
        if i % 200 == 0:
            print(f"  scanning chunks {i}/{n}…", file=sys.stderr)
        try:
            entry = a.get_entry_by_path(f"search-data/{chunk}.json")
            recs = json.loads(bytes(entry.get_item().content))
        except Exception as exc:  # noqa: BLE001
            print(f"  warn: chunk {chunk}: {exc}", file=sys.stderr)
            continue
        for rec in recs:
            ws = rec.get("ws")
            if isinstance(ws, str) and ws.startswith(("http://", "https://")):
                urls.add(ws.strip())
    return urls


async def _check_one(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    *,
    check_parking_body: bool = False,
) -> dict:
    """HEAD the URL, fall back to GET (some servers reject HEAD).

    Sets:
      status:        HTTP code or 'timeout'/'dns'/'ssl'/'conn'/'error'/
                     'parked' (parker-host detected) /'parked-body'
                     (parking phrase in body when check_parking_body).
      alive:         True iff 2xx/3xx AND not parked.
      final_url:     final URL after redirects.
      redirect_count
      reason:        human label — 'ok'/'http-NNN'/'parked'/etc.
    """
    out = {"last_checked": _now_iso()}
    try:
        # HEAD first — cheap. Some servers return 405 → retry GET.
        async with session.head(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            status = resp.status
            final_url = str(resp.url)
            redirect_count = len(resp.history)
            if status in (405, 501):
                # HEAD not supported. Try GET (will also fall through
                # to the parking-body check below if requested).
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=True,
                ) as gresp:
                    status = gresp.status
                    final_url = str(gresp.url)
                    redirect_count = len(gresp.history)
                    if check_parking_body and 200 <= status < 400:
                        body_chunk = await _read_body_chunk(gresp)
                        if _body_looks_parked(body_chunk):
                            out.update({
                                "status": "parked-body",
                                "alive": False,
                                "final_url": final_url,
                                "redirect_count": redirect_count,
                                "reason": "parking phrase in body",
                            })
                            return out
        # Parker-host check on the final URL after HEAD.
        if 200 <= status < 400 and _looks_parked(final_url):
            out.update({
                "status": "parked",
                "alive": False,
                "final_url": final_url,
                "redirect_count": redirect_count,
                "reason": "redirected to parker host",
            })
            return out
        # Optional body grep on the original (non-405) HEAD path —
        # we'd need to re-fetch via GET to read the body. Skip
        # unless explicitly asked since it doubles request count.
        if check_parking_body and 200 <= status < 400:
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=True,
                ) as gresp:
                    body_chunk = await _read_body_chunk(gresp)
                if _body_looks_parked(body_chunk):
                    out.update({
                        "status": "parked-body",
                        "alive": False,
                        "final_url": final_url,
                        "redirect_count": redirect_count,
                        "reason": "parking phrase in body",
                    })
                    return out
            except Exception:  # noqa: BLE001
                pass  # swallow — fall through to "alive" verdict
        out.update({
            "status": status,
            "alive": 200 <= status < 400,
            "final_url": final_url,
            "redirect_count": redirect_count,
            "reason": "ok" if 200 <= status < 400 else f"http-{status}",
        })
        return out
    except asyncio.TimeoutError:
        out.update({"status": "timeout", "alive": False, "reason": "timeout"})
    except aiohttp.ClientSSLError as exc:
        out.update({"status": "ssl", "alive": False,
                    "error": str(exc)[:100], "reason": "ssl-error"})
    except aiohttp.ClientConnectorDNSError as exc:
        out.update({"status": "dns", "alive": False,
                    "error": str(exc)[:100], "reason": "dns-failure"})
    except aiohttp.ClientConnectorError as exc:
        out.update({"status": "conn", "alive": False,
                    "error": str(exc)[:100], "reason": "connection-failed"})
    except aiohttp.ClientError as exc:
        out.update({"status": "error", "alive": False,
                    "error": str(exc)[:100], "reason": "client-error"})
    except UnicodeError as exc:
        out.update({"status": "url", "alive": False,
                    "error": str(exc)[:100], "reason": "bad-url"})
    return out


async def _read_body_chunk(resp: aiohttp.ClientResponse) -> str:
    """Read up to 32 KB of the body, lowercase-decoded for grep."""
    try:
        raw = await resp.content.read(32 * 1024)
    except Exception:  # noqa: BLE001
        return ""
    try:
        return raw.decode("utf-8", errors="ignore").lower()
    except Exception:  # noqa: BLE001
        return ""


def _body_looks_parked(body_lower: str) -> bool:
    if not body_lower:
        return False
    return any(phrase in body_lower for phrase in PARKING_BODY_PHRASES)


class _PerHostLimiter:
    """Cap concurrent requests per hostname so we don't hammer small sites."""

    def __init__(self, per_host: int) -> None:
        self._cap = per_host
        self._sems: dict[str, asyncio.Semaphore] = {}

    def for_host(self, host: str) -> asyncio.Semaphore:
        sem = self._sems.get(host)
        if sem is None:
            sem = asyncio.Semaphore(self._cap)
            self._sems[host] = sem
        return sem


async def _crawl(
    urls: list[str],
    *,
    concurrency: int,
    per_host_concurrency: int,
    timeout: int,
    progress_every: int,
    check_parking_body: bool = False,
    checkpoint_every: int = 0,
    checkpoint_cb=None,
) -> dict[str, dict]:
    """Returns {url: result} for every url in `urls`.

    If `checkpoint_every > 0` and `checkpoint_cb` is given, the
    callback is invoked with the partial `results` map every N
    completed URLs so the caller can persist progress mid-run.
    """
    results: dict[str, dict] = {}
    overall = asyncio.Semaphore(concurrency)
    per_host = _PerHostLimiter(per_host_concurrency)
    started = time.time()
    total = len(urls)
    done = 0
    alive = 0
    last_checkpoint = 0
    checkpoint_delta: dict[str, dict] = {}

    timeout_obj = aiohttp.ClientTimeout(
        total=timeout, connect=timeout, sock_read=timeout
    )
    connector = aiohttp.TCPConnector(
        limit=concurrency * 2,
        force_close=False,
    )
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    async with aiohttp.ClientSession(
        timeout=timeout_obj,
        connector=connector,
        headers=headers,
    ) as session:

        async def bounded(url: str) -> None:
            nonlocal done, alive, last_checkpoint
            try:
                host = urlparse(url).hostname or ""
            except Exception:  # noqa: BLE001
                host = ""
            host_sem = per_host.for_host(host)
            async with overall:
                async with host_sem:
                    res = await _check_one(
                        session, url, timeout,
                        check_parking_body=check_parking_body,
                    )
            results[url] = res
            checkpoint_delta[url] = res
            done += 1
            if res.get("alive"):
                alive += 1
            if progress_every > 0 and done % progress_every == 0:
                rate = done / max(0.001, time.time() - started)
                eta = (total - done) / max(0.001, rate)
                print(
                    f"  progress: {done}/{total} "
                    f"({100 * done // max(1, total)}%) "
                    f"alive={alive} "
                    f"rate={rate:.1f}/s eta={int(eta)}s",
                    file=sys.stderr,
                    flush=True,
                )
            # Persist mid-run so a Ctrl-C / OOM doesn't lose hours
            # of crawl. Checkpoint cadence is set by the caller.
            if (checkpoint_every > 0 and checkpoint_cb is not None
                    and done - last_checkpoint >= checkpoint_every):
                last_checkpoint = done
                try:
                    checkpoint_cb(checkpoint_delta)
                    checkpoint_delta.clear()
                except Exception as exc:  # noqa: BLE001
                    print(f"  warn: checkpoint failed: {exc}",
                          file=sys.stderr)

        await asyncio.gather(*(bounded(u) for u in urls))

    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--zim", type=Path, help="ZIM to extract `ws` URLs from")
    src.add_argument(
        "--urls", type=Path, help="text file with one URL per line"
    )
    p.add_argument(
        "--cache",
        type=Path,
        default=Path(__file__).parent.parent / "url_validation_cache.json",
        help="cache JSON path (read + write)",
    )
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--per-host-concurrency", type=int, default=4)
    p.add_argument("--timeout", type=int, default=12, help="seconds per request")
    p.add_argument(
        "--max-age-days",
        type=int,
        default=30,
        help="skip URLs whose cache entry is younger than this",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print plan, don't fetch"
    )
    p.add_argument("--progress-every", type=int, default=200)
    p.add_argument(
        "--check-parking-body",
        action="store_true",
        help="for URLs that return 200, also fetch up to 32 KB of "
             "body and grep for parking-page phrases. Doubles "
             "request count; use sparingly.",
    )
    p.add_argument(
        "--checkpoint-every",
        type=int,
        default=2000,
        help="persist the cache every N completed URLs so a long "
             "crawl can survive Ctrl-C / OOM without losing all "
             "state. Set to 0 to disable.",
    )
    args = p.parse_args()

    # 1. Load existing cache.
    cache: dict = {"version": 1, "checked_at": "", "entries": {}}
    if args.cache.exists():
        try:
            cache = json.loads(args.cache.read_text())
        except json.JSONDecodeError as exc:
            print(
                f"ERROR: --cache {args.cache} is not valid JSON: {exc}",
                file=sys.stderr,
            )
            return 1
    entries = cache.setdefault("entries", {})
    checkpoint_log = args.cache.with_suffix(args.cache.suffix + ".checkpoint.jsonl")
    if checkpoint_log.exists():
        try:
            with checkpoint_log.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    delta = json.loads(line)
                    if isinstance(delta, dict):
                        entries.update(delta)
            print(f"  merged checkpoint log {checkpoint_log}",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  warn: could not merge checkpoint log: {exc}",
                  file=sys.stderr)

    # 2. Gather input URLs.
    if args.zim:
        if not args.zim.exists():
            print(f"ERROR: --zim {args.zim} not found", file=sys.stderr)
            return 1
        all_urls = _extract_urls_from_zim(args.zim)
    else:
        if not args.urls.exists():
            print(f"ERROR: --urls {args.urls} not found", file=sys.stderr)
            return 1
        all_urls = {
            line.strip()
            for line in args.urls.read_text().splitlines()
            if line.strip().startswith(("http://", "https://"))
        }
    print(f"  {len(all_urls)} unique URLs in input", file=sys.stderr)

    # 3. Filter to URLs that need (re)checking.
    todo: list[str] = []
    fresh = 0
    for url in sorted(all_urls):
        existing = entries.get(url)
        if _is_recent(existing, args.max_age_days):
            fresh += 1
            continue
        todo.append(url)
    print(
        f"  {fresh} fresh in cache, {len(todo)} need checking",
        file=sys.stderr,
    )
    if not todo:
        print("  nothing to do", file=sys.stderr)
        return 0
    if args.dry_run:
        print(
            "  (--dry-run) would check the following URLs:",
            file=sys.stderr,
        )
        for u in todo[:50]:
            print(f"    {u}")
        if len(todo) > 50:
            print(f"    ...and {len(todo) - 50} more")
        return 0

    # 4. Crawl.
    print(
        f"  crawling {len(todo)} URLs "
        f"(concurrency={args.concurrency}, "
        f"per-host={args.per_host_concurrency}, "
        f"timeout={args.timeout}s)",
        file=sys.stderr,
    )
    started = time.time()

    # Checkpoint callback: append only the delta. The full JSON cache is
    # still written once at the end.
    def _save_checkpoint(partial: dict[str, dict]) -> None:
        for u, r in partial.items():
            entries[u] = r
        with checkpoint_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(partial, separators=(",", ":")) + "\n")

    results = asyncio.run(
        _crawl(
            todo,
            concurrency=args.concurrency,
            per_host_concurrency=args.per_host_concurrency,
            timeout=args.timeout,
            progress_every=args.progress_every,
            check_parking_body=args.check_parking_body,
            checkpoint_every=args.checkpoint_every,
            checkpoint_cb=_save_checkpoint,
        )
    )
    elapsed = time.time() - started
    print(
        f"  done in {elapsed:.0f}s ({len(results)} checked)",
        file=sys.stderr,
    )

    # 5. Merge into cache.
    for url, res in results.items():
        entries[url] = res
    cache["checked_at"] = _now_iso()

    # 6. Write atomically.
    tmp = args.cache.with_suffix(args.cache.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
    tmp.replace(args.cache)
    try:
        checkpoint_log.unlink()
    except FileNotFoundError:
        pass
    print(f"  wrote {args.cache}", file=sys.stderr)

    # 7. Summary.
    alive = sum(1 for e in entries.values() if e.get("alive"))
    dead = len(entries) - alive
    just_checked_alive = sum(
        1 for r in results.values() if r.get("alive")
    )
    just_checked_dead = len(results) - just_checked_alive
    print(
        f"  cache total: {len(entries)} URLs "
        f"({alive} alive, {dead} dead). "
        f"This run: {just_checked_alive} alive, {just_checked_dead} dead.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
