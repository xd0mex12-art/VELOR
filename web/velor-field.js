// ===== VELOR — живое поле =====
//
// Что это. Пространство позади кабинета: три больших светящихся объёма,
// разнесённых по глубине. Не обои, не заставка, не «AI-анимация» — поле
// показывает СОСТОЯНИЕ системы, и каждое его изменение поднимает не таймер, а
// настоящий вывод Директора, тот же, который словами написан в брифинге.
//
// Уровней ровно два, и путать их нельзя.
//
//   БАЗОВОЕ СОСТОЯНИЕ — форма всего поля. Одно в каждый момент.
//     waiting     — Директор не собрался: считать нечего. Объёмы разошлись.
//     normal      — бизнес идёт, срочного нет. Широко и спокойно.
//     risk        — у Директора есть риск уровня urgent. Объёмы сошлись.
//     opportunity — срочного нет, но есть возможность. Поле повело в сторону.
//
//   СИГНАЛ БИЗНЕСА — местное поведение и цветная примесь ПОВЕРХ любой формы.
//   Сигналов может действовать несколько сразу.
//     leads — сколько обращений ждут ответа. Янтарное скопление внутри
//             среднего объёма; сила растёт с числом и гаснет, когда ответили.
//
// И события — разовый ответ на то, что действительно произошло:
//
//   pulse('client')   — в базе появился новый клиент (бирюза: «получилось»).
//   pulse('data')     — число пересчиталось и стало другим (наружу).
//   pulse('decision') — появился вывод, которого в прошлом опросе не было (внутрь).
//   pulse('focus')    — человек раскрыл цепочку доказательства.
//   pulse('lead')     — пришло обращение, на которое ещё не ответили (янтарь).
//
// Вся форма и всё движение живут в CSS (см. «ЖИВОЕ ПОЛЕ» в velor.css). Здесь
// только разметка и переключение атрибутов: ни одного кадра на JS, ни одного
// requestAnimationFrame, ни канваса, ни библиотеки.
//
// Состояние и сигналы переживают переход между страницами через
// sessionStorage. Смысл не в красоте: если у бизнеса риск, он не перестаёт
// быть риском оттого, что владелец ушёл в «Настройки». Считает их только
// главная — там, где есть настоящие данные; остальные страницы показывают
// последнее посчитанное и ничего не выдумывают сами.
(function () {
  var KEY = 'velor_field_state';
  var KEY_LEADS = 'velor_field_leads';
  var STATES = { waiting: 1, normal: 1, risk: 1, opportunity: 1 };
  var PULSES = { client: 1, data: 1, decision: 1, focus: 1, lead: 1 };
  var el = null, pulseT = 0;

  function remember(key, value) {
    try {
      if (value) sessionStorage.setItem(key, value);
      else sessionStorage.removeItem(key);
    } catch (e) { /* приватный режим */ }
  }

  function recall(key) {
    try { return sessionStorage.getItem(key); } catch (e) { return null; }
  }

  // Сколько обращений без ответа — в силу сигнала. Ступени, а не плавная
  // шкала: разницу между «двумя» и «тремя» глаз всё равно не читает, а вот
  // «единицы / несколько / много» читает сразу. Точное число стоит числом
  // на экране, поле его не заменяет.
  function leadTier(n) {
    n = n | 0;
    if (n <= 0) return '';
    if (n <= 2) return 'few';
    if (n <= 5) return 'many';
    return 'heavy';
  }

  function build() {
    if (el || document.querySelector('.v-live')) return el;
    el = document.createElement('div');
    el.className = 'v-live';
    el.setAttribute('aria-hidden', 'true');
    // Начальное состояние — последнее посчитанное главной в этой сессии.
    // Нет такого — «normal»: поле не имеет права намекать на риск, которого
    // никто не считал.
    var saved = recall(KEY);
    el.dataset.field = STATES[saved] ? saved : 'normal';
    var leads = recall(KEY_LEADS);
    if (leads) el.dataset.leads = leads;
    // Пять вложенных элементов на объём — состояние, снос, деформация,
    // поворот, дыхание. Каждое движение идёт в своём ритме; собрать их в один
    // элемент нельзя, они слились бы в одну анимацию и снова читались бы как
    // цикл. Внутри — три смещённых ядра: из них складывается несимметричный
    // силуэт, который и отличает объём от круглого пятна.
    // Примесь сигнала живёт не во всех трёх объёмах, а в одном: коралл риска —
    // в среднем, бирюза возможности — в ближнем. Разложить их по разным
    // планам глубины важнее, чем сэкономить строку: два цвета в одном объёме
    // смешались бы в грязь.
    var volume = function (n, tint) {
      return '<span class="vf-v vf-v' + n + '"><span class="vf-l"><span class="vf-t">' +
             '<span class="vf-r"><span class="vf-o">' +
             '<i class="a"></i><i class="b"></i><i class="c"></i>' + tint +
             '</span></span></span></span></span>';
    };
    // Порядок слоёв — порядок по глубине, и воздух в нём НЕ самый дальний.
    // Дымка физически находится между глазом и сценой, а не за ней: поэтому
    // она пишется ПОВЕРХ объёмов и поверх колодца. Так она заодно чинит
    // побочный эффект колодца — пока воздух лежал под ним, колодец вычитал
    // его из середины экрана, и в кадре появлялось тёмное пятно.
    el.innerHTML =
      volume(1, '') + volume(2, '<u class="vf-risk"></u>') +
      volume(3, '<u class="vf-opp"></u>') +
      '<span class="vf-lead"><i></i><u></u></span>' +
      '<span class="vf-well"></span>' +
      '<span class="vf-air"></span>' +
      '<span class="vf-pulse"><i></i><u class="ok"></u></span>';
    document.body.insertBefore(el, document.body.firstChild);
    return el;
  }

  // ── БАЗОВОЕ СОСТОЯНИЕ ──
  function state(name) {
    if (!STATES[name]) return;
    var f = build();
    if (!f || f.dataset.field === name) return;
    f.dataset.field = name;
    remember(KEY, name);
  }

  // ── СИГНАЛ БИЗНЕСА ──
  // Не состояние: ложится поверх любой формы и живёт своей жизнью. Сейчас
  // сигнал один — обращения без ответа; список именованный, чтобы следующий
  // добавили сюда, а не подмешали в состояние.
  function signal(name, value) {
    if (name !== 'leads') return;
    var f = build();
    if (!f) return;
    var tier = leadTier(value);
    if ((f.dataset.leads || '') === tier) return;
    if (tier) f.dataset.leads = tier;
    else delete f.dataset.leads;   // ответили последнему — янтарь гаснет сам
    remember(KEY_LEADS, tier);
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

  // ── ПРИШЛИ КЛИЕНТЫ ──
  // Кабинет опрашивает сервер раз в 30 секунд, и за это окно человек может
  // прийти не один. Счётчик тогда прыгает сразу на несколько — а волна
  // поднималась одна, и трое пришедших выглядели как один.
  //
  // Поэтому здесь очередь: одна волна на человека, друг за другом. Не «ярче,
  // если больше» — сила события не про количество, а про факт: пришёл ещё
  // один. Считать волны глазом естественно, читать яркость как число — нет.
  //
  // Потолок в пять: дальше это уже не события, а поток, и двадцать волн
  // подряд превратили бы поле в мигалку. Точное число всё равно стоит
  // числом на экране, волна его не заменяет.
  var WAVE = 2700, GAP = 320, MAX_WAVES = 5;
  var waveT = 0, waiting = 0;

  function runWave() {
    var f = build();
    if (!f) return;
    f.dataset.pulse = 'client';
    clearTimeout(waveT);
    waveT = setTimeout(function () {
      delete f.dataset.pulse;
      if (waiting > 0) {
        waiting--;
        // Пауза нужна не для красоты: чтобы браузер перезапустил анимацию,
        // атрибут должен успеть исчезнуть и появиться снова.
        waveT = setTimeout(runWave, GAP);
      }
    }, WAVE);
  }

  function arrivals(n) {
    n = Math.max(1, Math.min(MAX_WAVES, n | 0));
    var f = build();
    if (!f) return;
    if (f.dataset.pulse === 'client') { waiting = Math.min(MAX_WAVES, waiting + n); return; }
    clearTimeout(pulseT);
    clearTimeout(waveT);
    waiting = n - 1;
    runWave();
  }

  window.VELOR_FIELD = {
    state: state,        // базовое состояние — форма поля
    signal: signal,      // сигнал бизнеса — примесь и местное поведение
    pulse: pulse,        // разовое событие
    arrivals: arrivals,
    mount: build
  };

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
