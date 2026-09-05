# -*- coding: utf-8 -*-
"""
ДЕМО-НАБОР: СТОМАТОЛОГИЯ. Три месяца жизни клиники за одну минуту.

Зачем. Показать VELOR на пустой базе нельзя: Директор молчит, потому что ему
не с чем сравнивать, а находки не появляются, потому что нечего находить. И
это правильное поведение — но показывать его некому. Скрипт создаёт клинику,
у которой есть прошлое: пациенты, обращения, приёмы и деньги за 90 дней.

Чего скрипт НЕ делает — и это главное. Он не пишет выводы. Нигде в файле нет
строки «конверсия упала» или «ответьте этим троим». Он кладёт только СОБЫТИЯ,
как их положила бы живая клиника, а находит проблемы сам VELOR теми же
детекторами, что работают у настоящего клиента. Если завтра порог детектора
изменится, демо изменится вместе с ним — потому что оно не подрисовано.

Что спрятано в данных (сам скрипт об этом молчит, это заметки для человека):
  • две недели назад подняли цену на импланты — покупать стали заметно реже,
    а в причинах отказа появилось «дорого» и «ушёл в другую клинику»;
  • вечерние и выходные обращения висят без ответа со среды;
  • закупка материалов выросла на треть без роста приёмов;
  • несколько пациентов не приходили на гигиену больше двух месяцев.

Отдельный бизнес — отдельный арендатор. Демо живёт под своим business_id и с
настоящими данными не пересекается ни одной строкой: в VELOR каждая таблица
и почти каждая функция принимают business_id, и demo не исключение.

    python tools/demo_dental.py                     # создать
    python tools/demo_dental.py --login demo        # свой логин
    python tools/demo_dental.py --clean             # убрать за собой

Удаление работает ТОЛЬКО для бизнеса с отметкой демо-набора в описании. На
любом другом скрипт останавливается: снести рабочего клиента опечаткой в
аргументе — не та цена, которую стоит платить за удобство.
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


def _hours_ago(hours):
    d = datetime.datetime.now() - datetime.timedelta(hours=hours)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _date(days_ago):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _rub(n):
    return f"{n:,}".replace(",", " ") + " ₽"


# ── ПРАЙС КЛИНИКИ ───────────────────────────────────────────────────────────
# Цены живут в памяти бизнеса, а не в коде ответов: продавец VELOR берёт цифры
# только оттуда и не имеет права назвать свою.
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

LOST_EARLY = ["не дозвонились", "выбрал(а) другое время", "передумал(а)"]
LOST_NOW = ["дорого", "дорого", "ушёл(ла) в другую клинику", "дорого",
            "нашёл(ла) дешевле", "ушёл(ла) в другую клинику"]


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


def _lead(con, bid, cid, service, status, born, *, won_at=None, lost_at=None,
          reason=None, intent="high", fit="good"):
    """Одна возможность. Дата события важнее даты создания — по ней считают воронку."""
    con.execute(
        """INSERT INTO leads (business_id, client_id, title, interest, status, source,
                              channel, value, currency, created_at, updated_at,
                              last_activity_at, converted_at, lost_at, lost_reason,
                              intent, fit)
           VALUES (?,?,?,?,?,?,?,?,'RUB',?,?,?,?,?,?,?,?)""",
        (bid, cid, service, service, status, MARK, "telegram", PRICE[service],
         born, won_at or lost_at or born, won_at or lost_at or born,
         won_at, lost_at, reason, intent, fit))
    return _last_id(con, "leads", bid)


def _visit(con, bid, cid, service, when, day):
    """Состоявшийся приём: заявка и деньги в кассе."""
    con.execute(
        """INSERT INTO orders (business_id, client_id, text, status, amount, created_at, source)
           VALUES (?,?,?,'выполнен',?,?,?)""",
        (bid, cid, service, PRICE[service], when, MARK))
    con.execute(
        """INSERT INTO finance_entries (business_id, kind, category, amount, note,
                                        op_date, created_at, source, client_id)
           VALUES (?,'income','приём',?,?,?,?,?,?)""",
        (bid, PRICE[service], service, day, when, MARK, cid))


def _routine(con, bid, clients):
    """
    Основной поток клиники: люди, которые просто пришли на приём.

    Не всякий приём вырастает из возможности — большинство записывается по
    телефону, повторно или по плану лечения. Без этого потока постоянные
    расходы не покрывались ничем, и Директор честно писал, что клиника в
    минусе: набор врал не выводом, а масштабом. Воронку этот поток не
    трогает — она считается по возможностям, а здесь их нет.
    """
    # Доли приёмов у небольшой клиники: основа — гигиена и лечение, дорогое
    # случается редко. Отсюда и средний чек, а не из желания красивой цифры.
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
    """Месяц-два назад: клиника работает ровно, ничего не происходит."""
    plan = [("Гигиена и чистка", 5), ("Лечение кариеса", 4), ("Коронка керамическая", 2),
            ("Имплант под ключ", 2), ("Отбеливание", 2), ("Удаление зуба", 3)]
    i = 0
    for service, count in plan:
        for _ in range(count):
            days = random.randint(30, 88)
            cid = clients[i % len(clients)]
            i += 1
            when, day = _ts(days, hour=11 + i % 7), _date(days)
            _lead(con, bid, cid, service, "won", _ts(days + 3, hour=9), won_at=when)
            _visit(con, bid, cid, service, when, day)
    # Те, кто перестал ходить: последний приём был давно, с тех пор тишина.
    for k, cid in enumerate(clients[-LAPSED:]):
        days = 42 + k * 8
        service = ["Лечение кариеса", "Гигиена и чистка", "Коронка керамическая",
                   "Гигиена и чистка", "Отбеливание", "Лечение кариеса"][k]
        when = _ts(days, hour=12 + k % 6)
        _lead(con, bid, cid, service, "won", _ts(days + 5, hour=10), won_at=when)
        _visit(con, bid, cid, service, when, _date(days))

    for k in range(4):                       # редкие отказы — фон, а не событие
        days = random.randint(32, 85)
        cid = clients[(i + k) % len(clients)]
        _lead(con, bid, cid, "Лечение кариеса", "lost", _ts(days + 4, hour=12),
              lost_at=_ts(days, hour=15), reason=LOST_EARLY[k % len(LOST_EARLY)],
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
        when, day = _ts(days, hour=10 + k), _date(days)
        _lead(con, bid, cid, service, "won", _ts(days + 4, hour=9), won_at=when)
        _visit(con, bid, cid, service, when, day)
    for k in range(3):
        days = random.randint(16, 27)
        _lead(con, bid, clients[(k * 5 + 1) % len(clients)], "Лечение кариеса", "lost",
              _ts(days + 5, hour=13), lost_at=_ts(days, hour=16),
              reason=LOST_EARLY[k % len(LOST_EARLY)], intent="medium", fit="unknown")

    # ПОСЛЕ: записались 4, отказались 6 — и причины у отказов теперь другие
    for k in range(4):
        days = random.randint(2, 13)
        cid = clients[(k * 7 + 2) % len(clients)]
        service = ["Гигиена и чистка", "Лечение кариеса", "Гигиена и чистка",
                   "Удаление зуба"][k]
        when, day = _ts(days, hour=12 + k % 6), _date(days)
        _lead(con, bid, cid, service, "won", _ts(days + 3, hour=10), won_at=when)
        _visit(con, bid, cid, service, when, day)
    for k in range(6):
        days = random.randint(1, 13)
        service = ["Имплант под ключ", "Имплант под ключ", "Коронка керамическая",
                   "Имплант под ключ", "Брекеты, полный курс", "Коронка керамическая"][k]
        _lead(con, bid, clients[(k * 4 + 3) % len(clients)], service, "lost",
              _ts(days + 4, hour=11), lost_at=_ts(days, hour=17),
              reason=LOST_NOW[k], intent="high", fit="good")


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
        con.execute(
            """INSERT INTO messages (business_id, client_id, role, content, created_at, channel)
               VALUES (?,?,'user',?,?,'telegram')""",
            (bid, cid, "Здравствуйте!", _hours_ago(hours + 6)))
        con.execute(
            """INSERT INTO messages (business_id, client_id, role, content, created_at, channel)
               VALUES (?,?,'assistant',?,?,'telegram')""",
            (bid, cid, "Здравствуйте! Слушаю вас.", _hours_ago(hours + 5)))
        for n, text in enumerate(said):
            con.execute(
                """INSERT INTO messages (business_id, client_id, role, content,
                                         created_at, channel)
                   VALUES (?,?,'user',?,?,'telegram')""",
                (bid, cid, text, _hours_ago(hours - n)))
        _lead(con, bid, cid, service, "qualified",
              _hours_ago(hours + 6), intent="high", fit="good")


def _money(con, bid):
    """
    Постоянные расходы клиники. Материалы в последний месяц заметно дороже —
    приёмов столько же. Скрипт про это молчит: числа лежат, вывод не написан.
    """
    for month in range(3):
        days = 15 + month * 30
        for category, amount, note in (
            ("аренда", 180000, "аренда помещения"),
            ("зарплата", 520000, "выплата врачам и администратору"),
            ("реклама", 40000, "продвижение"),
            ("прочее", 22000, "хозяйственные расходы и связь"),
        ):
            con.execute(
                """INSERT INTO finance_entries (business_id, kind, category, amount, note,
                                                op_date, created_at, source)
                   VALUES (?,'expense',?,?,?,?,?,?)""",
                (bid, category, amount, note, _date(days), _ts(days, hour=10), MARK))
    # Материалы в последнем месяце заметно дороже при том же числе приёмов.
    # Вывод отсюда делает VELOR, скрипт только кладёт суммы.
    for days, amount in ((70, 101000), (40, 108000), (10, 149000)):
        con.execute(
            """INSERT INTO finance_entries (business_id, kind, category, amount, note,
                                            op_date, created_at, source)
               VALUES (?,'expense','материалы',?,?,?,?,?)""",
            (bid, amount, "закупка расходных материалов", _date(days),
             _ts(days, hour=14), MARK))


# ── УДАЛЕНИЕ ────────────────────────────────────────────────────────────────
def clean():
    b = find_demo()
    if not b:
        print("Демо-клиники нет — удалять нечего.")
        return
    # Двойная проверка перед удалением: отметка в описании И имя. Стоимость
    # ошибки здесь — чужие данные, поэтому лишняя проверка дешевле сожаления.
    if BADGE not in (b.get("about") or "") or b.get("name") != NAME:
        print("СТОП: это не демо-бизнес. Ничего не удалено.")
        return
    database.delete_business(int(b["id"]))
    print("Демо-клиника №%s удалена вместе со всеми её записями." % b["id"])


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
    print("\nВход в кабинет:")
    print("  логин:  %s" % args.login)
    print("  пароль: %s" % password)
    print("\nПароль показан один раз и нигде не сохранён — запишите его.")
    print("Убрать за собой: python tools/demo_dental.py --clean")


if __name__ == "__main__":
    main()
