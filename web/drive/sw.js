// StreetZim Drive — service worker.
//
// Role: turn the Firebase-hosted /drive/ PWA into a fully offline viewer
// backed by whichever .zim file the user picks. Responsibilities:
//   1. Precache the viewer shell (HTML + MapLibre JS/CSS) on install.
//   2. Intercept fetches from /drive/viewer/* and serve them either from
//      the shell cache (known static assets) or from the user's local
//      ZIM via ZimReader.
//   3. Keep the ZIM Blob — or, for the online preview, the URL it is
//      streamed from by range requests — in IndexedDB so it survives SW
//      termination and re-launches.
//
// After install + ZIM pick, the app works with zero network requests.

importScripts('./fzstd.js', './zim-reader.js');

// Cache generation. scripts/sync-drive-viewer.sh (the firebase predeploy
// hook) rewrites the stamp on every deploy from the shell asset hashes
// (or from cloud/deploy_pwa.sh's git stamp via STAMP_OVERRIDE), so a
// changed viewer always produces a new precache and the old one is
// dropped on activate. Keep the 'streetzim-drive-shell-' prefix — the
// activate handler only deletes caches carrying it.
const SHELL_CACHE = 'streetzim-drive-shell-d334c2ed3d';

const SHELL_URLS = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png',
  './preview-config.js',
  './viewer/',
  './viewer/index.html',
  './viewer/places.html',
  // Canonical clean URL the viewer actually navigates to (Firebase
  // cleanUrls + trailingSlash). Caching only `places.html` left the
  // offline fallback with no entry for the URL that is requested.
  './viewer/places/',
  './viewer/routing-worker.js',
  './viewer/maplibre-gl.js',
  './viewer/maplibre-gl.css'
];

// Files in /drive/viewer/ that are always part of the shell, never
// the ZIM. Everything else under /drive/viewer/ is ZIM content.
// Firebase Hosting's `cleanUrls: true` redirects `places.html` →
// `places` (no extension), so we list both — otherwise the SW
// intercepts the redirected URL and tries to serve `places` from
// the ZIM, which 404s as "Not in ZIM: places".
const VIEWER_SHELL_NAMES = new Set([
  '',
  'index', 'index.html',
  'places', 'places.html',
  'maplibre-gl.js', 'maplibre-gl.css',
  // Routing worker source — served from Firebase for the PWA shell and
  // embedded directly in rebuilt ZIMs for native Kiwix.
  'routing-worker.js',
]);

// ---------- IndexedDB helpers (no dependency) ----------

const DB_NAME = 'streetzim-drive';
const DB_VERSION = 1;
const DB_STORE = 'zim';

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(DB_STORE)) {
        db.createObjectStore(DB_STORE, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbGet(id) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readonly');
    const req = tx.objectStore(DB_STORE).get(id);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error);
  });
}

async function idbPut(record) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readwrite');
    tx.objectStore(DB_STORE).put(record);
    tx.oncomplete = () => resolve();
    tx.onerror = (e) => reject((e && e.target && e.target.error) || tx.error || new Error('IndexedDB write failed'));
  });
}

async function idbDelete(id) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readwrite');
    tx.objectStore(DB_STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// ---------- ZIM reader (lazy singleton) ----------

let readerPromise = null;  // Promise<ZimReader|null>

function resetReader() {
  readerPromise = null;
}

// A record holds either a File/Blob picked on /drive/ or the URL of a
// ZIM to stream through HTTP range requests (the online preview of the
// archive.org-hosted regions — docs/online-preview.md). Both end up as
// the same ZimReader; only the byte source differs.
// A File opened "this session only": the reader reads slices straight out
// of the File, so nothing is copied. iPhone Safari will not store an 11 GB
// ZIM in IndexedDB at any price, but it will happily read one off disk --
// the copy exists for persistence across reloads, not for reading.
// Lost when the worker is terminated; the picker says so.
let sessionRec = null;

async function openReader(rec) {
  let source = rec.blob;
  if (rec.url) {
    source = new self.StreetZimHttpSource(rec.url);
    await source.open();
  }
  const r = new self.StreetZimReader(source);
  await r.open();
  return r;
}

// Any valid ZIM opens (Wikipedia, an old build); only one with a
// map-config.json is a map the viewer can show. Refusing here, before a
// record is stored, beats a "ZIM loaded" card whose Open viewer bounces
// straight back.
async function requireMap(r, what) {
  const cfg = await r.findEntry('map-config.json');
  if (!cfg) throw new Error((what || 'this ZIM') + ' is not a StreetZim map (no map-config.json)');
}

async function getReader() {
  if (readerPromise) return readerPromise;
  const p = (async () => {
    const rec = await idbGet('current');
    if (!rec || !(rec.blob || rec.url)) return null;
    return openReader(rec);
  })();
  // A failed open (transient IDB error, file moved/modified under a
  // persisted File handle) must not be memoised — every later request
  // would 500 until the SW happened to be terminated.
  const wrapped = p.catch((err) => {
    if (readerPromise === wrapped) readerPromise = null;
    throw err;
  });
  readerPromise = wrapped;
  return wrapped;
}

// ---------- Lifecycle ----------

// Wrap a fetched response in a fresh Response object before caching.
// Firebase's `cleanUrls: true` means `/drive/viewer/places.html` → 301
// `/drive/viewer/places`. A plain fetch(url, redirect:'follow') returns a
// Response whose `.redirected === true`. iOS Safari refuses to use any
// such response for a *navigation* ("Response served by service worker
// has redirections"), so we rebuild the Response from the body + status
// + headers — the manual constructor has no redirect chain, which iOS
// accepts. We keep `redirect: 'follow'` so the body is still the final
// clean-URL content.
async function cacheClean(cache, request, response) {
  const body = await response.blob();
  const clean = new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
  return cache.put(request, clean);
}

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // Bypass the browser HTTP cache — without {cache:'reload'}, cache.add
    // can pull a stale copy that a prior deploy left in Safari's disk
    // cache (e.g. HTML with max-age=3600 that hasn't expired yet).
    await Promise.all(SHELL_URLS.map(async (url) => {
      try {
        // HTML/manifest: bypass the HTTP cache outright (a prior deploy
        // can leave a stale copy in Safari's disk cache). Scripts and
        // styles: revalidate — a 304 lets the ~1 MB MapLibre bundle come
        // from the HTTP cache instead of being downloaded again on every
        // deploy, which mattered on cellular.
        const res = await fetch(url, { cache: /\.(js|css)$/.test(url) ? 'no-cache' : 'reload' });
        if (!res || !res.ok) throw new Error('status ' + (res && res.status));
        await cacheClean(cache, url, res);
      } catch (err) {
        console.warn('[sw] skip', url, err);
      }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // caches.keys() is origin-wide, not scope-wide — only touch our
    // own generations so another worker on this origin keeps its data.
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((n) => n.startsWith('streetzim-drive-shell-') && n !== SHELL_CACHE)
        .map((n) => caches.delete(n))
    );
    await self.clients.claim();
  })());
});

// ---------- Messages from the page ----------

self.addEventListener('message', (event) => {
  const msg = event.data || {};
  event.waitUntil((async () => {
    let reply = { ok: true };
    try {
      if (msg.type === 'set-zim') {
        // Validate BEFORE persisting. Writing first meant a non-ZIM
        // (or truncated) file stuck as `current`: every request then
        // 500'd and the picker hid the Remove button because status
        // reported "not loaded", so the user couldn't clear it.
        // `url` streams the ZIM by range requests instead of reading a
        // local Blob; `sourceUrl` is the human-facing origin of those
        // bytes (the archive.org download link) when `url` is a proxy.
        let rec;
        if (msg.url) {
          if (!/^https?:\/\//i.test(String(msg.url))) {
            throw new Error('set-zim: url must be http(s)');
          }
          rec = { id: 'current', url: String(msg.url),
                  sourceUrl: String(msg.sourceUrl || msg.url),
                  name: msg.name || 'zim', addedAt: Date.now() };
        } else {
          rec = { id: 'current', blob: msg.blob,
                  name: msg.name || 'zim', addedAt: Date.now() };
        }
        const r = await openReader(rec);
        await requireMap(r, rec.name);
        const sessionOnly = !!msg.session && !msg.url;
        if (sessionOnly) {
          // Skip the copy entirely. Reads come from the File reference.
          await idbDelete('current').catch(() => {});
          sessionRec = { name: rec.name, addedAt: rec.addedAt };
        } else {
          sessionRec = null;
          await idbPut(rec);
        }
        readerPromise = Promise.resolve(r);
        reply.info = r.info;
        reply.source = rec.url ? 'url' : 'file';
        reply.session = sessionOnly;
      } else if (msg.type === 'check-zim') {
        // Validate a File without persisting it. The picker then writes
        // the record itself: a window has no event time limit, whereas
        // Chromium ends a service-worker message event after five
        // minutes — which copying a multi-GB file on a phone exceeds.
        const r = new self.StreetZimReader(msg.blob);
        await r.open();
        await requireMap(r, msg.name);
        reply.info = r.info;
      } else if (msg.type === 'reload-zim') {
        // The page put a new `current` record in IndexedDB.
        resetReader();
        const r = await getReader();
        if (!r) throw new Error('no ZIM record to open');
        reply.info = r.info;
        const rec = await idbGet('current').catch(() => null);
        reply.source = rec && rec.url ? 'url' : 'file';
      } else if (msg.type === 'ping') {
        // The viewer's preview banner every few seconds: bytes moved so
        // far from the memoised reader, no IndexedDB round trip, and
        // enough activity to keep the worker from being idle-killed.
        const r = readerPromise ? await readerPromise.catch(() => null) : null;
        reply.loaded = !!r;
        reply.stats = (r && r.file && r.file.stats) ? r.file.stats : null;
        reply.cache = r ? r.cacheStats : null;
        reply.sw = Object.assign({}, swStats);
      } else if (msg.type === 'clear-zim') {
        await idbDelete('current');
        sessionRec = null;
        resetReader();
      } else if (msg.type === 'status') {
        let openError = null;
        const r = await getReader().catch((err) => {
          openError = String(err && err.message || err);
          return null;
        });
        reply.info = r ? r.info : null;
        // Why the record could not be opened (QuotaExceededError,
        // NotReadableError, HTTP 404 from archive.org…) so the picker
        // can say so instead of guessing.
        reply.openError = openError;
        reply.loaded = !!r;
        // `present` ≠ `loaded`: a persisted File handle can stop being
        // readable later (file moved / edited on disk). The picker uses
        // this to keep the Remove button available so the user can
        // clear a record that no longer opens.
        const rec = await idbGet('current').catch(() => null);
        reply.present = !!(rec && (rec.blob || rec.url)) || !!sessionRec;
        reply.name = (rec && rec.name) || (sessionRec && sessionRec.name) || null;
        reply.session = !!sessionRec && !rec;
        // Where the bytes come from — 'file' for a local pick, 'url' for
        // the online preview. The picker's status line and the viewer's
        // preview banner (with its Download link) key off this.
        // A session-only open has no IDB record: `source` must fall back to
        // sessionRec, or the picker reports "not loaded" for a map that IS
        // open. (An earlier fix set this above and was silently overwritten
        // here -- this is the authoritative assignment.)
        reply.source = rec && rec.url ? 'url'
                     : (rec && rec.blob) ? 'file'
                     : sessionRec ? 'file' : null;
        reply.url = rec && rec.url ? (rec.sourceUrl || rec.url) : null;
        reply.sizeBytes = r ? r.size : null;
        reply.stats = (r && r.file && r.file.stats) ? r.file.stats : null;
        reply.cache = r ? r.cacheStats : null;
        reply.sw = Object.assign({}, swStats);
      } else {
        reply = { ok: false, error: 'unknown message type' };
      }
    } catch (err) {
      reply = { ok: false, error: String(err && err.message || err) };
    }
    if (event.ports && event.ports[0]) event.ports[0].postMessage(reply);
  })());
});

// ---------- Fetch interception ----------

// "bytes=start-end" (end optional) against a `total`-byte body →
// {start, end} inclusive, 416 for a start past the end, or null when the
// header is absent/invalid and the whole body should be sent.
// RFC 7233 §2.1: an explicit last-byte-pos < first-byte-pos makes the
// whole Range header syntactically invalid → ignore it (full 200). The
// open-ended form "bytes=N-" has no last-byte-pos, so that rule cannot
// apply to it; a start past the end is a genuine 416 in both forms.
function parseRange(range, total) {
  const m = /^bytes=(\d+)-(\d*)$/.exec(range || '');
  if (!m) return null;
  const start = parseInt(m[1], 10);
  if (isNaN(start)) return null;
  const last = m[2] ? parseInt(m[2], 10) : total - 1;
  if (m[2] && last < start) return null;
  if (start >= total) return 416;
  return { start, end: Math.min(last, total - 1) };
}

function notSatisfiable(total) {
  return new Response(null, {
    status: 416,
    statusText: 'Range Not Satisfiable',
    headers: { 'Content-Range': 'bytes */' + total }
  });
}

function rangeResponse(data, range, mime) {
  const r = parseRange(range, data.byteLength);
  if (r === null) return null;
  if (r === 416) return notSatisfiable(data.byteLength);
  const start = r.start, end = r.end;
  const slice = data.subarray(start, end + 1);
  return new Response(slice, {
    status: 206,
    statusText: 'Partial Content',
    headers: {
      'Content-Type': mime,
      'Content-Length': String(slice.byteLength),
      'Content-Range': 'bytes ' + start + '-' + (start + slice.byteLength - 1) + '/' + data.byteLength,
      'Accept-Ranges': 'bytes'
    }
  });
}

function okResponse(data, mime) {
  return new Response(data, {
    status: 200,
    headers: {
      'Content-Type': mime,
      'Content-Length': String(data.byteLength),
      'Cache-Control': 'no-cache',
      'Accept-Ranges': 'bytes'
    }
  });
}

function notFound(path) {
  return new Response('Not in ZIM: ' + path, {
    status: 404,
    headers: { 'Content-Type': 'text/plain' }
  });
}

function noZim() {
  return new Response('No ZIM loaded', {
    status: 503,
    headers: { 'Content-Type': 'text/plain' }
  });
}

// The viewer probes for both the v10+ spatial layout and the v8/v9
// monolithic layout, expecting one to be absent. Returning 404 for
// the missing variant is *correct* but the browser surfaces it as
// "Failed to load resource" in the console even though the JS
// handles the .ok=false path. Map known-optional probes to
// 204 No Content + X-Streetzim-Absent header — quiet, and the JS
// already treats !ok || empty body as "fall back".
const OPTIONAL_PROBE_PATHS = new Set([
  'routing-data/graph-cells-index.bin',
  'routing-data/graph.bin',
  'routing-data/graph-chunk-manifest.json',
  'routing-data/graph-geoms.bin',
  'routing-data/graph-geoms-chunk-manifest.json',
]);

// Wikipedia articles open by a full-page navigation from the map. A Home
// Screen web app on iOS has no browser chrome, so an article opened there
// was a dead end. ZIMs built since 2026-09 carry their own "Back to map"
// bar (cloud/wiki_articles.py); for older ones the PWA adds the same bar
// here, and in both cases the link becomes the absolute viewer URL —
// `../index.html` would be answered by Firebase with a redirect, which
// iOS refuses for a navigation served through a service worker.
const BACK_BAR_HTML =
  '<nav class="sz-back"><a href="/drive/viewer/" ' +
  'onclick="if(history.length>1){history.back();return false}">' +
  '&#8592; Back to map</a></nav>';
const BACK_BAR_CSS =
  '<link rel="icon" href="data:,">' +   // no /favicon.ico probe (404 noise)
  '<style>.sz-back{position:sticky;top:0;z-index:1;background:#fff;' +
  'margin:-1em -1em .5em;padding:calc(.3em + env(safe-area-inset-top,0px)) 1em .3em;' +
  'border-bottom:1px solid #eee}.sz-back a{display:inline-flex;align-items:center;' +
  'min-height:44px;color:#2563eb;font-weight:600;text-decoration:none}</style>';

function withBackToMap(data) {
  let html;
  try { html = new TextDecoder('utf-8').decode(data); } catch (e) { return data; }
  if (html.indexOf('class="sz-back"') >= 0) {
    html = html.replace(/(<nav class="sz-back"><a href=")(?:\.\.\/)+index\.html"/,
                        '$1/drive/viewer/"');
  } else {
    html = html.replace(/<\/head>/i, BACK_BAR_CSS + '</head>')
               .replace(/<body\b[^>]*>/i, (m) => m + BACK_BAR_HTML);
  }
  return new TextEncoder().encode(html);
}

// Entries at least this big that sit in a raw (uncompressed) cluster are
// streamed straight from the file or the network instead of being read
// into the worker's memory first. Which entries are raw is the builders'
// call: cloud/repackage_zim.py and cloud/swap_viewer_rust.py store
// routing-data/graph.bin and any routing item of 200 MB or more
// uncompressed, and zstd-compress the rest — a compressed cluster has to
// be decoded whole, so a 100 MB routing chunk still passes through
// memory (once, and it is not cached). Lowering that builder threshold
// to this one would let the streaming path carry every big routing item
// (docs/mobile-browser-review.md, "What remains").
const STREAM_MIN_BYTES = 4 * 1024 * 1024;

// Reads of the same path in flight at once share one read: the cells
// index is requested by the main thread and the worker together on
// older viewers.
const inflightEntryReads = new Map();   // lookupPath → Promise<{mime, data, url}>

// Counters the tests read through `status`/`ping` — the memory claims
// ("streamed, not buffered"; "one read for concurrent requests") are
// not observable from a page any other way.
const swStats = { streamed: 0, dedupedReads: 0, notices: 0, lastNotice: null };

// Tell open viewer pages that the streamed source is failing (archive.org
// 5xx, the proxy's daily quota, no network — or, `permanent`, a 4xx that
// no retry will fix), so the preview banner can say so instead of tiles
// silently not arriving. One notice per status per 30 s.
let lastUpstreamNotice = 0, lastUpstreamStatus = null;
function notifyUpstreamTrouble(err, event) {
  const status = (err && err.status) || 0;
  const now = Date.now();
  if (status === lastUpstreamStatus && now - lastUpstreamNotice < 30000) return;
  lastUpstreamNotice = now;
  lastUpstreamStatus = status;
  swStats.notices++;
  swStats.lastNotice = status + ' ' + String(err && err.message || err).slice(0, 120);
  const p = self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then((clients) => {
      for (const c of clients) {
        c.postMessage({ type: 'streetzim-upstream', status,
                        permanent: !!(err && err.permanent),
                        message: String(err && err.message || err) });
      }
    }).catch(() => {});
  // Keep the worker alive until the notice is out; without this a worker
  // with nothing else pending can be torn down first.
  if (event) { try { event.waitUntil(p); } catch (e) {} }
}

async function streamRaw(reader, span, mime, request, event) {
  const total = span.length;
  let start = 0, end = total - 1, status = 200;
  const r = parseRange(request.headers.get('range'), total);
  if (r === 416) return notSatisfiable(total);
  if (r) { start = r.start; end = r.end; status = 206; }
  const headers = {
    'Content-Type': mime,
    'Content-Length': String(end - start + 1),
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'no-cache'
  };
  if (status === 206) headers['Content-Range'] = 'bytes ' + start + '-' + end + '/' + total;
  const src = reader.file;
  // A corrupt offset table, or a file truncated on disk after the pick:
  // Blob.slice would clamp silently and the declared Content-Length go
  // unmet; the buffered path throws for the same case.
  if (span.offset + span.length > src.size) {
    throw new Error('ZimReader: blob ' + span.offset + '+' + span.length + ' past the end of the file (' + src.size + ')');
  }
  if (src.remote) {
    // A verified 206 whose body has not been read: hand its native
    // stream on untouched, so the browser pipes it past the worker's
    // thread. A drop after the headers reaches the page as a failed
    // body read (which it retries); the next request the worker opens
    // against a dead origin raises the banner note.
    const res = await src.openRange(span.offset + start, span.offset + end);
    swStats.streamed++;
    return new Response(res.body, { status, headers });
  }
  // A Blob body streams from disk as the page reads it; nothing is
  // materialised in the worker.
  swStats.streamed++;
  return new Response(src.slice(span.offset + start, span.offset + end + 1), { status, headers });
}

async function serveFromZim(viewerPath, request, event) {
  try {
    const reader = await getReader();
    if (!reader) return noZim();
    // Percent-decode so paths with special chars (e.g. wiki-article titles
    // like "AT%26T_Park" or "Foo_%28Bar%29") match the raw ZIM entry path.
    let lookupPath = viewerPath;
    try { lookupPath = decodeURIComponent(viewerPath); } catch (e) {}
    lookupPath = self.StreetZimReader.normalizePath(lookupPath);
    const entry = await reader.findEntry(lookupPath);
    if (!entry) {
      if (OPTIONAL_PROBE_PATHS.has(viewerPath)) {
        // Tried 204 No Content; Chromium fires both response(204) AND
        // requestfailed(net::ERR_ABORTED) for null-body 204s, which
        // makes Puppeteer (and devtools panel) flag it as a failure.
        // 200 OK + empty body + X-Streetzim-Absent header is quiet
        // and lets the JS detect "absent" via the header.
        return new Response('', {
          status: 200,
          statusText: 'OK',
          headers: {
            'Content-Type': 'application/octet-stream',
            'Content-Length': '0',
            'X-Streetzim-Absent': '1'
          }
        });
      }
      return notFound(viewerPath);
    }
    const isArticle = viewerPath.startsWith('wiki-article/') && /^text\/html/i.test(entry.mime);
    if (!isArticle) {
      const span = await reader.rawBlobSpan(entry.cluster, entry.blob);
      if (span && span.length >= STREAM_MIN_BYTES) {
        // `await`, not a bare `return`: a promise returned from inside
        // the try block would skip the catch below, and an origin outage
        // while the range was being opened came back to the page as a
        // network failure instead of the 503 + notice it is meant to
        // get.
        return await streamRaw(reader, span, entry.mime, request, event);
      }
    }
    let read = inflightEntryReads.get(lookupPath);
    if (read) swStats.dedupedReads++;
    if (!read) {
      read = reader.readEntry(entry);
      inflightEntryReads.set(lookupPath, read);
      read.then(() => {}, () => {}).then(() => {
        if (inflightEntryReads.get(lookupPath) === read) inflightEntryReads.delete(lookupPath);
      });
    }
    const got = await read;
    if (isArticle) return okResponse(withBackToMap(got.data), got.mime);
    const range = request.headers.get('range');
    if (range) {
      const rr = rangeResponse(got.data, range, got.mime);
      if (rr) return rr;
    }
    return okResponse(got.data, got.mime);
  } catch (err) {
    console.error('[sw] ZIM lookup failed for', viewerPath, err);
    const message = String(err && err.message || err);
    if (err && err.upstream) {
      // The streamed source, not the ZIM, and it may come back (network,
      // 5xx, the proxy's 429): say so and ask for a retry.
      notifyUpstreamTrouble(err, event);
      return new Response('Upstream error: ' + message, {
        status: 503,
        headers: {
          'Content-Type': 'text/plain',
          'Retry-After': '30',
          'X-Streetzim-Upstream': String(err.status || 'network')
        }
      });
    }
    if (err && err.status) {
      // A 4xx from the origin (the file was renamed or removed, the
      // proxy refused the item): no retry will change it, so no
      // Retry-After, and the notice says to pick the map again.
      err.permanent = true;
      notifyUpstreamTrouble(err, event);
      return new Response('Upstream error: ' + message, {
        status: 502,
        headers: { 'Content-Type': 'text/plain', 'X-Streetzim-Upstream': String(err.status) }
      });
    }
    return new Response('ZIM error: ' + message, {
      status: 500,
      headers: { 'Content-Type': 'text/plain' }
    });
  }
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // HEAD is answered like GET without the body, so a probe of a ZIM path
  // sees the entry's status and headers rather than the host's 404 page.
  if (req.method !== 'GET' && req.method !== 'HEAD') return;

  const url = new URL(req.url);

  // Only intercept within our own scope. Firebase assets outside /drive/
  // (e.g. analytics for web/index.html) fall through to the network.
  if (url.origin !== location.origin) return;
  if (!url.pathname.startsWith('/drive/')) return;

  // Viewer scope: /drive/viewer/*
  const viewerPrefix = '/drive/viewer/';
  if (url.pathname === viewerPrefix || url.pathname.startsWith(viewerPrefix)) {
    const rest = url.pathname.slice(viewerPrefix.length);
    const firstSegment = rest.split('/')[0] || '';
    // Firebase cleanUrls + trailingSlash:true canonicalizes
    // `places.html` to `places/` (with trailing slash). Treat that
    // as the same shell asset as the un-slashed name — strip a
    // single trailing slash before deciding shell vs ZIM data.
    const restNoSlash = rest.endsWith('/') ? rest.slice(0, -1) : rest;
    // Runtime-cache key without the query string: lookups use
    // ignoreSearch, so storing `?debug=1` / `?bust=<ts>` variants
    // separately only accumulated ~370 KB copies of the viewer HTML.
    const cacheKey = url.origin + url.pathname;
    if (VIEWER_SHELL_NAMES.has(firstSegment) && !restNoSlash.includes('/')) {
      // Shell asset — NETWORK-FIRST. Stale cached HTML/JS was the
      // 2026-04-25 frustration: deploys were live on Firebase but
      // users saw old bundles for an indefinite window because the
      // SW served the cache. Network-first means: when online, you
      // ALWAYS see the current deploy. Cache only kicks in offline.
      event.respondWith((async () => {
        try {
          // 'no-cache' (not 'no-store'): still revalidates with Firebase on
          // every load so a deploy is visible immediately, but a 304 lets
          // the ~1.5 MB of MapLibre JS/CSS come from the HTTP cache instead
          // of being re-downloaded on every launch.
          const net = await fetch(req, { cache: 'no-cache' });
          // GET only: caching a HEAD answer would store an empty body
          // under the key the offline fallback serves.
          if (net && net.ok && req.method === 'GET') {
            const copy = net.clone();
            // Keep the SW alive until the cache write lands, and never
            // let a failed put surface as an unhandled rejection. The
            // waitUntil itself is guarded too: if an engine ever refused
            // it here, the outer catch would otherwise swap this fresh
            // response for the stale cache.
            try {
              event.waitUntil(
                caches.open(SHELL_CACHE)
                  .then((c) => cacheClean(c, cacheKey, copy))
                  .catch(() => {})
              );
            } catch (_) {}
          }
          return net;
        } catch (e) {
          // Offline fallback only. ignoreSearch: the picker forwards
          // ?debug=1 / ?route=… into /drive/viewer/?… and the shell
          // was precached without a query string.
          const cached = await caches.match(req, { ignoreSearch: true });
          return cached || notFound(rest);
        }
      })());
      return;
    }
    // Data path — serve from ZIM.
    event.respondWith(req.method === 'HEAD'
      ? serveFromZim(rest, req, event).then((r) => {
          if (r.body) { try { r.body.cancel(); } catch (e) {} }
          return new Response(null, { status: r.status, statusText: r.statusText, headers: r.headers });
        })
      : serveFromZim(rest, req, event));
    return;
  }

  // build-info.js must never be cached — it's the "am I on the fresh
  // deploy?" indicator. Network-first, no cache.
  if (url.pathname === '/drive/build-info.js') {
    event.respondWith(fetch(req, { cache: 'no-store' }).catch(() =>
      new Response('/* offline */', {
        status: 200,
        headers: { 'Content-Type': 'text/javascript' }
      })
    ));
    return;
  }

  // Picker page + shell — NETWORK-FIRST too. Same reason as the
  // viewer above: when online, always reflect the current deploy.
  event.respondWith((async () => {
    try {
      const net = await fetch(req, { cache: 'no-cache' });
      if (net && net.ok && req.method === 'GET') {
        const copy = net.clone();
        try {
          event.waitUntil(
            caches.open(SHELL_CACHE)
              .then((c) => cacheClean(c, url.origin + url.pathname, copy))
              .catch(() => {})
          );
        } catch (_) {}
      }
      return net;
    } catch (e) {
      const cached = await caches.match(req, { ignoreSearch: true });
      return cached || notFound(url.pathname);
    }
  })());
});
