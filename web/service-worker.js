// service-worker.js
// Minimal service worker -- required by Chrome/Android for a PWA to be
// "installable" (Add to Home Screen) and for share_target registration to
// take effect. Deliberately simple: passes all requests straight through to
// the network (no offline caching yet -- this app is read/write against a
// live local backend, so an offline cache would show stale data. Revisit if
// an offline-first mode is wanted later).

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request));
});
