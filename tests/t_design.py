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

print("\n== ЖИВОЕ ПОЛЕ: ПРОСТРАНСТВО, А НЕ ОБОИ ==")
import math as _math

FIELD_JS = io.open(WEB / "velor-field.js", encoding="utf-8").read()
# Запреты проверяем по коду: в комментариях этого файла как раз объясняется,
# почему ни канваса, ни покадрового цикла здесь нет.
FIELD_CODE = "\n".join(l for l in FIELD_JS.splitlines()
                       if not l.strip().startswith("//"))
FIELD_ALL = CSS[CSS.index("ЖИВОЕ ПОЛЕ"):CSS.index("body > footer")]
FIELD_MOB = FIELD_ALL[FIELD_ALL.index("@media(max-width:700px)"):]
FIELD_CSS = FIELD_ALL[:FIELD_ALL.index("@media(max-width:700px)")]

# Поле — часть кабинета, а не украшение одной страницы.
no_field = [n for n in CABINET
            if "nav.js" in io.open(WEB / n, encoding="utf-8").read()
            and "velor-field.js" not in io.open(WEB / n, encoding="utf-8").read()]
check("поле подключено на всех страницах кабинета", not no_field, no_field)

# Ни библиотеки, ни канваса, ни кадрового цикла. Эффект целиком композиторный.
for banned in ("canvas", "THREE", "three.min", "requestAnimationFrame",
               "WebGL", "getContext"):
    check(f"поле обходится без {banned}", banned not in FIELD_CODE, banned)
check("поле не рисуется в JS покадрово", "setInterval" not in FIELD_CODE)

# Поле показывает состояние — оно не имеет права изображать данные.
for banned in ("particle", "star", "neural", "network"):
    check(f"поле не притворяется данными: нет «{banned}»",
          banned not in FIELD_CODE.lower() and banned not in FIELD_CSS.lower(), banned)

# Корень поля назывался .v-field — тем же именем, что и обёртка поля ввода в
# разделе «ФОРМА». Из-за этого фон получал `margin-bottom` от формы и был на
# 20px короче экрана: у fixed-элемента с inset:0 нижний отступ вычитается из
# высоты. Имена разведены.
check("корень поля не делит имя с компонентом формы",
      "'v-live'" in FIELD_JS and ".v-live{" in FIELD_CSS
      and ".v-field{" not in FIELD_CSS)

print("\n== ТРИ ОБЪЁМА, РАЗНЕСЁННЫХ ПО ГЛУБИНЕ ==")
# Прошлая редакция была сплошной светящейся массой во весь экран — и читалась
# как холодный переливающийся градиент, то есть как приём, а не как
# пространство. Свет собран в три отдельных объёма, и главное в них не
# яркость, а пустота между ними.
BOXRE = (r"\.vf-v(\d)\{\s*left:\s*(-?[\d.]+)vw;\s*top:\s*(-?[\d.]+)vh;\s*"
         r"width:\s*([\d.]+)vw;\s*height:\s*([\d.]+)vh;\s*\}")
BOX = {int(m.group(1)): tuple(float(x) for x in m.groups()[1:])
       for m in re.finditer(BOXRE, FIELD_CSS)}
check("объёмов ровно три", len(BOX) == 3 and set(BOX) == {1, 2, 3}, sorted(BOX))
check("каждый крупнее экрана — у объёма не должно быть видимой границы",
      all(g[2] >= 50 and g[3] >= 45 for g in BOX.values()),
      {i: (g[2], g[3]) for i, g in BOX.items()})
check("каждый частично уходит за край viewport",
      all(g[0] < 0 or g[1] < 0 or g[0] + g[2] > 100 or g[1] + g[3] > 100
          for g in BOX.values()), BOX)

CTR = {i: (g[0] + g[2] / 2, g[1] + g[3] / 2) for i, g in BOX.items()}
pairs = [(a, b) for a in CTR for b in CTR if a < b]
dist = {(a, b): _math.hypot(CTR[a][0] - CTR[b][0], CTR[a][1] - CTR[b][1]) for a, b in pairs}
check("центры объёмов разнесены далеко — иначе три массы сольются в одну",
      min(dist.values()) >= 55, {k: round(v) for k, v in dist.items()})

CORE = {}
for m in re.finditer(r"\.vf-v(\d) \.vf-o > i\.([abc])\{\s*left:\s*(-?[\d.]+)%;\s*top:\s*(-?[\d.]+)%;\s*"
                     r"width:\s*([\d.]+)%;\s*height:\s*([\d.]+)%;\s*\}", FIELD_CSS):
    CORE[(int(m.group(1)), m.group(2))] = tuple(float(x) for x in m.groups()[2:])
check("у каждого объёма три смещённых ядра", len(CORE) == 9, sorted(CORE))


def _core_centre(v, k):
    cl, ct, cw, ch = CORE[(v, k)]
    L, T, W, H = BOX[v]
    return (L + W * cl / 100 + W * cw / 200, T + H * ct / 100 + H * ch / 200)


# Три одинаковых круга — это не объёмы. Ядра должны стоять несимметрично:
# центр их совокупности заметно смещён относительно центра рамки объёма.
for v in (1, 2, 3):
    cs = [_core_centre(v, k) for k in "abc"]
    mx = sum(c[0] for c in cs) / 3 - CTR[v][0]
    my = sum(c[1] for c in cs) / 3 - CTR[v][1]
    off = _math.hypot(mx / BOX[v][2], my / BOX[v][3])
    check(f"объём {v} несимметричен — силуэт дольчатый, а не круглый",
          off >= .04, round(off, 3))
check("раскладка ядер у объёмов разная — не один силуэт под тремя поворотами",
      len({tuple(sorted(CORE[(v, k)] for k in "abc")) for v in (1, 2, 3)}) == 3)

# Между объёмами обязана оставаться пустота, и стоять она должна там, где
# читают: ни одно ядро не имеет права светить сердцевиной в середину экрана.
inside = [(v, k) for v in (1, 2, 3) for k in "abc"
          if 22 <= _core_centre(v, k)[0] <= 78 and 18 <= _core_centre(v, k)[1] <= 82]
check("ни одна сердцевина не стоит посреди экрана — там воздух", not inside, inside)

print("\n== ГЛУБИНА СДЕЛАНА ПРИЗНАКАМИ, А НЕ 3D ==")
DRIFT = {int(m.group(1)): tuple(float(x) for x in m.groups()[1:])
         for m in re.finditer(r"@keyframes vf-drift(\d)\{ 0%,100%\{ transform:translate3d\(\s*(-?[\d.]+)vw,\s*"
                              r"(-?[\d.]+)vh,0\); \}\s*50%\{ transform:translate3d\(\s*(-?[\d.]+)vw,\s*"
                              r"(-?[\d.]+)vh,0\); \} \}", FIELD_CSS)}
FORM = {int(m.group(1)): tuple(float(x) for x in m.groups()[1:])
        for m in re.finditer(r"@keyframes vf-form(\d)\{ 0%,100%\{ transform:scale\(([\d.]+),([\d.]+)\); \}\s*"
                             r"50%\{ transform:scale\(([\d.]+),([\d.]+)\); \} \}", FIELD_CSS)}
TURN = {int(m.group(1)): tuple(float(x) for x in m.groups()[1:])
        for m in re.finditer(r"@keyframes vf-turn(\d)\{ 0%,100%\{ transform:rotate\((-?[\d.]+)deg\); \}\s*"
                             r"50%\{ transform:rotate\((-?[\d.]+)deg\); \} \}", FIELD_CSS)}
GLOW = {int(m.group(1)): tuple(float(x) for x in m.groups()[1:])
        for m in re.finditer(r"@keyframes vf-glow(\d)\{ 0%,100%\{ opacity:([\d.]+); \}\s*"
                             r"50%\{ opacity:([\d.]+); \} \}", FIELD_CSS)}
check("движение всех объёмов читается из CSS",
      len(DRIFT) == len(FORM) == len(TURN) == len(GLOW) == 3,
      (sorted(DRIFT), sorted(FORM), sorted(TURN), sorted(GLOW)))

# 1. Параллакс. Дальний проходит по экрану заметно меньше ближнего — это и
#    есть ощущение расстояния, и оно бесплатное.
travel = {v: _math.hypot(DRIFT[v][2] - DRIFT[v][0], DRIFT[v][3] - DRIFT[v][1]) for v in DRIFT}
check("ходы объёмов различаются — иначе плоскость, а не глубина",
      len({round(t, 1) for t in travel.values()}) == 3, {v: round(t, 1) for v, t in travel.items()})
check("самый дальний идёт медленнее самого ближнего вдвое и более",
      max(travel.values()) >= 2 * min(travel.values()), {v: round(t, 1) for v, t in travel.items()})

STOP = {}
for m in re.finditer(r"\.vf-v(\d) \.vf-o > i\.([abc])\{ background:radial-gradient\(closest-side,\s*(.+?)\); \}",
                     FIELD_CSS, re.S):
    STOP[(int(m.group(1)), m.group(2))] = [
        (float(a.group(5)) / 100.0, float(a.group(4)),
         (int(a.group(1)), int(a.group(2)), int(a.group(3))))
        for a in re.finditer(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)\s+([\d.]+)%", m.group(3))]
check("цвет каждого ядра задан", len(STOP) == 9, sorted(STOP))

# 2. Размытость. Вторая остановка — «насколько далеко»: у дальнего спад самый
#    пологий. И у всех он мягкий: тугая кромка выдала бы фигуру, и объём снова
#    стал бы пятном (этому научила третья редакция поля).
soft = {v: STOP[(v, "a")][1][0] for v in (1, 2, 3)}
# Мягкость меряется не одной остановкой, а тем, ЧЕМ кончается объём: у него
# должен быть длинный тихий хвост, а не край. Тугая кромка выдала бы фигуру,
# и объём снова стал бы пятном — этому научила третья редакция поля.
tail = {v: STOP[(v, "a")][-2] for v in (1, 2, 3)}
check("свет гаснет длинным хвостом, а не кромкой",
      all(t[0] >= .6 for t in tail.values()), {v: t[0] for v, t in tail.items()})
check("и хвост действительно тихий — на краю объёма его почти нет",
      all(tail[v][1] <= STOP[(v, "a")][0][1] * .25 for v in (1, 2, 3)),
      {v: (tail[v][1], STOP[(v, "a")][0][1]) for v in (1, 2, 3)})
check("степень размытия у объёмов разная — это признак расстояния",
      len(set(soft.values())) == 3, soft)
check("дальний размыт сильнее ближнего", soft[1] > soft[3], soft)

# 3. Перекрытие. Порядок в разметке и есть порядок по глубине.
check("ближний написан поверх дальнего",
      FIELD_JS.index("volume(1") < FIELD_JS.index("volume(2") < FIELD_JS.index("volume(3"))

# Фильтр над анимируемым поддеревом заставил бы браузер каждый кадр заново
# растрировать и размывать полный экран. Тот же признак дают остановки.
check("размытие сделано градиентом, а не filter", "filter:" not in FIELD_CSS)

print("\n== ПАЛИТРА: ХОЛОДНАЯ ГЛУБИНА, А НЕ ЦВЕТ СОСТОЯНИЯ ==")
HUE = {v: STOP[(v, "a")][0][2] for v in (1, 2, 3)}
check("фон — глубокий, а не чистый чёрный", "--void:#08080b" in CSS.replace(" ", ""))
check("оттенки объёмов различаются", len(set(HUE.values())) == 3, HUE)
check("дальний — полночный синий: синего в нём больше красного",
      HUE[1][2] > HUE[1][0] * 1.5, HUE[1])
check("ближний — сиреневый: он светлее и теплее дальнего",
      HUE[3][0] > HUE[1][0] and sum(HUE[3]) > sum(HUE[1]), (HUE[1], HUE[3]))
check("в поле нет смысловых цветов продукта вне примесей",
      all(not (c[1] > c[0] and c[1] > c[2]) for c in HUE.values()), HUE)
# Внутри объёма свет уходит от сердцевины к краю в холод — так он читается как
# объём с источником, а не как ровно закрашенная область.
for v in (1, 2, 3):
    st = STOP[(v, "a")]
    check(f"внутри объёма {v} свет уходит от сердцевины к краю",
          sum(st[0][2]) > sum(st[-2][2]), (st[0][2], st[-2][2]))

print("\n== ЧЕТЫРЕ РИТМА НА ОБЪЁМ И ПРОСТЫЕ ПЕРИОДЫ ==")
PER = {}
for kind, pat in (("снос", r"\.vf-v(\d) \.vf-l\{ animation:vf-drift\d (\d+)s"),
                  ("форма", r"\.vf-v(\d) \.vf-t\{ animation:vf-form\d (\d+)s"),
                  ("поворот", r"\.vf-v(\d) \.vf-r\{ animation:vf-turn\d (\d+)s"),
                  ("свет", r"\.vf-v(\d) \.vf-o\{ animation:vf-glow\d (\d+)s")):
    for m in re.finditer(pat, FIELD_CSS):
        PER.setdefault(int(m.group(1)), {})[kind] = int(m.group(2))
check("у каждого объёма четыре независимых ритма",
      len(PER) == 3 and all(len(v) == 4 for v in PER.values()), PER)


def _prime(n):
    return n > 1 and all(n % k for k in range(2, int(n ** .5) + 1))


periods = sorted(v for d in PER.values() for v in d.values())
check("все периоды — простые числа, поэтому картина не повторяется",
      all(_prime(v) for v in periods), periods)
check("периоды не совпадают между собой", len(periods) == len(set(periods)), periods)
check("движение медленное: быстрейший ритм — под тридцать секунд",
      min(periods) >= 17, periods)
check("но не настолько, чтобы его не было видно", max(periods) <= 90, periods)

# Объёмы не едут строем: у каждого своё направление сноса и свой характер
# движения — один расширяется, другой ведёт вбок, третий сжимается.
dirs = {v: (DRIFT[v][2] > DRIFT[v][0], DRIFT[v][3] > DRIFT[v][1]) for v in DRIFT}
check("направления сноса не совпадают у всех троих", len(set(dirs.values())) >= 2, dirs)
growth = {v: (FORM[v][2] * FORM[v][3]) - (FORM[v][0] * FORM[v][1]) for v in FORM}
check("характер деформации разный: кто-то расширяется, кто-то сжимается",
      max(growth.values()) > 0 > min(growth.values()), {v: round(g, 2) for v, g in growth.items()})
check("поворот у каждого свой", len({TURN[v] for v in TURN}) == 3, TURN)

# Дыхание яркости: глубина нужна, чтобы состав света менялся, но верхняя
# граница — это максимум, а видит человек среднее. Минимумы подняты намеренно.
check("у каждого объёма своя волна яркости",
      len({GLOW[v][0] for v in GLOW}) == 3, GLOW)
check("волна заметная, а не мерцание",
      all(abs(a - b) >= .3 for a, b in GLOW.values()),
      {v: round(abs(a - b), 2) for v, (a, b) in GLOW.items()})
check("объёмы гаснут вразнобой, иначе гаснет всё разом",
      len({round(a, 2) for a, b in GLOW.values()}) == 3, GLOW)

print("\n== ВОЗДУХ: ГЛУБОКИЙ КОСМОС НЕ БЫВАЕТ ЧЁРНЫМ ==")
# Пока воздуха не было, половина экрана оставалась чистым --void, и поле
# читалось не как пространство, а как «три пятна на пустоте». Слой поднимает
# ПОЛ, а не потолок: под пиком объёмов он почти не считается, а там, где
# объёмов нет, разница между «чёрное» и «глубокое синее» видна сразу.
AIRM = re.search(r"\.vf-air\{[^}]*?linear-gradient\((\d+)deg,\s*(.+?)\); \}", FIELD_CSS, re.S)
check("воздух есть", AIRM is not None)
AIRA = float(AIRM.group(1))
AIRS = [(float(a.group(5)) / 100.0, float(a.group(4)),
         (int(a.group(1)), int(a.group(2)), int(a.group(3))))
        for a in re.finditer(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)\s+([\d.]+)%", AIRM.group(2))]
check("у воздуха нет нулевых остановок — чёрных мест на экране не остаётся",
      len(AIRS) >= 3 and all(a > 0 for _, a, _ in AIRS), [a for _, a, _ in AIRS])
check("воздух холодный: синего в нём больше красного",
      all(c[2] > c[0] for _, _, c in AIRS), [c for _, _, c in AIRS])
check("воздух заметно тише объёмов — он пол, а не источник",
      max(a for _, a, _ in AIRS) < min(STOP[(v, "a")][0][1] for v in (1, 2, 3)) * 1.4,
      (max(a for _, a, _ in AIRS), [STOP[(v, "a")][0][1] for v in (1, 2, 3)]))
check("воздух неподвижен",
      "animation" not in FIELD_CSS[FIELD_CSS.index(".vf-air{"):FIELD_CSS.index(".vf-air{") + 300])
# Дымка стоит между глазом и сценой, а не за ней. Порядок не косметический:
# под колодцем воздух вычитался из середины экрана, и там появлялось тёмное
# пятно — колодец должен снимать свет объёмов, а не пол.
check("воздух написан поверх объёмов и колодца",
      FIELD_JS.index("vf-air") > FIELD_JS.index("vf-well"))

print("\n== КОЛОДЕЦ: ПРОСТРАНСТВО ВОКРУГ ТЕКСТА ==")
# Не «затемнение ради контраста», а то самое пространство вокруг содержимого:
# свет проходит рядом с текстом и частично за ним, но источник — никогда под
# ним. Благодаря колодцу свет по краям можно держать заметно ярче.
WELLRE = (r"radial-gradient\(ellipse ([\d.]+)% ([\d.]+)% at ([\d.]+)% ([\d.]+)%,\s*(.+?)\); \}")
_w = re.search(r"\.vf-well\{(.+?)\n", FIELD_CSS, re.S)
check("колодец есть", _w is not None)
_wm = re.search(WELLRE, FIELD_CSS, re.S)
check("колодец стоит над колонкой текста", _wm is not None
      and 40 <= float(_wm.group(3)) <= 60, _wm.group(3) if _wm else None)
_wstops = [float(a.group(1)) for a in re.finditer(r"rgba\(8,8,11,([\d.]+)\)", _wm.group(5))]
check("в середине колодец глубже, чем по краю",
      _wstops == sorted(_wstops, reverse=True) and _wstops[0] > _wstops[-1], _wstops)
check("колодец неподвижен: это пространство, а не эффект",
      "animation" not in FIELD_CSS[FIELD_CSS.index(".vf-well{"):
                                   FIELD_CSS.index(".vf-well{") + 400])

print("\n== БАЗОВОЕ СОСТОЯНИЕ ЗАДАЁТ ФОРМУ ПОЛЯ ==")
# Четыре состояния должны различаться КОМПОЗИЦИЕЙ: разошлись / широко /
# сошлись / повело в сторону. Цвет только помогает это почувствовать —
# закрыв цвет, состояние всё равно надо понимать.
ST = {}
for st in ("waiting", "risk", "opportunity"):
    ST[st] = {int(m.group(1)): (float(m.group(2)), float(m.group(3)))
              for m in re.finditer(r'\.v-live\[data-field="%s"\] \.vf-v(\d)\{ '
                                   r'transform:translate\(\s*(-?[\d.]+)%%,\s*(-?[\d.]+)%%\)' % st,
                                   FIELD_CSS)}
    check(f"состояние «{st}» двигает все три объёма", len(ST[st]) == 3, ST[st])
check("норма — база: отдельного правила ей не нужно",
      'data-field="normal"' not in FIELD_CSS)


def _shift(st, v):
    """Куда уходит объём в vw/vh — проценты считаются от его же рамки."""
    tx, ty = ST[st][v]
    return BOX[v][2] * tx / 100, BOX[v][3] * ty / 100


def _toward_centre(st, v):
    """Положительное — объём пошёл к середине экрана, отрицательное — от неё."""
    dx, dy = _shift(st, v)
    cx, cy = CTR[v]
    было = _math.hypot(cx - 50, cy - 50)
    стало = _math.hypot(cx + dx - 50, cy + dy - 50)
    return было - стало


check("ЖДУ: объёмы расходятся от центра — пространство пустеет",
      all(_toward_centre("waiting", v) < 0 for v in (1, 2, 3)),
      {v: round(_toward_centre("waiting", v), 1) for v in (1, 2, 3)})
check("РИСК: объёмы сходятся к центру — кадр становится собраннее",
      all(_toward_centre("risk", v) > 0 for v in (1, 2, 3)),
      {v: round(_toward_centre("risk", v), 1) for v in (1, 2, 3)})
_opp = [_shift("opportunity", v) for v in (1, 2, 3)]
check("ВОЗМОЖНОСТЬ: все три ведёт в одну сторону — появилась точка притяжения",
      len({(x > 0, y > 0) for x, y in _opp}) == 1, _opp)
check("и вместе с направлением поле вытягивается вдоль него",
      len(re.findall(r'\.v-live\[data-field="opportunity"\] \.vf-v\d\{ [^}]*scale\(1\.\d+,\s*\.\d+\)',
                     FIELD_CSS)) == 3)
# Разрежено и стянуто — разные вещи не только по направлению, но и по яркости.
LEV = {m.group(1): float(m.group(2))
       for m in re.finditer(r'\.v-live\[data-field="(\w+)"\]\{ --vf-level:([\d.]+); \}', FIELD_CSS)}
check("у ЖДУ света меньше, у РИСКА — больше нормы",
      LEV.get("waiting", 1) < 1 < LEV.get("risk", 1), LEV)
check("но ЖДУ не выключено: глубина остаётся", LEV.get("waiting", 0) >= .5, LEV)

print("\n== СИГНАЛЫ БИЗНЕСА — ОТДЕЛЬНЫЙ УРОВЕНЬ ==")
# Базовое состояние задаёт форму, сигнал — местное поведение и примесь.
# Их нельзя смешивать: сигналов может действовать несколько сразу и при любой
# форме, а состояние в каждый момент одно.
check("примеси по умолчанию погашены",
      re.search(r"\.v-live \.vf-o > u\{ opacity:0", FIELD_CSS) is not None)
check("риск красит коралловым, возможность — бирюзовым",
      "u.risk{ background:radial-gradient" in FIELD_CSS
      and "255,107,107" in FIELD_CSS and "47,212,178" in FIELD_CSS)
check("примеси разведены по разным объёмам — иначе два цвета смешаются в грязь",
      ".vf-v2 .vf-o > u.risk{" in FIELD_CSS and ".vf-v3 .vf-o > u.opp{" in FIELD_CSS)
# У объёма, чей центр за краем viewport, примесь «по центру» просто не
# показалась бы — поэтому у неё своя рамка внутри объёма.
TINT = {m.group(2): (int(m.group(1)), tuple(float(x) for x in m.groups()[2:]))
        for m in re.finditer(r"\.vf-v(\d) \.vf-o > u\.(risk|opp)\{\s*left:\s*(-?[\d.]+)%;\s*"
                             r"top:\s*(-?[\d.]+)%;\s*width:\s*([\d.]+)%;\s*height:\s*([\d.]+)%;\s*\}",
                             FIELD_CSS)}
check("у каждой примеси своя рамка внутри объёма", len(TINT) == 2, sorted(TINT))
for _k, (_v, (_l, _t, _w, _h)) in TINT.items():
    _cx = BOX[_v][0] + BOX[_v][2] * _l / 100 + BOX[_v][2] * _w / 200
    _cy = BOX[_v][1] + BOX[_v][3] * _t / 100 + BOX[_v][3] * _h / 200
    check(f"примесь «{_k}» стоит в видимой части объёма, а не за краем экрана",
          -10 <= _cx <= 110 and -10 <= _cy <= 110, (round(_cx), round(_cy)))

# ── НЕОТВЕЧЕННЫЕ ОБРАЩЕНИЯ ──
AMB = {m.group(1): float(m.group(2))
       for m in re.finditer(r'\.v-live\[data-leads="(\w+)"\]\{\s*--vf-amber:([\d.]+); \}', FIELD_CSS)}
check("у янтарного сигнала три ступени силы", len(AMB) == 3, AMB)
check("сила растёт с числом обращений",
      AMB.get("few", 1) < AMB.get("many", 0) < AMB.get("heavy", 0), AMB)
check("при нуле обращений янтаря нет вовсе: по умолчанию сигнал выключен",
      re.search(r"--vf-amber:\s*0;", FIELD_CSS) is not None
      and "if (n <= 0) return '';" in FIELD_JS)
check("янтарь гаснет плавно, а не пропадает",
      "transition:opacity 2.4s" in FIELD_CSS.split(".vf-lead > i{")[1].split("}")[0])
_leadrules = re.findall(r'[^\n]*data-leads[^\n]*', FIELD_CSS)
check("сигнал ложится поверх ЛЮБОЙ формы: в его правилах нет состояния",
      len(_leadrules) >= 4 and all("data-field" not in r for r in _leadrules), _leadrules)
_leadw = re.findall(r"\.vf-lead\{[^}]*?width:\s*([\d.]+)vw", FIELD_CSS)
check("янтарь — местное скопление, а не заливка экрана",
      len(_leadw) == 1 and float(_leadw[0]) <= 55, _leadw)
check("и он живёт: медленно ходит и дышит",
      "vf-leadrift" in FIELD_CSS and "vf-leadbeat" in FIELD_CSS)
check("при сигнале поле тихо тянет к месту, которое ждёт ответа",
      re.search(r'\.v-live\[data-leads\] \.vf-v2 \.vf-o\{ transform:', FIELD_CSS) is not None)

# В коде уровни тоже должны быть разными сущностями, а не одним словарём.
check("состояние и сигнал — разные функции поля",
      "function state(name)" in FIELD_JS and "function signal(name, value)" in FIELD_JS)
check("и хранятся раздельно, чтобы переживать переход между страницами",
      "velor_field_state" in FIELD_JS and "velor_field_leads" in FIELD_JS)
check("ступени считает поле, а не страница", "function leadTier" in FIELD_JS)
check("ответили последнему — сигнал снимается сам",
      "delete f.dataset.leads" in FIELD_JS)

print("\n== СОБЫТИЯ: ПЯТЬ, И У КАЖДОГО СВОЁ НАПРАВЛЕНИЕ ==")
check("приход клиента — отдельное событие", "@keyframes vf-arrive" in FIELD_CSS)
check("и оно бирюзовое: в языке продукта это «получилось»",
      re.search(r"\.vf-pulse > u\.ok\{ background:radial-gradient\(closest-side,\s*"
                r"rgba\(47,212,178", FIELD_CSS) is not None)
check("на приходе клиента сиреневая волна гасится — цвет не смешивается",
      '.v-live[data-pulse="client"] .vf-pulse > i{ opacity:0; }' in FIELD_CSS)
_ok_alphas = [float("0." + x) for x in
              re.findall(r"rgba\(47,212,178,\.(\d+)\)",
                         FIELD_CSS.split("u.ok{")[1].split("}")[0])]
check("зелёная волна ярче прочих: это то, ради чего поднимают глаза",
      _ok_alphas and max(_ok_alphas) >= 0.30, _ok_alphas)
check("событие «новые данные» расходится наружу",
      re.search(r"@keyframes vf-wave\{.*?scale\(\.45\).*?scale\(1\.35\)",
                FIELD_CSS, re.S) is not None)
check("событие «новый вывод» сходится внутрь",
      re.search(r"@keyframes vf-converge\{.*?scale\(1\.42\).*?scale\(\.72\)",
                FIELD_CSS, re.S) is not None)
check("раскрытая цепочка отвечает тише всех", "@keyframes vf-focus" in FIELD_CSS)
# Пришло обращение без ответа — событие МЕСТНОЕ: волна из центра экрана
# сказала бы «в системе что-то произошло», а произошло в конкретном месте.
check("обращение без ответа отвечает местно, а не во весь экран",
      '.v-live[data-pulse="lead"] .vf-lead > u{' in FIELD_CSS
      and '[data-pulse="lead"] .vf-pulse' not in FIELD_CSS)
check("и кольцо расширяется чуть-чуть, а не через весь кадр",
      re.search(r"@keyframes vf-notice\{.*?scale\(\.40\).*?scale\(1\.02\)",
                FIELD_CSS, re.S) is not None)
check("событие не накладывается само на себя",
      "if (!f || f.dataset.pulse) return;" in FIELD_JS)
check("клиент известен как событие поля", "client: 1" in FIELD_JS)
check("и обращение без ответа тоже", "lead: 1" in FIELD_JS)
# Приход человека — причина, вывод Директора — следствие. Показывать причину
# полезнее, поэтому у неё старшинство: клиент → вывод → просто новое число.
check("приход клиента старше прочих событий",
      DASH.index("if (arrived)") < DASH.index("else if (decided)")
      < DASH.index("else if (changed)"))
check("считается рост, а не любое изменение", "now > _prevClients" in DASH)
check("считается СКОЛЬКО пришло, а не «пришёл ли»",
      "now - _prevClients" in DASH and "function clientsArrived" in DASH)
check("на каждого поднимается своя волна", "VELOR_FIELD.arrivals(arrived)" in DASH)
check("очередь волн есть в поле", "function arrivals" in FIELD_JS and "function runWave" in FIELD_JS)
_wave = int(re.search(r"var WAVE = (\d+)", FIELD_JS).group(1))
_gap = int(re.search(r"GAP = (\d+)", FIELD_JS).group(1))
_anim = float(re.search(r'data-pulse="client"\] \.vf-pulse\{ animation:vf-arrive ([\d.]+)s', FIELD_CSS).group(1))
check("волна снимается позже, чем кончается её анимация",
      _wave >= _anim * 1000, (_wave, _anim))
check("между волнами есть пауза на перезапуск анимации", _gap >= 100, _gap)
check("у очереди есть потолок", re.search(r"MAX_WAVES = (\d)", FIELD_JS) is not None)
check("потолок разумный", 3 <= int(re.search(r"MAX_WAVES = (\d)", FIELD_JS).group(1)) <= 6)
check("берётся общее число клиентов, а не окно за 30 дней",
      "d.clients.total" in DASH.replace(" ", "") or "clients && d.clients.total" in DASH)

print("\n== ПОЛЕ НЕ МЕШАЕТ РАБОТАТЬ ==")
check("поле не перехватывает нажатия", "pointer-events:none" in FIELD_CSS)
check("поле скрыто от скринридера", "aria-hidden" in FIELD_JS)
check("содержимое всегда выше поля",
      "body main{" in CSS.replace("\n", " ")
      and "body > footer{ position:relative; z-index:1; }" in CSS)


def _kf_props(block):
    out = set()
    for m in re.finditer(r"@keyframes\s+(vf-[\w-]+)\s*\{", block):
        i, depth = m.end(), 1
        while depth:
            depth += (block[i] == "{") - (block[i] == "}")
            i += 1
        out |= set(re.findall(r"([a-z-]+)\s*:", block[m.end():i - 1]))
    return out


_props = _kf_props(FIELD_CSS)
check("в кадрах поля двигаются только transform и opacity",
      _props <= {"transform", "opacity"}, sorted(_props))
check("кадры вообще есть", bool(_props))

rm = FIELD_ALL[FIELD_ALL.index("prefers-reduced-motion"):]
check("при reduce движение выключено", "animation:none !important" in rm)
# Форма состояний задана ПЕРЕХОДОМ на .vf-v, а не анимацией, поэтому она
# переживает выключение движения: человек по-прежнему видит, что происходит.
_stopped = rm[:rm.index("animation:none !important")]
check("при reduce состояние всё равно читается формой",
      ".vf-v{" not in _stopped and ".vf-v," not in _stopped and " .vf-v " not in _stopped
      and "transition:transform 1.8s" in FIELD_CSS, _stopped[-160:])
check("при reduce объёмам розданы статичные веса", rm.count(".vf-o{ opacity:") >= 3,
      rm.count(".vf-o{ opacity:"))

check("на узком экране пик приглушён",
      re.search(r"--vf-mob:\s*\.(\d+)", FIELD_MOB) is not None, FIELD_MOB[:120])
MOBBOX = {int(m.group(1)): tuple(float(x) for x in m.groups()[1:])
          for m in re.finditer(BOXRE, FIELD_MOB)}
check("на узком экране объёмов по-прежнему три: снять объём значит снять глубину",
      len(MOBBOX) == 3 and "display:none" not in FIELD_MOB, sorted(MOBBOX))
check("но света на них меньше",
      all(MOBBOX[v][2] * MOBBOX[v][3] < BOX[v][2] * BOX[v][3] for v in (1, 2, 3)),
      {v: (MOBBOX[v][2:], BOX[v][2:]) for v in (1, 2, 3)})
_mw = re.search(WELLRE, FIELD_MOB, re.S)
check("а колодец шире: колонка текста здесь во весь экран",
      float(_mw.group(1)) > float(_wm.group(1)), (_mw.group(1), _wm.group(1)))

print("\n== ЯРКОСТЬ ПОЛЯ ОГРАНИЧЕНА КОНТРАСТОМ, А НЕ ВКУСОМ ==")
# Под самой светлой точкой поля лежит текст, и он обязан оставаться читаемым.
# Считаем не «сумму альф», а верхнюю границу того, что бывает на экране: у
# каждого объёма свои независимые периоды, поэтому в какой-то момент он может
# оказаться в самой выгодной для этой точки фазе. Берём максимум по фазам
# объёма и складываем объёмы — честная верхняя оценка, а не выдуманное
# «все сердцевины в одной точке».
LSTOP = [(float(a.group(5)) / 100.0, float(a.group(4)), (int(a.group(1)), int(a.group(2)), int(a.group(3))))
         for a in re.finditer(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)\s+([\d.]+)%",
                              FIELD_CSS.split(".vf-lead > i{")[1].split("}")[0])]
_beat = FIELD_CSS.split("@keyframes vf-leadbeat{")[1].split("}}")[0]
LBEAT = max(float(x) for x in re.findall(r"scale\(([\d.]+)\)", _beat))
LEADRE = (r"\.vf-lead\{[^}]*?left:\s*(-?[\d.]+)vw;\s*top:\s*(-?[\d.]+)vh;\s*"
          r"width:\s*([\d.]+)vw;\s*height:\s*([\d.]+)vh;")


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _over(fg, a, bg):
    return tuple(bg[i] + (fg[i] - bg[i]) * a for i in range(3))


def _token(name):
    return _hex(re.search(r"--%s:\s*(#[0-9a-fA-F]{6})" % name, CSS).group(1))


def _mix(a, b, t):
    return a + (b - a) * t


def _ez(t):
    u = 0.5 - 0.5 * _math.cos(2 * _math.pi * t)
    return u * u * (3 - 2 * u)


def _grad(stops, d):
    if d >= 1:
        return 0.0, (0, 0, 0)
    for k in range(1, len(stops)):
        if d <= stops[k][0]:
            p0, a0, c0 = stops[k - 1]
            p1, a1, c1 = stops[k]
            f = 0 if p1 == p0 else (d - p0) / (p1 - p0)
            return _mix(a0, a1, f), tuple(_mix(c0[j], c1[j], f) for j in range(3))
    return 0.0, (0, 0, 0)


_void, _bone, _ash = _token("void"), _token("bone"), _token("ash")


def _air(px, py, VW, VH):
    """Доля вдоль оси линейной растяжки по правилам CSS (0deg — вверх)."""
    th = _math.radians(AIRA)
    ux, uy = _math.sin(th), -_math.cos(th)
    L = abs(VW * ux) + abs(VH * uy)
    t = min(1.0, max(0.0, ((px - VW / 2) * ux + (py - VH / 2) * uy) / L + .5))
    for k in range(1, len(AIRS)):
        if t <= AIRS[k][0]:
            p0, a0, c0 = AIRS[k - 1]
            p1, a1, c1 = AIRS[k]
            f = 0 if p1 == p0 else (t - p0) / (p1 - p0)
            return _mix(a0, a1, f), tuple(_mix(c0[j], c1[j], f) for j in range(3))
    return AIRS[-1][1], AIRS[-1][2]


def _peak(block, box, mob, VW, VH, col0, col1):
    """Самая светлая точка экрана и самая светлая точка под главным ответом."""
    lead = tuple(float(x) for x in re.search(LEADRE, block).groups())
    wm = re.search(WELLRE, block, re.S)
    wr = tuple(float(wm.group(i)) / 100 for i in (1, 2, 3, 4))
    wstop = [(float(a.group(2)) / 100.0, float(a.group(1)))
             for a in re.finditer(r"rgba\(8,8,11,([\d.]+)\)\s+([\d.]+)%", wm.group(5))]
    P, PH = 4, [i / 4 for i in range(4)]
    best_all = best_txt = (0.0, None, None)
    for yi in range(17):
        py = yi * VH / 16
        for xi in range(23):
            px = xi * VW / 22
            col = _void
            for v in (1, 2, 3):
                L, T, W, H = box[v]
                left, top, w, h = L * VW / 100, T * VH / 100, W * VW / 100, H * VH / 100
                cx, cy = left + w / 2, top + h / 2
                bestset, besta = [], -1.0
                for pf in PH:
                    dx = _mix(DRIFT[v][0], DRIFT[v][2], _ez(pf)) * VW / 100
                    dy = _mix(DRIFT[v][1], DRIFT[v][3], _ez(pf)) * VH / 100
                    for pz in PH:
                        sx = _mix(FORM[v][0], FORM[v][2], _ez(pz))
                        sy = _mix(FORM[v][1], FORM[v][3], _ez(pz))
                        for pr in PH:
                            th = _math.radians(_mix(TURN[v][0], TURN[v][1], _ez(pr)))
                            qx, qy = px - dx, py - dy
                            qx, qy = cx + (qx - cx) / sx, cy + (qy - cy) / sy
                            ux, uy = qx - cx, qy - cy
                            lx = cx + ux * _math.cos(-th) - uy * _math.sin(-th)
                            ly = cy + ux * _math.sin(-th) + uy * _math.cos(-th)
                            for pg in PH:
                                g = _mix(GLOW[v][0], GLOW[v][1], _ez(pg)) * mob
                                acc, tot = [], 0.0
                                for k in "abc":
                                    cl, ct, cw, ch = CORE[(v, k)]
                                    ex = left + cl * w / 100 + cw * w / 200
                                    ey = top + ct * h / 100 + ch * h / 200
                                    d = _math.hypot((lx - ex) / (cw * w / 200),
                                                    (ly - ey) / (ch * h / 200))
                                    a, c = _grad(STOP[(v, k)], d)
                                    if a > 0:
                                        acc.append((a * g, c))
                                        tot += a * g
                                if tot > besta:
                                    besta, bestset = tot, acc
                for a, c in bestset:
                    col = _over(c, a, col)
            # янтарь в полную силу — сигнал «много обращений»
            lcx = lead[0] * VW / 100 + lead[2] * VW / 200
            lcy = lead[1] * VH / 100 + lead[3] * VH / 200
            d = _math.hypot((px - lcx) / (lead[2] * VW / 200 * LBEAT),
                            (py - lcy) / (lead[3] * VH / 200 * LBEAT))
            a, c = _grad(LSTOP, d)
            if a > 0:
                col = _over(c, a * mob, col)
            dw = _math.hypot((px / VW - wr[2]) / wr[0], (py / VH - wr[3]) / wr[1])
            aw = 0.0
            for k in range(1, len(wstop)):
                if dw <= wstop[k][0]:
                    p0, a0 = wstop[k - 1]
                    p1, a1 = wstop[k]
                    aw = _mix(a0, a1, 0 if p1 == p0 else (dw - p0) / (p1 - p0))
                    break
            if aw > 0:
                col = _over((8, 8, 11), aw, col)
            # Дымка — последней: она между глазом и сценой.
            _aa, _ac = _air(px, py, VW, VH)
            col = _over(_ac, _aa * mob, col)
            Lv = _lum(col)
            if Lv > best_all[0]:
                best_all = (Lv, tuple(round(x) for x in col), (round(px), round(py)))
            # Полоса главного ответа: там стоит крупный вывод и строка
            # «как посчитано» — ровно то, что колодец обязан беречь.
            if col0 <= px <= col1 and 109 <= py <= 509 and Lv > best_txt[0]:
                best_txt = (Lv, tuple(round(x) for x in col), (round(px), round(py)))
    return best_all, best_txt


_mob = float(re.search(r"--vf-mob:\s*(\.\d+)", FIELD_MOB).group(1))
_wide_all, _wide_txt = _peak(FIELD_CSS, BOX, 1.0, 1440.0, 900.0, 160.0, 1280.0)
_mob_all, _mob_txt = _peak(FIELD_MOB, MOBBOX, _mob, 375.0, 812.0, 18.0, 357.0)

for _name, _pk, _need in (("широкий экран", _wide_all, 4.6), ("телефон", _mob_all, 4.6)):
    _main = (_lum(_bone) + .05) / (_pk[0] + .05)
    _quiet = (_lum(_ash) + .05) / (_pk[0] + .05)
    check(f"{_name}: самая светлая точка поля rgb{_pk[1]} в {_pk[2]}", _pk[1] is not None)
    check(f"{_name}: основной текст поверх неё — {_main:.2f}:1 (нужно 7)", _main >= 7, round(_main, 2))
    check(f"{_name}: самый тихий текст — {_quiet:.2f}:1 (нужно {_need})", _quiet >= _need, round(_quiet, 2))

# Колодец существует ровно ради этого: под главным ответом должно быть
# заметно спокойнее, чем в самой светлой точке экрана. Если разница пропала —
# колодец сломан, и яркость поля придётся снимать.
_edge = (_lum(_ash) + .05) / (_wide_all[0] + .05)
_inner = (_lum(_ash) + .05) / (_wide_txt[0] + .05)
check(f"под главным ответом спокойнее, чем в самом светлом месте: "
      f"{_inner:.2f}:1 против {_edge:.2f}:1",
      _inner >= _edge * 1.05, (round(_inner, 2), round(_edge, 2)))
# Поле выкручено близко к границе намеренно: заметная прибавка яркости уронит
# проверку выше, и потолок здесь — «стало нечитаемо», а не «некрасиво».
check("поле стоит у границы, а не далеко от неё", _edge < 7, round(_edge, 2))

print("\n== КОЛЬЦА СОБЫТИЙ НЕ ГАСЯТ ТЕКСТ ==")
# События длятся секунды, но и это не повод класть светлое поверх читаемого.
# Считаем худший момент: самая яркая полоса кольца поверх самой яркой точки
# поля. Крупный текст — заголовок и числа — обязан остаться читаемым.
for _name, _sel, _rgb, _min in (("зелёная волна клиента", "u.ok{", (47, 212, 178), 4.5),
                                ("янтарное кольцо обращения", ".vf-lead > u{", (255, 184, 41), 4.5)):
    _band = max(float(x) for x in re.findall(r"rgba\(\d+,\d+,\d+,\.(\d+)\)",
                                             FIELD_CSS.split(_sel)[1].split("}")[0]))
    _band = _band / 100.0
    _under = _over(_rgb, _band, _wide_all[1])
    _c = (_lum(_bone) + .05) / (_lum(_under) + .05)
    check(f"{_name}: крупный текст под ней — {_c:.2f}:1 (нужно {_min})", _c >= _min, round(_c, 2))
# Кольцо, а не заливка: в середине прозрачно, иначе строка под ним гасла бы
# на всю волну, а не на мгновение прохода полосы.
check("волна клиента — кольцо: центр прозрачен",
      re.search(r"u\.ok\{ background:radial-gradient\(closest-side,\s*rgba\(47,212,178,0\) 0%",
                FIELD_CSS) is not None)
check("янтарное кольцо тоже пустое в середине",
      re.search(r"\.vf-lead > u\{[^}]*?rgba\(255,184,41,0\) 0%", FIELD_CSS, re.S) is not None)

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
check("импульс поднимает событие, а не таймер",
      "if (arrived)" in DASH and "else if (decided)" in DASH
      and "else if (changed)" in DASH and "setInterval(() => window.VELOR_FIELD" not in DASH)
check("новый вывод определяется по ключам рекомендаций",
      "function newDecision(dir)" in DASH and "r.key || r.title" in DASH)
# Янтарный сигнал берётся из канонического счётчика воронки, а не из нового
# определения «без ответа», заведённого ради фона.
check("обращения без ответа берутся из воронки, а не выдумываются",
      "/api/leads?status=open" in DASH and "stats.waiting" in DASH)
check("сигнал поднимается числом, а не признаком",
      "VELOR_FIELD.signal('leads', n)" in DASH)
check("импульс — только на рост, дальше сигнал живёт присутствием",
      "n > _prevWaiting" in DASH and "VELOR_FIELD.pulse('lead')" in DASH)
# Отдельный запрос ради фона не имеет права задерживать экран: числа, разбор
# и «что требует внимания» не ждут его ни секунды.
check("запрос за сигналом не задерживает страницу",
      "await leadsWaiting()" not in DASH and "leadsWaiting().then(" in DASH)


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
          "Именной допуск — живое поле" in d and "29–89" in d)
    # Уровни поля — архитектурное решение, а не описание: если документ
    # перестанет их разделять, следующая правка склеит их и в коде.
    check("документ разделяет форму состояния и сигнал бизнеса",
          "Базовое состояние" in d and "Сигнал бизнеса" in d)
    check("состояния поля описаны таблицей",
          all(w in d for w in ("waiting", "normal", "risk", "opportunity")))

print(f"\nИТОГО: успешно {ok}, провалено {fail}")
sys.exit(1 if fail else 0)
