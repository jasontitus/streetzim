// Cloudflare Workers entry point of the build worker (streetzim-build-proxy):
// the /build/ page's range proxy and the language versions' webseed, apart
// from the preview proxy so either can be switched off alone.
//
//   /merged/<region>/<lang>/<uuid>/<file>.zim   a language version, served
//                                               virtually (merged-webseed.js)
//   /<archive.org item>/<file>.zim              a ranged read of one archive.org
//                                               file (the preview proxy's handler)
//
// The free plan's 100,000 requests a day are per ACCOUNT: this worker shares
// them with streetzim-preview-proxy. WEBSEED_DAILY_BUDGET (default 50000)
// caps /merged/ requests per UTC day so the webseed cannot use up the
// preview's share: counted in the METER Durable Object when it is bound
// (wrangler.toml), else per isolate (approximate). Over the budget /merged/
// answers 503 at once, which still counts as a request. Switches: MERGED=0
// (no /merged/), OFF=1 (nothing) — both still count each request; only
// `workers_dev = false` with no route, or deleting the worker, stops the
// count. Values: 1/true/yes/on and 0/false/no/off.
import { handleRequest } from '../preview-proxy/ia-range-proxy.js';
import { createMergedHandler, isolateMeter } from './merged-webseed.js';

export function flag(value, dflt) {
  if (value == null || value === '') return dflt;
  const v = String(value).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(v)) return true;
  if (['0', 'false', 'no', 'off'].includes(v)) return false;
  return dflt;
}

const off = (msg) => new Response(msg + '\n', { status: 503, headers: { 'Access-Control-Allow-Origin': '*', 'Retry-After': '3600' } });

// The shared daily meter: each isolate adds up its own requests and reports
// them to the Durable Object every FLUSH_EVERY requests or FLUSH_MS, learning
// the day's total in return (so the cap is exact to within a few batches).
const FLUSH_EVERY = 25, FLUSH_MS = 20000;
export function doMeter(ns, budget) {
  // pending: counted here, not reported; inflight: reported, answer not back
  let pendingCount = 0, inflight = 0, total = 0, day = '', lastFlush = 0;
  const stub = ns.get(ns.idFromName('webseed'));
  async function flush() {
    const add = pendingCount; pendingCount = 0; inflight += add; lastFlush = Date.now();
    try {
      const r = await stub.fetch(`https://meter/add?n=${add}`);
      const j = await r.json();
      inflight -= add;
      if (j.day === day || !day) { day = j.day; total = j.n; }
    } catch (e) { inflight -= add; pendingCount += add; }
  }
  return {
    allow(ctx) {
      const today = new Date().toISOString().slice(0, 10);
      if (today !== day) { day = today; total = 0; }
      pendingCount += 1;
      const used = total + inflight + pendingCount;
      if (pendingCount >= FLUSH_EVERY || Date.now() - lastFlush > FLUSH_MS) {
        const p = flush();
        if (ctx && typeof ctx.waitUntil === 'function') ctx.waitUntil(p);
      }
      return !(budget > 0) || used <= budget;
    },
  };
}

// Durable Object: one instance ("webseed") holding {day, n}. Additions run
// one after another (the runtime's input gate does that for storage too;
// the queue keeps it so wherever this runs).
export class WebseedMeter {
  constructor(state) { this.state = state; this.queue = Promise.resolve(); }
  fetch(request) {
    const add = Math.max(0, Number(new URL(request.url).searchParams.get('n')) || 0);
    const run = this.queue.then(async () => {
      const day = new Date().toISOString().slice(0, 10);
      let rec = (await this.state.storage.get('count')) || { day, n: 0 };
      if (rec.day !== day) rec = { day, n: 0 };
      rec = { day: rec.day, n: rec.n + add };
      await this.state.storage.put('count', rec);
      return new Response(JSON.stringify(rec), { headers: { 'Content-Type': 'application/json' } });
    });
    this.queue = run.catch(() => {});
    return run;
  }
}

let merged = null;

export default {
  fetch(request, env, ctx) {
    env = env || {};
    if (flag(env.OFF, false)) return off('switched off');
    const path = new URL(request.url).pathname;
    if (path.startsWith('/merged/')) {
      if (!flag(env.MERGED, true)) return off('language-version downloads are switched off');
      if (!merged) {
        const budget = Number(env.WEBSEED_DAILY_BUDGET || 50000);
        merged = createMergedHandler({
          site: env.SITE_ORIGIN, upstream: env.UPSTREAM,
          meter: env.METER ? doMeter(env.METER, budget) : isolateMeter(budget),
        });
      }
      return merged(request, ctx);
    }
    return handleRequest(request);
  },
};

// tests: a fresh handler after changing env
export function _reset() { merged = null; }
