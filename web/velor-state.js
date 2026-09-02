// ===== VELOR CORE — присутствие сотрудника: знак и слово =====
//
// Одно состояние, два носителя. Знак (`[data-velor-core]`) — абстрактная форма,
// которая живёт непрерывно. Слово (`[data-velor-state]`) стоит рядом и говорит
// то же самое буквами.
//
// Правило знака: движение — норма, остановка — сигнал. Пока VELOR работает,
// знак дышит всегда; замирает он только там, где нужно решение владельца, и
// на сбое. Поэтому «замер» читается как событие.
//
// Почему два: по форме и цвету нельзя отличить «думает» от «сбоя связи» —
// цвет в этой системе никогда не единственный носитель смысла. И наоборот:
// одно слово в углу экрана не даёт ощущения, что рядом кто-то есть.
//
// Прежнее ядро было трёхмерной сферой на three.js: 656 КБ ради свечения,
// которое ничего не сообщало. Здесь — инлайновый SVG и CSS, ноль запросов,
// а состояний стало больше.
//
// Публичный API не менялся:
//   window.VELOR_CORE.setState('thinking' | 'processing' | 'error' | …)
//   window.VELOR_CORE.insight()
// Старые имена состояний (idle / analyzing / generating / insight) работают
// по-прежнему — они переведены в новые, а не отброшены.

(function () {
  // Знак. Абстрактная форма: пять полос разной длины и смещения. Ничего не
  // изображает — ни ИИ, ни документа, ни логотипа. Живёт непрерывно: каждая
  // полоса дрейфует в своём периоде, периоды не кратны друг другу, поэтому
  // силуэт не повторяется минутами. vc-gap перерезает строй на сбое.
  //
  // До этого было две попытки, и обе оказались не тем: рамка с линиями
  // читалась как иконка из панели инструментов, треугольник логотипа — как
  // логотип. Присутствие не изображается предметом.
  var MARK =
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
      '<rect class="vc-b vc-b1" x="3.5" y="3"    width="13"   height="1.9" rx=".95"/>' +
      '<rect class="vc-b vc-b2" x="6.5" y="7.6"  width="9.5"  height="1.9" rx=".95"/>' +
      '<rect class="vc-b vc-b3" x="2.5" y="11.2" width="17"   height="1.9" rx=".95"/>' +
      '<rect class="vc-b vc-b4" x="8"   y="15"   width="7"    height="1.9" rx=".95"/>' +
      '<rect class="vc-b vc-b5" x="4.5" y="19.4" width="12"   height="1.9" rx=".95"/>' +
      '<rect class="vc-gap"     x="9.5" y="11.2" width="3.2"  height="1.9"/>' +
    '</svg>';

  // Состояние → [слово, тон слова]. Форму знака задаёт CSS по data-core.
  var S = {
    connected:  ['на связи',       'calm'],
    thinking:   ['думает',         'work'],
    processing: ['разбирает',      'work'],
    writing:    ['пишет ответ',    'work'],
    found:      ['нашёл',          'warn'],
    warning:    ['нужна проверка', 'warn'],
    error:      ['сбой связи',     'bad']
  };

  // Имена, которыми ядро звали раньше. Ни один существующий вызов не трогаем.
  var ALIAS = {
    idle:'connected', calm:'connected', ok:'connected',
    analyzing:'processing', generating:'writing',
    insight:'found', pulse:'found', review:'warning', warn:'warning'
  };

  var state = 'connected';
  var backTimer = 0;

  function norm(name) {
    var n = ALIAS[name] || name;
    return S[n] ? n : 'connected';
  }

  function paint(key) {
    var w = S[key];

    var marks = document.querySelectorAll('[data-velor-core]');
    for (var i = 0; i < marks.length; i++) {
      // Разметку вставляем один раз: перерисовка знака на каждом обновлении
      // сбрасывала бы дыхание и импульс на первый кадр.
      if (!marks[i].firstChild) {
        marks[i].className = marks[i].className
          ? marks[i].className + ' v-core' : 'v-core';
        marks[i].innerHTML = MARK;
      }
      marks[i].setAttribute('data-core', key);
    }

    var words = document.querySelectorAll('[data-velor-state]');
    for (var j = 0; j < words.length; j++) {
      words[j].className = 'v-pulse ' + w[1];
      // Точка — оформление, слово — смысл. Скринридер читает только слово.
      words[j].innerHTML = '<i aria-hidden="true"></i><span>' + w[0] + '</span>';
    }
  }

  function setState(name) {
    if (name === 'insight' || name === 'pulse' || name === 'found') return insight();
    state = norm(name);
    if (!backTimer) paint(state);
  }

  // Находка — одиночный импульс поверх текущего состояния. Не мигание: одно
  // движение и одно слово на две с половиной секунды, потом возврат туда, где
  // сотрудник был. Состояние не теряется.
  function insight() {
    paint('found');
    clearTimeout(backTimer);
    backTimer = setTimeout(function () {
      backTimer = 0;
      paint(state);
    }, 2500);
  }

  window.VELOR_CORE = {
    setState: setState,
    state: setState,
    insight: insight,
    pulse: insight,
    // для блоков, которые появляются позже (строка отчёта двери, модалки)
    mount: function () { paint(backTimer ? 'found' : state); }
  };
  window.ARINA_CORE_PULSE = insight;   // совместимость со старыми вызовами

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { paint(state); });
  } else {
    paint(state);
  }
})();
