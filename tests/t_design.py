# -*- coding: utf-8 -*-
"""
Визуальная система VELOR — проверки, которые нельзя провести глазами.

Красоту тест не судит. Он охраняет ровно те решения, которые ломаются молча и
поодиночке: кто-то дописал `font-size:13.5px`, кто-то вернул сиреневый ореол,
кто-то завёл свою палитру на новой странице — и через месяц кабинет опять
выглядит как десять разных продуктов.

Источник правды — VELOR_DESIGN_SYSTEM.md. Если правило ниже устарело, сначала
правится документ, потом тест.
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


CSS = io.open(WEB / "velor.css", encoding="utf-8").read()
# Заглушки-редиректы и лендинг живут по своим правилам: у первых нет своего
# оформления вовсе, второй — маркетинговая страница, а не рабочий экран.
STUBS = {"tools.html", "integrations.html"}
LANDING = {"index.html"}
ALL = sorted(os.path.basename(p) for p in glob.glob(str(WEB / "*.html")))
CABINET = [n for n in ALL if n not in STUBS and n not in LANDING]


def styles(name):
    src = io.open(WEB / name, encoding="utf-8").read()
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", src, re.S))


def body(name):
    src = io.open(WEB / name, encoding="utf-8").read()
    src = re.sub(r"<style.*?</style>", "", src, flags=re.S)
    return src


print("== ОДНА СИСТЕМА НА ВЕСЬ ПРОДУКТ ==")
outside = [n for n in CABINET if "velor.css" not in io.open(WEB / n, encoding="utf-8").read()]
check("каждый экран подключает velor.css", not outside, outside)

own_tokens = []
for n in CABINET:
    st = styles(n)
    for m in re.finditer(r":root\s*\{([^}]*)\}", st):
        vars_ = re.findall(r"(--[a-z0-9-]+)\s*:", m.group(1))
        # Своя переменная страницы допустима, если её нет в общей системе:
        # это локальное имя, а не вторая палитра.
        clash = [v for v in vars_ if (v + ":") in CSS.replace(" ", "")]
        if clash:
            own_tokens.append((n, clash))
check("ни одна страница не переопределяет общие токены", not own_tokens, own_tokens)

scales = []
for n in CABINET:
    st = styles(n)
    if re.search(r"--(sp|fs|r)-[a-z0-9]+\s*:", st):
        scales.append(n)
check("шкалы отступов, кеглей и радиусов объявлены только в velor.css",
      not scales, scales)


print("\n== ШКАЛЫ ЗАКРЫТЫ ==")
# Дробные кегли — самый частый след «подогнал на глаз». В шкале их нет.
DRIFT = re.compile(r"font-size:\s*\d+\.\d+px")
drift = [(n, DRIFT.findall(styles(n))) for n in CABINET if DRIFT.search(styles(n))]
check("нет дробных размеров шрифта (13.5px и такого же рода)", not drift, drift)

bad_rad = []
for n in CABINET:
    for m in re.finditer(r"border-radius:\s*([^;}\n]+)", styles(n)):
        v = m.group(1).strip()
        if "var(" in v or "%" in v or v == "999px":
            continue
        nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)px", v)]
        # Мелкие радиусы (полоски прогресса, засечки) шкалы не касаются —
        # там 3–6px это физика элемента, а не решение о форме карточки.
        if nums and max(nums) > 6:
            bad_rad.append((n, v))
check("радиус берётся из шкалы", not bad_rad, bad_rad[:8])


print("\n== СВЕЧЕНИЕ И ГРАДИЕНТЫ ==")
glow = []
for n in CABINET:
    st = styles(n)
    for m in re.finditer(r"box-shadow:\s*0 0 \d+px[^;}]*", st):
        glow.append((n, m.group(0)[:50]))
    for m in re.finditer(r"drop-shadow\(0 0 \d+px[^)]*\)", st):
        glow.append((n, m.group(0)[:50]))
check("сиреневых ореолов нет ни на кнопках, ни на карточках", not glow, glow[:8])
check("в шкале теней нет свечения", "--glow-iris" not in CSS)

# Градиент допустим, только если у него есть работа. Разрешённых видов три:
#   затемнение под фиксированной шапкой (иначе текст страницы читается поверх
#   текста меню), шиммер скелета и направленная подсветка строки под курсором.
ALLOWED = (
    "linear-gradient(180deg,rgba(0,0,0,",       # затемнение под шапкой
    "linear-gradient(90deg,rgba(255,255,255,",  # шиммер
    "linear-gradient(90deg,transparent,rgba(255,255,255,",
    "linear-gradient(90deg,var(--tint-iris",    # подсветка строки
    "linear-gradient(90deg,rgba(128,82,255,",
    "linear-gradient(90deg,var(--surface)",
    # Метка «здесь говорит VELOR» слева от брифинга. Ровная линия во всю высоту
    # читается как рамка контейнера; линия, которая гаснет к низу, читается как
    # акцент при утверждении. Работа есть — значит, градиент допустим.
    "linear-gradient(tobottom,var(--iris",
)
stray = []
for n in CABINET:
    st = re.sub(r"\s+", "", styles(n))
    for m in re.finditer(r"(linear|radial|conic)-gradient\([^)]*", st):
        g = m.group(0)
        if not any(g.startswith(a.replace(" ", "")) for a in ALLOWED):
            stray.append((n, g[:60]))
check("градиент остался только там, где он работает", not stray, stray[:8])


print("\n== УКРАШЕНИЕ УБРАНО ==")
check("arina-core.js удалён: на него не ссылалась ни одна страница",
      not (WEB / "arina-core.js").exists())
users = [n for n in ALL if "ambientCanvas" in io.open(WEB / n, encoding="utf-8").read()]
check("поля летающих частиц нет ни на одной странице", not users, users)
check("частицы удалены вместе с кодом, а не отключены флагом",
      not (WEB / "core-live.js").exists())

# ЯДРО VELOR. Знак присутствия — не «ИИ-сфера»: он собран из тех же элементов,
# что и остальной кабинет, и весит столько, сколько весит инлайновый SVG.
STATE = io.open(WEB / "velor-state.js", encoding="utf-8").read()
check("трёхмерное ядро не вернулось", not (WEB / "core-live.js").exists()
      and "THREE" not in STATE and "three.min" not in STATE)
three = sorted(n for n in ALL if "three.min.js" in io.open(WEB / n, encoding="utf-8").read())
check("three.js не грузится ни на одном экране кабинета", three == ["index.html"], three)
check("знак — инлайновый SVG, а не картинка и не канвас",
      "<svg viewBox" in STATE and "canvas" not in STATE.lower())
cores = sorted(n for n in ALL if "data-velor-core" in io.open(WEB / n, encoding="utf-8").read())
check("знак присутствия стоит там, где у VELOR есть состояние",
      cores == ["dashboard.html", "home.html", "inbox.html", "work.html"], cores)
for n in cores:
    src = io.open(WEB / n, encoding="utf-8").read()
    # Форму и цвет читает не каждый: по ним нельзя отличить «думает» от «сбоя
    # связи». Слово обязано стоять рядом со знаком, а скрипт — быть подключён.
    # Знак никогда не стоит один: рядом либо строка состояния словом, либо
    # заголовок двери, который прямо называет, что произошло с данными.
    check(f"{n}: знак не стоит без слова",
          "data-velor-state" in src or "v-door-say" in src)
    check(f"{n}: знак подключён", "velor-state.js" in src)

# Семь состояний. Каждое обязано отличаться ФОРМОЙ — длиной нитей, разрывом,
# местом метки, — а не только цветом: иначе это раскраска, а не состояние.
flatc = CSS.replace(" ", "").replace(chr(10), "")
STATES = ["connected", "thinking", "processing", "writing", "found", "warning", "error"]
for st in STATES:
    check(f"состояние «{st}» описано в системе", '[data-core="%s"]' % st in flatc)
# Знак живёт непрерывно, и это осознанный допуск к §15. Значит, сторожить надо
# три вещи: движение действительно есть; оно остаётся в объявленном бюджете
# амплитуды; и оно останавливается ровно там, где остановка означает событие.
sig = {}
for st in STATES:
    parts = re.findall(r'\[data-core="%s"\][^{]*\{([^}]*)\}' % st, flatc)
    sig[st] = ";".join(parts)

for st in ("connected", "thinking", "processing", "writing"):
    check(f"в состоянии «{st}» знак не замирает", "animation:vc-" in sig[st])
for st in ("warning", "error"):
    # В знаке, который дышит всегда, остановка и есть сигнал.
    check(f"в состоянии «{st}» движение остановлено намеренно", "animation:none" in sig[st])

# Форма. Каждое состояние обязано выглядеть иначе даже без движения — иначе при
# prefers-reduced-motion семь состояний схлопнутся в одно.
statics = {}
for st in STATES:
    statics[st] = ";".join(sorted(re.findall(r"transform:[^;}]+", sig[st])))
distinct = [st for st in STATES if st != "connected"]
check("ни одно состояние не повторяет форму другого",
      len({statics[st] for st in distinct}) == len(distinct),
      {st: statics[st][:40] for st in distinct})
for st in ("thinking", "processing", "writing", "found", "warning", "error"):
    check(f"«{st}» отличается формой, а не только цветом", bool(statics[st]))

# Бюджет амплитуды покоя: ±1.5 из 24 единиц. Если однажды кто-то решит «сделать
# поживее», тест скажет об этом раньше, чем это увидит владелец.
drift = re.search(r"@keyframesvc-drift\{([^}]*)\}", flatc)
check("покой описан дрейфом, а не миганием", bool(drift))
amp = max(abs(float(x)) for x in re.findall(r"translateX\((-?[\d.]+)px\)", drift.group(1))) if drift else 99
check("амплитуда покоя остаётся в бюджете (<= 1.5 из 24)", amp <= 1.5, amp)

# Периоды дрейфа не кратны друг другу — иначе силуэт зациклится и движение
# начнёт читаться как анимация, а не как жизнь.
periods = re.findall(r"animation:vc-drift([\d.]+)s", sig["connected"])
check("у каждой полосы свой период", len(set(periods)) == 5, periods)

check("сбой перерезает строй, и это видно без цвета",
      '[data-core="error"].vc-gap{opacity:1;}' in flatc)

# Абстрактность — требование владельца: знак не изображает предмет. Ни рамки
# (читается как иконка), ни треугольника логотипа (читается как логотип).
check("знак не изображает предмет", "M12 2.5" not in STATE and "<circle" not in STATE)
check("рамки вокруг знака нет", "rx=\"6.5\"" not in STATE)

# prefers-reduced-motion: движение выключается, состояние остаётся. Форма
# задана статикой, анимация только добавляется сверху — значит, «без движения»
# не означает «без состояния».
rm = flatc.split("@media(prefers-reduced-motion:reduce)")
check("при выключенном движении знак не теряет состояние",
      any(".v-core*{animation:none!important" in b for b in rm[1:]))

check("состояние сотрудника осталось как компонент", ".v-pulse" in CSS)
for w in ("на связи", "думает", "пишет ответ", "разбирает", "нашёл",
          "нужна проверка", "сбой"):
    check(f"состояние «{w}» названо словом", w in STATE)
check("старые имена состояний продолжают работать",
      all(a in STATE for a in ("idle", "analyzing", "generating", "insight")))
check("старый публичный вызов VELOR_CORE сохранён",
      "window.VELOR_CORE" in STATE and "setState" in STATE and "insight" in STATE)
callers = [n for n in ALL if "VELOR_CORE" in io.open(WEB / n, encoding="utf-8").read()]
check("страницы, звавшие ядро, подключают его",
      all("velor-state.js" in io.open(WEB / n, encoding="utf-8").read() for n in callers),
      callers)
check("ядро описано в документе",
      "VELOR CORE" in io.open(ROOT / "VELOR_DESIGN_SYSTEM.md", encoding="utf-8").read())

# Зарубка — единственная декоративная мелочь в системе, и она же служебная:
# отмечает начало смыслового блока и текущий пункт меню. Одна форма на всё.
NAV = io.open(WEB / "nav.js", encoding="utf-8").read()
check("зарубка стоит перед метками разделов и чисел",
      ".v-kpi .k::before" in CSS and ".v-panel > h2::before" in CSS)
check("тот же приём отмечает текущий раздел меню", ".vn-lk.on::before" in NAV)
check("сиреневая таблетка активного пункта убрана",
      "'.vn-lk.on{ color:var(--bone); background:rgba(255,255,255,.07); }'" in NAV)

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-⛿]")
emo = [(n, "".join(sorted(set(EMOJI.findall(body(n)))))) for n in CABINET
       if EMOJI.search(body(n))]
check("эмодзи не работают иконками", not emo, emo)


print("\n== ДВИЖЕНИЕ НЕ ПРЯЧЕТ СОДЕРЖИМОЕ ==")
# `*{animation:none!important}` останавливает появление на первом кадре, и
# элемент с opacity:0 остаётся невидимым навсегда. Для всех, кто включил
# «уменьшить движение», это не «без анимации», а «без раздела».
reduced_block = ""
m = re.search(r"@media\(prefers-reduced-motion: reduce\)\s*\{([^}]*\{[^}]*\}[^}]*)\}", CSS)
if m:
    reduced_block = m.group(1)
reduced_all = "".join(re.findall(r"prefers-reduced-motion[^{]*\{(.*?)\n\}", CSS, re.S))
ghosts = []
for n in CABINET:
    st = styles(n)
    for m in re.finditer(r"\.([a-z0-9-]+)\s*\{([^}]*)\}", st):
        blk = m.group(2)
        if "opacity:0" not in blk.replace(" ", ""):
            continue
        if "animation:" not in blk or "animation:none" in blk.replace(" ", ""):
            continue
        cls = m.group(1)
        # Сброс может стоять и в velor.css (общий), и на самой странице.
        page_reset = re.search(
            r"prefers-reduced-motion.*?\.%s\b" % re.escape(cls), st, re.S)
        if ("." + cls) not in reduced_all and not page_reset:
            ghosts.append((n, "." + cls))
check("у каждого появления есть сброс для reduced-motion", not ghosts, ghosts)
check("velor.css возвращает содержимому видимость",
      "opacity:1 !important" in reduced_all)


print("\n== ONE DOOR ==")
INBOX = io.open(WEB / "inbox.html", encoding="utf-8").read()
DASH = io.open(WEB / "dashboard.html", encoding="utf-8").read()
check("состояния приёма описаны в системе, а не в странице",
      '.v-door[data-state="dragging"]' in CSS)
for st_name in ("empty", "dragging", "sending", "processing",
                "done", "review", "duplicate", "failed"):
    check(f"состояние «{st_name}» существует", st_name + ":" in INBOX or
          st_name + " " in INBOX)
check("приём — компонент системы", 'class="drop v-door"' in INBOX)
check("на главной та же дверь", 'class="onedoor v-door"' in DASH)
# Главное правило состояний: человек должен узнать не только «не вышло», но и
# что стало с тем, что он отдал.
fails = re.findall(r"door\('failed'[^;]*;", INBOX) + re.findall(r"state\('fail'[^;]*;", DASH)
bad = [f for f in fails if "не изменены" not in f and "DOOR" not in f]
check("каждое «не получилось» говорит, что данные не изменены", not bad, bad[:4])
check("шаблон отказа тоже это говорит", "Данные не изменены" in INBOX)
check("вход остался один: главная бьёт в тот же приёмник",
      "'/api/inbox'" in DASH and "/api/inbox/intake" in INBOX)


print("\n== КОМПОНЕНТЫ ==")
for role, sel in (("первичная", ".v-btn{"), ("вторичная", ".v-btn.ghost{"),
                  ("тихая", ".v-btn.quiet{"), ("опасная", ".v-btn.danger{"),
                  ("выключенная", ".v-btn:disabled{")):
    check(f"кнопка: роль «{role}» описана", sel in CSS.replace(" ", ""))
for comp in (".v-field", ".v-label", ".v-hint", ".v-err", ".v-tag",
             ".v-empty", ".v-fail", ".v-sk", ".v-kpi", ".v-panel", ".v-row"):
    check(f"компонент {comp} живёт в системе", comp in CSS)


print("\n== ИЕРАРХИЯ ==")
check("название страницы больше не витрина", "font-size:var(--fs-h1)" in CSS.replace(" ", ""))
huge = [n for n in CABINET
        if re.search(r"h1[^{]*\{[^}]*font-size:\s*clamp\([^)]*[5-9]\dpx", styles(n))]
check("ни одна страница не рисует заголовок в полэкрана", not huge, huge)
check("шкала кеглей объявлена целиком",
      all(t in CSS for t in ("--fs-micro", "--fs-caption", "--fs-sm", "--fs-body",
                             "--fs-base", "--fs-lead", "--fs-h3", "--fs-h2",
                             "--fs-h1", "--fs-display")))
check("нейтрали сведены, а не чистые",
      "--void:#08080b" in CSS.replace(" ", "") and "--bone:#f7f7fa" in CSS.replace(" ", ""))
check("акцент бренда не тронут", "--iris:#8052ff" in CSS.replace(" ", ""))
check("смысловые имена цвета есть",
      all(t in CSS for t in ("--ok:", "--warn:", "--bad:", "--info:")))


print("\n== ЦВЕТ РАБОТАЕТ СМЫСЛОМ ==")
# Экран без цвета читается как «ничего не происходит» — но цвет ради цвета
# запрещён тем же документом. Поэтому проверяем не наличие красок, а то, что
# язык состояния описан в системе и применён по данным, а не на глаз.
flat = CSS.replace(" ", "")
for cls in (".v-kpi.v.ok", ".v-kpi.v.bad", ".v-kpi.v.info", ".v-kpi.v.zero"):
    check("цифра умеет говорить состоянием: " + cls, cls in flat)
for t in ("--tint-ok:", "--tint-warn:", "--tint-bad:", "--tint-iris:"):
    check("подложка состояния " + t.strip(":") + " объявлена", t in flat)
check("бейдж состояния окрашен подложкой, а не только точкой",
      ".v-badge.good{color:var(--ink-verdant);background:var(--tint-ok);}" in flat)
DASH_SRC = io.open(WEB / "dashboard.html", encoding="utf-8").read()
check("главная красит числа по ключу метрики, а не наугад",
      "function kpiTone" in DASH_SRC)
# Деньги — янтарь, результат — зелёный. Пока выручка и прибыль были одного
# цвета, «пришло» не отличалось от «заработал»: цвет был раскраской, а не языком.
check("деньги говорят янтарём", ".v-kpi.v.money" in flat)
check("выручка перестала притворяться результатом", "' money'" in DASH_SRC)
check("зелёное осталось за результатом, а не за оборотом",
      "case 'profit':" in DASH_SRC and "' ok'" in DASH_SRC)
# Материал панели: одна световая грань сверху вместо стекла и градиента.
check("у поверхности есть верхняя грань", "--edge:inset01px0" in flat)
check("грань применена к панелям и карточкам чисел",
      flat.count("box-shadow:var(--edge)") >= 3)
for key in ("revenue", "expenses", "profit", "margin", "clients_new", "orders_new"):
    check("метрика " + key + " получила свой цвет", ("'" + key + "'") in DASH_SRC)

# Контраст поверхностей. Карточку должно быть видно на фоне страницы, а линия
# обязана делить: при .022 и .08 экран читался как сплошное чёрное поле.
def _alpha(token):
    m = re.search(re.escape(token) + r"rgba\(255,255,255,\.(\d+)\)", flat)
    return int((m.group(1) + "000")[:3]) if m else -1

check("карточка различима на фоне", _alpha("--surface:") >= 40, _alpha("--surface:"))
check("линия действительно делит", _alpha("--hairline:") >= 100, _alpha("--hairline:"))
check("лестница поверхностей осталась лестницей",
      _alpha("--surface:") < _alpha("--surface-2:") < _alpha("--surface-3:"),
      (_alpha("--surface:"), _alpha("--surface-2:"), _alpha("--surface-3:")))

print("\n== ГЛАВНАЯ: СНАЧАЛА СМЫСЛ, ПОТОМ ДАННЫЕ ==")
DASH = io.open(WEB / "dashboard.html", encoding="utf-8").read()
order = ["briefZone", "evidenceZone", "stateZone", "onedoor"]
pos = [DASH.index('id="%s"' % z) if ('id="%s"' % z) in DASH else DASH.index('class="%s' % z)
       for z in order]
check("порядок экрана: брифинг → доказательство → состояние → подробности",
      pos == sorted(pos), list(zip(order, pos)))
check("первым блоком идёт ответ, а не число",
      DASH.index('id="briefHead"') < DASH.index('id="kpi"'))
# Не литеральный clamp: правило звучит «ответ крупнее названия вкладки», и
# охранять надо его, а не конкретные пиксели.
_bh = re.search(r"\.brief-head\{[^}]*font-size:clamp\((\d+)px",
                DASH.replace(" ", "").replace("\n", ""))
_h1 = re.search(r"--fs-h1:(\d+)px", CSS.replace(" ", ""))
check("заголовок брифинга крупнее заголовка страницы",
      bool(_bh and _h1) and int(_bh.group(1)) > int(_h1.group(1)),
      (_bh and _bh.group(1), _h1 and _h1.group(1)))

# Сетки из шести одинаковых плиток больше нет: числа делит воздух и линия.
check("шесть одинаковых карточек убраны", 'class="v-kpi six"' not in DASH)
check("числа стоят открытой сеткой", ".figs{" in DASH.replace(" ", ""))
cards = DASH.count("border-radius:var(--r-lg)") + DASH.count("border-radius:var(--r-xl)")
check("карточек на главной стало меньше", cards <= 5, cards)

print("\n== ДОКАЗАТЕЛЬСТВО СТРОИТСЯ ИЗ НАСТОЯЩИХ ПОЛЕЙ ==")
# Цепочка не имеет права выдумывать шаги. Каждый её шаг берётся из поля,
# которое директор действительно возвращает.
for field in ("f.source", "f.detail", "f.title", "f.numbers", "f.href"):
    check(f"шаг цепочки берётся из {field}", field in DASH)
check("решение привязано к своему факту ключом, а не соседством",
      "'do_'" in DASH and "r.key.slice(3)" in DASH)
check("нет решения — шаг не рисуется", "else if (f.href)" in DASH)
check("цепочка раскрывается разметкой, а не скриптом",
      "<details class=\"ev\"" in DASH)

print("\n== ГРАФИК — ДОКАЗАТЕЛЬСТВО, А НЕ УКРАШЕНИЕ ==")
check("ряд запрашивается у сервера, а не рисуется из воздуха",
      "/api/series" in DASH)
check("линия молчит, когда движения меньше трёх дней",
      "vals.filter(v => v).length < 3" in DASH)
check("у маржи ряда нет намеренно",
      "SERIES_OF" in DASH and "margin" not in DASH.split("SERIES_OF")[1][:220])
check("спарклайн скрыт от скринридера: смысл несут число и источник",
      'class="spark ${tone}" viewBox="0 0 ${w} ${h}" aria-hidden="true"' in DASH)
check("у каждого числа осталась строка «как посчитано»",
      'class="src">${esc(m.source)}' in DASH)

print("\n== ОБНОВЛЕНИЕ ВИДНО, НО НЕ МЕШАЕТ ==")
check("изменившееся число помечается", ".fig .v.moved{" in DASH.replace(" ", "")
      or ".fig .v.moved{" in DASH)
check("новое число всплывает на место старого", "@keyframes figSwap" in DASH)
check("подмена укладывается в 400 мс", "animation:figSwap .4s" in DASH.replace("  ", " "))
check("отметка гаснет сама, а не мигает", "@keyframes figMark" in DASH)
check("состояние обновления названо словом", "обновлено только что" in DASH)
check("всплывающих окон при обновлении нет", "alert(" not in DASH)

print("\n== ЖИВОЕ ПОЛЕ: СОСТОЯНИЕ, А НЕ ОБОИ ==")
FIELD_JS = io.open(WEB / "velor-field.js", encoding="utf-8").read()
# Запреты проверяем по коду: в комментариях этого файла как раз объясняется,
# почему ни канваса, ни покадрового цикла здесь нет.
FIELD_CODE = "\n".join(l for l in FIELD_JS.splitlines()
                       if not l.strip().startswith("//"))

# Поле — часть кабинета, а не украшение одной страницы.
no_field = [n for n in CABINET
            if "nav.js" in io.open(WEB / n, encoding="utf-8").read()
            and "velor-field.js" not in io.open(WEB / n, encoding="utf-8").read()]
check("поле подключено на всех страницах кабинета", not no_field, no_field)

# Ни библиотеки, ни канваса, ни кадрового цикла. Эффект целиком композиторный:
# градиент растрируется один раз, дальше двигается только матрица слоя.
for banned in ("canvas", "THREE", "three.min", "requestAnimationFrame",
               "WebGL", "getContext"):
    check(f"поле обходится без {banned}", banned not in FIELD_CODE, banned)
check("поле не рисуется в JS покадрово", "setInterval" not in FIELD_CODE)

# Поле показывает состояние — оно не имеет права изображать данные.
for banned in ("particle", "star", "neural", "node", "dot", "network"):
    check(f"поле не притворяется данными: нет «{banned}»",
          banned not in FIELD_CODE.lower() and banned not in CSS.lower().split(
              "живое поле")[-1].split("body > footer")[0], banned)

# Четыре состояния, и каждое отличается ФОРМОЙ, а не только оттенком.
FIELD_CSS = CSS[CSS.index("ЖИВОЕ ПОЛЕ"):CSS.index("body > footer")]
for st in ("waiting", "risk", "opportunity"):
    blk = [m for m in re.findall(
        r'\.v-field\[data-field="%s"\][^{]*\{[^}]*\}' % st, FIELD_CSS)]
    check(f"состояние «{st}» задано", bool(blk), st)
    check(f"«{st}» меняет форму, а не только цвет",
          any("transform" in b for b in blk), [b[:60] for b in blk])
check("норма — база: отдельного правила ей не нужно",
      'data-field="normal"' not in FIELD_CSS)

# Смысловая примесь поднимается только настоящим выводом Директора.
check("примесь риска и возможности по умолчанию погашена",
      re.search(r"\.v-field \.vf-l > u\{[^}]*opacity:0", FIELD_CSS) is not None)
check("риск красит коралловым, возможность — бирюзовым",
      "u.risk{ background:radial-gradient" in FIELD_CSS
      and "255,107,107" in FIELD_CSS and "47,212,178" in FIELD_CSS)

# Три события, и «данные» и «вывод» идут в разные стороны: данные приходят и
# расходятся, вывод — сходится. Направление здесь несёт смысл.
check("событие «новые данные» расходится наружу",
      re.search(r"@keyframes vf-wave\{.*?scale\(\.45\).*?scale\(1\.35\)",
                FIELD_CSS, re.S) is not None)
check("событие «новый вывод» сходится внутрь",
      re.search(r"@keyframes vf-converge\{.*?scale\(1\.42\).*?scale\(\.72\)",
                FIELD_CSS, re.S) is not None)
check("раскрытая цепочка отвечает тише всех",
      "@keyframes vf-focus" in FIELD_CSS)
check("событие не накладывается само на себя",
      "if (!f || f.dataset.pulse) return;" in FIELD_JS)

# Периоды дрейфа не кратны друг другу — иначе кадр начинает повторяться и
# читается как зацикленная заставка.
periods = [float(x) for x in re.findall(r"animation:vf-d\d (\d+)s", FIELD_CSS)]
check("три слоя с разной скоростью", len(periods) == 3, periods)
check("периоды не кратны друг другу",
      all(max(a, b) % min(a, b) != 0 for i, a in enumerate(periods)
          for b in periods[i + 1:]), periods)
check("движение медленное: самый быстрый слой — десятки секунд",
      periods and min(periods) >= 30, periods)

# Поле не мешает работать.
check("поле не перехватывает нажатия", "pointer-events:none" in FIELD_CSS)
check("поле скрыто от скринридера", 'aria-hidden' in FIELD_JS)
check("содержимое всегда выше поля",
      "body main{" in CSS.replace("\n", " ") and "body > footer{ position:relative; z-index:1; }" in CSS)

# Меньше движения — не значит «плоско»: дрейф выключается, глубина остаётся.
rm = FIELD_CSS[FIELD_CSS.index("prefers-reduced-motion"):]
check("при reduce анимация поля выключена", "animation:none !important" in rm)
check("при reduce слои остаются на своих местах: transform задан базой",
      FIELD_CSS.count("transform:translate3d(0,0,0)") >= 2)

# На узком экране три объёма дают не глубину, а мутное пятно.
mob = FIELD_CSS[FIELD_CSS.index("max-width:700px"):]
check("на узком экране светлый слой снят", ".v-field .vf-l3{ display:none; }" in mob)
check("на узком экране пик приглушён", "--vf-mob:.72" in mob)
# Состояние и экран пишут в РАЗНЫЕ множители: селектор состояния тяжелее
# медиа-запроса, и общая переменная означала бы, что узкий экран не действует.
check("состояние и экран не спорят за одну переменную",
      "--vf-gain:calc(var(--vf-level) * var(--vf-mob))" in FIELD_CSS
      and "--vf-gain" not in FIELD_CSS.split("СОСТОЯНИЯ")[1])

# Цена поля — не «на глаз», а свойство кода: анимировать разрешено ровно
# transform и opacity. Всё остальное (width, height, top, left, filter,
# background-position) заставляет браузер считать раскладку или заново
# рисовать пиксели каждый кадр — на объёме в 124vw это и есть тормоза.
def _kf_props(css_block):
    out = set()
    for m in re.finditer(r"@keyframes\s+(vf-[\w-]+)\s*\{", css_block):
        i, depth = m.end(), 1
        while depth:
            depth += (css_block[i] == "{") - (css_block[i] == "}")
            i += 1
        out |= set(re.findall(r"([a-z-]+)\s*:", css_block[m.end():i - 1]))
    return out


_props = _kf_props(FIELD_CSS)
check("в кадрах поля двигаются только transform и opacity",
      _props <= {"transform", "opacity"}, sorted(_props))
check("кадры вообще есть", bool(_props))
check("переход состояния тоже дешёвый",
      not re.search(r"transition:(?![^;]*\b(?:opacity|transform)\b)[^;]*"
                    r"(width|height|top|left|filter|margin|padding)", FIELD_CSS))

print("\n== ПОЛЕ ГОВОРИТ ТО ЖЕ, ЧТО БРИФИНГ ==")
# Состояние поля выводится из настоящего разбора Директора: срочный риск,
# непустой список возможностей, неготовый Директор. Ни таймера, ни случайности.
check("состояние считается из ответа Директора", "function fieldState(dir)" in DASH)
check("риск — это риск уровня urgent у Директора",
      "r.level === 'urgent'" in DASH)
check("возможность — непустой список возможностей",
      "(dir.opportunities || []).length" in DASH)
check("нет данных — поле ждёт, а не изображает работу",
      "if (!dir || !dir.ready) return 'waiting'" in DASH)
check("сервер молчит — поле тоже",
      "VELOR_FIELD.state('waiting')" in DASH)
check("импульс поднимает изменившееся число, а не таймер",
      "decided || changed" in DASH)
check("новый вывод определяется по ключам рекомендаций",
      "function newDecision(dir)" in DASH and "r.key || r.title" in DASH)

print("\n== ГЛАВНАЯ: ТРИ ГЛАВНЫХ ЧИСЛА, ОСТАЛЬНЫЕ ВПОЛГОЛОСА ==")
check("деньги набраны в полную величину", "const MAJOR = { revenue:1, expenses:1, profit:1 }" in DASH)
check("остальные числа тише, но не спрятаны", ".fig.minor{" in DASH.replace(" ", "")
      or ".fig.minor{" in DASH)
check("ни одно число с сервера не выброшено",
      re.search(r"\(dir\.metrics \|\| \[\]\)\.map", DASH) is not None
      and re.search(r"dir\.metrics[^\n]*slice", DASH) is None)

print("\n== ПУТЬ, У КОТОРОГО ЕСТЬ КОНЕЦ ==")
INBOX = io.open(WEB / "inbox.html", encoding="utf-8").read()
check("нить доказательства обрывается на последнем шаге",
      ".ev-step:last-child::after" in DASH)
check("нить приёма обрывается на последней стадии",
      ".pipe-step:last-child::after" in INBOX)
check("последняя стадия отмечена узлом — материал сейчас здесь",
      ".pipe-step:last-child::before" in INBOX)
check("цепочка раскрывается за 250 мс лесенкой",
      "animation:evStep .25s" in DASH and "animation-delay:.04s" in DASH)

print("\n== ДОКУМЕНТ ==")
DOC = ROOT / "VELOR_DESIGN_SYSTEM.md"
check("VELOR_DESIGN_SYSTEM.md существует", DOC.exists())
if DOC.exists():
    d = io.open(DOC, encoding="utf-8").read()
    for part in ("Цвет", "Типографика", "Отступы", "Сетка", "Радиусы", "Тени",
                 "Компоненты", "Движение", "Доступность", "Запрещённые приёмы",
                 "One Door", "Пустые состояния", "Ошибки",
                 "Живое поле"):
        check(f"в документе есть раздел «{part}»", part in d)
    # Документ — источник правды. Именной допуск к правилу «постоянного
    # фонового движения нет» должен быть назван и ограничен прямо в нём,
    # иначе через месяц исключение станет разрешением.
    check("допуск живому полю назван и ограничен",
          "Именной допуск — живое поле" in d and "71 / 53 / 37" in d)
    check("состояния поля описаны таблицей",
          all(w in d for w in ("waiting", "normal", "risk", "opportunity")))

print(f"\nИТОГО: успешно {ok}, провалено {fail}")
sys.exit(1 if fail else 0)
