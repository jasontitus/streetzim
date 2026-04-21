// StreetZim Drive — minimal service worker for the PWA shell.
//
// Phase 2a goal: make "Add to Home Screen" work and keep the app shell
// available when cell coverage is spotty. Map tiles still need network;
// Phase 2b will move to a local-ZIM reader served via maplibregl.addProtocol.

const SHELL_CACHE = 'streetzim-drive-shell-v1';
const SHELL_URLS = [
  './',
  './index.html',
  './manifest.webmanifest',
  'https://unpkg.com/maplibre-gl@5.23.0/dist/maplibre-gl.css',
  'https://unpkg.com/maplibre-gl@5.23.0/dist/maplibre-gl.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      // Don't fail install if one asset refuses to cache (CDN CORS quirks etc.)
      Promise.all(SHELL_URLS.map((url) =>
        cache.add(url).catch((err) => console.warn('[sw] skip', url, err))
      ))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== SHELL_CACHE).map((n) => caches.delete(n)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Tiles: network-first with cache fallback. Keeps the map fresh when we
  // have signal, shows last-seen tiles when we don't.
  if (/\.tile\.openstreetmap\.org$/i.test(url.hostname)) {
    event.respondWith(
      fetch(req).then((resp) => {
        // Only cache successful opaque/basic responses
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open('streetzim-drive-tiles-v1').then((c) => c.put(req, copy));
        }
        return resp;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // Shell: cache-first, refresh in background.
  event.respondWith(
    caches.match(req).then((cached) => {
      const fetchPromise = fetch(req).then((resp) => {
        if (resp && resp.ok && (url.origin === location.origin || url.hostname === 'unpkg.com')) {
          const copy = resp.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(req, copy));
        }
        return resp;
      }).catch(() => cached);
      return cached || fetchPromise;
    })
  );
});
