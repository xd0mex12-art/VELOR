// ===== VELOR AI — общая навигация кабинета =====
// Одна разметка на все страницы. Раздел называет ВОПРОС владельца, а не
// внутреннюю сущность продукта:
//
//   Брифинг   — что происходит с бизнесом и что с этим делать?
//   Входящие  — что я хочу передать VELOR?
//   Реестр    — записи бизнеса: кто, что заказал, за сколько.
//   Работа    — что VELOR разобрал сам и что предлагает.
//   Знание    — что VELOR знает о деле и откуда.
//   Ещё       — настройка, выгрузка, служебное.
//
// Раньше разделов тоже было шесть, но они делили продукт по сущностям:
// «Продажи» отдельно от «Финансов», а разборы VELOR были рассыпаны по «Ещё».
// Теперь всё, что VELOR ДУМАЕТ, лежит в «Работе», всё, что он ЗНАЕТ, — в
// «Знании», а всё, что бизнес ЗАПИСАЛ, — в «Реестре».
//
// Почему разделов шесть, а не пять. «Входящие» — единственная дверь, через
// которую в систему попадает материал; спрятать её в подраздел значит убрать
// главный вход в продукт на два клика. Остальные пять — ровно как задумано.
//
// Ни одна страница не удалена. «Продвижение» ушло из меню: это генерация
// контента, а VELOR — про разбор бизнеса. Instagram переехал в «Подключения»:
// это источник данных, а не смысловой раздел.
(function () {
  var SECTIONS = [
    { t: 'Брифинг', h: 'dashboard.html', kids: [
      { t: 'Обзор',              h: 'dashboard.html' },
      // Разговор с VELOR — тот же вопрос «что происходит», заданный словами.
      { t: 'Спросить',           h: 'home.html' },
      // Риски и возможности — это части ответа «что происходит», а не отдельная
      // тема. Раньше они лежали в «Ещё» — там, где их никто не искал.
      { t: 'Риски',              h: 'risks.html' },
      { t: 'Возможности',        h: 'opportunities.html' },
      { t: 'Утренний брифинг',   h: 'briefing.html' },
      { t: 'Неделя',             h: 'weekly.html' },
      { t: 'Цели',               h: 'goals.html' },
    ] },
    // Входящие — единственная дверь для любого материала о бизнесе. Внутри
    // делить нечего: разбираться, что прислали, — работа VELOR, а не владельца.
    { t: 'Входящие', h: 'inbox.html', kids: [
      { t: 'Входящие', h: 'inbox.html' },
    ] },
    // Реестр — то, что бизнес записал. Деньги и продажи разделять незачем:
    // это один и тот же поток, просто с разных сторон.
    { t: 'Реестр', h: 'clients.html', kids: [
      { t: 'Клиенты',        h: 'clients.html' },
      // Страница называлась «Возможности» — ровно как раздел разбора VELOR.
      // Два разных смысла под одним словом; здесь это люди, проявившие
      // интерес и ещё ничего не купившие.
      { t: 'Интерес',        h: 'leads.html' },
      { t: 'Заказы',         h: 'orders.html' },
      { t: 'Финансы',        h: 'finance.html' },
      { t: 'Импорт выписки', h: 'import.html' },
    ] },
    // Работа — всё, что VELOR разобрал сам: находки, решения, идеи, сравнения.
    { t: 'Работа', h: 'work.html', kids: [
      { t: 'Что происходит',   h: 'work.html' },
      { t: 'Совет директоров', h: 'board.html' },
      { t: 'Идеи',             h: 'ideas.html' },
      { t: 'Конкуренты',       h: 'research.html' },
      // Результаты — то, что владелец уносит наружу.
      { t: 'Результаты',       h: 'results.html' },
      { t: 'Полномочия',       h: 'autonomy.html' },
    ] },
    // Знание — что VELOR знает о деле и откуда. Память, ручные знания, поиск
    // и история событий отвечают на один вопрос и стояли в трёх местах.
    { t: 'Знание', h: 'knowledge.html', kids: [
      { t: 'Память бизнеса',  h: 'knowledge.html' },
      { t: 'База знаний',     h: 'memory.html' },
      { t: 'Поиск',           h: 'search.html' },
      { t: 'История бизнеса', h: 'timeline.html' },
      { t: 'Дневник',         h: 'journal.html' },
    ] },
    { t: 'Ещё', h: 'more.html', kids: [
      { t: 'Ещё',         h: 'more.html' },
      { t: 'Настройки',   h: 'settings.html' },
      { t: 'Подключения', h: 'connections.html' },
      { t: 'Тариф',       h: 'plans.html' },
      { t: 'Экспорт',     h: 'export.html' },
      { t: 'Инструкция',  h: 'guide.html' },
    ] },
  ];

  // Раздел владельца сервиса. Показывается ТОЛЬКО своему аккаунту и добавляется
  // к обычным разделам, а не заменяет их: свой продукт надо каждый день видеть
  // глазами клиента — иначе его болячки замечаешь последним.
  var OWNER_SECTION = { t: 'VELOR', h: 'owner.html', kids: [
    { t: 'Клиенты VELOR', h: 'owner.html' },
    { t: 'Журнал ошибок', h: 'errors.html' },
  ] };

  // Роль читаем из самого пропуска. Это решение только про то, что РИСОВАТЬ:
  // доступ проверяет сервер на каждом запросе, и подделанный здесь ответ не
  // откроет ни одной чужой строки — только нарисует пункт меню, который потом
  // ответит отказом.
  function isOwner() {
    try {
      var t = localStorage.getItem('coreon_biz_token') || '';
      var body = t.split('.')[1];
      if (!body) return false;
      var json = atob(body.replace(/-/g, '+').replace(/_/g, '/')
                          + '==='.slice((body.length + 3) % 4));
      return JSON.parse(decodeURIComponent(escape(json))).role === 'owner';
    } catch (e) { return false; }
  }

  if (isOwner()) SECTIONS.push(OWNER_SECTION);

  // Страницы, которых нет в меню, всё равно должны подсвечивать «свой»
  // раздел — иначе, открыв «Экспорт» со страницы «Ещё», человек видит
  // навигацию без единой активной вкладки и перестаёт понимать, где он.
  var HOME_OF = {
    // Уведомления живут на колокольчике справа, а не в строке разделов.
    'notifications.html': 'more.html',
    // Каналы — источники данных: их место в «Подключениях».
    'vk.html': 'more.html',
    'instagram.html': 'more.html',
    'integrations.html': 'more.html',
    // «Продвижение» убрано из меню: генерация контента — не то, чем занят
    // VELOR. Страница жива по прямой ссылке и подсвечивает «Работу».
    'growth.html': 'work.html',
    'tools.html': 'work.html',
    // Журнал ошибок — служебная страница владельца сервиса.
    'errors.html': 'owner.html',
  };

  var here = (location.pathname.split('/').pop() || 'dashboard.html').toLowerCase();

  // Активный раздел — тот, чей адрес совпадает с текущей страницей ЛИБО у кого
  // текущая страница среди подразделов. Так подсветка верхней строки не зависит
  // от того, на каком именно подразделе ты стоишь.
  function isActiveSection(sec) {
    if (sec.h === here) return true;
    if (sec.kids.some(function (k) { return k.h === here; })) return true;
    return HOME_OF[here] === sec.h;
  }
  var activeSection = null;
  for (var i = 0; i < SECTIONS.length; i++) { if (isActiveSection(SECTIONS[i])) { activeSection = SECTIONS[i]; break; } }

  var css = document.createElement('style');
  css.textContent = [
    // шапка — постоянная «оболочка»: при переходе между разделами она остаётся на
    // месте (view-transition), поэтому смена раздела ощущается как обновление
    // содержимого внутри одной системы.
    '.vn{ view-transition-name: velor-shell; }',
    '.vn{ position:fixed; top:0; left:0; right:0; z-index:60; display:flex; align-items:center;',
    '  justify-content:space-between; gap:18px; padding:16px 24px; background:rgba(0,0,0,.72);',
    '  backdrop-filter:blur(14px); border-bottom:1px solid rgba(255,255,255,.07); }',
    '.vn a{ text-decoration:none; color:inherit; }',
    '.vn-badge{ display:inline-block; min-width:17px; height:17px; padding:0 5px; margin-left:6px;',
    '  border-radius:9px; background:var(--bad); color:var(--bone); font-size:var(--fs-micro); font-weight:600;',
    '  line-height:17px; text-align:center; vertical-align:1px; }',
    '.vn-logo{ display:flex; align-items:center; gap:9px; font-weight:500; font-size:var(--fs-lead); letter-spacing:.12em; flex:none; }',
    '.vn-sub{ font-weight:500; font-size:var(--fs-micro); letter-spacing:.16em; text-transform:uppercase; color:var(--ash);',
    '  border-left:1px solid rgba(255,255,255,.1); padding-left:9px; }',
    // верхняя строка — только 6 разделов, крупнее и с воздухом
    '.vn-mid{ display:flex; align-items:center; gap:6px; flex:1; min-width:0; justify-content:center; }',
    '.vn-lk{ padding:9px 16px; border-radius:20px; font-weight:400; font-size:var(--fs-body); letter-spacing:.01em;',
    '  color:var(--ash); white-space:nowrap; transition:color .25s, background .25s; background:none; border:none;',
    '  font-family:inherit; cursor:pointer; }',
    '.vn-lk:hover{ color:var(--bone); background:rgba(255,255,255,.06); }',
    // Текущий раздел раньше был сиреневой таблеткой — самый обычный приём,
    // какой стоит в любом дашборде. Теперь его отмечает зарубка VELOR: та же
    // ирисовая чёрточка 5x2, что стоит перед меткой раздела на страницах и
    // внутри знака присутствия. Отдельно её не замечают — она и не должна
    // бросаться в глаза; но она повторяется на каждом экране, и через неделю
    // именно по ней интерфейс опознаётся.
    '.vn-lk.on{ color:var(--bone); background:rgba(255,255,255,.07); }',
    '.vn-lk.on::before, .vn-sub-lk.on::before{ content:""; display:inline-block;',
    '  width:5px; height:2px; border-radius:1px; background:var(--iris);',
    '  margin-right:8px; vertical-align:3px; }',
    '.vn-right{ display:flex; align-items:center; gap:14px; flex:none; }',
    // колокольчик уведомлений — всегда справа; цифра непрочитанного гаснет, когда открыл
    '.vn-bell{ position:relative; display:flex; align-items:center; justify-content:center;',
    '  width:44px; height:44px; border-radius:14px; color:var(--silver); background:rgba(255,255,255,.05);',
    '  border:1px solid rgba(255,255,255,.09); transition:color .2s, background .2s; }',
    '.vn-bell:hover{ color:var(--bone); background:rgba(255,255,255,.1); }',
    '.vn-bell.on{ color:var(--bone); background:rgba(128,82,255,.28); border-color:transparent; }',
    '.vn-bell svg{ width:18px; height:18px; display:block; }',
    '.vn-bell .vn-badge{ position:absolute; top:-6px; right:-6px; margin:0; }',
    '.vn-biz{ font-weight:200; font-size:var(--fs-caption); color:var(--ash); max-width:150px; overflow:hidden;',
    '  text-overflow:ellipsis; white-space:nowrap; }',
    // Высота цели, а не только текста: кнопка была 15px — попасть в неё пальцем
    // можно было только со второго раза. Отступ добавлен по вертикали, шапку он
    // не растит: её высоту держит колокольчик 44px.
    '.vn-out{ font-weight:400; font-size:var(--fs-caption); letter-spacing:.14em; text-transform:uppercase; color:var(--ash);',
    '  background:none; border:none; font-family:inherit; cursor:pointer; transition:color .25s;',
    '  min-height:44px; padding:0 4px; display:inline-flex; align-items:center; }',
    '.vn-out:hover{ color:var(--bone); }',
    // вторая строка — подразделы активного раздела
    // Высота шапки НЕ прописана числом. Она зависит от самого высокого
    // элемента внутри (сейчас это колокольчик 44px) и меняется от шрифта,
    // масштаба и длины названия бизнеса. Пока здесь стояло «57px», вторая
    // строка на 20px заезжала под первую и подразделы срезало наполовину.
    // Значения ниже — только запасные, до первого измерения.
    '.vn-sub-bar{ view-transition-name: velor-subbar; position:fixed;',
    '  top:var(--vn-h,77px); left:0; right:0; z-index:59;',
    '  height:47px; box-sizing:border-box;',
    '  display:flex; align-items:center; gap:4px; padding:8px 24px; overflow-x:auto; scrollbar-width:none;',
    '  background:rgba(0,0,0,.55); backdrop-filter:blur(14px); border-bottom:1px solid rgba(255,255,255,.06); }',
    '.vn-sub-bar::-webkit-scrollbar{ display:none; }',
    '.vn-sub-lk{ padding:6px 13px; border-radius:16px; font-weight:400; font-size:var(--fs-sm); color:var(--ash);',
    '  white-space:nowrap; transition:color .2s, background .2s; }',
    '.vn-sub-lk:hover{ color:var(--bone); background:rgba(255,255,255,.05); }',
    '.vn-sub-lk.on{ color:var(--bone); background:rgba(255,255,255,.07); }',
    '.vn-spacer{ width:100%; height:var(--vn-sub-h,47px); }',
    // бургер
    '.vn-burger{ display:none; width:44px; height:44px; border-radius:14px; background:rgba(255,255,255,.05);',
    '  border:1px solid rgba(255,255,255,.1); cursor:pointer; padding:0; }',
    '.vn-burger span{ display:block; width:16px; height:1.5px; margin:3.5px auto; background:var(--bone);',
    '  transition:transform .3s, opacity .2s; }',
    '.vn-burger.open span:nth-child(1){ transform:translateY(5px) rotate(45deg); }',
    '.vn-burger.open span:nth-child(2){ opacity:0; }',
    '.vn-burger.open span:nth-child(3){ transform:translateY(-5px) rotate(-45deg); }',
    // мобильное меню
    '.vn-sheet{ position:fixed; top:0; left:0; right:0; z-index:58;',
    '  padding:calc(var(--vn-h,77px) + 22px) 20px 26px;',
    '  background:rgba(6,6,8,.98); backdrop-filter:blur(20px); border-bottom:1px solid rgba(255,255,255,.08);',
    '  display:none; max-height:100dvh; overflow-y:auto; }',
    '.vn-sheet.open{ display:block; animation:vnDrop .32s cubic-bezier(.22,1,.36,1); }',
    '@keyframes vnDrop{ from{ opacity:0; transform:translateY(-12px) } to{ opacity:1; transform:none } }',
    '.vn-sheet .vn-gr{ font-weight:600; font-size:var(--fs-micro); letter-spacing:.2em; text-transform:uppercase;',
    '  color:var(--ink-iris); padding:16px 4px 8px; }',
    '.vn-sheet .vn-kids{ display:grid; grid-template-columns:repeat(2,1fr); gap:6px 10px; margin-bottom:6px; }',
    '.vn-sheet .vn-sub-lk{ display:block; padding:12px 14px; font-size:var(--fs-body); border-radius:14px;',
    '  background:rgba(255,255,255,.04); color:var(--silver); }',
    '.vn-sheet .vn-sub-lk.on{ background:rgba(255,255,255,.07); color:var(--bone); }',
    '.vn-foot{ display:flex; align-items:center; justify-content:space-between; gap:12px;',
    '  margin-top:20px; padding-top:18px; border-top:1px solid rgba(255,255,255,.08); }',
    // пороги: сначала прячем «имя бизнеса», потом уходим в бургер (и прячем под-строку)
    '@media(max-width:1180px){ .vn-biz{ display:none; } }',
    '@media(max-width:1000px){ .vn-mid{ display:none; } .vn-right .vn-out{ display:none; }',
    '  .vn-burger{ display:block; } .vn-sub-bar{ display:none; } .vn-spacer{ display:none; } }',
    '@media(prefers-reduced-motion: reduce){ .vn *,.vn-sheet,.vn-sub-bar{ animation:none !important; transition:none !important; } }',
  ].join('\n');
  document.head.appendChild(css);

  function topLink(sec) {
    var on = (sec === activeSection) ? ' on' : '';
    var badge = sec.kids.some(function (k) { return k.badge; }) ? '<span class="vn-badge" hidden></span>' : '';
    return '<a class="vn-lk' + on + '" href="' + sec.h + '">' + sec.t + badge + '</a>';
  }
  function subLink(k) {
    return '<a class="vn-sub-lk' + (k.h === here ? ' on' : '') + '" href="' + k.h + '">' + k.t +
      (k.badge ? '<span class="vn-badge" hidden></span>' : '') + '</a>';
  }

  var nav = document.createElement('nav');
  nav.className = 'vn';
  nav.innerHTML =
    '<a class="vn-logo" href="dashboard.html">' +
      '<svg width="16" height="16" viewBox="0 0 18 18" aria-hidden="true"><path d="M9 1 L17 16 L1 16 Z" fill="#8052ff"/></svg>' +
      'VELOR<span class="vn-sub">AI Employee</span></a>' +
    '<div class="vn-mid">' + SECTIONS.map(topLink).join('') + '</div>' +
    '<div class="vn-right">' +
      '<a class="vn-bell' + (here === 'notifications.html' ? ' on' : '') + '" href="notifications.html" aria-label="Уведомления">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>' +
        '<span class="vn-badge" hidden></span></a>' +
      '<span class="vn-biz" id="bizName">бизнес</span>' +
      '<button class="vn-out" id="bizLogout">Выйти</button>' +
      '<button class="vn-burger" id="vnBurger" aria-label="Меню" aria-expanded="false">' +
        '<span></span><span></span><span></span></button>' +
    '</div>';

  // Под-строка: только если в активном разделе больше одного подраздела.
  var subBar = null;
  if (activeSection && activeSection.kids.length > 1) {
    subBar = document.createElement('div');
    subBar.className = 'vn-sub-bar';
    subBar.innerHTML = activeSection.kids.map(subLink).join('');
  }

  // Мобильное меню: все шесть разделов, каждый — со своими подразделами.
  var sheet = document.createElement('div');
  sheet.className = 'vn-sheet';
  sheet.id = 'vnSheet';
  sheet.innerHTML =
    SECTIONS.map(function (sec) {
      return '<div class="vn-gr">' + sec.t + '</div>' +
        '<div class="vn-kids">' + sec.kids.map(subLink).join('') + '</div>';
    }).join('') +
    '<div class="vn-foot"><span class="vn-biz" style="display:block" id="bizNameMob">бизнес</span>' +
    '<button class="vn-out" id="bizLogoutMob">Выйти</button></div>';

  document.addEventListener('DOMContentLoaded', function () {
    // страница могла нарисовать свой старый <nav> — убираем, чтобы не было двух шапок
    var old = document.querySelector('body > nav:not(.vn)');
    if (old) old.remove();
    var mob = document.getElementById('bizNameMob');
    if (mob) mob.textContent = localStorage.getItem('coreon_biz_name') || 'бизнес';
    var biz = document.getElementById('bizName');
    if (biz) biz.textContent = localStorage.getItem('coreon_biz_name') || 'бизнес';
  });

  document.body ? place() : document.addEventListener('DOMContentLoaded', place);
  function place() {
    // отступ в потоке под вторую строку: контент страниц опускается ровно на высоту
    // под-навигации (у страниц свой padding рассчитан только на верхнюю строку).
    if (subBar) {
      var spacer = document.createElement('div');
      spacer.className = 'vn-spacer';
      document.body.insertBefore(spacer, document.body.firstChild);
    }
    document.body.insertBefore(sheet, document.body.firstChild);
    if (subBar) document.body.insertBefore(subBar, document.body.firstChild);
    document.body.insertBefore(nav, document.body.firstChild);
    measure();
  }

  // Сколько на самом деле занимают строки навигации.
  //
  // Измеряем, а не считаем: высота шапки складывается из её содержимого, и
  // меняется от загрузки шрифта, масштаба страницы и длины названия бизнеса.
  // Любое число, вписанное сюда руками, однажды разойдётся с настоящим — и
  // разойдётся молча: вторая строка просто уедет под первую.
  function measure() {
    var root = document.documentElement.style;
    root.setProperty('--vn-h', nav.offsetHeight + 'px');
    if (subBar) root.setProperty('--vn-sub-h', subBar.offsetHeight + 'px');
  }

  // Шрифт догружается после первого кадра и делает шапку выше. Один замер при
  // вставке этого не поймает, поэтому пересчитываем на каждое изменение
  // размеров шапки, а не только на resize окна.
  if (window.ResizeObserver) {
    new ResizeObserver(measure).observe(nav);
  } else {
    window.addEventListener('resize', measure);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(measure);
  }

  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeSheet(); });

  // бургер
  var burger = nav.querySelector('#vnBurger');
  function closeSheet() {
    sheet.classList.remove('open'); burger.classList.remove('open');
    burger.setAttribute('aria-expanded', 'false');
  }
  burger.addEventListener('click', function (e) {
    e.stopPropagation();
    var open = sheet.classList.toggle('open');
    burger.classList.toggle('open', open);
    burger.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  sheet.addEventListener('click', function (e) { if (e.target.closest('a')) closeSheet(); });
  window.addEventListener('resize', function () { if (innerWidth > 1000) closeSheet(); });

  // выход — обе кнопки (десктоп и мобильное меню)
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#bizLogout') && !e.target.closest('#bizLogoutMob')) return;
    localStorage.removeItem('coreon_biz_token');
    localStorage.removeItem('coreon_biz_name');
    localStorage.removeItem('coreon_biz_refresh');   // не оставляем «хвост» сессии
    location.replace('biz-login.html');
  });

  // Цифра непрочитанного на колокольчике. Единая точка управления, чтобы страница
  // уведомлений могла погасить её сразу, как только человек их открыл, — число не
  // копится, а исчезает при прочтении.
  window.VELOR_setUnread = function (n) {
    n = n | 0;
    [].forEach.call(document.querySelectorAll('.vn-badge'), function (b) {
      if (n > 0) { b.textContent = n > 99 ? '99+' : n; b.hidden = false; }
      else { b.hidden = true; b.textContent = ''; }
    });
  };
  if (localStorage.getItem('coreon_biz_token')) {
    fetch('/api/notifications/count', {
      headers: { 'X-Auth': localStorage.getItem('coreon_biz_token') }
    }).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (d) window.VELOR_setUnread(d.unread);
    }).catch(function () {});
  }

  // ── TRIAL: ненавязчивый отсчёт + экран окончания (данные из /api/trial) ──
  // Логика целиком на бэке (TrialService). Здесь только показ состояния.
  if (localStorage.getItem('coreon_biz_token')) {
    var tcss = document.createElement('style');
    tcss.textContent = [
      '.vt-bar{ position:fixed; left:0; right:0; bottom:0; z-index:70; display:flex; align-items:center;',
      '  justify-content:center; gap:14px; padding:11px 18px; font-size:var(--fs-sm); color:var(--bone);',
      '  background:rgba(128,82,255,.16); backdrop-filter:blur(12px); border-top:1px solid rgba(128,82,255,.3); }',
      '.vt-bar.urgent{ background:rgba(255,107,107,.16); border-top-color:rgba(255,107,107,.4); color:var(--ink-coral); }',
      '.vt-bar a{ color:var(--bone); font-weight:600; text-decoration:none; background:var(--iris); padding:7px 15px;',
      '  border-radius:999px; white-space:nowrap; } .vt-bar a:hover{ filter:brightness(1.15); }',
      '.vt-bar .vt-x{ background:none; border:none; color:inherit; opacity:.6; cursor:pointer; font-size:var(--fs-base); }',
      '.vt-ov{ position:fixed; inset:0; z-index:130; display:flex; align-items:center; justify-content:center;',
      '  padding:22px; background:rgba(0,0,0,.72); backdrop-filter:blur(6px); }',
      '.vt-modal{ width:100%; max-width:440px; border-radius:26px; padding:34px; text-align:center;',
      '  border:1px solid rgba(255,255,255,.1); background:var(--sheet); }',
      '.vt-modal h2{ font-weight:500; font-size:var(--fs-h2); letter-spacing:-.02em; margin-bottom:8px; }',
      '.vt-modal .sub{ color:var(--ash); font-size:var(--fs-body); line-height:1.55; margin-bottom:20px; }',
      '.vt-stats{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:18px 0 22px; text-align:left; }',
      '.vt-stats div{ border:1px solid rgba(255,255,255,.08); border-radius:14px; padding:12px 14px; }',
      '.vt-stats .n{ font-weight:500; font-size:var(--fs-h3); color:var(--lilac-2); } .vt-stats .k{ font-size:var(--fs-caption); color:var(--ash); margin-top:2px; }',
      '.vt-modal .go{ display:block; width:100%; padding:14px; border-radius:16px; background:var(--iris); color:var(--bone);',
      '  font-weight:600; font-size:var(--fs-body); text-decoration:none; } .vt-modal .go:hover{ filter:brightness(1.15); }',
      '.vt-modal .look{ display:inline-block; margin-top:14px; color:var(--ash); font-size:var(--fs-sm); cursor:pointer; }',
    ].join('');
    document.head.appendChild(tcss);

    fetch('/api/trial').then(function (r) { return r.ok ? r.json() : null; }).then(function (t) {
      if (!t) return;
      if (t.read_only) return vtLocked(t);
      if (t.notice && sessionStorage.getItem('vt_dismiss') !== t.notice.text) vtBanner(t.notice);
    }).catch(function () {});

    function vtBanner(notice) {
      var bar = document.createElement('div');
      bar.className = 'vt-bar' + (notice.level === 'urgent' ? ' urgent' : '');
      bar.innerHTML = '<span>' + notice.text + '</span>' +
        '<a href="plans.html">Выбрать тариф</a>' +
        '<button class="vt-x" aria-label="Скрыть">✕</button>';
      document.body.appendChild(bar);
      bar.querySelector('.vt-x').onclick = function () {
        sessionStorage.setItem('vt_dismiss', notice.text); bar.remove();
      };
    }

    function vtLocked(t) {
      // Экран окончания показываем один раз за сессию; дальше — тонкая полоса.
      // На дашборде модалку НЕ показываем — там своя карточка «AI ждёт возвращения».
      var s = t.stats || {};
      if (here !== 'dashboard.html' && sessionStorage.getItem('vt_locked_seen') !== '1') {
        sessionStorage.setItem('vt_locked_seen', '1');
        var ov = document.createElement('div'); ov.className = 'vt-ov';
        ov.innerHTML = '<div class="vt-modal">' +
          '<h2>Ваш пробный период завершён</h2>' +
          '<p class="sub">За это время VELOR поработал для вас. Все данные сохранены — ' +
          'чтобы продолжить работу, выберите тариф.</p>' +
          '<div class="vt-stats">' +
            '<div><div class="n">' + (s.messages || 0) + '</div><div class="k">сообщений обработано</div></div>' +
            '<div><div class="n">' + (s.orders || 0) + '</div><div class="k">заявок создано</div></div>' +
            '<div><div class="n">' + (s.clients || 0) + '</div><div class="k">клиентов в базе</div></div>' +
            '<div><div class="n">' + (s.recommendations || 0) + '</div><div class="k">рекомендаций</div></div>' +
          '</div>' +
          '<a class="go" href="plans.html">Продолжить работу</a>' +
          '<span class="look">Пока посмотреть данные</span></div>';
        document.body.appendChild(ov);
        ov.querySelector('.look').onclick = function () { ov.remove(); };
        ov.addEventListener('click', function (e) { if (e.target === ov) ov.remove(); });
      }
      var bar = document.createElement('div');
      bar.className = 'vt-bar urgent';
      bar.innerHTML = '<span>Пробный период завершён — режим просмотра. Данные сохранены.</span>' +
        '<a href="plans.html">Оформить подписку</a>';
      document.body.appendChild(bar);
    }
  }

  // Мгновенное открытие разделов: браузер заранее подгружает HTML-оболочку по
  // ховеру/касанию. Вместе с кроссфейдом — ощущение единого приложения.
  try {
    if (HTMLScriptElement.supports && HTMLScriptElement.supports('speculationrules')) {
      var sr = document.createElement('script');
      sr.type = 'speculationrules';
      sr.textContent = JSON.stringify({
        prefetch: [{
          source: 'document',
          eagerness: 'moderate',
          where: { and: [ { href_matches: '/*.html' },
                          { not: { selector_matches: '[data-noprefetch]' } } ] }
        }]
      });
      document.head.appendChild(sr);
    }
  } catch (e) { /* прогрессивное улучшение — тихо игнорируем */ }
})();
