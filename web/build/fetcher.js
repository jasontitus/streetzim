// Range requests for the language builder, shared by the page (packs) and
// assembler-worker.js (the base map, when the visitor has no copy of it).
// Plain script: defines self.SZFetch in a window or a worker.
//
// fetchRetrying(url, out, outAt, a, b, size, signal, net, onBytes, onRetry, opt)
// fills out[a - outAt, b - outAt) with bytes [a, b) of url:
// - a request is aborted when no byte has arrived for stallMs (also while
//   waiting for the answer);
// - a laggard is asked for again at once (up to slowTries times per range):
//   after slowAfterMs at under slowBps, and under a quarter of the median
//   speed of the requests that have finished (the proxy's upstream sometimes
//   trickles one request while the next is fast; when every request is slow,
//   asking again only costs another wait for the first byte);
// - any other failure is retried after a backoff of 1, 2, 4, ... s (capped at
//   maxBackoffMs), `tries` requests in all; a 4xx other than 408/429 is not;
// - every retry resumes from the first missing byte;
// - with patienceMs, `tries` failures do not end it: it then waits for the
//   connection (the 'online' event, or 20-60 s) and tries again, until no
//   byte has arrived from any request sharing `net` for patienceMs. Each
//   second of that wait calls onWait(ms without a byte).
// net collects the speeds (see newNet); onBytes(n) reports progress and
// onRetry(note) explains each retry.
(function (g) {
  'use strict';
  const DEFAULTS = { stallMs: 30000, tries: 5, maxBackoffMs: 8000, slowAfterMs: 20000, slowBps: 50e3, slowTries: 3, patienceMs: 0, onWait: null };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  class FetchError extends Error {
    constructor(msg, permanent) { super(msg); this.permanent = !!permanent; }
  }
  const newNet = () => ({ requests: 0, bytes: 0, ms: 0, ttfb: [], mbps: [], retries: 0, slow_retries: 0, errors: 0, resets: 0, hash_retries: 0,
    waits: 0, wait_ms: 0, lastByteAt: performance.now() });
  const online = () => !(self.navigator && self.navigator.onLine === false);
  // Until the 'online' event (when the browser says it is offline: up to
  // 60 s), or 20 s; aborted with the signal.
  function waitForConnection(signal, tick) {
    return new Promise((resolve, reject) => {
      const t0 = performance.now();
      const on = typeof self.addEventListener === 'function';
      const done = () => { clearTimeout(timer); clearInterval(every); if (on) self.removeEventListener('online', done); signal.removeEventListener('abort', stop); resolve(); };
      const stop = () => { done(); reject(aborted()); };
      const timer = setTimeout(done, online() ? 20000 : 60000);
      const every = setInterval(() => tick(performance.now() - t0), 1000);
      if (on) self.addEventListener('online', done);
      signal.addEventListener('abort', stop, { once: true });
    });
  }
  function pct(a, p) { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; }
  const aborted = () => new DOMException('aborted', 'AbortError');

  async function fetchRange(url, a, b, size, signal, net, onChunk, crawlCheck, opt) {
    const ac = new AbortController();
    const outer = () => ac.abort();
    if (signal.aborted) throw aborted();
    signal.addEventListener('abort', outer, { once: true });
    let timer = null, stalled = false, n = 0;
    const arm = () => { clearTimeout(timer); timer = setTimeout(() => { stalled = true; ac.abort(); }, opt.stallMs); };
    const t0 = performance.now();
    arm();
    try {
      const r = await fetch(url, { headers: { Range: `bytes=${a}-${b - 1}` }, cache: 'no-store', signal: ac.signal });
      const ttfb = performance.now() - t0;
      if (r.status !== 206) {
        const permanent = r.status >= 400 && r.status < 500 && r.status !== 408 && r.status !== 429;
        throw new FetchError(r.status === 200 ? 'the server ignored the range request' : `HTTP ${r.status}`, permanent);
      }
      const cr = r.headers.get('Content-Range');
      const m = cr && /^bytes (\d+)-(\d+)\/(\d+|\*)$/.exec(cr);
      if (cr && (!m || +m[1] !== a || +m[2] !== b - 1 || (m[3] !== '*' && +m[3] !== size))) throw new FetchError(`unexpected Content-Range ${cr}`);
      const reader = r.body.getReader();
      for (;;) {
        arm();
        const { done, value } = await reader.read();
        if (done) break;
        if (n + value.length > b - a) throw new FetchError('the server sent too many bytes');
        onChunk(value); n += value.length;
        net.lastByteAt = performance.now();
        const ms = performance.now() - t0;
        const bps = n / (ms / 1000);
        if (crawlCheck && n < b - a && ms > opt.slowAfterMs && bps < opt.slowBps && net.mbps.length && bps * 4 < pct(net.mbps, 0.5) * 1e6) {
          reader.cancel().catch(() => {});
          const e = new FetchError(`only ${Math.round(bps / 1e3)} KB/s`); e.slow = true; throw e;
        }
      }
      if (n !== b - a) throw new FetchError(`the server sent ${n} of ${b - a} bytes`);
      const ms = performance.now() - t0;
      net.requests++; net.ttfb.push(Math.round(ttfb));
      net.mbps.push(+(n / 1e6 / (ms / 1000)).toFixed(2));
      if (net.mbps.length > 200) net.mbps.shift();
      if (net.ttfb.length > 200) net.ttfb.shift();
    } catch (e) {
      if (signal.aborted) throw aborted();
      if (stalled) throw new FetchError(`no data for ${opt.stallMs / 1000} s`);
      if (e instanceof FetchError) throw e;
      net.resets++;
      throw new FetchError('the connection failed (' + (e.message || e) + ')');
    } finally {
      net.bytes += n; net.ms += performance.now() - t0;
      clearTimeout(timer);
      signal.removeEventListener('abort', outer);
    }
  }

  async function fetchRetrying(url, out, outAt, a, b, size, signal, net, onBytes, onRetry, options) {
    const opt = Object.assign({}, DEFAULTS, options);
    let pos = a, failures = 0, slow = 0;
    for (;;) {
      try {
        await fetchRange(url, pos, b, size, signal, net, (chunk) => { out.set(chunk, pos - outAt); pos += chunk.length; onBytes(chunk.length); }, slow < opt.slowTries, opt);
        return;
      } catch (e) {
        if (e.name === 'AbortError' || e.permanent) throw e;
        net.errors++;
        if (e.slow) { slow++; net.slow_retries++; onRetry(`${e.message}; asking again`); continue; }
        if (++failures >= opt.tries) {
          const idle = performance.now() - net.lastByteAt;
          if (!opt.patienceMs) throw new FetchError(`${e.message}, ${opt.tries} tries`);
          if (idle >= opt.patienceMs) throw new FetchError(`${e.message}; nothing arrived for ${Math.round(idle / 60000)} min`);
          net.waits++;
          const w0 = performance.now();
          await waitForConnection(signal, () => { if (opt.onWait) opt.onWait(performance.now() - net.lastByteAt); });
          net.wait_ms += performance.now() - w0;
          continue;                    // stays past `tries`: the next failure waits again
        }
        net.retries++;
        onRetry(`${e.message}; trying again (${failures + 1} of ${opt.tries})`);
        await sleep(Math.min(opt.maxBackoffMs, 1000 * 2 ** (failures - 1)));
      }
    }
  }

  g.SZFetch = { FetchError, newNet, pct, fetchRetrying, sleep };
})(self);
