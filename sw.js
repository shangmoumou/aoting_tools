// Service Worker：让三个工具装上后【断网也能用】，并支持点一下就更新。
//
// 设计要点
// ─────────────────────────────────────────────────────────
// 1) 只对同源资源生效。camweb 的画面走 http://127.0.0.1:8765，
//    那是实时的 MJPEG 流，绝不能被缓存 —— 所以 fetch 里遇到它直接放行。
//
// 2) version.json 永远走网络（network-only）。
//    它被缓存住的话就永远发现不了新版本了，那就白做了。
//
// 3) 工具页面用 stale-while-revalidate：
//    先给缓存（秒开），同时后台去拉新的，下次访问就是新版。
//    这样既离线可用，又不会停在旧版。
//
// 4) 收到 update 消息时强制重新拉全部资源，然后 skipWaiting。
//    不 skipWaiting 的话，新 SW 要等所有标签页关掉才生效 ——
//    用户点了"更新"却没反应，最容易被当成 bug。

// 缓存名带版本号：升级门户时把这里 +1，旧缓存会被清理。
// 名字不变的话，旧版页面会一直从缓存里被返回 —— 表现为
// "页面卡在加载中不动"，而服务端其实一切正常，极难排查。
const CACHE = 'tools-portal-v2';

// 预缓存清单从 version.json 动态取，而不是在这里写死。
// 写死的话，每加一个工具都要记得同步改这里 —— 迟早会忘，
// 然后新工具在离线时打不开，还很难查。
async function precacheList(){
  const base = ['./', './index.html', './sw.js', './version.json'];
  try {
    const r = await fetch('./version.json', {cache:'no-store'});
    const j = await r.json();
    return base.concat(j.precache || []);
  } catch(e){
    return base;              // 离线首次安装：至少把壳子装上
  }
}

self.addEventListener('install', ev => {
  ev.waitUntil(
    precacheList()
      .then(list => caches.open(CACHE).then(c => c.addAll(list)))
      .catch(() => {})        // 个别资源失败不能让整个安装挂掉
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', ev => {
  ev.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        // 删掉所有 tools-portal-* 旧缓存，不只是"名字不等于 CACHE"的。
        // 只过滤不等的话，历史遗留的旧版本会一直留在用户机器上。
        keys.filter(k => k.startsWith('tools-portal-') && k !== CACHE)
            .map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// 本地 Python 服务的地址。这些请求不能碰：
//   · MJPEG 是长连接流，缓存它毫无意义还会把画面卡住
//   · 跨域请求被 SW 接管后容易出各种诡异问题
function isLocalService(url) {
  return url.hostname === '127.0.0.1'
      || url.hostname === 'localhost'
      || url.hostname === '[::1]';
}

async function refreshAll() {
  const c = await caches.open(CACHE);
  const list = await precacheList();
  // cache: 'reload' 绕过浏览器 HTTP 缓存，确保拿到服务器上的真身
  await Promise.all(list.map(p => c.add(new Request(p, { cache: 'reload' }))));
}

self.addEventListener('message', ev => {
  if (!ev.data) return;
  if (ev.data.action === 'update') {
    ev.waitUntil(
      refreshAll()
        .then(() => self.skipWaiting())
        .catch(() => self.skipWaiting())
    );
  }
});

self.addEventListener('fetch', ev => {
  const req = ev.request;
  if (req.method !== 'GET') return;

  let url;
  try { url = new URL(req.url); } catch (e) { return; }

  // 本地服务 / 其他域：完全不介入
  if (isLocalService(url) || url.origin !== self.location.origin) return;

  // 版本清单：永远走网络。缓存住就永远发现不了更新了。
  if (url.pathname.endsWith('/version.json')) {
    ev.respondWith(
      fetch(req, { cache: 'no-store' }).catch(() => caches.match(req))
    );
    return;
  }

  // 其余：先给缓存，后台更新
  ev.respondWith(
    caches.match(req).then(cached => {
      const net = fetch(req).then(res => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
        }
        return res;
      }).catch(() => cached);
      return cached || net;
    })
  );
});
