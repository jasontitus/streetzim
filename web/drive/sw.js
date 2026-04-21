/* StreetZim Drive (preview) service worker
 * Strategy:
 *   - Precache the app shell (HTML, manifest, icons) cache-first.
 *   - Runtime cache MapLibre CDN assets stale-while-revalidate.
 *   - OSM tiles: network-first with cache fallback, capped entries.
 * Bump CACHE_VER on each deploy to invalidate old shells.
 */
const CACHE_VER   = "drive-v1";
const SHELL_CACHE = "shell-" + CACHE_VER;
const CDN_CACHE   = "cdn-" + CACHE_VER;
const TILE_CACHE  = "tiles-" + CACHE_VER;
const TILE_MAX    = 400;

const SHELL = [
  "./",
  "index.html",
  "manifest.webmanifest",
  "icon.svg",
  "icon-192.png",
  "icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL_CACHE)
      .then((c) => c.addAll(SHELL).catch(() => { /* best effort */ }))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter((k) => !k.endsWith(CACHE_VER))
      .map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

async function trimCache(name, max) {
  const cache = await caches.open(name);
  const keys = await cache.keys();
  if (keys.length <= max) return;
  for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Shell
  if (url.origin === location.origin && url.pathname.startsWith("/drive/")) {
    e.respondWith((async () => {
      const cached = await caches.match(req);
      if (cached) return cached;
      try {
        const resp = await fetch(req);
        const cache = await caches.open(SHELL_CACHE);
        cache.put(req, resp.clone()).catch(() => {});
        return resp;
      } catch {
        if (req.mode === "navigate") return caches.match("/drive/index.html");
        throw new Error("offline");
      }
    })());
    return;
  }

  // MapLibre CDN
  if (url.host === "unpkg.com") {
    e.respondWith((async () => {
      const cache = await caches.open(CDN_CACHE);
      const cached = await cache.match(req);
      const networked = fetch(req).then((resp) => {
        if (resp && resp.ok) cache.put(req, resp.clone()).catch(() => {});
        return resp;
      }).catch(() => cached);
      return cached || networked;
    })());
    return;
  }

  // OSM tiles
  if (url.host.endsWith("tile.openstreetmap.org")) {
    e.respondWith((async () => {
      const cache = await caches.open(TILE_CACHE);
      try {
        const resp = await fetch(req);
        if (resp && resp.ok) {
          cache.put(req, resp.clone()).catch(() => {});
          trimCache(TILE_CACHE, TILE_MAX);
        }
        return resp;
      } catch {
        const cached = await cache.match(req);
        if (cached) return cached;
        throw new Error("offline");
      }
    })());
    return;
  }
});
