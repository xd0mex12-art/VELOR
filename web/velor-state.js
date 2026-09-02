// ===== VELOR — состояние сотрудника словом =====
//
// Состояние VELOR показывает живое ядро — трёхмерная сфера на «Обзоре» и
// «Спросить». Но свечение читает не каждый и не всегда: оттенок и скорость
// пульсации — это не слово, а по нему нельзя понять, сотрудник думает или
// у него сбой связи. Этот файл добавляет второй, читаемый носитель того же
// сигнала: точку и слово в шапке экрана.
//
// Ядро и слово — не два механизма, а один: window.VELOR_CORE остаётся
// единственной точкой управления, и вызов уходит в обе стороны. На страницах,
// где ядра нет, работает только слово — API от этого не меняется.
//
// Правило то же, что во всей системе: цвет никогда не единственный носитель
// смысла. У «сбоя связи» есть и коралловый цвет, и надпись.

(function () {
  // Ядро подключается раньше и уже заняло window.VELOR_CORE. Забираем его
  // себе и вызываем следом за собой, а не вместо.
  var core = window.VELOR_CORE || null;
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
    if (core) core.setState(name);
  }

  // Находка — короткая янтарная отметка поверх текущего состояния. Не мигание:
  // одно изменение слова на две с половиной секунды, потом обратно.
  function insight() {
    paint('insight');
    if (core) core.insight();
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
