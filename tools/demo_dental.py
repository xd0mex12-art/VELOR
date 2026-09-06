# -*- coding: utf-8 -*-
"""
Демо-набор: частная стоматология «Дентарика».

Зачем он есть
─────────────
Показать VELOR пустым нельзя: продукт весь про выводы из данных, а из нуля
данных вывод один — «данных нет». Этот набор даёт клинику с трёхмесячной
историей, у которой всё сходится: пациенты, разговоры, возможности, приёмы и
деньги — одно и то же событие, увиденное с разных сторон.

Чего здесь принципиально НЕТ
────────────────────────────
Ни одного написанного вывода. Ни одной находки, вписанной руками. Всё, что
VELOR скажет об этой клинике, он выведет сам детекторами из `initiatives.py`.
Набор кладёт факты — думает продукт. Иначе демо превращается в театр: красиво
на показе и разваливается на первом же вопросе «а откуда он это взял?».

Связность
─────────
Раньше набор был таблицей чисел без историй: 11 сообщений на 34 пациента,
возможности не помнили разговора, из которого родились, а выигранные не
ссылались на приём, которым закончились. Собственная сверка продукта
(`leads.reconcile`) находила на нём 48 противоречий — то есть VELOR сам знал,
что данные не сходятся, и молча с этим жил.

Теперь цепочка замкнута: разговор → возможность → приём → деньги. У каждой
возможности есть первое и последнее сообщение, у каждой выигранной — заявка,
у каждой проигранной — причина кодом, а не человеческой фразой (человеческая
лежит рядом, в meta: она для чтения, код — для подсчёта).

Что при этом НЕ связано и почему
────────────────────────────────
Основной поток приёмов (`_routine`) возможностей не имеет и переписки не
рождает. Так и в жизни: большинство записывается по телефону, повторно или по
плану лечения. Привязать всё ко всему ради красивой цифры значило бы соврать —
воронка показала бы конверсию, которой не было.
"""
import argparse
import datetime
import os
import random
import secrets
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database                                                   # noqa: E402

MARK = "demo-dental"                 # источник строк: по нему видно происхождение
BADGE = "[ДЕМО-НАБОР VELOR]"         # отметка бизнеса: по ней и только по ней удаляем
NAME = "Демо: стоматология «Дентарика»"

random.seed(20260906)                # одинаковый набор при каждом запуске


# ── ВРЕМЯ ───────────────────────────────────────────────────────────────────
# Всё считается от «сейчас»: набор, созданный месяц назад, должен показывать
# ту же картину, что и созданный сегодня, иначе демо протухает само по себе.
def _ts(days_ago, hour=None, minute=0):
    d = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    if hour is not None:
        d = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _shift(stamp, minutes):
    """Сдвинуть отметку на несколько минут — реплики идут не одновременно."""
    d = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    return (d + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _hours_ago(hours):
    d = datetime.datetime.now() - datetime.timedelta(hours=hours)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _date(days_ago):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _rub(n):
    return f"{n:,}".replace(",", " ") + " ₽"


# ── ПРАЙС КЛИНИКИ ───────────────────────────────────────────────────────────
# Цены живут в памяти бизнеса, а не в коде ответов: продавец VELOR берёт цифры
# только оттуда и не имеет права назвать свою. В разговорах ниже звучат ровно
# эти суммы — расхождение прайса с перепиской первым же заметит покупатель.
SERVICES = [
    ("Гигиена и чистка", 6500, "Профессиональная чистка, снятие налёта и камня. Приём 60 минут."),
    ("Лечение кариеса", 9800, "Одна поверхность, световая пломба. Приём 60–90 минут."),
    ("Удаление зуба", 5400, "Простое удаление под местной анестезией."),
    ("Коронка керамическая", 42000, "Безметалловая коронка, две явки."),
    ("Имплант под ключ", 78000, "Имплант, абатмент и коронка. Цена поднята 2 недели назад с 68 000."),
    ("Брекеты, полный курс", 165000, "Металлическая система, наблюдение 18 месяцев."),
    ("Отбеливание", 24000, "Кабинетное отбеливание, один сеанс."),
]
PRICE = {n: p for n, p, _ in SERVICES}

PATIENTS = [
    "Ирина Соколова", "Максим Дорохов", "Анна Петровская", "Сергей Гущин",
    "Ольга Ремизова", "Дмитрий Ковалёв", "Наталья Ерохина", "Артём Лапшин",
    "Елена Ждан", "Павел Тихомиров", "Марина Векшина", "Роман Заславский",
    "Юлия Мельник", "Игорь Панфилов", "Ксения Барсукова", "Владимир Ошуев",
    "Алина Терентьева", "Никита Ходырев", "Светлана Гаврилина", "Олег Прошин",
    "Вера Кузьмичёва", "Антон Салтыков", "Дарья Бельская", "Кирилл Ямщиков",
    "Полина Ждановская", "Егор Наумов", "Лидия Асташова", "Тимур Валеев",
    "Жанна Коробова", "Степан Гурьев", "Алиса Ветрова", "Борис Ильченко",
    "Регина Ахметова", "Валентин Пожарский",
]

LAPSED = 6                           # пациентов, которые пролечились и не вернулись

# Причина проигрыша хранится КОДОМ: по коду продукт группирует «почему теряем».
# Человеческая фраза лежит рядом в meta — её читают, а не считают. Раньше в
# колонку писалась фраза, и разбор потерь схлопывался в «прочее».
LOST_EARLY = [
    ("no_response", "Не дозвонились: три попытки, трубку не берёт."),
    ("timing", "Просил вечер после 19:00 — свободных окон не было."),
    ("other", "Передумал(а), сказал(а) «пока отложу»."),
]
LOST_NOW = [
    ("price", "Дорого: «за эти деньги подумаю ещё»."),
    ("price", "Дорого: сравнил(а) с прошлым годом, цена выросла."),
    ("competitor", "Ушёл(ла) в другую клинику рядом с домом."),
    ("price", "Дорого: просил(а) рассрочку, которой у нас нет."),
    ("price", "Нашёл(ла) дешевле на 15 тысяч."),
    ("competitor", "Ушёл(ла) туда, где взяли на этой неделе."),
]


# ── ПОИСК И ЗАЩИТА ──────────────────────────────────────────────────────────
def find_demo():
    """Демо-бизнес — тот, у кого в описании стоит отметка. Другого признака нет."""
    for b in database.list_businesses_with_stats():
        if BADGE in (b.get("about") or ""):
            return b
    return None


# ── СОЗДАНИЕ ────────────────────────────────────────────────────────────────
def build(login, password):
    if find_demo():
        print("Демо-клиника уже есть. Сначала: python tools/demo_dental.py --clean")
        return None

    bid = database.create_business(
        name=NAME,
        about=BADGE + " Частная стоматология, два кресла, четыре врача. "
              "Приём по записи, основной поток — гигиена и лечение, "
              "дорогие направления — импланты и ортодонтия.",
        greeting="Здравствуйте! Это «Дентарика». Подскажите, что беспокоит — "
                 "подберу время приёма.",
        login=login, password=password,
    )
    database.update_business(
        bid,
        knowledge="\n".join("%s — %s. %s" % (n, _rub(p), d) for n, p, d in SERVICES),
        tone="спокойный, без давления",
        ai_name="Дентарика",
        about=BADGE + " Частная стоматология, два кресла, четыре врача.",
    )
    for name, price, desc in SERVICES:
        database.add_fact(bid, "service", name,
                          body="%s. Цена %s." % (desc, _rub(price)),
                          data={"price": price}, verified=True)

    with database._connect() as con:
        clients = _clients(con, bid)
        _routine(con, bid, clients)      # основной поток записи
        _history(con, bid, clients)      # 90 дней обычной жизни
        _windows(con, bid, clients)      # две недели «до» и две «после»
        _waiting(con, bid, clients)      # обращения без ответа
        _money(con, bid)                 # аренда, зарплаты, материалы
    return bid


def _clients(con, bid):
    """Пациенты. Дата регистрации размазана по трём месяцам — как приходили."""
    out = []
    for i, name in enumerate(PATIENTS):
        days = 88 - int(i * 88 / len(PATIENTS))
        con.execute(
            """INSERT INTO clients (business_id, name, phone, created_at, source, notes)
               VALUES (?,?,?,?,?,?)""",
            (bid, name, "+7 9%02d %03d-%02d-%02d" % (i % 100, 100 + i, i % 60, (i * 7) % 60),
             _ts(days, hour=10 + i % 8), MARK,
             "Пришёл(ла) по рекомендации" if i % 4 == 0 else None))
        out.append(_last_id(con, "clients", bid))
    return out


def _last_id(con, table, bid):
    return con.execute("SELECT MAX(id) FROM %s WHERE business_id = ?" % table,
                       (bid,)).fetchone()[0]


# ── РАЗГОВОРЫ ───────────────────────────────────────────────────────────────
#
# Возможность без разговора — это цифра без причины. Открыв такую в кабинете,
# покупатель видит «Имплант, 78 000, выиграна» и не может спросить «а как?».
# Здесь у каждой возможности есть переписка, из которой она выросла.

def _say(con, bid, cid, start, lines):
    """
    Записать разговор. Возвращает (id первой реплики, id последней).

    Реплики расставлены по минутам: одинаковое время у всех сообщений выглядит
    как импорт, а не как разговор, и ломает порядок в кабинете.
    """
    first = last = None
    for n, (role, text) in enumerate(lines):
        con.execute(
            """INSERT INTO messages (business_id, client_id, role, content, created_at, channel)
               VALUES (?,?,?,?,?,'telegram')""",
            (bid, cid, role, text, _shift(start, n * random.choice((2, 3, 4, 7, 11)))))
        last = _last_id(con, "messages", bid)
        if first is None:
            first = last
    return first, last


# Как люди спрашивают на самом деле: коротко, с опечатками, без вежливых
# формул. Гладкий текст в демо сразу читается как сочинение, а не как жизнь.
OPENERS = {
    "Гигиена и чистка": [
        "Здравствуйте! Хочу записаться на чистку, сколько стоит?",
        "Добрый день. Давно не чистил зубы, налёт видно. Сколько выйдет?",
        "Сколько стоит гигиена? И надолго это по времени",
    ],
    "Лечение кариеса": [
        "Здравствуйте, кажется дырка в зубе. Сколько будет лечение?",
        "Болит когда холодное. Пломбу поставить сколько стоит?",
        "Добрый день! Скол на переднем зубе, что можно сделать?",
    ],
    "Удаление зуба": [
        "Здравствуйте. Зуб мудрости мешает, надо удалять. Цена?",
        "Нужно удалить зуб, сколько это у вас",
    ],
    "Коронка керамическая": [
        "Добрый день! Врач сказал нужна коронка. Сколько у вас?",
        "Здравствуйте, интересует керамическая коронка, цена и сроки",
    ],
    "Имплант под ключ": [
        "Здравствуйте! Сколько будет имплант с коронкой под ключ?",
        "Добрый вечер. Удалили зуб, нужен имплант. Во сколько обойдётся?",
        "Интересует имплантация. Цена под ключ какая?",
    ],
    "Брекеты, полный курс": [
        "Здравствуйте! Хочу поставить брекеты, сколько стоит полный курс?",
        "Добрый день, дочери 14 лет, нужны брекеты. Цена и сроки?",
    ],
    "Отбеливание": [
        "Здравствуйте, сколько стоит отбеливание за один раз?",
        "Хочу отбелить зубы к свадьбе. Сколько и за сколько дней?",
    ],
}

# Что человек отвечает, когда соглашается. Разное: кто-то сразу, кто-то
# уточняет, кто-то торгуется и всё равно приходит.
AGREE = [
    ["Хорошо, записывайте", "Спасибо!"],
    ["Давайте. А в субботу есть окно?", "Ок, подходит"],
    ["Понял, беру. Только можно после шести?"],
    ["Ладно, дороговато, но давайте", "Записывайте на ближайшее"],
    ["А рассрочка есть?", "Понятно. Тогда просто записывайте"],
]

# Отказ — репликой, из которой видно КАКОЙ это отказ. По ней потом
# и проверяется, что причина в базе стоит та же самая.
REFUSE = {
    "price": [
        ["Ого. Дороговато для меня, я подумаю", "Спасибо"],
        ["Я думал будет дешевле. Пока отложу"],
        ["А рассрочку не делаете? Жаль. Тогда не буду"],
        ["В прошлом году было дешевле. Не готов сейчас"],
        ["Нашёл на 15 тысяч дешевле, извините"],
    ],
    "competitor": [
        ["Спасибо, но я уже записался рядом с домом"],
        ["Меня взяли на этой неделе в другой клинике. Извините"],
    ],
    "timing": [
        ["А после семи вечера никак? Днём не могу", "Тогда пока не получится"],
    ],
    "other": [
        ["Спасибо, я подумаю", "Пока отложу"],
    ],
}


def _talk_won(con, bid, cid, service, start):
    """Разговор, который закончился записью."""
    lines = [("user", random.choice(OPENERS[service])),
             ("assistant", "Здравствуйте! %s — %s. %s"
              % (service, _rub(PRICE[service]),
                 "Могу записать на ближайшие дни, скажите удобное время."))]
    for text in random.choice(AGREE):
        lines.append(("user", text))
    lines.append(("assistant", "Записала. Ждём вас, напомним за день до приёма."))
    return _say(con, bid, cid, start, lines)


def _talk_lost(con, bid, cid, service, start, code):
    """
    Разговор, который закончился отказом.

    У «не дозвонились» финальной реплики клиента нет — и не должно быть: тишина
    и есть тот самый отказ. Дописать туда «извините, не буду» значило бы
    подделать событие, которого не происходило.
    """
    lines = [("user", random.choice(OPENERS[service])),
             ("assistant", "Здравствуйте! %s — %s. Записать вас на консультацию?"
              % (service, _rub(PRICE[service])))]
    if code == "no_response":
        return _say(con, bid, cid, start, lines)
    for text in random.choice(REFUSE.get(code) or REFUSE["other"]):
        lines.append(("user", text))
    lines.append(("assistant", "Поняла, спасибо. Будем рады, если передумаете."))
    return _say(con, bid, cid, start, lines)


# ── ЗВЕНЬЯ ЦЕПОЧКИ ──────────────────────────────────────────────────────────

def _lead(con, bid, cid, service, status, born, *, won_at=None, lost_at=None,
          code=None, human=None, order_id=None, msgs=None,
          intent="high", fit="good"):
    """
    Одна возможность. Дата события важнее даты создания — по ней считают воронку.

    order_id и границы разговора обязательны не формально: без первого
    «выиграно» повисает без денег, без вторых — без причины. Ровно это и ловит
    сверка звеньев (`leads.reconcile`).
    """
    first_mid, last_mid = msgs or (None, None)
    meta = ('{"причина": "%s"}' % human.replace('"', "'")) if human else None
    con.execute(
        """INSERT INTO leads (business_id, client_id, title, interest, status, source,
                              channel, value, currency, created_at, updated_at,
                              last_activity_at, converted_at, lost_at, lost_reason,
                              intent, fit, order_id, first_message_id, last_message_id,
                              meta)
           VALUES (?,?,?,?,?,?,?,?,'RUB',?,?,?,?,?,?,?,?,?,?,?,?)""",
        (bid, cid, service, service, status, MARK, "telegram", PRICE[service],
         born, won_at or lost_at or born, won_at or lost_at or born,
         won_at, lost_at, code, intent, fit, order_id, first_mid, last_mid, meta))
    return _last_id(con, "leads", bid)


def _visit(con, bid, cid, service, when, day):
    """Состоявшийся приём: заявка и деньги в кассе. Возвращает номер заявки."""
    con.execute(
        """INSERT INTO orders (business_id, client_id, text, status, amount, created_at, source)
           VALUES (?,?,?,'выполнен',?,?,?)""",
        (bid, cid, service, PRICE[service], when, MARK))
    oid = _last_id(con, "orders", bid)
    con.execute(
        """INSERT INTO finance_entries (business_id, kind, category, amount, note,
                                        op_date, created_at, source, client_id)
           VALUES (?,'income','приём',?,?,?,?,?,?)""",
        (bid, PRICE[service], service, day, when, MARK, cid))
    return oid


def _won(con, bid, cid, service, days, hour, talk_days_before=3):
    """Полное звено: разговор → возможность → приём → деньги. Одним вызовом."""
    start = _ts(days + talk_days_before, hour=max(9, hour - 1))
    msgs = _talk_won(con, bid, cid, service, start)
    when, day = _ts(days, hour=hour), _date(days)
    oid = _visit(con, bid, cid, service, when, day)
    _lead(con, bid, cid, service, "won", start, won_at=when, order_id=oid, msgs=msgs)


def _lost(con, bid, cid, service, days, hour, code, human, *, intent, fit):
    """Полное звено отказа: разговор → возможность → причина кодом."""
    start = _ts(days + 4, hour=max(9, hour - 2))
    msgs = _talk_lost(con, bid, cid, service, start, code)
    _lead(con, bid, cid, service, "lost", start, lost_at=_ts(days, hour=hour),
          code=code, human=human, msgs=msgs, intent=intent, fit=fit)


# ── ПОТОКИ ──────────────────────────────────────────────────────────────────

def _routine(con, bid, clients):
    """
    Основной поток клиники: люди, которые просто пришли на приём.

    Не всякий приём вырастает из возможности — большинство записывается по
    телефону, повторно или по плану лечения. Без этого потока постоянные
    расходы не покрывались ничем, и Директор честно писал, что клиника в
    минусе: набор врал не выводом, а масштабом. Воронку этот поток не
    трогает — она считается по возможностям, а здесь их нет, и переписки
    здесь тоже нет: по телефону в мессенджер не пишут.
    """
    mix = (["Гигиена и чистка"] * 7 + ["Лечение кариеса"] * 6 + ["Удаление зуба"] * 3
           + ["Коронка керамическая"] * 2 + ["Отбеливание"] + ["Имплант под ключ"])
    # Последние в списке в поток не попадают: это те, кто пролечился и перестал
    # приходить. В живой клинике такие есть всегда, и заметить их — работа
    # VELOR, а не скрипта. Если бы поток покрывал всех, «пациент не вернулся»
    # стало бы невозможным по построению — то есть демо врало бы умолчанием.
    pool = clients[:-LAPSED]
    n = 0
    for days in range(1, 90):
        if (datetime.datetime.now() - datetime.timedelta(days=days)).weekday() == 6:
            continue                     # воскресенье — выходной
        for _ in range(random.choice((2, 2, 3, 3, 4))):
            service = random.choice(mix)
            cid = pool[random.randrange(len(pool))]
            n += 1
            _visit(con, bid, cid, service, _ts(days, hour=9 + n % 10), _date(days))


def _history(con, bid, clients):
    """
    Месяц-два назад: клиника работает ровно, ничего не происходит.

    Пациентов берём подряд, а не случайно: так покрытие перепиской получается
    ровным, и «у кого-то есть история, у кого-то нет» перестаёт быть лотереей.
    """
    plan = [("Гигиена и чистка", 5), ("Лечение кариеса", 4), ("Коронка керамическая", 2),
            ("Имплант под ключ", 2), ("Отбеливание", 2), ("Удаление зуба", 3)]
    i = 0
    for service, count in plan:
        for _ in range(count):
            days = random.randint(30, 88)
            cid = clients[i % len(clients)]
            i += 1
            _won(con, bid, cid, service, days, 11 + i % 7)

    # Те, кто перестал ходить: последний приём был давно, с тех пор тишина.
    for k, cid in enumerate(clients[-LAPSED:]):
        days = 42 + k * 8
        service = ["Лечение кариеса", "Гигиена и чистка", "Коронка керамическая",
                   "Гигиена и чистка", "Отбеливание", "Лечение кариеса"][k]
        _won(con, bid, cid, service, days, 12 + k % 6, talk_days_before=5)

    for k in range(4):                       # редкие отказы — фон, а не событие
        days = random.randint(32, 85)
        cid = clients[(i + k) % len(clients)]
        code, human = LOST_EARLY[k % len(LOST_EARLY)]
        _lost(con, bid, cid, "Лечение кариеса", days, 15, code, human,
              intent="medium", fit="unknown")


def _windows(con, bid, clients):
    """
    Две недели «до» и две недели «после».

    Числа подобраны так, чтобы разница была не шумом: в обоих окнах закрыто по
    десять возможностей — иначе «упало вдвое» означало бы «было две, стало
    одна». Что именно из этого следует, решает VELOR, а не скрипт.
    """
    # ДО: 7 записались, 3 отказались
    for k in range(7):
        days = random.randint(15, 27)
        cid = clients[(k * 3) % len(clients)]
        service = ["Имплант под ключ", "Гигиена и чистка", "Коронка керамическая",
                   "Лечение кариеса", "Имплант под ключ", "Отбеливание",
                   "Гигиена и чистка"][k]
        _won(con, bid, cid, service, days, 10 + k, talk_days_before=4)
    for k in range(3):
        days = random.randint(16, 27)
        code, human = LOST_EARLY[k % len(LOST_EARLY)]
        _lost(con, bid, clients[(k * 5 + 1) % len(clients)], "Лечение кариеса",
              days, 16, code, human, intent="medium", fit="unknown")

    # ПОСЛЕ: записались 4, отказались 6 — и причины у отказов теперь другие
    for k in range(4):
        days = random.randint(2, 13)
        cid = clients[(k * 7 + 2) % len(clients)]
        service = ["Гигиена и чистка", "Лечение кариеса", "Гигиена и чистка",
                   "Удаление зуба"][k]
        _won(con, bid, cid, service, days, 12 + k % 6)
    for k in range(6):
        days = random.randint(1, 13)
        service = ["Имплант под ключ", "Имплант под ключ", "Коронка керамическая",
                   "Имплант под ключ", "Брекеты, полный курс", "Коронка керамическая"][k]
        code, human = LOST_NOW[k]
        _lost(con, bid, clients[(k * 4 + 3) % len(clients)], service,
              days, 17, code, human, intent="high", fit="good")


def _waiting(con, bid, clients):
    """
    Люди написали и ждут. Ответа в переписке после их сообщения нет — именно
    так VELOR и понимает, что обращение висит: сравнивает последнее входящее
    с последним исходящим, а не читает пометку «не отвечено».
    """
    talks = [
        (clients[4], "Имплант под ключ", 41,
         ["Здравствуйте! Сколько будет имплант с коронкой?",
          "И есть ли рассрочка?"]),
        (clients[11], "Брекеты, полный курс", 27,
         ["Добрый вечер. Хочу поставить брекеты, дочери 14 лет.",
          "Можно записаться на субботу?"]),
        (clients[19], "Имплант под ключ", 20,
         ["Мне удалили зуб в другой клинике, нужен имплант. Возьмётесь?"]),
    ]
    for cid, service, hours, said in talks:
        # Разговор начался нормально: мы отвечали. Тишина наступила в конце.
        lines = [("user", "Здравствуйте!"),
                 ("assistant", "Здравствуйте! Слушаю вас.")]
        lines += [("user", t) for t in said]
        first, last = _say(con, bid, cid, _hours_ago(hours + 6), lines)
        _lead(con, bid, cid, service, "qualified", _hours_ago(hours + 6),
              msgs=(first, last), intent="high", fit="good")


def _money(con, bid):
    """Постоянные расходы клиники. Материалы растут — это и есть находка."""
    fixed = [("аренда", 180000), ("зарплаты", 520000), ("реклама", 40000),
             ("прочее", 22000)]
    for month, mat in ((2, 101000), (1, 108000), (0, 149000)):
        day = _date(month * 30 + 5)
        for cat, amount in fixed:
            con.execute(
                """INSERT INTO finance_entries (business_id, kind, category, amount,
                                                note, op_date, created_at, source)
                   VALUES (?,'expense',?,?,?,?,?,?)""",
                (bid, cat, amount, cat.capitalize(), day, _ts(month * 30 + 5, hour=10), MARK))
        con.execute(
            """INSERT INTO finance_entries (business_id, kind, category, amount,
                                            note, op_date, created_at, source)
               VALUES (?,'expense','материалы',?,'Расходники и импланты',?,?,?)""",
            (bid, mat, day, _ts(month * 30 + 5, hour=11), MARK))


# ── УДАЛЕНИЕ ────────────────────────────────────────────────────────────────
def clean():
    """
    Убрать демо и только демо.

    Два признака вместо одного намеренно: отметка в описании И точное имя.
    Скрипт, который удаляет бизнес по одному совпадению, однажды удалит не тот.
    """
    b = find_demo()
    if not b:
        print("Демо-клиники нет — удалять нечего.")
        return
    if (b.get("name") or "") != NAME:
        print("Найден бизнес с отметкой, но с другим именем — не трогаю: %r" % b.get("name"))
        return
    database.delete_business(b["id"])
    print("Демо-клиника удалена (бизнес №%s)." % b["id"])


def main():
    ap = argparse.ArgumentParser(description="Демо-набор: стоматология")
    ap.add_argument("--clean", action="store_true", help="удалить демо-клинику")
    ap.add_argument("--login", default="demo-dental", help="логин для входа в кабинет")
    ap.add_argument("--password", default=None, help="пароль (по умолчанию — случайный)")
    args = ap.parse_args()

    database.init_db()
    if args.clean:
        clean()
        return

    password = args.password or secrets.token_urlsafe(9)
    bid = build(args.login, password)
    if not bid:
        return

    span = database.data_span(bid)
    print("\nДемо-клиника готова. Бизнес №%s — «%s»" % (bid, NAME))
    print("  пациентов:    %d" % database.count_clients(bid))
    print("  возможностей: %d" % database.count_leads(bid))
    print("  данных за:    %d дней" % (span.get("days") or 0))

    # Сверка звеньев — не украшение вывода. Набор, на котором продукт сам
    # находит противоречия, показывать нельзя: первый же вопрос покупателя
    # «а почему тут не сходится» останется без ответа.
    try:
        import leads as _leads
        flaws = _leads.reconcile(bid)
        print("  противоречий: %d%s" % (len(flaws), "" if not flaws else "  ← ПОЧИНИТЬ"))
    except Exception as e:
        print("  сверка не прошла: %s" % e)

    print("\nВход в кабинет:")
    print("  логин:  %s" % args.login)
    print("  пароль: %s" % password)
    print("\nПароль показан один раз и нигде не сохранён — запишите его.")
    print("Убрать за собой: python tools/demo_dental.py --clean")


if __name__ == "__main__":
    main()
