// 離線快取（PWA）：網路優先、失敗時用上次的快取，所以有網路時永遠拿到最新版，沒網路時仍能看上一次的資料。
// 只處理本站 GET；更新 Worker 等外部網址一律不經過快取。
const CACHE = 'cntw-v4';
const SHELL = ['./', 'index.html', 'app.js', 'shared.js', 'shared.css', 'theme.js', 'config.js', 'redirect.js', 'manifest.webmanifest',
  'm/', 'm/index.html', 'm/m.js', 'icons/icon-192.png', 'icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  // 資料檔帶 ?t= 防快取參數：以不含參數的網址存，離線時仍找得到
  const key = url.pathname.includes('/data/') ? url.origin + url.pathname : req;
  e.respondWith(
    fetch(req).then(res => {
      if (res.ok && res.type === 'basic') { const copy = res.clone(); caches.open(CACHE).then(c => c.put(key, copy)); }
      return res;
    }).catch(() => caches.match(key, { ignoreSearch: true }).then(r => r || caches.match(req, { ignoreSearch: true }))
      .then(r => r || new Response('離線中，且沒有這個檔案的快取', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } })))
  );
});
