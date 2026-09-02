// ===== VELOR — состояние сотрудника =====
//
// Что было. На «Обзоре» и «Спросить» состояние VELOR показывала трёхмерная
// светящаяся сфера из нитей и узлов: 480 строк на three.js плюс 654 КБ самой
// библиотеки, чтобы сообщить одно из пяти слов — «на связи», «думает», «пишет»,
// «разбирает», «сбой». На «Спросить» она занимала больше половины экрана и
// стояла ровно там, где человек ждёт ответ.
//
// Почему убрано. Светящийся «мозг ИИ» — приём, который выдаёт интерфейс,
// собранный вокруг технологии, а не вокруг дела. VELOR продаёт себя как
// сотрудника: сотрудник не светится, он отвечает. Сигнал сохранён целиком,
// исчезла только его трёхмерная форма.
//
// Что осталось. Тот же публичный API — window.VELOR_CORE.setState(...) и
// .insight() — поэтому вызовы на страницах не переписывались. Индикатор
// рисуется точкой и словом: цвет никогда не единственный носитель смысла.

(function () {
  var WORDS = {
    idle:       ['на связи',    'calm'],
    thinking:   ['думает',      'work'],
    generating: ['пишет ответ', 'work'],
    analyzing:  ['разбирает',   'work'],
    error:      ['сбой связи',  'bad'],
    insight:    ['нашёл',       'warn']
  };

  var state = 'idle';
  var insightTimer = 0;

  function nodes() {
    return document.querySelectorAll('[data-velor-state]');
  }

  function paint(key) {
    var w = WORDS[key] || WORDS.idle;
    var list = nodes();
    for (var i = 0; i < list.length; i++) {
      var el = list[i];
      el.className = 'v-pulse ' + w[1];
      // Точка — оформление, слово — смысл. Скринридер читает только слово.
      el.innerHTML = '<i aria-hidden="true"></i><span>' + w[0] + '</span>';
    }
  }

  function setState(name) {
    if (name === 'insight' || name === 'pulse') return insight();
    state = WORDS[name] ? name : 'idle';
    if (!insightTimer) paint(state);
  }

  // Находка — короткая янтарная отметка поверх текущего состояния. Не мигание:
  // одно изменение слова на две с половиной секунды, потом обратно.
  function insight() {
    paint('insight');
    clearTimeout(insightTimer);
    insightTimer = setTimeout(function () {
      insightTimer = 0;
      paint(state);
    }, 2500);
  }

  window.VELOR_CORE = {
    setState: setState,
    state: setState,
    insight: insight,
    pulse: insight
  };
  window.ARINA_CORE_PULSE = insight;   // совместимость со старыми вызовами

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { paint(state); });
  } else {
    paint(state);
  }
})();
