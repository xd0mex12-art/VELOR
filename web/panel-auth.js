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

  window.fetch = function (url, opts) {
    if (!isApi(url) || String(url).indexOf('/api/refresh') >= 0) {
      return origFetch(url, opts);
    }
    return origFetch(url, withAuth(opts)).then(function (r) {
      if (r.status !== 401) return r;
      // access истёк — пробуем обновить и повторить запрос ровно один раз
      return refreshAccess().then(function (ok) {
        if (!ok) { clearAndLogin(); return r; }
        return origFetch(url, withAuth(opts));
      });
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
