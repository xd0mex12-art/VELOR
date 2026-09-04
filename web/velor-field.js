// ===== VELOR — живое поле =====
//
// Что это. Фон кабинета, который показывает СОСТОЯНИЕ системы. Не обои, не
// заставка, не «AI-анимация»: у поля ровно четыре состояния, и каждое из них
// поднимает не таймер, а настоящий вывод Директора — тот же, который словами
// написан в брифинге на главной.
//
//   waiting     — Директор не собрался: считать нечего. Поле разрежено.
//   normal      — бизнес идёт, срочного нет. Поле спокойное и глубокое.
//   risk        — у Директора есть риск уровня urgent. Поле стягивается.
//   opportunity — срочного нет, но есть возможность. Поле тянется к одной области.
//
// И три события — разовый ответ на то, что действительно произошло:
//
//   pulse('client')   — в базе появился новый клиент. Единственное цветное:
//                       бирюза значит «получилось», и новый человек — ровно это.
//   pulse('data')     — число пересчиталось и стало другим.
//   pulse('decision') — появился вывод, которого в прошлом опросе не было.
//   pulse('focus')    — человек раскрыл цепочку доказательства.
//
// Вся форма и всё движение живут в CSS (см. «ЖИВОЕ ПОЛЕ» в velor.css). Здесь
// только разметка и переключение атрибутов: ни одного кадра на JS, ни одного
// requestAnimationFrame, ни канваса, ни библиотеки.
//
// Состояние переживает переход между страницами через sessionStorage. Смысл не
// в красоте: если у бизнеса риск, он не перестаёт быть риском оттого, что
// владелец ушёл в «Настройки». Считает состояние только главная — там, где
// есть настоящие данные; остальные страницы показывают последнее посчитанное
// и ничего не выдумывают сами.
(function () {
  var KEY = 'velor_field_state';
  var STATES = { waiting: 1, normal: 1, risk: 1, opportunity: 1 };
  var PULSES = { client: 1, data: 1, decision: 1, focus: 1 };
  var el = null, pulseT = 0;

  function build() {
    if (el || document.querySelector('.v-field')) return el;
    el = document.createElement('div');
    el.className = 'v-field';
    el.setAttribute('aria-hidden', 'true');
    // Начальное состояние — последнее посчитанное главной в этой сессии.
    // Нет такого — «norm»: поле не имеет права намекать на риск, которого
    // никто не считал.
    var saved = null;
    try { saved = sessionStorage.getItem(KEY); } catch (e) { /* приватный режим */ }
    el.dataset.field = STATES[saved] ? saved : 'normal';
    // Четыре вложенных элемента на ленту — снос, поворот, толщина, состояние.
    // Каждое преобразование идёт в своём ритме; собрать их в один элемент
    // нельзя — они бы слились в одну анимацию и снова читались как цикл.
    // Три вложенных элемента на долю — течение, размер, волна прозрачности.
    // Каждое движение идёт в своём ритме; собрать их в один элемент нельзя,
    // они слились бы в одну анимацию и снова читались как цикл.
    var lobe = function (n) {
      return '<span class="vf-l vf-l' + n + '"><span class="vf-t"><span class="vf-o">' +
             '<i></i><u class="risk"></u><u class="opp"></u>' +
             '</span></span></span>';
    };
    el.innerHTML = lobe(1) + lobe(2) + lobe(3) + lobe(4) + lobe(5) +
      '<span class="vf-pulse"><i></i><u class="ok"></u></span>';
    document.body.insertBefore(el, document.body.firstChild);
    return el;
  }

  function state(name) {
    if (!STATES[name]) return;
    var f = build();
    if (!f || f.dataset.field === name) return;
    f.dataset.field = name;
    try { sessionStorage.setItem(KEY, name); } catch (e) { /* приватный режим */ }
  }

  // Событие не накладывается само на себя: пока идёт одно, второе его не
  // перебивает. Иначе частый опрос превратил бы поле в мигалку.
  function pulse(kind) {
    kind = PULSES[kind] ? kind : 'data';
    var f = build();
    if (!f || f.dataset.pulse) return;
    f.dataset.pulse = kind;
    clearTimeout(pulseT);
    pulseT = setTimeout(function () { delete f.dataset.pulse; }, 3000);
  }

  window.VELOR_FIELD = { state: state, pulse: pulse, mount: build };

  // Раскрытая цепочка доказательства — тоже настоящее событие: VELOR показал
  // ход рассуждения. Событие toggle не всплывает, поэтому слушаем на фазе
  // захвата. Схлопывание ничего не поднимает: закрыть — не значит узнать.
  document.addEventListener('toggle', function (e) {
    var t = e.target;
    if (t && t.tagName === 'DETAILS' && t.classList.contains('ev') && t.open) pulse('focus');
  }, true);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();
