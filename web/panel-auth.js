// ===== VELOR AI — охранник панели бизнеса (JWT) =====
// Подключается ПЕРВЫМ на dashboard/clients/settings.
// 1) нет access-токена — отправляем на biz-login.html
// 2) ко всем запросам /api/ добавляем access-токен (X-Auth)
// 3) если access истёк (401) — молча обновляем его по refresh-токену и
//    повторяем запрос; если refresh недействителен — на вход.
(function () {
  var ACCESS = 'coreon_biz_token';     // access-токен (короткий)
  var REFRESH = 'coreon_biz_refresh';  // refresh-токен (длинный)

  if (!localStorage.getItem(ACCESS)) {
    location.replace('biz-login.html');
    return;
  }

  var origFetch = window.fetch.bind(window);

  function isApi(url) {
    var u = String(url);
    return u.indexOf('/api/') === 0 || u.indexOf('/api/') === location.origin.length;
  }

  function clearAndLogin() {
    localStorage.removeItem(ACCESS);
    localStorage.removeItem(REFRESH);
    localStorage.removeItem('coreon_biz_name');
    location.replace('biz-login.html');
  }

  // Обновление access по refresh. Общий промис, чтобы параллельные 401
  // не дёргали /api/refresh много раз.
  var refreshing = null;
  function refreshAccess() {
    if (refreshing) return refreshing;
    var rt = localStorage.getItem(REFRESH);
    if (!rt) return Promise.resolve(false);
    refreshing = origFetch('/api/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt })
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      refreshing = null;
      if (!d || !d.token) return false;
      localStorage.setItem(ACCESS, d.token);
      return true;
    }).catch(function () { refreshing = null; return false; });
    return refreshing;
  }

  function withAuth(opts) {
    opts = opts || {};
    var o = {};
    for (var k in opts) o[k] = opts[k];
    o.headers = Object.assign({}, opts.headers, { 'X-Auth': localStorage.getItem(ACCESS) });
    return o;
  }

  // ===== общая полоса «сервер не отвечает» =====
  // Показывается, когда запрос к /api/ не дошёл или сервер ответил 5xx.
  // Гаснет сама, как только следующий запрос прошёл.
  var banner = null;

  function pageHasOwnFail() {
    // Страница сама рассказывает про сбой, если у неё есть место под это
    // (#failZone) или полоса уже нарисована. Проверять только нарисованную
    // полосу мало: общий баннер успевает выскочить раньше, чем страница
    // обработает свою же ошибку, и человек читает про одно и то же дважды.
    return !!(document.getElementById('failZone') || document.querySelector('.v-fail'));
  }

  function hideOffline() {
    if (banner) { banner.remove(); banner = null; }
  }

  function showOffline() {
    if (banner || !document.body || pageHasOwnFail()) return;
    banner = document.createElement('div');
    banner.id = 'velor-offline';
    banner.setAttribute('role', 'alert');
    banner.innerHTML =
      '<span class="vo-t">Сервер не отвечает.</span>' +
      '<span class="vo-d">Данные на этой странице могут быть неполными.</span>' +
      '<button type="button" class="vo-go">Обновить</button>' +
      '<button type="button" class="vo-x" aria-label="Скрыть сообщение">×</button>';
    banner.querySelector('.vo-go').addEventListener('click', function () { location.reload(); });
    banner.querySelector('.vo-x').addEventListener('click', hideOffline);
    document.body.appendChild(banner);
  }

  var css = document.createElement('style');
  css.textContent = [
    '#velor-offline{ position:fixed; left:50%; transform:translateX(-50%); top:112px; z-index:70;',
    '  display:flex; align-items:center; gap:12px; flex-wrap:wrap; max-width:min(680px,calc(100vw - 32px));',
    '  padding:14px 18px; border-radius:20px; border:1px solid rgba(255,107,107,.35);',
    '  background:rgba(28,10,10,.94); backdrop-filter:blur(12px); color:#fff;',
    '  font-family:inherit; font-size:15px; box-shadow:0 18px 50px -22px rgba(0,0,0,.7); }',
    '#velor-offline .vo-t{ font-weight:500; }',
    '#velor-offline .vo-d{ color:#bdbdbd; font-weight:200; font-size:14px; }',
    '#velor-offline button{ font-family:inherit; cursor:pointer; }',
    '#velor-offline .vo-go{ min-height:44px; padding:11px 20px; border:none; border-radius:999px;',
    '  background:#8052ff; color:#fff; font-weight:600; font-size:14px; }',
    '#velor-offline .vo-x{ width:44px; height:44px; border:none; border-radius:12px; background:none;',
    '  color:#9a9a9a; font-size:20px; line-height:1; }',
    '#velor-offline .vo-x:hover{ color:#fff; background:rgba(255,255,255,.08); }',
    '@media(max-width:560px){ #velor-offline{ top:auto; bottom:16px; } }',
  ].join('\n');
  (document.head || document.documentElement).appendChild(css);

  window.fetch = function (url, opts) {
    if (!isApi(url) || String(url).indexOf('/api/refresh') >= 0) {
      return origFetch(url, opts);
    }
    return origFetch(url, withAuth(opts)).then(function (r) {
      if (r.status === 401) {
        // access истёк — пробуем обновить и повторить запрос ровно один раз
        return refreshAccess().then(function (ok) {
          if (!ok) { clearAndLogin(); return r; }
          return origFetch(url, withAuth(opts));
        });
      }
      // 4xx — это ответ по делу (нет прав, не найдено, лимит): страница
      // разберётся сама. Полосу поднимаем только когда сервер не в порядке.
      if (r.status >= 500) showOffline(); else hideOffline();
      return r;
    }, function (e) {
      showOffline();
      throw e;                 // страница по-прежнему видит свою ошибку
    });
  };

  // имя бизнеса и кнопка выхода в шапке (если есть места)
  window.addEventListener('DOMContentLoaded', function () {
    var name = localStorage.getItem('coreon_biz_name') || 'бизнес';
    var slot = document.getElementById('bizName');
    if (slot) slot.textContent = name;
    var out = document.getElementById('bizLogout');
    if (out) out.addEventListener('click', function () {
      var rt = localStorage.getItem(REFRESH);
      // сначала отзываем refresh на сервере, затем чистим и уходим на вход
      origFetch('/api/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: rt || '' })
      }).catch(function () {}).then(clearAndLogin);
    });
  });
})();
