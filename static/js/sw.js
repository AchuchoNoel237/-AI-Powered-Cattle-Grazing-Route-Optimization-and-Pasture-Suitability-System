// static/js/sw.js
// ============================================================
// Service Worker — caches the app shell so it loads offline.
// Does NOT cache API responses (route data changes per request);
// offline route display relies on localStorage (see app.js),
// which this service worker enables access to by keeping the
// page itself loadable without a network connection.
// ============================================================

const CACHE_NAME = "transhumance-app-shell-v1";

const APP_SHELL_FILES = [
    "/",
    "/static/css/style.css",
    "/static/js/app.js",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js",
];

// --- Install: cache the app shell ---
self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(APP_SHELL_FILES);
        })
    );
    self.skipWaiting();
});

// --- Activate: clean up old cache versions ---
self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

// --- Fetch: serve from cache when offline, network first when online ---
self.addEventListener("fetch", (event) => {
    const url = new URL(event.request.url);

    // Never cache API calls — routes must always be fresh when online
    if (url.pathname.startsWith("/api/")) {
        return; // let it hit the network normally; if offline, it'll fail
                // and app.js's catch block handles showing the cached route
    }

    event.respondWith(
        fetch(event.request)
            .then((networkResponse) => {
                // Update the cache with the latest version when online
                return caches.open(CACHE_NAME).then((cache) => {
                    cache.put(event.request, networkResponse.clone());
                    return networkResponse;
                });
            })
            .catch(() => {
                // Offline — serve from cache instead
                return caches.match(event.request);
            })
    );
});