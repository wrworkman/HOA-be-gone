// SBR Violation Tracker — Service Worker
// Caches the app shell so it loads offline, even without network.

const CACHE = 'sbr-violations-v1';
const SHELL = [
  './',
  './index.html',
  './manifest.json',
  'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js',
];

// Install: cache the app shell
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(SHELL))
  );
  self.skipWaiting();
});

// Activate: remove old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: cache-first for shell, network-first for API calls
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Always go to network for Supabase (data/storage) and Nominatim (geocoding)
  if (url.hostname.includes('supabase.co') || url.hostname.includes('nominatim.openstreetmap.org')) {
    event.respondWith(
      fetch(event.request).catch(() => new Response('{"error":"offline"}', {
        headers: { 'Content-Type': 'application/json' }
      }))
    );
    return;
  }

  // Cache-first for everything else (app shell)
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  );
});
