// StreetZim Drive — service worker.
//
// Role: turn the Firebase-hosted /drive/ PWA into a fully offline viewer
// backed by whichever .zim file the user picks. Responsibilities:
//   1. Precache the viewer shell (HTML + MapLibre JS/CSS) on install.
//   2. Intercept fetches from /drive/viewer/* and serve them either from
//      the shell cache (known static assets) or from the user's local
//      ZIM via ZimReader.
//   3. Keep the ZIM Blob in IndexedDB so it survives SW termination and
//      re-launches.
//
// After install + ZIM pick, the app works with zero network requests.

importScripts('./fzstd.js', './zim-reader.js');

// Bump this when the shell changes (new maplibre, new viewer HTML, etc.).
// The sync script writes a stamp to web/drive/viewer/.version which the
// page reads on load and posts to the SW — we compare and clear stale
// caches. For now just hand-bump on big changes.
const SHELL_CACHE = 'streetzim-drive-shell-675b3bfa8d-d094828';

const SHELL_URLS = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png',
  './viewer/',
  './viewer/index.html',
  './viewer/places.html',
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
  'maplibre-gl.js', 'maplibre-gl.css'
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
    tx.onerror = () => reject(tx.error);
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

async function getReader() {
  if (readerPromise) return readerPromise;
  readerPromise = (async () => {
    const rec = await idbGet('current');
    if (!rec || !rec.blob) return null;
    const r = new self.StreetZimReader(rec.blob);
    await r.open();
    return r;
  })();
  return readerPromise;
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
        const res = await fetch(url, { cache: 'reload' });
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
    const names = await caches.keys();
    await Promise.all(
      names.filter((n) => n !== SHELL_CACHE).map((n) => caches.delete(n))
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
        await idbPut({
          id: 'current',
          blob: msg.blob,
          name: msg.name || 'zim',
          addedAt: Date.now()
        });
        resetReader();
        // Force-open so the page sees any error immediately.
        const r = await getReader();
        reply.info = r ? r.info : null;
      } else if (msg.type === 'clear-zim') {
        await idbDelete('current');
        resetReader();
      } else if (msg.type === 'status') {
        const r = await getReader().catch(() => null);
        reply.info = r ? r.info : null;
        reply.loaded = !!r;
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

function rangeResponse(data, range, mime) {
  // Parse "bytes=start-end" (end optional)
  const m = /^bytes=(\d+)-(\d*)$/.exec(range);
  if (!m) return null;
  const start = parseInt(m[1], 10);
  const end = m[2] ? parseInt(m[2], 10) : data.byteLength - 1;
  if (isNaN(start) || start >= data.byteLength) return null;
  const slice = data.subarray(start, Math.min(end + 1, data.byteLength));
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

async function serveFromZim(viewerPath, request) {
  try {
    const reader = await getReader();
    if (!reader) return noZim();
    const entry = await reader.read(viewerPath);
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
    const range = request.headers.get('range');
    if (range) {
      const rr = rangeResponse(entry.data, range, entry.mime);
      if (rr) return rr;
    }
    return okResponse(entry.data, entry.mime);
  } catch (err) {
    console.error('[sw] ZIM lookup failed for', viewerPath, err);
    return new Response('ZIM error: ' + (err && err.message || err), {
      status: 500,
      headers: { 'Content-Type': 'text/plain' }
    });
  }
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

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
    if (VIEWER_SHELL_NAMES.has(firstSegment) && !restNoSlash.includes('/')) {
      // Shell asset — NETWORK-FIRST. Stale cached HTML/JS was the
      // 2026-04-25 frustration: deploys were live on Firebase but
      // users saw old bundles for an indefinite window because the
      // SW served the cache. Network-first means: when online, you
      // ALWAYS see the current deploy. Cache only kicks in offline.
      event.respondWith((async () => {
        try {
          const net = await fetch(req, { cache: 'no-store' });
          if (net && net.ok) {
            const copy = net.clone();
            caches.open(SHELL_CACHE).then((c) => cacheClean(c, req, copy));
          }
          return net;
        } catch (e) {
          // Offline fallback only.
          const cached = await caches.match(req);
          return cached || notFound(rest);
        }
      })());
      return;
    }
    // Data path — serve from ZIM.
    event.respondWith(serveFromZim(rest, req));
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
      const net = await fetch(req, { cache: 'no-store' });
      if (net && net.ok) {
        const copy = net.clone();
        caches.open(SHELL_CACHE).then((c) => cacheClean(c, req, copy));
      }
      return net;
    } catch (e) {
      const cached = await caches.match(req);
      return cached || notFound(url.pathname);
    }
  })());
});
