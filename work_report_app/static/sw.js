// Work Report System — Service Worker
// Strategy: network-first for everything (this app is data-driven and
// changes constantly), with a minimal offline fallback page and
// long-lived caching only for static assets (icons, manifest).
// This avoids ever showing stale work reports / job data to the user.

const STATIC_CACHE = "wrs-static-v2";
const STATIC_ASSETS = [
  "/static/manifest.json",
  "/static/icons/icon-192x192.png",
  "/static/icons/icon-512x512.png",
  "/static/offline.html",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== STATIC_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Only handle GET requests; let POST (form submits) go straight to network
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Static assets: cache-first (icons/manifest rarely change)
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req))
    );
    return;
  }

  // Everything else (pages, data): network-first, fall back to offline page
  event.respondWith(
    fetch(req)
      .then((res) => res)
      .catch(() => caches.match("/static/offline.html"))
  );
});
