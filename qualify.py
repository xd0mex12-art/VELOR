# -*- coding: utf-8 -*-
"""
ОЦЕНКА ВОЗМОЖНОСТИ — кому владелец должен ответить первым и почему.

Не балл, а ответ
────────────────
VELOR не говорит «у этого лида 78 баллов». Такой ответ невозможно проверить и
невозможно оспорить: за числом не видно, дорогая это сделка или горячая, наша
или чужая, и что с ней делать сейчас. Поэтому здесь нет одного балла. Есть
четыре разных вопроса, и на каждый отвечают отдельно:

    ХОЧЕТ ЛИ КУПИТЬ      intent   низкое / среднее / высокое
    НАШЕ ЛИ ЭТО          fit      не знаем / низкое / среднее / высокое
    СКОЛЬКО ДЕНЕГ        value    названная сумма или оценка по своему прайсу
    ЧТО ДЕЛАТЬ СЕЙЧАС    priority низкий / средний / высокий / срочно

Смешивать их нельзя. Заказ на сто тысяч, по которому человек просто спросил
цену, — это не горячий лид. Заказ на пять тысяч, который просят оформить
сегодня, — горячий. Деньги и вероятность покупки живут в разных измерениях, и
единый балл склеил бы их в цифру, которая врёт в обе стороны.

Наблюдение, а не догадка
────────────────────────
Оценка собирается из СИГНАЛОВ — из того, что человек написал сам. У каждого
сигнала есть слово, которым он доказан, номер сообщения и время. Поэтому на
любой вопрос «почему высокое намерение» есть ответ цитатой, а не «модель так
решила». Правила дешевле модели, работают без ключа и объяснимы; модель может
добавить свои наблюдения, но одна, без слов клиента, поднять намерение до
высокого не может — это та же граница «факт против предположения», по которой
живёт вся остальная память бизнеса.

Что хранится и что считается
────────────────────────────
Хранится то, что не меняется со временем: намерение, соответствие, оценка
суммы, названная дата и сами сигналы. Приоритет НЕ хранится — он зависит от
времени («бизнес молчит третий час», «нужно послезавтра»), а записанное время
устаревает молча и начинает врать. Он считается в момент показа. Исключение
одно: приоритет, поставленный рукой владельца, — он в колонке и сильнее любого
расчёта.

Чего здесь нет
──────────────
Вероятности покупки в процентах. Пока не накоплено достаточно закрытых сделок,
«вероятность 73%» — это ложная точность: цифра выглядит как знание, а получена
из ниоткуда. Сигналы и исходы сохраняются так, чтобы потом можно было сравнить
выигранные возможности с проигранными и узнать, какие признаки действительно
предсказывают покупку. Сначала факты, потом статистика.
"""
from __future__ import annotations

import datetime
import logging
import re

import database

log = logging.getLogger("velor.qualify")

LOW, MEDIUM, HIGH = "low", "medium", "high"
UNKNOWN = "unknown"
URGENT = "urgent"

INTENT_ORDER = (LOW, MEDIUM, HIGH)
FIT_ORDER = (UNKNOWN, LOW, MEDIUM, HIGH)
PRIORITY_ORDER = (LOW, MEDIUM, HIGH, URGENT)

INTENT_RU = {LOW: "Низкое", MEDIUM: "Среднее", HIGH: "Высокое"}
FIT_RU = {UNKNOWN: "Не знаем", LOW: "Низкое", MEDIUM: "Среднее", HIGH: "Высокое"}
PRIORITY_RU = {LOW: "Низкий", MEDIUM: "Средний", HIGH: "Высокий",
               URGENT: "Срочно"}

INTENT_HINT = {
    LOW:    "человек интересуется, но о покупке пока не говорил",
    MEDIUM: "спрашивает конкретное: цену, наличие, доставку",
    HIGH:   "прямо говорит, что хочет купить",
}
FIT_HINT = {
    UNKNOWN: "судить не по чему — в памяти бизнеса нет услуг и товаров",
    LOW:     "то, чего мы не делаем или больше не продаём",
    MEDIUM:  "прямого совпадения с каталогом нет",
    HIGH:    "это есть у нас в каталоге",
}

# Сколько дней сигнал остаётся актуальным. «Можно заказать завтра?» через два
# месяца — уже не то же самое обещание. Историю при этом не трогаем: устаревает
# не факт, а его актуальность для сегодняшнего действия.
FRESH_DAYS = 14

# Через сколько часов молчания бизнеса разговор начинает стоить денег.
GAP_URGENT_HOURS = 1
GAP_HIGH_HOURS = 3

# Насколько близкая дата считается срочной.
SOON_DAYS = 2
NEAR_DAYS = 7

SIGNALS_LIMIT = 60


# ── сигналы ────────────────────────────────────────────────────────────────
# (ключ, вид, сила, что это значит по-русски, выражение)
#
# Сила «high» — это заявление о покупке, а не о любопытстве. Разница между
# «мне интересны свадебные букеты» и «нужен букет 15 августа, можно заказать?»
# и есть разница между интересом и намерением, и проходит она ровно здесь.

SIGNALS = [
    # ── намерение: прямое ──
    ("order_now", "intent", HIGH, "просит оформить заказ",
     r"хочу\s+(?:за)?каз\w*|хочу\s+купить|готов\w*\s+заказать|"
     r"(?:оформ\w+|сделайте|сделать|разместить)\s+заказ|давайте\s+оформ\w+|"
     r"можно\s+(?:ли\s+)?оформ\w+|беру\b|берём\b|заказыва\w+"),
    ("book_me", "intent", HIGH, "просит записать",
     r"запишите\s+меня|хочу\s+записаться|можно\s+(?:ли\s+)?записаться|"
     r"записаться\s+(?:на|к)\b"),
    ("payment", "intent", HIGH, "спрашивает про оплату",
     r"где\s+оплат\w+|как\s+оплат\w+|можно\s+(?:ли\s+)?оплат\w+|"
     r"предоплат\w+|скинуть\s+деньги|куда\s+перевести|реквизит\w+"),
    ("agreed", "intent", HIGH, "согласился",
     r"\bсогласен\b|\bсогласна\b|подтвержда\w+|\bдавайте\b|\bберём\b|"
     r"меня\s+устраивает|\bпойд[её]т\b"),

    # ── намерение: конкретный коммерческий вопрос ──
    ("price_ask", "intent", MEDIUM, "спросил цену",
     r"скольк\w*\s+(?:это\s+)?(?:будет\s+)?(?:стоит|стоят|стоить|обойд\w+|выйдет)|"
     r"\bпо\s?ч[её]м\b|\bцен[аыу]\b|\bстоимост\w*|\bпрайс\w*"),
    ("stock_ask", "intent", MEDIUM, "спросил про наличие",
     r"есть\s+(?:ли\s+)?в\s+наличии|\bв\s+наличии\b|\bесть\s+ли\b|остал\w+\s+ли"),
    ("delivery_ask", "intent", MEDIUM, "спросил про доставку",
     r"\bдоставк\w*|привез[её]т\w*|\bсамовывоз\w*|довез[её]т\w*"),
    ("spec_ask", "intent", MEDIUM, "уточняет, что именно нужно",
     r"как(?:ие|ой|ая)\s+есть|из\s+чего|\bразмер\w*|\bцвет\w*|\bсостав\b|"
     r"сколько\s+по\s+времени|как\s+долго|\bварианты\b"),
    ("slot_ask", "intent", MEDIUM, "спрашивает свободное время",
     r"свободн\w+\s+(?:время|окошк\w*|дат\w*|мест\w*)|есть\s+ли\s+мест\w*"),

    # ── намерение: общее любопытство ──
    ("general", "intent", LOW, "общий интерес",
     r"расскажите\s+подробнее|интересу\w+|хотел\w*\s+узнать|подскажите\b|"
     r"а\s+что\s+у\s+вас\s+есть"),

    # ── срочность ──
    ("today", "timing", HIGH, "нужно сегодня-завтра",
     r"\bсегодня\b|\bзавтра\b|\bсрочно\b|как\s+можно\s+скорее|\bпоскорее\b|"
     r"\bсейчас\s+нужн\w*"),

    # ── отказ ──
    ("refuse", "negative", HIGH, "сказал, что не нужно",
     r"\bне\s+нуж(?:ен|н\w*)|передумал\w*|\bотказ\w*"),
    # «Не нужно» и «не пишите мне» — разные просьбы, и путать их дорого.
    # Первая закрывает одну возможность, вторая закрывает человека навсегда:
    # после неё писать нельзя ни по этой сделке, ни по любой следующей.
    ("do_not_contact", "negative", HIGH, "попросил больше не писать",
     r"не\s+пишите\s+(?:мне\s+)?больше|больше\s+не\s+пишите|"
     r"не\s+пишите\s+мне\b|не\s+беспоко\w+|перестаньте\s+писать|"
     r"отпишите\s+меня|удалите\s+мой\s+(?:номер|контакт)"),
    ("expensive", "negative", MEDIUM, "сказал, что дорого",
     r"\bдорог\w*\b|дороговат\w*|не\s+по\s+карману|не\s+укладыва\w+\s+в\s+бюджет"),
    ("competitor", "negative", HIGH, "нашёл в другом месте",
     r"нашл\w*\s+(?:в\s+)?друг\w+|заказал\w*\s+(?:в\s+)?друг\w+|у\s+конкурент\w+|"
     r"уже\s+(?:заказал\w*|купил\w*)\s+(?:в\s+)?друг\w*"),
]

COMPILED = [(k, kind, level, title, re.compile(rx, re.I))
            for k, kind, level, title, rx in SIGNALS]

# Сколько штук чего. «50 букетов» — это факт о размере сделки, а не догадка.
_QTY = re.compile(r"\b(\d{1,4})\s+([а-яё]{3,})", re.I)
_QTY_SKIP = re.compile(r"^(?:руб|рубл|тысяч|час|часов|минут|дн|дней|год|лет|"
                       r"штук)$", re.I)


def _now():
    """
    Сейчас — в UTC, потому что этими часами меряется ДЛИТЕЛЬНОСТЬ.

    Времена в базе пишет SQLite через datetime('now'), а он возвращает UTC.
    Разность «сейчас минус запись» обязана считаться в тех же часах, иначе
    «бизнес молчит третий час» ошибётся ровно на часовой пояс.
    """
    return datetime.datetime.utcnow()


def _today():
    """
    Сегодня — по МЕСТНОМУ времени, потому что это календарная дата.

    «Завтра» из сообщения клиента принадлежит тому дню, в котором живёт
    бизнес, а не тому, который сейчас в Гринвиче. Пока здесь стоял UTC,
    клиент, написавший «завтра» в час ночи по Москве, получал вчерашнюю дату:
    в UTC сутки ещё не сменились, и владелец видел заявку на день раньше, чем
    просили.

    Двое часов в одном файле — не небрежность, а следствие того, что здесь
    считаются две разные вещи: длительность и дата. Мерить их одними часами
    можно только там, где часовой пояс равен нулю.
    """
    return datetime.datetime.now().date()


def _parse(when):
    s = str(when or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def _hours_since(when):
    dt = _parse(when)
    return None if not dt else max(0.0, (_now() - dt).total_seconds() / 3600)


def _days_since(when):
    h = _hours_since(when)
    return None if h is None else h / 24


def _up(value, order, step=1):
    i = order.index(value) if value in order else 0
    return order[max(0, min(len(order) - 1, i + step))]


def _down(value, order, step=1):
    return _up(value, order, -step)


# ── дата, к которой нужно ──────────────────────────────────────────────────
# Придумывать дату нельзя ни при каких обстоятельствах: «нужно к пятнице» —
# это обещание, и если пятница взята из воздуха, владелец опоздает на встречу,
# которой не было. Поэтому берём только то, что человек назвал сам.

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
          "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11,
          "декабр": 12}
WEEKDAYS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
            "пятниц": 4, "суббот": 5, "воскресень": 6}

_DAY_MONTH = re.compile(r"\b(\d{1,2})\s*(январ|феврал|март|апрел|ма|июн|июл|"
                        r"август|сентябр|октябр|ноябр|декабр)\w*", re.I)
_WEEKDAY = re.compile(r"\b(?:в|во|к|до)\s+(понедельник|вторник|сред|четверг|"
                      r"пятниц|суббот|воскресень)\w*", re.I)


def wanted_date(text, today=None):
    """
    Дата, к которой человеку нужно, — если он её действительно назвал.

    Возвращает ISO-строку или None. None здесь — нормальный ответ: у половины
    разговоров даты нет, и «нет» честнее подставленной.
    """
    s = (text or "").strip()
    if not s:
        return None
    day = today or _today()

    low = s.lower()
    if re.search(r"\bсегодня\b", low):
        return day.isoformat()
    if re.search(r"\bзавтра\b", low):
        return (day + datetime.timedelta(days=1)).isoformat()
    if re.search(r"\bпослезавтра\b", low):
        return (day + datetime.timedelta(days=2)).isoformat()

    m = _DAY_MONTH.search(s)
    if m:
        num = int(m.group(1))
        month = MONTHS.get(m.group(2).lower())
        if month and 1 <= num <= 31:
            year = day.year
            try:
                got = datetime.date(year, month, num)
            except ValueError:
                return None
            # Названный день уже прошёл — значит, речь про следующий год:
            # «8 марта» в декабре это не прошлый март.
            if got < day:
                try:
                    got = datetime.date(year + 1, month, num)
                except ValueError:
                    return None
            return got.isoformat()

    m = _WEEKDAY.search(s)
    if m:
        want = WEEKDAYS.get(m.group(1).lower())
        if want is not None:
            ahead = (want - day.weekday()) % 7 or 7
            return (day + datetime.timedelta(days=ahead)).isoformat()

    # Числовые форматы разбирает тот же код, что и в документах: две системы
    # дат в одном продукте однажды разойдутся на день.
    try:
        import understanding
        got = understanding.find_date(s)
        if got:
            return got
    except Exception:
        pass
    return None


def quantity(text):
    """Сколько штук просят. Возвращает (число, слово) или (None, None)."""
    for m in _QTY.finditer(text or ""):
        num, word = int(m.group(1)), m.group(2)
        if num < 2 or num > 9999 or _QTY_SKIP.match(word):
            continue
        return num, word
    return None, None


# ── чтение сообщения ───────────────────────────────────────────────────────

def read(text, *, message_id=None, at=None, source="client"):
    """
    Что слышно в одном сообщении. Возвращает список сигналов с происхождением.

    Происхождение обязательно: сигнал без сообщения и цитаты нельзя ни
    проверить, ни оспорить, а значит, ему нельзя верить.
    """
    out = []
    s = (text or "").strip()
    if not s:
        return out
    stamp = at or database.now()
    seen = set()
    for key, kind, level, title, rx in COMPILED:
        m = rx.search(s)
        if not m or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "kind": kind, "level": level, "title": title,
                    "quote": m.group(0).strip().lower()[:60],
                    "message_id": message_id, "at": stamp, "source": source,
                    # Сказанное человеком — факт. Всё остальное (в том числе
                    # наблюдения модели) фактом не считается и одно намерение
                    # до высокого не поднимает.
                    "fact": source in ("client", "owner")})
    when = wanted_date(s)
    if when:
        out.append({"key": "date_named", "kind": "timing", "level": HIGH,
                    "title": "назвал дату", "quote": when,
                    "message_id": message_id, "at": stamp, "source": source,
                    "fact": source in ("client", "owner")})
    try:
        import leads as _leads
        amount, _cur = _leads.stated_value(s)
    except Exception:
        amount = None
    if amount:
        out.append({"key": "budget_named", "kind": "value", "level": HIGH,
                    "title": "назвал сумму", "quote": str(amount),
                    "message_id": message_id, "at": stamp, "source": source,
                    "fact": source in ("client", "owner")})
    num, word = quantity(s)
    if num:
        out.append({"key": "quantity", "kind": "value", "level": MEDIUM,
                    "title": "назвал количество", "quote": "%d %s" % (num, word),
                    "message_id": message_id, "at": stamp, "source": source,
                    "fact": source in ("client", "owner")})
    return out


def merge(old, new):
    """
    Добавить наблюдения к уже собранным.

    Один и тот же сигнал в одном и том же сообщении не удваивается, но тот же
    сигнал в НОВОМ сообщении — это отдельное наблюдение: повторение и есть
    усиление намерения, и терять его нельзя.
    """
    have = {(s.get("key"), s.get("message_id")) for s in (old or [])}
    out = list(old or [])
    for s in new or []:
        if (s.get("key"), s.get("message_id")) in have:
            continue
        have.add((s.get("key"), s.get("message_id")))
        out.append(s)
    return out[-SIGNALS_LIMIT:]


# ── намерение ──────────────────────────────────────────────────────────────

def intent_of(signals):
    """
    Хочет ли человек купить. Возвращает (уровень, причины).

    Три сложенных «средних» сигнала — не три слабых, а растущее намерение:
    «сколько стоит» → «а доставка есть?» → «нужно завтра» это один разговор,
    в котором человек подходит к покупке. Поэтому связка «конкретный вопрос +
    названная дата или сумма» поднимает намерение до высокого, даже если слова
    «хочу заказать» не прозвучало.
    """
    facts = [s for s in signals or [] if s.get("fact")]
    strong = [s for s in facts if s["kind"] == "intent" and s["level"] == HIGH]
    medium = [s for s in facts if s["kind"] == "intent" and s["level"] == MEDIUM]
    weak = [s for s in facts if s["kind"] == "intent" and s["level"] == LOW]
    timing = [s for s in facts if s["kind"] == "timing"]
    money = [s for s in facts if s["kind"] == "value"]
    bad = [s for s in signals or [] if s["kind"] == "negative"]

    why = []
    level = LOW
    if strong:
        level = HIGH
        why += [_reason(s) for s in strong[:3]]
    elif medium and (timing or money) and len({s["key"] for s in medium}) >= 2:
        level = HIGH
        why.append("несколько конкретных вопросов подряд и названы сроки или сумма")
        why += [_reason(s) for s in (medium[:2] + (timing or money)[:1])]
    elif medium:
        level = MEDIUM
        why += [_reason(s) for s in medium[:3]]
    elif weak:
        level = LOW
        why += [_reason(s) for s in weak[:2]]
    else:
        why.append("прямых слов о покупке пока не было")

    if bad:
        # Отказ не закрывает возможность сам — это состояние, и ставит его
        # человек. Но делать вид, что отказа не было, тоже нельзя.
        level = _down(level, INTENT_ORDER)
        why.append("но: " + _reason(bad[-1]))
    return level, why[:5]


def _reason(signal):
    q = (signal.get("quote") or "").strip()
    line = signal.get("title") or signal.get("key") or ""
    if q:
        line += ": «%s»" % q
    if signal.get("message_id"):
        line += " (сообщение №%s)" % signal["message_id"]
    return line


# ── соответствие бизнесу ───────────────────────────────────────────────────
# Fit — это не «богатый клиент». Это «делаем ли мы такое вообще». Судим по
# памяти самого бизнеса: по каталогу и по правилам, которые владелец записал
# своими словами. Ничего не выдумываем: пустая память — это «не знаем», а не
# «плохо».

_ONLY = re.compile(r"\bтольк[оа]\b|\bне\s+работа\w+\s+(?:с|в|по)\b|"
                   r"\bне\s+доставля\w+\b|\bне\s+выезжа\w+\b", re.I)
_PLACE = re.compile(r"\b(?:в|во|до|по|из)\s+([А-ЯЁ][а-яё\-]{2,})")


def _place_stems(text):
    """Города и районы, названные в тексте, — в сравнимом виде."""
    out = set()
    for m in _PLACE.finditer(text or ""):
        word = m.group(1).lower()
        out.add(word[:max(4, len(word) - 2)])
    return out


def fit_of(business_id, text, extra=""):
    """
    Наше ли это. Возвращает (уровень, причины, найденные позиции, риски).

    Уровни:
      high    — такое есть в каталоге;
      medium  — каталог есть, но совпадения нет: судить рано, а не плохо;
      low     — правило бизнеса прямо это исключает, либо позиция снята с продажи;
      unknown — памяти нет вовсе, судить не по чему.
    """
    hay = (str(text or "") + " " + str(extra or "")).strip()
    why, risks, found = [], [], []
    try:
        import graph
        services = database.list_facts(business_id, "service") or []
        products = database.list_facts(business_id, "product") or []
        rules = database.list_facts(business_id, "rule") or []
        company = database.list_facts(business_id, "company") or []
    except Exception:
        log.exception("Память бизнеса не прочиталась (biz %s)", business_id)
        return UNKNOWN, ["память бизнеса не прочиталась"], [], []

    catalog = list(services) + list(products)
    if not catalog:
        return (UNKNOWN,
                ["в памяти бизнеса нет ни услуг, ни товаров — сравнивать не с чем"],
                [], [])

    for row in catalog:
        score = graph.mention_score(row.get("title"), hay)
        if score >= 0.5:
            found.append({"id": row["id"], "title": row.get("title"),
                          "body": row.get("body") or "", "score": round(score, 2),
                          "verified": bool(row.get("verified", 1))})

    # Ограничение владельца сильнее совпадения по каталогу: «доставка только по
    # Москве» — это его собственные слова, и спорить с ними догадкой нельзя.
    asked = _place_stems(hay)
    for row in list(rules) + list(company):
        line = (row.get("title") or "") + " " + (row.get("body") or "")
        if not _ONLY.search(line):
            continue
        allowed = _place_stems(line)
        if not allowed:
            continue
        outside = [p for p in asked if p not in allowed]
        if asked and outside:
            why.append("правило бизнеса: «%s»" % _cut(line, 90))
            why.append("а человек называет другое место")
            return LOW, why, found, risks
        # Место не назвали. Молчать нельзя — ограничение может всё отменить, —
        # но и объявлять несоответствие не за что. Показываем как вопрос к
        # владельцу, и только если разговор вообще про это: иначе напоминание
        # о доставке висело бы над каждой возможностью и перестало читаться.
        if not asked and graph.mention_score(row.get("title"), hay) >= 0.5:
            risks.append("Проверьте ограничение: «%s»" % _cut(line, 90))

    if found:
        why.append("есть у нас: " + ", ".join(_cut(f["title"], 40) for f in found[:3]))
        return HIGH, why, found, risks

    archived = []
    try:
        for kind in ("service", "product"):
            for row in database.list_facts(business_id, kind, archived=True) or []:
                if graph.mention_score(row.get("title"), hay) >= 0.5:
                    archived.append(row.get("title"))
    except Exception:
        archived = []
    if archived:
        why.append("снято с продажи: " + ", ".join(_cut(a, 40) for a in archived[:2]))
        return LOW, why, found, risks

    why.append("в каталоге такого не нашлось — возможно, просто названо иначе")
    return MEDIUM, why, found, risks


def _cut(s, n):
    s = re.sub(r"\s+", " ", str(s or "").strip())
    return s if len(s) <= n else s[:n - 1] + "…"


# ── деньги ─────────────────────────────────────────────────────────────────

def _wstem(w):
    w = str(w or "").lower().replace("ё", "е")
    return w[:max(3, len(w) - 2)]


def _same_thing(title, word):
    """
    Про одно ли это. «50 роз» и «Роза красная» — да; «50 роз» и «Букет» — нет.

    Сравниваем основы с обеих сторон: у существительного в тексте окончание
    своё («роз»), а в названии позиции своё («Роза»), и совпадение по целому
    слову тут не работает никогда.
    """
    want = _wstem(word)
    if not want:
        return False
    for t in re.findall(r"[\wа-яё]+", str(title or "").lower().replace("ё", "е")):
        got = _wstem(t)
        if got.startswith(want) or want.startswith(got):
            return True
    return False


def _by_word(business_id, word):
    """Позиции каталога, которые называются этим словом. Пусто — значит нет."""
    out = []
    if not business_id or not word:
        return out
    try:
        for kind in ("product", "service"):
            for row in database.list_facts(business_id, kind) or []:
                if _same_thing(row.get("title"), word):
                    out.append({"id": row["id"], "title": row.get("title"),
                                "body": row.get("body") or "", "score": 1.0})
    except Exception:
        return []
    return out


def value_of(lead, found, signals):
    """
    Сколько денег может стоять за разговором. Возвращает (сумма, чем считали, оценка?).

    Названная сумма — факт и всегда сильнее расчёта. Расчёт по своему же прайсу
    возможен, но он остаётся оценкой и в поле «сумма» не попадает никогда:
    иначе завтра нельзя будет отличить, что клиент сказал, а что мы прикинули.
    """
    said = lead.get("value")
    if said:
        if "value" in (lead.get("owner_fields") or []):
            src = "вы указали её сами"
        else:
            # Откуда взялась сумма, помнит сам лид. Писать «клиент назвал сам»
            # там, где цифра приехала из оформленной заявки, — мелкая ложь,
            # но именно на таких мелочах доверие к цифрам и теряется.
            src = {"order": "из оформленной заявки",
                   "client": "клиент назвал её сам"}.get(
                       (lead.get("meta") or {}).get("value_src"),
                       "клиент назвал её сам")
        return int(said), src, False

    qty = next((s for s in signals or [] if s.get("key") == "quantity"), None)
    num, word = 1, ""
    if qty:
        parts = str(qty.get("quote") or "").split()
        try:
            num = int(parts[0])
        except (ValueError, IndexError):
            num = 1
        word = parts[1] if len(parts) > 1 else ""

    # «50 роз» — это пятьдесят роз, а не пятьдесят букетов. Считаем по той
    # позиции каталога, о которой человек говорил, а не по первой попавшейся:
    # иначе оценка ошибается в тридцать раз и перестаёт быть полезной.
    items = list(found or [])
    if word:
        if not any(_same_thing(f.get("title"), word) for f in items):
            # Короткое слово каталог по-хорошему не узнаёт: «роз» и «Роза
            # красная» для поиска по тексту — разные строки. Спрашиваем каталог
            # напрямую, иначе «50 роз» посчитается по цене букета и ошибётся в
            # тридцать раз.
            items = _by_word(lead.get("business_id"), word) + items
        items.sort(key=lambda f: 0 if _same_thing(f.get("title"), word) else 1)

    price, basis = None, None
    for f in items:
        try:
            import understanding
            money = understanding.find_money(f.get("body") or "")
        except Exception:
            money = []
        if money:
            price = min(m["amount"] for m in money)
            basis = "по прайсу: %s — %s ₽" % (_cut(f["title"], 40), _money(price))
            break
    if not price:
        return None, None, False

    if num > 1:
        return price * num, basis + " × %d (клиент назвал количество)" % num, True
    return price, basis, True


def _money(n):
    return "{:,}".format(int(n or 0)).replace(",", " ")


# ── что делать сейчас ──────────────────────────────────────────────────────

def gap_for(lead, gap):
    """
    Относится ли молчание бизнеса к этой возможности.

    Возможность, заведённая рукой владельца, не выросла из переписки — и
    давнее «здравствуйте», на которое когда-то не ответили, не делает её
    срочной. Правило живёт здесь одно на всех: и экран возможности, и обход
    находок должны отвечать на «бизнес молчит?» одинаково.
    """
    if not lead.get("first_message_id"):
        return {}
    return gap or {}


def priority_of(lead, *, intent, fit, value, gap=None, signals=None,
                value_estimated=False):
    """
    На кого владельцу смотреть прямо сейчас. Возвращает (уровень, причины, риски).

    Это НЕ вероятность покупки. Это ответ на вопрос «где мы теряем деньги, если
    ничего не сделаем сегодня». Поэтому сюда входит то, чего нет ни в
    намерении, ни в соответствии: сколько бизнес молчит, насколько близка
    названная дата и не остыл ли разговор.

    Единственная дверь: этим же расчётом пользуется обход находок. Второй
    ответ на вопрос «кто важнее» означал бы, что список возможностей и главная
    страница спорят друг с другом на глазах у владельца.
    """
    why, risks = [], []
    if (lead.get("status") or "") not in database.LEAD_OPEN:
        return LOW, ["возможность закрыта"], risks

    # Рука владельца старше расчёта: он знает про клиента то, чего нет ни в
    # одном сообщении.
    if "priority" in set(lead.get("owner_fields") or []) and lead.get("priority"):
        return lead["priority"], ["вы поставили это сами"], risks

    gap = gap_for(lead, gap)
    waiting = _hours_since(gap.get("last_in_at")) if gap.get("unanswered") else None
    fresh_days = _days_since(lead.get("last_activity_at") or lead.get("created_at"))
    stale = fresh_days is not None and fresh_days > FRESH_DAYS
    days_left = _days_until(lead.get("wanted_at"))

    level = LOW
    if intent == HIGH:
        level = HIGH
        why.append("человек прямо говорит, что хочет купить")
    elif intent == MEDIUM:
        level = MEDIUM
        why.append("спрашивает конкретное — цену, наличие, доставку")
    else:
        why.append("пока это общий интерес")

    # Молчание бизнеса срочно ровно до тех пор, пока разговор жив. Вопрос,
    # оставшийся без ответа сорок дней назад, — это не «ответить в течение
    # часа», а другой разговор, который надо начинать заново.
    if stale:
        waiting = None

    if waiting is not None and waiting >= GAP_URGENT_HOURS and intent == HIGH:
        level = URGENT
        why.append("бизнес не ответил уже %s" % _hours_ru(waiting))
        risks.append("Клиент готов купить, а ответа нет — это самый дорогой час.")
    elif waiting is not None and waiting >= GAP_HIGH_HOURS:
        level = _up(level, PRIORITY_ORDER)
        why.append("бизнес не ответил уже %s" % _hours_ru(waiting))
        risks.append("Вопрос без ответа — обычная причина уйти к другим.")

    if days_left is not None and days_left <= SOON_DAYS and intent != LOW:
        level = URGENT if intent == HIGH else _up(level, PRIORITY_ORDER)
        why.append("нужно к %s" % lead["wanted_at"])
    elif days_left is not None and days_left <= NEAR_DAYS and intent != LOW:
        level = _up(level, PRIORITY_ORDER)
        why.append("дата близко: %s" % lead["wanted_at"])

    # Деньги поднимают внимание, но не создают его: холодный разговор о ста
    # тысячах остаётся холодным разговором. И поднимает их только НАЗВАННАЯ
    # сумма: собственная оценка по прайсу есть почти у каждой возможности, и
    # поднимать по ней приоритет — значит поднять его всем сразу, то есть
    # никому.
    if value and not value_estimated and intent != LOW and level != URGENT:
        level = _up(level, PRIORITY_ORDER)
        why.append("названа сумма: %s ₽" % _money(value))

    if fit == LOW:
        level = _down(level, PRIORITY_ORDER)
        why.append("но это не то, чем мы занимаемся")
    if stale:
        level = _down(level, PRIORITY_ORDER)
        why.append("разговор молчит больше %d дней — сигналы устарели" % FRESH_DAYS)
    if any(s["kind"] == "negative" for s in signals or []):
        level = _down(level, PRIORITY_ORDER)
        why.append("клиент уже говорил «нет» — сначала выяснить, что не подошло")

    return level, why[:6], risks


def _days_until(day):
    if not day:
        return None
    try:
        want = datetime.date.fromisoformat(str(day)[:10])
    except ValueError:
        return None
    return (want - _now().date()).days


def _hours_ru(hours):
    h = int(hours or 0)
    if h < 1:
        return "меньше часа"
    if h < 24:
        return "%d ч" % h
    d = h // 24
    tail = "день" if d % 10 == 1 and d % 100 != 11 else (
        "дня" if 2 <= d % 10 <= 4 and not 12 <= d % 100 <= 14 else "дней")
    return "%d %s" % (d, tail)


def next_action(lead, *, priority, intent, gap=None, signals=None):
    """
    Что делать. Одно предложение, которое можно выполнить, не думая.

    Само VELOR при этом ничего не отправляет: писать клиенту за владельца —
    отдельное решение и отдельный этап.
    """
    if (lead.get("status") or "") not in database.LEAD_OPEN:
        return None, None
    gap = gap or {}
    negative = any(s["kind"] == "negative" for s in signals or [])
    if negative:
        return ("Спросить, что не подошло",
                "Человек говорил «нет». Прямое предложение сейчас оттолкнёт, "
                "а честный вопрос — нет.")
    if gap.get("unanswered") and priority in (URGENT, HIGH):
        return ("Ответить сейчас",
                "Клиент ждёт ответа, и это самый дорогой момент разговора.")
    if gap.get("unanswered"):
        return ("Ответить сегодня", "Вопрос клиента остался без ответа.")
    if intent == HIGH:
        return ("Довести до заявки",
                "Подтвердите дату и сумму — человек уже готов.")
    if intent == MEDIUM:
        return ("Продолжить разговор",
                "Спросите, что именно нужно и к какому сроку.")
    return ("Наблюдать", "Разговор пока общий — тратить время рано.")


# ── сборка ─────────────────────────────────────────────────────────────────

def observe(business_id, lead_id, text, *, message_id=None, source="client",
            extra_signals=None):
    """
    Услышать новое сообщение и пересчитать оценку возможности.

    Хранится только то, что не устаревает: намерение, соответствие, оценка
    суммы, названная дата, сами сигналы. Приоритет считается в момент показа —
    записанный, он врал бы уже через час.
    """
    try:
        lead = database.get_lead(int(lead_id), business_id)
        if not lead:
            return None
        owner = set(lead.get("owner_fields") or [])
        got = read(text, message_id=message_id, source=source)
        for s in extra_signals or []:
            s = dict(s)
            s.setdefault("kind", "intent")
            s.setdefault("level", MEDIUM)
            s.setdefault("at", database.now())
            s["source"] = s.get("source") or "ai"
            s["fact"] = False
            got.append(s)
        signals = merge(lead.get("signals"), got)

        fields = {"signals": signals, "qualified_at": database.now()}

        level, _why = intent_of(signals)
        if "intent" not in owner:
            fields["intent"] = level

        hay = " ".join(x for x in (lead.get("title"), lead.get("interest"), text) if x)
        fit, _fwhy, found, _risks = fit_of(business_id, hay)
        if "fit" not in owner:
            fields["fit"] = fit

        when = next((s["quote"] for s in signals if s.get("key") == "date_named"), None)
        if when and "wanted_at" not in owner:
            fields["wanted_at"] = when

        amount, _basis, estimated = value_of(lead, found, signals)
        if estimated and amount and "estimated_value" not in owner:
            fields["estimated_value"] = amount

        database.update_lead(lead_id, business_id, **fields)
        return database.get_lead(int(lead_id), business_id)
    except Exception:
        log.exception("Оценка возможности не пересчиталась (biz %s, lead %s)",
                      business_id, lead_id)
        return None


def view(lead, gap=None):
    """
    Оценка возможности для показа: уровни, деньги, причины, риск и действие.

    Ничего не пишет в базу. Всё, что зависит от времени, считается здесь и
    сейчас — иначе «бизнес молчит третий час» через сутки так и осталось бы
    третьим часом.
    """
    if not lead:
        return {}
    owner = set(lead.get("owner_fields") or [])
    signals = list(lead.get("signals") or [])
    business_id = lead.get("business_id")

    intent = lead.get("intent") or LOW
    intent_why = []
    if "intent" in owner:
        intent_why = ["вы поставили это сами"]
    else:
        intent, intent_why = intent_of(signals)

    hay = " ".join(x for x in (lead.get("title"), lead.get("interest")) if x)
    if "fit" in owner:
        fit, fit_why, found, risks = (lead.get("fit") or UNKNOWN,
                                      ["вы поставили это сами"], [], [])
    else:
        fit, fit_why, found, risks = fit_of(business_id, hay)

    value, value_src, estimated = value_of(lead, found, signals)

    if gap is None:
        gap = database.last_exchange(business_id, lead.get("client_id"))
    gap = gap_for(lead, gap)

    priority, prio_why, prio_risks = priority_of(
        lead, intent=intent, fit=fit, value=value, gap=gap, signals=signals,
        value_estimated=bool(estimated))

    action, action_hint = next_action(lead, priority=priority, intent=intent,
                                      gap=gap, signals=signals)
    waiting = _hours_since(gap.get("last_in_at")) if gap.get("unanswered") else None

    return {
        "intent": intent, "intent_ru": INTENT_RU.get(intent, intent),
        "intent_hint": INTENT_HINT.get(intent, ""), "intent_why": intent_why,
        "fit": fit, "fit_ru": FIT_RU.get(fit, fit),
        "fit_hint": FIT_HINT.get(fit, ""), "fit_why": fit_why,
        "priority": priority, "priority_ru": PRIORITY_RU.get(priority, priority),
        "priority_why": prio_why,
        "money": value, "money_src": value_src, "money_estimated": bool(estimated),
        "wanted_at": lead.get("wanted_at"),
        "waiting_hours": round(waiting, 1) if waiting is not None else None,
        "waiting_ru": _hours_ru(waiting) if waiting is not None else "",
        "unanswered": bool(gap.get("unanswered")),
        "risks": (prio_risks + risks)[:3],
        "action": action, "action_hint": action_hint,
        "signals": signals[-12:],
    }


def rank(view_dict):
    """Ключ сортировки: сначала то, где деньги уходят быстрее всего."""
    p = view_dict.get("priority") or LOW
    order = {URGENT: 3, HIGH: 2, MEDIUM: 1, LOW: 0}
    return (order.get(p, 0), view_dict.get("waiting_hours") or 0,
            view_dict.get("money") or 0)
