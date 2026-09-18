// StreetZim online preview — byte-range proxy in front of archive.org.
//
// Why it exists: archive.org's download servers honour HTTP Range
// requests but send no CORS headers, and its /cors/ endpoint sends CORS
// headers but ignores Range (it streams the whole file, measured
// 2026-09-18 — see docs/online-preview.md). A browser therefore cannot
// read a ZIM off archive.org piecemeal. This proxy sits in between: it
// forwards GET/HEAD requests for one StreetZim item's file — Range header
// and all — to https://archive.org/download/, follows the redirect to
// the storage node, and hands the bytes back with the CORS headers the
// /drive/ viewer needs. Nothing is buffered; the upstream body streams
// through.
//
// Runtime-neutral: handleRequest(Request) → Promise<Response> uses the
// Fetch API only, so the same file runs on Cloudflare Workers
// (worker.js), Deno, or Node ≥ 18 (serve-local.mjs).

const UPSTREAM = 'https://archive.org/download/';

// Only StreetZim's own items, only .zim files. This is not a general
// archive.org proxy — anything else would just burn the quota.
const ITEM_RE = /^streetzim-[a-z0-9-]+$/;
const FILE_RE = /^[A-Za-z0-9._-]+\.zim$/;

// A single ascending range, which is also the only form browsers send
// without a CORS preflight.
const RANGE_RE = /^bytes=\d+-\d*$/;

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Range, If-Range',
  'Access-Control-Expose-Headers':
    'Content-Range, Content-Length, Accept-Ranges, ETag, Last-Modified',
  'Access-Control-Max-Age': '86400',
};

const PASS_THROUGH = [
  'content-type', 'content-length', 'content-range', 'etag', 'last-modified',
];

function reply(status, message) {
  return new Response(message + '\n', {
    status,
    headers: { ...CORS, 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

export async function handleRequest(request) {
  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: CORS });
  }
  const url = new URL(request.url);
  const m = /^\/([^/]+)\/([^/]+)$/.exec(url.pathname);
  if (!m) return reply(404, 'expected /<archive.org item>/<file>.zim');
  const item = m[1];
  const file = m[2];
  if (!ITEM_RE.test(item) || !FILE_RE.test(file)) {
    return reply(403, 'only StreetZim ZIMs (streetzim-* items) are served here');
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    return reply(405, 'GET or HEAD only');
  }
  // The range arrives as a Range header and, from the viewer, also as
  // `?bytes=start-end`. The query form exists for proxies that sit behind
  // a CDN: Firebase Hosting keys its cache on path + query and its docs
  // do not promise to forward Range to a function, so the query is the
  // form that survives and caches correctly. It wins when both are given.
  const q = url.searchParams.get('bytes');
  const range = (q && /^\d+-\d*$/.test(q)) ? 'bytes=' + q : request.headers.get('Range');
  if (request.method === 'GET' && !(range && RANGE_RE.test(range))) {
    return reply(400, 'a single byte range is required, as "Range: bytes=start-end" ' +
      'or "?bytes=start-end"; for the whole file download it from archive.org directly');
  }

  const headers = new Headers();
  if (range) headers.set('Range', range);
  const ifRange = request.headers.get('If-Range');
  if (ifRange) headers.set('If-Range', ifRange);

  let upstream;
  try {
    upstream = await fetch(UPSTREAM + item + '/' + file, {
      method: request.method,
      headers,
      redirect: 'follow',
    });
  } catch (err) {
    return reply(502, 'archive.org unreachable: ' + (err && err.message || err));
  }

  const out = new Headers(CORS);
  for (const name of PASS_THROUGH) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  out.set('Accept-Ranges', 'bytes');
  // Dated filenames never change content, so browsers and CDNs may keep
  // the aligned blocks the viewer asks for (its in-memory cache dies
  // with the service worker). `Vary: Range` keeps a shared cache from
  // handing one range's bytes to another when the range came only in
  // the header; a ?bytes= query is already part of the cache key.
  out.set('Cache-Control', upstream.ok ? 'public, max-age=86400, s-maxage=86400' : 'no-store');
  out.set('Vary', 'Range');

  return new Response(request.method === 'HEAD' ? null : upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: out,
  });
}
