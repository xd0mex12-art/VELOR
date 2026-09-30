/* Кто привёл клиента.
 *
 * Партнёр даёт ссылку вида velor-ucnt.onrender.com/?ref=dima. Человек по ней
 * приходит, смотрит, уходит думать — и регистрируется через несколько дней.
 * Чтобы доля не потерялась, код запоминается в браузере на 90 дней и сам
 * подставляется в форму регистрации.
 *
 * Запоминаем ТОЛЬКО код партнёра и дату. Ни адреса, ни поведения, ни чего-либо
 * о самом человеке здесь нет: это не слежка, а подпись «меня прислал такой-то».
 *
 * Вписанный руками код всегда побеждает запомненный: человек назвал его
 * сознательно, а в браузере мог остаться чужой от прошлого раза.
 */
(function () {
  var KEY = 'velor_ref';
  var DAYS = 90;

  function save(code) {
    try {
      localStorage.setItem(KEY, JSON.stringify({
        code: String(code).trim().toLowerCase(),
        at: Date.now()
      }));
    } catch (e) { /* приватный режим — просто забудем */ }
  }

  // Код из адреса: ?ref=dima. Записывается один раз за переход.
  try {
    var fromUrl = new URLSearchParams(location.search).get('ref');
    if (fromUrl) save(fromUrl);
  } catch (e) {}

  // Отдать запомненный код, если он ещё не протух.
  window.velorRefCode = function () {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return '';
      var v = JSON.parse(raw);
      if (!v || !v.code) return '';
      if (Date.now() - (v.at || 0) > DAYS * 86400000) {
        localStorage.removeItem(KEY);
        return '';
      }
      return v.code;
    } catch (e) { return ''; }
  };
})();
