const CACHE = 'petstore-v1';

const STATIC = [
  '/',
  '/static/css/style.css',
  '/static/js/main.js',
  '/static/manifest.json',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png',
];

// Install — pre-cache static shell
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

// Activate — delete old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch strategy:
//   - API / checkout / admin  → network-only (no cache)
//   - static assets (.css/.js/.png/.webp/.woff) → cache-first
//   - HTML pages → network-first, fallback to cache
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // skip non-GET and non-same-origin
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;

  // skip admin, api, webhooks
  if (/^\/(admin|api\/|webhooks)/.test(url.pathname)) return;

  // static assets → cache-first
  if (/\.(css|js|png|jpg|jpeg|webp|svg|ico|woff2?|ttf)$/.test(url.pathname)) {
    e.respondWith(
      caches.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then(c => c.put(e.request, clone));
          }
          return res;
        });
      })
    );
    return;
  }

  // HTML pages → network-first
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then(c => c || caches.match('/')))
  );
});

// ── Push Notifications ──
self.addEventListener('push', e => {
  if (!e.data) return;
  const d = e.data.json();
  e.waitUntil(
    self.registration.showNotification(d.title || 'Bella Pet 🐾', {
      body:    d.body  || '',
      icon:    '/static/img/icon-192.png',
      badge:   '/static/img/icon-192.png',
      vibrate: [200, 100, 200],
      tag:     d.tag  || 'bella-push',
      renotify: true,
      data:    { url: d.url || '/' },
      actions: [
        { action: 'open',    title: 'تصفح المنتج' },
        { action: 'dismiss', title: 'إغلاق' },
      ],
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  if (e.action === 'dismiss') return;
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(wins => {
      for (const w of wins) {
        if (w.url.includes(url) && 'focus' in w) return w.focus();
      }
      return clients.openWindow(url);
    })
  );
});
