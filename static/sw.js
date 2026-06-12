/* 対話のおけいこ Service Worker
 *
 * PWAのインストール(ホーム画面追加)を可能にするための最小構成。
 * コードの陳腐化を避けるため、HTML/JS/CSS/APIは常にネットワークを使い、
 * 大きくて変化しないアセット(VRMモデル・アイコン)だけをキャッシュする。
 */
const CACHE_NAME = "taiwa-lesson-v1";
const CACHEABLE = [/^\/models\//, /^\/icons\//];

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const cacheable =
    event.request.method === "GET" &&
    url.origin === location.origin &&
    CACHEABLE.some((re) => re.test(url.pathname));

  if (!cacheable) {
    return; // 通常のネットワーク処理に任せる(常に最新)
  }
  // モデル等はキャッシュ優先(初回のみダウンロード)
  event.respondWith(
    caches.open(CACHE_NAME).then(async (cache) => {
      const hit = await cache.match(event.request);
      if (hit) return hit;
      const res = await fetch(event.request);
      if (res.ok) cache.put(event.request, res.clone());
      return res;
    })
  );
});
