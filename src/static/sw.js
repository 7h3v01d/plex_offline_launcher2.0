// sw.js — Plex Offline Launcher service worker
// Caches the app shell so the UI loads instantly even before Plex responds.
// Media streams are never cached (they come from Plex directly).

const CACHE_NAME = 'plex-launcher-shell-v1';

// App shell assets — everything needed to render the UI frame.
// Plex API calls (library data, images, streams) always go to the network.
const SHELL_URLS = [
    '/static/js/hls.min.js',
    '/static/manifest.json',
];

// ── Install: pre-cache shell assets ─────────────────────────────────────────
self.addEventListener('install', function (e) {
    e.waitUntil(
        caches.open(CACHE_NAME).then(function (cache) {
            return cache.addAll(SHELL_URLS);
        }).then(function () {
            return self.skipWaiting();
        })
    );
});

// ── Activate: remove old caches ───────────────────────────────────────────────
self.addEventListener('activate', function (e) {
    e.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(
                keys.filter(function (k) { return k !== CACHE_NAME; })
                    .map(function (k) { return caches.delete(k); })
            );
        }).then(function () {
            return self.clients.claim();
        })
    );
});

// ── Fetch: shell-first for static assets, network-first for everything else ──
self.addEventListener('fetch', function (e) {
    var url = new URL(e.request.url);

    // Never intercept Plex API calls, stream URLs, or image proxies
    var isPlexAPI = url.pathname.startsWith('/api/') ||
                    url.pathname.startsWith('/proxy/') ||
                    url.pathname.includes('/transcode/') ||
                    url.pathname.includes('/library/') ||
                    url.pathname.includes('/:/');

    if (isPlexAPI) {
        // Pure network — don't interfere
        return;
    }

    // Static assets: cache-first (hls.js, manifest)
    if (url.pathname.startsWith('/static/')) {
        e.respondWith(
            caches.match(e.request).then(function (cached) {
                return cached || fetch(e.request).then(function (response) {
                    // Cache new static assets on first fetch
                    if (response.ok) {
                        var clone = response.clone();
                        caches.open(CACHE_NAME).then(function (cache) {
                            cache.put(e.request, clone);
                        });
                    }
                    return response;
                });
            })
        );
        return;
    }

    // HTML pages: network-first with cache fallback.
    // This means you always get fresh Plex data when online,
    // but get a cached shell when offline (Plex will show its own error
    // for the data parts, which is correct behaviour).
    e.respondWith(
        fetch(e.request).then(function (response) {
            if (response.ok && e.request.method === 'GET') {
                var clone = response.clone();
                caches.open(CACHE_NAME).then(function (cache) {
                    cache.put(e.request, clone);
                });
            }
            return response;
        }).catch(function () {
            return caches.match(e.request);
        })
    );
});
