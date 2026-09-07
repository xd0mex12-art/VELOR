# -*- coding: utf-8 -*-
"""
Демо-набор под любой бизнес: «стоматологическая клиника», «автосервис»,
«юридическая компания» — что попросят.

Зачем он есть
─────────────
Показать VELOR пустым нельзя: продукт весь про выводы из данных, а из нуля
данных вывод один — «данных нет». Набор даёт компанию с трёхмесячной историей,
у которой всё сходится: клиенты, разговоры, возможности, заявки и деньги — одно
и то же событие, увиденное с разных сторон.

Чего здесь принципиально НЕТ
────────────────────────────
Ни одного написанного вывода. Ни одной находки, вписанной руками. Всё, что
VELOR скажет об этой компании, он выведет сам детекторами из `initiatives.py`.
Набор кладёт факты — думает продукт. Иначе демо превращается в театр: красиво
на показе и разваливается на первом же вопросе «а откуда он это взял?».

Почему набор перестал быть стоматологией
────────────────────────────────────────
VELOR не медицинский продукт. Клиники сейчас удобны для звонков, но показывать
автосервису стоматологию — значит заставлять его переводить каждый экран на
свой язык. Поэтому разделено надвое:

  • МЕХАНИКА (этот файл, низ) — связность и сюжет. Она одинакова для всех:
    разговор → возможность → заявка → деньги, два сравнимых окна, зависшие
    обращения, растущая статья расходов. Именно её читают детекторы.
  • ПРОФИЛЬ (`profiles.py`) — язык отрасли: услуги с ценами, статьи расходов,
    как зовут клиента и как называется заявка.

Механика ничего не знает про отрасль. Она берёт услуги и раскладывает их по
цене: дешёвые идут в поток (их покупают часто), дорогие — в недавние потери
(на них и спорят о цене). Поэтому новый профиль — это несколько строк, а не
второй такой файл.

Связность
─────────
Цепочка замкнута: разговор → возможность → приём → деньги. У каждой
возможности есть первое и последнее сообщение, у каждой выигранной — заявка, у
каждой проигранной — причина кодом, а не человеческой фразой (человеческая
лежит рядом, в meta: она для чтения, код — для подсчёта). Сверка продукта
(`leads.reconcile`) на готовом наборе обязана давать ноль противоречий.

Заявки создаются со связью на услугу (`graph.link_order_items`) — как их
создаёт живой продукт. Раньше набор писал заказы прямым SQL мимо этого шага, и
Директор на демо честно писал «какая услуга приносит больше — сказать нельзя»,
хотя в живом бизнесе связь есть и вывод считается. Демо недоговаривало о
собственном продукте.

Что при этом НЕ связано и почему
────────────────────────────────
Основной поток заявок (`_routine`) возможностей не имеет и переписки не
рождает. Так и в жизни: большинство записывается по телефону, повторно или по
плану. Привязать всё ко всему ради красивой цифры значило бы соврать — воронка
показала бы конверсию, которой не было.
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database                                                   # noqa: E402
import graph                                                      # noqa: E402
import profiles                                                   # noqa: E402

MARK = "demo-velor"                  # источник строк: по нему видно происхождение
BADGE = "[ДЕМО-НАБОР VELOR]"         # отметка бизнеса: по ней и только по ней удаляем

random.seed(20260907)                # одинаковый набор при каждом запуске


# ── ВРЕМЯ ───────────────────────────────────────────────────────────────────
def _ts(days_ago, hour=None, minute=0):
    d = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    if hour is not None:
        d = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _shift(stamp, minutes):
    d = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    return (d + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _hours_ago(hours):
    return (datetime.datetime.now()
            - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _date(days_ago):
    return (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()


def _rub(n):
    return f"{n:,}".replace(",", " ") + " ₽"


# ── ПОИСК И ЗАЩИТА ──────────────────────────────────────────────────────────
def find_demos():
    """Все демо-бизнесы. Признак один — отметка в описании."""
    return [b for b in database.list_businesses_with_stats()
            if BADGE in (b.get("about") or "")]


def find_demo():
    """Первый демо-бизнес — для тех, кому нужен «какой-нибудь»."""
    got = find_demos()
    return got[0] if got else None


# ── СОЗДАНИЕ ────────────────────────────────────────────────────────────────
def build(login=None, password=None, request="стоматологическая клиника"):
    """
    Собрать демо-набор под запрошенный вид бизнеса.

    request — то, что владелец написал своими словами: «автосервис»,
    «юридическая компания», «студия маникюра». Профиль подбирается по нему.
    """
    p = profiles.profile_for(request)
    login = login or ("demo-" + p["kind"])
    password = password or secrets.token_urlsafe(9)

    name = "Демо: " + p["title"]
    try:
        bid = database.create_business(
            name=name, about=BADGE + " " + p["about"], greeting=p["greeting"],
            login=login, password=password)
    except Exception:
        # Логин занят — такой набор уже собран. Наборов может быть несколько,
        # но двух одинаковых быть не должно: у них совпал бы вход.
        return None
    if not bid:
        return None

    database.update_business(
        bid,
        knowledge="\n".join("%s — %s. %s" % (n, _rub(pr), d) for n, pr, d in p["services"]),
        tone=p["tone"], ai_name=p["ai_name"], about=BADGE + " " + p["about"])
    for n, pr, d in p["services"]:
        database.add_fact(bid, "service", n, body="%s. Цена %s." % (d, _rub(pr)),
                          data={"price": pr}, verified=True)

    with database._connect() as con:
        clients = _clients(con, bid, p)
        _routine(con, bid, clients, p)      # основной поток заявок
        _history(con, bid, clients, p)      # 90 дней обычной жизни
        _windows(con, bid, clients, p)      # две недели «до» и две «после»
        _waiting(con, bid, clients, p)      # обращения без ответа
        _money(con, bid, p)                 # постоянные расходы и растущая статья

    # Связи «заявка включает услугу» ставим тем же кодом, что и живой продукт.
    # Делать это внутри транзакции нельзя: graph открывает своё соединение.
    _link_services(bid)
    # Цели владельца. Ставятся от уже записанных чисел, а не выдумываются.
    _goals(bid, p)
    # Оценку возможностей делает продукт, а не скрипт: иначе список и карточка
    # расходятся на глазах у зрителя.
    _qualify(bid)
    # И находки тоже. Демо, у которого «Работа VELOR» пуста до первого
    # получасового обхода, показывать нельзя.
    _notice(bid)
    return {"business_id": bid, "login": login, "password": password,
            "name": name, "kind": p["kind"], "profile_source": p.get("source", "каталог")}


def _link_services(bid):
    for o in database.get_orders(bid, limit=10000) or []:
        try:
            graph.link_order_items(bid, o["id"], o.get("text") or "")
        except Exception:
            continue


def _goals(bid, p):
    """
    Две цели: по деньгам и по людям — как их ставит живой владелец.

    Планка считается от того, что бизнес показывает на самом деле, и ставится
    ВЫШЕ факта: цель, которая уже выполнена, ничего не говорит, а цель с
    потолка врала бы о наборе. Месяц отсчитывается с первого числа — так его и
    считают в жизни.
    """
    import datetime
    since = datetime.date.today().replace(day=1).isoformat()
    with database._connect() as con:
        income = con.execute(
            """SELECT COALESCE(SUM(amount),0) FROM finance_entries
                WHERE business_id = ? AND kind = 'income'
                  AND COALESCE(op_date, date(created_at)) >= ?""", (bid, since)).fetchone()[0] or 0
        clients = con.execute(
            """SELECT COUNT(*) FROM clients WHERE business_id = ?
                 AND date(created_at) >= ?""", (bid, since)).fetchone()[0] or 0
    month_end = (datetime.date.today().replace(day=28)
                 + datetime.timedelta(days=4))
    month_end = (month_end - datetime.timedelta(days=month_end.day)).isoformat()
    # Планка примерно вдвое выше набранного к этому дню: месяц ещё идёт, и по
    # темпу цель достижима — отставание в проценте, а не приговор.
    database.add_goal(bid, "income", p.get("goal_money") or "Выручка за месяц",
                      _round_to(max(int(income * 2.2), 100000), 10000),
                      deadline=month_end, started_on=since)
    database.add_goal(bid, "clients", p.get("goal_people") or "Новые клиенты за месяц",
                      max(int(clients * 2) + 1, 5),
                      deadline=month_end, started_on=since)


def _qualify(bid):
    """
    Пересчитать оценку каждой возможности по её же переписке.

    Той самой функцией, которой продукт слушает живого клиента. Скрипт больше
    не утверждает, что намерение высокое, — он даёт VELOR прочитать слова и
    согласиться или не согласиться. Что получится, то и увидит зритель: и в
    списке, и в карточке, и в объяснении «почему».
    """
    import qualify
    for lead in database.list_leads(bid, limit=1000) or []:
        for msg in database.lead_messages(bid, lead["id"]) or []:
            if (msg.get("role") or "") != "user":
                continue
            qualify.observe(bid, lead["id"], msg.get("content") or "",
                            message_id=msg.get("id"))


def _notice(bid):
    """Пройти по бизнесу теми же детекторами, что и получасовой обход."""
    import initiatives
    try:
        initiatives.scan(bid, use_ai=False)     # без ИИ: набор собирается и без ключей
    except Exception:
        pass


def _clients(con, bid, p):
    """Клиенты. Дата регистрации размазана по трём месяцам — как приходили."""
    names = p["clients"]
    out = []
    for i, name in enumerate(names):
        con.execute(
            """INSERT INTO clients (business_id, name, phone, created_at, source, notes)
               VALUES (?,?,?,?,?,?)""",
            (bid, name, "+7 9%02d %03d-%02d-%02d" % (i % 100, 100 + i, i % 60, (i * 7) % 60),
             _ts(88 - int(i * 88 / len(names)), hour=10 + i % 8), MARK,
             "Пришли по рекомендации" if i % 4 == 0 else None))
        out.append(_last_id(con, "clients", bid))
    return out


def _last_id(con, table, bid):
    return con.execute("SELECT MAX(id) FROM %s WHERE business_id = ?" % table,
                       (bid,)).fetchone()[0]


# ── РАЗГОВОРЫ ───────────────────────────────────────────────────────────────
#
# Возможность без разговора — это цифра без причины. Открыв такую в кабинете,
# покупатель видит «Имплант, 78 000, выиграна» и не может спросить «а как?».

def _say(con, bid, cid, start, lines):
    """
    Записать разговор. Возвращает (id первой реплики, id последней).

    Реплики расставлены по минутам: одинаковое время у всех сообщений выглядит
    как импорт, а не как разговор, и ломает порядок в кабинете.
    """
    first = last = None
    for k, (role, text) in enumerate(lines):
        con.execute(
            """INSERT INTO messages (business_id, client_id, role, content, created_at)
               VALUES (?,?,?,?,?)""",
            (bid, cid, role, text, _shift(start, k * 3)))
        last = _last_id(con, "messages", bid)
        first = first or last
    return first, last


def _talk_won(con, bid, cid, service, price, start, p):
    """Разговор, который закончился сделкой."""
    lines = [("user", random.choice(profiles.openers(service, p))),
             ("assistant", "Здравствуйте! %s — %s. %s" % (service, _rub(price), p["ask"]))]
    for text in random.choice(p["agree"]):
        lines.append(("user", text))
    lines.append(("assistant", p["done"]))
    return _say(con, bid, cid, start, lines)


def _talk_lost(con, bid, cid, service, price, start, code, p):
    """
    Разговор, который закончился отказом.

    У «не дозвонились» финальной реплики клиента нет — и не должно быть: тишина
    и есть тот самый отказ. Дописать туда «извините, не буду» значило бы
    подделать событие, которого не происходило.
    """
    lines = [("user", random.choice(profiles.openers(service, p))),
             ("assistant", "Здравствуйте! %s — %s. %s" % (service, _rub(price), p["ask"]))]
    if code == "no_response":
        return _say(con, bid, cid, start, lines)
    for text in random.choice(profiles.REFUSE.get(code) or profiles.REFUSE["other"]):
        lines.append(("user", text))
    lines.append(("assistant", "Поняли, спасибо. Будем рады, если передумаете."))
    return _say(con, bid, cid, start, lines)


# ── ЗВЕНЬЯ ЦЕПОЧКИ ──────────────────────────────────────────────────────────

def _lead(con, bid, cid, service, price, status, born, *, won_at=None, lost_at=None,
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
        (bid, cid, service, service, status, MARK, "telegram", price,
         born, won_at or lost_at or born, won_at or lost_at or born,
         won_at, lost_at, code, intent, fit, order_id, first_mid, last_mid, meta))
    return _last_id(con, "leads", bid)


def _visit(con, bid, cid, service, price, when, day, p):
    """Состоявшаяся заявка: работа сделана и деньги в кассе. Возвращает её номер."""
    con.execute(
        """INSERT INTO orders (business_id, client_id, text, status, amount, created_at, source)
           VALUES (?,?,?,'выполнен',?,?,?)""",
        (bid, cid, service, price, when, MARK))
    oid = _last_id(con, "orders", bid)
    con.execute(
        """INSERT INTO finance_entries (business_id, kind, category, amount, note,
                                        op_date, created_at, source, client_id)
           VALUES (?,'income',?,?,?,?,?,?,?)""",
        (bid, p["income_category"], price, service, day, when, MARK, cid))
    return oid


def _won(con, bid, cid, service, price, days, hour, p, talk_days_before=3):
    """Полное звено: разговор → возможность → заявка → деньги. Одним вызовом."""
    start = _ts(days + talk_days_before, hour=max(9, hour - 1))
    msgs = _talk_won(con, bid, cid, service, price, start, p)
    when, day = _ts(days, hour=hour), _date(days)
    oid = _visit(con, bid, cid, service, price, when, day, p)
    _lead(con, bid, cid, service, price, "won", start, won_at=when, order_id=oid, msgs=msgs)


def _lost(con, bid, cid, service, price, days, hour, code, human, p, *, intent, fit):
    """Полное звено отказа: разговор → возможность → причина кодом."""
    start = _ts(days + 4, hour=max(9, hour - 2))
    msgs = _talk_lost(con, bid, cid, service, price, start, code, p)
    _lead(con, bid, cid, service, price, "lost", start, lost_at=_ts(days, hour=hour),
          code=code, human=human, msgs=msgs, intent=intent, fit=fit)


# ── ПОТОКИ ──────────────────────────────────────────────────────────────────
#
# Отрасль сюда не попадает вовсе: услуги раскладываются по цене. Дешёвые —
# поток (их покупают часто), дорогие — недавние потери (на них и спорят о
# цене). Так сюжет получается одинаковым у стоматологии и у автосервиса, а
# слова — разными.

def _cheap(p):
    """Услуги дешевле медианы плюс сама медиана — то, чем живут каждый день."""
    order = sorted(p["services"], key=lambda s: s[1])
    return order[:max(2, (len(order) + 1) // 2)]


def _pricey(p):
    """Три самые дорогие: именно из-за них торгуются и уходят к другим."""
    return sorted(p["services"], key=lambda s: -s[1])[:3]


LAPSED = 6                           # клиентов, которые купили и не вернулись


def _routine(con, bid, clients, p):
    """
    Основной поток: люди, которые просто пришли и купили.

    Не всякая заявка вырастает из возможности — большинство приходит по
    телефону, повторно или по плану. Без этого потока постоянные расходы не
    покрывались бы ничем, и Директор честно писал бы, что бизнес в минусе:
    набор врал бы не выводом, а масштабом. Воронку поток не трогает — она
    считается по возможностям, а здесь их нет, и переписки здесь тоже нет: по
    телефону в мессенджер не пишут.

    Последние в списке клиентов в поток не попадают: это те, кто купил и
    перестал приходить. Такие есть всегда, и заметить их — работа VELOR, а не
    скрипта. Если бы поток покрывал всех, «клиент не вернулся» стало бы
    невозможным по построению, то есть демо врало бы умолчанием.
    """
    cheap = _cheap(p)
    mix = []
    for k, (name, price, _) in enumerate(cheap):
        mix += [(name, price)] * (len(cheap) - k + 1)     # чем дешевле, тем чаще
    pool = clients[:-LAPSED]
    n = 0
    for days in range(1, 90):
        if (datetime.datetime.now() - datetime.timedelta(days=days)).weekday() == 6:
            continue                     # воскресенье — выходной
        for _ in range(3):
            # Услуги перебираем по кругу, а не жребием. Случайный выбор при
            # разбросе цен от 900 до 520 000 давал выручку, скачущую на
            # десятки процентов между месяцами, — и Директор честно писал
            # «выручка выросла на 72%». Это была не история бизнеса, а
            # дрожание генератора: демо начинало объяснять собственный шум.
            name, price = mix[n % len(mix)]
            n += 1
            _visit(con, bid, pool[n % len(pool)], name, price,
                   _ts(days, hour=9 + n % 10), _date(days), p)


def _history(con, bid, clients, p):
    """
    Месяц-два назад: компания работает ровно, ничего не происходит.

    Клиентов берём подряд, а не случайно: так покрытие перепиской получается
    ровным, и «у кого-то есть история, у кого-то нет» перестаёт быть лотереей.
    """
    # Прошлые месяцы наполняем ТЕМ ЖЕ кругом услуг и в том же количестве, что
    # и текущий. Иначе выручка едет сама собой: раньше история состояла почти
    # из одних дешёвых услуг, а обе двухнедельные витрины с дорогими попадали
    # в текущий месяц целиком — и Директор честно писал «выручка выросла на
    # 79%», объясняя не бизнес, а раскладку скрипта.
    #
    # Теперь месяцы сравнимы по составу, и различий между ними ровно два, оба
    # намеренные: растущая статья расходов и упавшая конверсия.
    ring = _pricey(p) + _cheap(p)
    cheap = _cheap(p)
    i = 0
    for lo, hi in ((62, 86), (32, 56)):      # позапрошлый и прошлый месяцы
        for k in range(9):
            name, price, _ = ring[k % len(ring)]
            i += 1
            _won(con, bid, clients[i % len(clients)], name, price,
                 random.randint(lo, hi), 11 + i % 7, p)

    # Те, кто перестал приходить: последняя покупка была давно, с тех пор тишина.
    for k, cid in enumerate(clients[-LAPSED:]):
        name, price, _ = cheap[k % len(cheap)]
        _won(con, bid, cid, name, price, 42 + k * 8, 12 + k % 6, p, talk_days_before=5)

    for k in range(4):                       # редкие отказы — фон, а не событие
        name, price, _ = cheap[k % len(cheap)]
        code, human = profiles.LOST_EARLY[k % len(profiles.LOST_EARLY)]
        _lost(con, bid, clients[(i + k) % len(clients)], name, price,
              random.randint(32, 85), 15, code, human, p, intent="medium", fit="unknown")


def _windows(con, bid, clients, p):
    """
    Две недели «до» и две недели «после».

    Числа подобраны так, чтобы разница была не шумом: в обоих окнах закрыто по
    десять возможностей — иначе «упало вдвое» означало бы «было две, стало
    одна». Что именно из этого следует, решает VELOR, а не скрипт.
    """
    cheap, pricey = _cheap(p), _pricey(p)

    # Услуги в обоих окнах берём из одного и того же круга: разной должна быть
    # только конверсия. Когда в прошлом окне стояли дорогие, а в нынешнем
    # дешёвые, выручка падала сама собой на четверть — и «упала выручка»
    # спорило с «выросли расходы» за место главной причины, хотя обе цифры
    # были подстроены скриптом.
    ring = pricey + cheap

    # ДО: 7 купили, 3 отказались
    for k in range(7):
        name, price, _ = ring[k % len(ring)]
        _won(con, bid, clients[(k * 3) % len(clients)], name, price,
             random.randint(15, 27), 10 + k, p, talk_days_before=4)
    for k in range(3):
        name, price, _ = cheap[k % len(cheap)]
        code, human = profiles.LOST_EARLY[k % len(profiles.LOST_EARLY)]
        _lost(con, bid, clients[(k * 5 + 1) % len(clients)], name, price,
              random.randint(16, 27), 16, code, human, p, intent="medium", fit="unknown")

    # ПОСЛЕ: купили 4, отказались 6 — и причины у отказов теперь другие.
    #
    # Два дня из четырёх заданы жёстко: вчера и сегодня. Случайный выбор из
    # 1..13 оставлял вчерашний день пустым примерно в каждом втором наборе, и
    # брифинг за день — экран, который открывают первым, — показывал нули по
    # всем строкам у бизнеса, который работает каждый день.
    days_after = [0, 1] + [random.randint(3, 13) for _ in range(2)]
    for k in range(4):
        name, price, _ = ring[k % len(ring)]
        _won(con, bid, clients[(k * 7 + 2) % len(clients)], name, price,
             days_after[k], 12 + k % 6, p)
    for k in range(6):
        name, price, _ = pricey[k % len(pricey)]
        code, human = profiles.LOST_NOW[k]
        _lost(con, bid, clients[(k * 4 + 3) % len(clients)], name, price,
              random.randint(1, 13), 17, code, human, p, intent="high", fit="good")


def _waiting(con, bid, clients, p):
    """
    Люди написали и ждут. Ответа в переписке после их сообщения нет — именно
    так VELOR и понимает, что обращение висит: сравнивает последнее входящее
    с последним исходящим, а не читает пометку «не отвечено».
    """
    pricey = _pricey(p)
    for k, (cid_idx, hours) in enumerate(((4, 41), (11, 27), (19, 20))):
        name, price, _ = pricey[k % len(pricey)]
        cid = clients[cid_idx % len(clients)]
        lines = [("user", "Здравствуйте!"),
                 ("assistant", "Здравствуйте! Слушаю вас.")]
        lines += [("user", t) for t in profiles.waiting_lines(name, k)]
        first, last = _say(con, bid, cid, _hours_ago(hours + 6), lines)
        _lead(con, bid, cid, name, price, "qualified", _hours_ago(hours + 6),
              msgs=(first, last), intent="high", fit="good")


def _income_of_month(con, bid, month):
    """Сколько компания заработала в этом месяце — по уже записанным приходам."""
    row = con.execute(
        """SELECT COALESCE(SUM(amount),0) FROM finance_entries
            WHERE business_id = ? AND kind = 'income'
              AND op_date >= ? AND op_date < ?""",
        (bid, _date(month * 30 + 30), _date(month * 30))).fetchone()
    return int(row[0] or 0)


def _round_to(n, step=100):
    return int(round(n / step) * step)


def _money(con, bid, p):
    """
    Постоянные расходы. Одна статья растёт — это и есть находка.

    Суммы считаются ОТ ВЫРУЧКИ, а не задаются числом в профиле. Когда они были
    числами, набор приходилось балансировать под каждую отрасль вручную, и три
    профиля из шести показывали убыток: у магазина посуды выручка с чеков по
    три тысячи не покрывала те же 650 000 расходов, что у стоматологии.
    Демо, на котором бизнес в минусе, показывать нельзя — обсуждать будут не
    продукт, а придуманное банкротство.

    Профиль задаёт только пропорции: какие статьи и в каких долях. Доля
    расходов в выручке растёт от месяца к месяцу — отсюда и падение прибыли,
    и растущая статья. Сюжет один, цифры у каждой отрасли свои.
    """
    grow_cat = p["growing"]
    grows = p.get("grow_share", (0.13, 0.15, 0.21))
    base = p.get("fixed_share", 0.55)
    weights = p["expenses"]
    total_w = sum(w for _, w in weights) or 1.0

    # Постоянные расходы считаем от СРЕДНЕЙ выручки за три месяца и держим
    # одинаковыми. Когда они считались от выручки своего месяца, получалось
    # «расходы на зарплаты выросли на 52%» — при том, что никого не нанимали:
    # цифра просто ехала следом за выручкой. Аренда и зарплата так себя не
    # ведут, и находка о них была бы враньём.
    #
    # Меняется одна статья — та, что и должна расти. Тогда у падения прибыли
    # ровно одна причина, и владелец на показе видит её сразу.
    months = [(k, m, _income_of_month(con, bid, m)) for k, m in enumerate((2, 1, 0))]
    live = [inc for _, _, inc in months if inc > 0]
    if not live:
        return
    avg = sum(live) / len(live)

    # Дни, по которым раскладывается месяц расходов. Раньше весь месяц писался
    # одним днём, и календарный месяц-до-сегодня содержал полный набор расходов
    # против неполной выручки — брифинг показывал убыток у прибыльного бизнеса.
    # Пять платежей по окну — и доля, попавшая в текущий месяц, растёт вместе с
    # долей выручки. Заодно это просто правдоподобнее: никто не платит аренду,
    # зарплату и закупку в один день.
    # Дни выбраны так, чтобы обе половины месяца получили ПОРОВНУ платежей:
    # 1, 5, 9 — в первые две недели, 15, 19, 23 — во вторые. Иначе Директор,
    # который сравнивает две недели с двумя предыдущими, видел бы «расходы на
    # зарплаты снизились на 33%» там, где ничего не менялось: просто в одно
    # окно попало два платежа, а в другое три.
    SPREAD = (1, 5, 9, 15, 19, 23)

    def _pay(month, cat, amount, hour):
        """Один расход, разложенный на пять платежей внутри своего окна."""
        part = _round_to(amount / len(SPREAD))
        for n, shift in enumerate(SPREAD):
            back = month * 30 + shift
            # Последний платёж добирает остаток: пять округлённых частей не
            # обязаны сложиться ровно в исходную сумму, а расходы у нас точные.
            take = part if n < len(SPREAD) - 1 else amount - part * (len(SPREAD) - 1)
            if take <= 0:
                continue
            con.execute(
                """INSERT INTO finance_entries (business_id, kind, category, amount,
                                                note, op_date, created_at, source)
                   VALUES (?,'expense',?,?,?,?,?,?)""",
                (bid, cat, int(take), cat.capitalize(), _date(back),
                 _ts(back, hour=hour), MARK))

    for k, month, income in months:
        if income <= 0:
            continue
        rest = avg * base
        for cat, weight in weights:
            _pay(month, cat, _round_to(rest * weight / total_w), 10)
        _pay(month, grow_cat, _round_to(avg * grows[k]), 11)


# ── УДАЛЕНИЕ ────────────────────────────────────────────────────────────────
def clean(business_id=None):
    """
    Убрать демо и только демо.

    Отметка в описании — единственный признак, и проверяется он у каждого
    бизнеса отдельно. Скрипт, который удаляет по одному совпадению имени,
    однажды удалит не тот.
    """
    removed = []
    for b in find_demos():
        if business_id and b["id"] != int(business_id):
            continue
        database.delete_business(b["id"])
        removed.append(b["id"])
    return removed


def main():
    ap = argparse.ArgumentParser(description="Демо-набор VELOR под любой бизнес")
    ap.add_argument("kind", nargs="?", default="стоматологическая клиника",
                    help="вид бизнеса своими словами: автосервис, юридическая компания…")
    ap.add_argument("--clean", action="store_true", help="удалить все демо-наборы")
    ap.add_argument("--login", default=None)
    ap.add_argument("--password", default=None)
    args = ap.parse_args()

    database.init_db()
    if args.clean:
        got = clean()
        print("Удалено демо-наборов: %d" % len(got) if got else "Демо-наборов нет.")
        return

    got = build(args.login, args.password, args.kind)
    if not got:
        print("Не собралось: такой логин уже занят.")
        return
    bid = got["business_id"]
    span = database.data_span(bid)
    print("\nДемо готово. Бизнес №%s — «%s»" % (bid, got["name"]))
    print("  профиль:      %s (%s)" % (got["kind"], got["profile_source"]))
    print("  клиентов:     %d" % database.count_clients(bid))
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
    print("  логин:  %s" % got["login"])
    print("  пароль: %s" % got["password"])
    print("\nПароль показан один раз и нигде не сохранён — запишите его.")
    print("Убрать за собой: python tools/demo_business.py --clean")


if __name__ == "__main__":
    main()
