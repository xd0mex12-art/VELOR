# -*- coding: utf-8 -*-
"""
Навигация кабинета после перестройки оболочки.

Проверяем не красоту, а обещания, которые легко нарушить незаметно:
  • верхних разделов ровно шесть, и каждый ведёт на существующую страницу;
  • НИ ОДНА страница старого меню не потерялась — у каждой есть новый адрес
    и до неё можно дойти (§«скрыть ≠ сломать»);
  • ни одной ссылки в никуда: каждый href из кабинета указывает на файл,
    который существует;
  • в кабинете не осталось кнопок, честно говорящих «Скоро»;
  • старые адреса (tools.html, integrations.html) не отвечают 404, а ведут
    туда, куда переехали;
  • на главной есть дверь во «Входящие», и она бьёт в тот же приёмник;
  • каждая страница пригодна для телефона (есть viewport) и не заявляет
    ширину, от которой появится горизонтальная прокрутка.
"""
import io, os, re, sys, glob
import pathlib as _pl

sys.stdout.reconfigure(encoding="utf-8")
ROOT = _pl.Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
os.chdir(ROOT)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


NAV = io.open(WEB / "nav.js", encoding="utf-8").read()
PAGES = {os.path.basename(p) for p in glob.glob(str(WEB / "*.html"))}


def sections():
    """Разобрать SECTIONS из nav.js. Разбираем исходник, а не копию списка:
    копия однажды разошлась бы с меню, и тест перестал бы проверять меню."""
    body = NAV[NAV.index("var SECTIONS = ["):NAV.index("  var here =")]
    out, cur = [], None
    for line in body.split("\n"):
        top = re.search(r"\{\s*t:\s*'([^']+)',\s*h:\s*'([^']+)',\s*kids:", line)
        if top:
            cur = {"title": top.group(1), "href": top.group(2), "kids": []}
            out.append(cur)
            continue
        kid = re.search(r"\{\s*t:\s*'([^']+)',\s*h:\s*'([^']+)'\s*\}", line)
        if kid and cur is not None:
            cur["kids"].append({"title": kid.group(1), "href": kid.group(2)})
    return out


SECS = sections()
NAV_HREFS = {s["href"] for s in SECS} | {k["href"] for s in SECS for k in s["kids"]}
HOME_OF = dict(re.findall(r"'([a-z0-9_-]+\.html)':\s*'([a-z0-9_-]+\.html)'",
                          NAV[NAV.index("var HOME_OF"):NAV.index("  var here =")]))

print("\n== ШЕСТЬ РАЗДЕЛОВ ==")
check("разделов ровно шесть", len(SECS) == 6, [s["title"] for s in SECS])
check("названия — вопросы владельца, а не сущности продукта",
      [s["title"] for s in SECS] ==
      ["Брифинг", "Входящие", "Реестр", "Работа", "Знание", "Ещё"],
      [s["title"] for s in SECS])
for s in SECS:
    check(f"«{s['title']}» ведёт на существующую страницу", s["href"] in PAGES, s["href"])
check("каждый подраздел существует",
      all(k["href"] in PAGES for s in SECS for k in s["kids"]),
      [k["href"] for s in SECS for k in s["kids"] if k["href"] not in PAGES])
check("первый подраздел совпадает с самим разделом",
      all(s["kids"][0]["href"] == s["href"] for s in SECS),
      [s["title"] for s in SECS if s["kids"][0]["href"] != s["href"]])

print("\n== ВНУТРЕННИХ СЛОВ В МЕНЮ НЕТ ==")
# Лид, инициатива, действие, память, агент — это архитектура VELOR. Владелец
# не обязан их знать, чтобы пользоваться кабинетом.
INTERNAL = ("лид", "инициатив", "action", "agent", "entity")
titles = " ".join([s["title"] for s in SECS] +
                  [k["title"] for s in SECS for k in s["kids"]]).lower()
for w in INTERNAL:
    check(f"в меню нет слова «{w}»", w not in titles, titles)

print("\n== НИ ОДНА СТАРАЯ СТРАНИЦА НЕ ПОТЕРЯЛАСЬ ==")
# Меню до перестройки. Список зафиксирован здесь намеренно: он и есть
# обещание «ничего полезного не пропало случайно».
OLD_NAV = [
    "dashboard.html", "briefing.html", "weekly.html", "timeline.html", "search.html",
    "clients.html", "leads.html", "orders.html", "instagram.html", "goals.html",
    "finance.html", "import.html", "export.html",
    "home.html", "board.html", "risks.html", "opportunities.html", "ideas.html",
    "journal.html", "growth.html", "research.html",
    "inbox.html", "knowledge.html", "memory.html", "tools.html",
    "settings.html", "autonomy.html", "plans.html", "connections.html",
    "integrations.html", "guide.html", "notifications.html",
]
MORE = io.open(WEB / "more.html", encoding="utf-8").read()
more_links = set(re.findall(r'href="([a-z0-9_-]+\.html)"', MORE))


def reachable(page):
    """До страницы можно дойти: она в меню, у неё есть «свой» раздел,
    на неё ведёт «Ещё», или она переехала и говорит куда."""
    if page in NAV_HREFS or page in HOME_OF or page in more_links:
        return True
    src = io.open(WEB / page, encoding="utf-8").read()
    return "http-equiv=\"refresh\"" in src


for page in OLD_NAV:
    check(f"{page} по-прежнему доступна", reachable(page), page)

print("\n== ПЕРЕЕХАВШИЕ АДРЕСА НЕ ЛОМАЮТСЯ ==")
for old, new in (("tools.html", "results.html"), ("integrations.html", "connections.html")):
    src = io.open(WEB / old, encoding="utf-8").read()
    check(f"{old} существует", old in PAGES)
    check(f"{old} ведёт на {new}", new in src, src[:200])
    check(f"{old} объясняет, что переехало и куда", "переехал" in src.lower()
          or "стали" in src.lower(), src[:400])

print("\n== ЧТО VELOR ДУМАЕТ И ЧТО ЗНАЕТ — В СВОИХ РАЗДЕЛАХ ==")
# Раньше разборы VELOR (совет директоров, риски, возможности, идеи, конкуренты)
# лежали в «Ещё» — там, где их не ищут. Теперь у каждого есть своё место, и
# место это должно быть определённым, а не «где-то в меню».
WHERE = {s["href"]: s["title"] for s in SECS}
OF = {}
for s in SECS:
    for k in s["kids"]:
        OF[k["href"]] = s["title"]
for page, sec in (("risks.html", "Брифинг"), ("opportunities.html", "Брифинг"),
                  ("board.html", "Работа"), ("ideas.html", "Работа"),
                  ("research.html", "Работа"),
                  ("knowledge.html", "Знание"), ("memory.html", "Знание"),
                  ("search.html", "Знание"), ("timeline.html", "Знание"),
                  ("journal.html", "Знание"),
                  ("clients.html", "Реестр"), ("leads.html", "Реестр"),
                  ("orders.html", "Реестр"), ("finance.html", "Реестр")):
    check(f"{page} живёт в разделе «{sec}»", OF.get(page) == sec, OF.get(page))

check("«Реестр» собрал продажи и деньги в один раздел",
      {"clients.html", "orders.html", "finance.html"} <=
      {k["href"] for s in SECS if s["title"] == "Реестр" for k in s["kids"]})

print("\n== ДВА РАЗНЫХ СМЫСЛА НЕ НОСЯТ ОДНО ИМЯ ==")
# leads.html и opportunities.html обе назывались «Возможности»: одно — люди,
# проявившие интерес, другое — вывод Директора о росте. Одно слово на два
# смысла делает меню бесполезным.
all_titles = [k["title"] for s in SECS for k in s["kids"]]
check("в подразделах нет двух одинаковых названий",
      len(all_titles) == len(set(all_titles)),
      [t for t in all_titles if all_titles.count(t) > 1])

print("\n== ЛИШНЕЕ УБРАНО ИЗ МЕНЮ, НО НЕ ИЗ ПРОДУКТА ==")
# «Продвижение» — генерация контента; VELOR про разбор бизнеса, а не про посты.
# Instagram — источник данных, его место в «Подключениях».
check("«Продвижение» не занимает место в меню", "growth.html" not in NAV_HREFS)
check("«Продвижение» всё ещё достижимо", "growth.html" in HOME_OF)
check("Instagram не отдельный раздел меню", "instagram.html" not in NAV_HREFS)
check("Instagram открывается из «Подключений»",
      "instagram.html" in io.open(ROOT / "connections.py", encoding="utf-8").read())

print("\n== НИ ОДНОЙ ССЫЛКИ В НИКУДА ==")
dead = []
for path in sorted(glob.glob(str(WEB / "*.html"))):
    src = io.open(path, encoding="utf-8").read()
    for href in set(re.findall(r'href="([a-z0-9_-]+\.html)[^"]*"', src)):
        if href not in PAGES:
            dead.append((os.path.basename(path), href))
check("все внутренние ссылки ведут на существующие страницы", not dead, dead[:6])

print("\n== «СКОРО» БОЛЬШЕ НЕ ОБЕЩАЕТСЯ ==")
promises = []
for path in sorted(glob.glob(str(WEB / "*.html"))):
    src = io.open(path, encoding="utf-8").read()
    for pat in ("Скоро будет", "Coming soon", "coming soon", "ещё готовится"):
        if pat in src:
            promises.append((os.path.basename(path), pat))
check("в кабинете нет надписей «Скоро будет»", not promises, promises)
# Слово «Скоро» на карточке коннектора допустимо и правдиво: там оно про
# чужой сервис, для которого адаптера ещё нет, и кнопки под ним нет тоже.
tools_src = io.open(WEB / "tools.html", encoding="utf-8").read()
check("страница «Инструменты» больше не рисует карточки «Скоро»",
      "class=\"tool" not in tools_src)

print("\n== РЕЗУЛЬТАТЫ ЗАМЕНИЛИ ИНСТРУМЕНТЫ ==")
RES = io.open(WEB / "results.html", encoding="utf-8").read()
check("страница результатов есть", "results.html" in PAGES)
check("берёт реестр с сервера", "/api/outputs/kinds" in RES)
check("собирает через общий endpoint", "'/api/outputs'" in RES)
check("качает файл по токену, а не прямой ссылкой", "/file`" in RES and "blob()" in RES)
check("догадка ИИ подписана отдельно", "Догадка ИИ, не расчёт" in RES)
check("результаты стоят в меню", "results.html" in NAV_HREFS)

print("\n== РАБОТА VELOR СОБРАНА В ОДНОМ МЕСТЕ ==")
WORK = io.open(WEB / "work.html", encoding="utf-8").read()
check("страница есть", "work.html" in PAGES)
check("берёт находки", "/api/initiatives" in WORK)
check("берёт очередь решений и журнал", "/api/actions" in WORK)
check("разрешение и отказ идут через тот же журнал действий",
      'data-pact="approve"' in WORK and 'data-pact="reject"' in WORK
      and "/api/actions/${id}/${btn.dataset.pact}" in WORK)
check("четыре понятных вкладки",
      all(x in WORK for x in ("Требует решения", "VELOR заметил",
                              "VELOR сделал", "История")))

print("\n== ДВЕРЬ ВНУТРЬ НА ГЛАВНОЙ ==")
DASH = io.open(WEB / "dashboard.html", encoding="utf-8").read()
check("на главной есть «Передать VELOR»", "Передать VELOR" in DASH)
check("и она бьёт в тот же приёмник, что «Входящие»", "'/api/inbox'" in DASH)
check("файлы уводят в полное окно приёма", 'href="inbox.html"' in DASH)
check("второго приёмника не завелось", DASH.count("/api/inbox") <= 2, DASH.count("/api/inbox"))

print("\n== ВХОДЯЩИЕ — ПОСТОЯННОЕ МЕСТО, А НЕ ЗНАКОМСТВО ==")
INBOX = io.open(WEB / "inbox.html", encoding="utf-8").read()
check("заголовок больше не про «расскажите о бизнесе»",
      "Расскажите VELOR о вашем бизнесе" not in INBOX)
check("принимает файлы перетаскиванием", "dragover" in INBOX or "drop" in INBOX)
check("принимает вставку из буфера", "paste" in INBOX)
check("показывает, что VELOR понял", "VELOR понял" in INBOX)
check("и историю решений по материалу", "История решений" in INBOX)

print("\n== ВЫСОТА ШАПКИ НЕ УГАДЫВАЕТСЯ ==")
# Пока вторая строка была прибита к «top:57px», а шапка выросла до 77px
# (колокольчик 44px плюс отступы), подразделы на 20px заезжали под первую
# строку и срезались наполовину. Любое число, вписанное сюда руками, сходится
# с реальностью ровно до следующей правки шапки — и расходится молча.
check("вторая строка встаёт под измеренную шапку",
      "top:var(--vn-h" in NAV and "top:57px" not in NAV, "в nav.js остался top:57px")
check("отступ в потоке равен измеренной второй строке",
      "height:var(--vn-sub-h" in NAV and "height:47px; }" not in NAV)
check("мобильное меню начинается под измеренной шапкой",
      "calc(var(--vn-h" in NAV and "padding:82px 20px 26px" not in NAV)
check("замер делается в рантайме", "offsetHeight" in NAV and "--vn-h" in NAV)
check("и повторяется, когда шапка меняет размер",
      "ResizeObserver" in NAV or "fonts.ready" in NAV)


print("\n== ВЁРСТКА НЕ СПОРИТ САМА С СОБОЙ ==")
# Ребёнок, просящий целую строку (flex-basis:100%), в контейнере без
# flex-wrap не переносится — он сжимает соседей до нуля, и текст вылезает за
# свои коробки одно поверх другого. Ровно так «Карта знаний» рисовала
# название, число и приписку об источнике друг на друге.
conflicts = []
for path in sorted(glob.glob(str(WEB / "*.html"))):
    src = io.open(path, encoding="utf-8").read()
    for m in re.finditer(r"\.([a-z0-9-]+)\s+\.[a-z0-9-]+\s*\{[^}]*flex-basis:\s*100%", src):
        parent = m.group(1)
        # Достаточно, чтобы перенос был объявлен хоть где-то для этого класса:
        # он может стоять и в медиазапросе рядом с самим flex-basis.
        if not re.search(r"\.%s\b[^{]*\{[^}]*flex-wrap" % re.escape(parent), src):
            conflicts.append((os.path.basename(path), "." + parent))
check("ни один flex-basis:100% не стоит в контейнере без переноса",
      not conflicts, conflicts)

print("\n== ТЕЛЕФОН ==")
no_viewport, wide = [], []
for path in sorted(glob.glob(str(WEB / "*.html"))):
    name = os.path.basename(path)
    src = io.open(path, encoding="utf-8").read()
    if "name=\"viewport\"" not in src:
        no_viewport.append(name)
    # Жёсткая ширина в пикселях у самой страницы — прямой путь к
    # горизонтальной прокрутке на 375px.
    if re.search(r"body\s*\{[^}]*\bmin-width:\s*\d{3,}px", src):
        wide.append(name)
check("у каждой страницы есть viewport", not no_viewport, no_viewport)
check("ни одна страница не требует широкого экрана", not wide, wide)
for page in ("results.html", "work.html", "more.html"):
    src = io.open(WEB / page, encoding="utf-8").read()
    check(f"{page} имеет правила под узкий экран", "max-width:760px" in src
          or "max-width:600px" in src or "minmax(" in src, page)

print(f"\nИТОГО: успешно {ok}, провалено {fail}")
sys.exit(1 if fail else 0)
