# -*- coding: utf-8 -*-
"""
Живой поток событий для показа VELOR.

Зачем. Кабинет опрашивает сервер раз в 30 секунд и показывает изменения:
число всплывает на место старого, живое поле отвечает волной, а при новом
выводе Директора — сжатием к центру. Увидеть это на статичной базе нельзя:
нечему меняться. Скрипт добавляет НАСТОЯЩИЕ записи в настоящие таблицы, и
кабинет реагирует на них ровно так же, как реагировал бы на живого клиента.

Чего скрипт НЕ делает. Не трогает фронт, не дёргает анимации напрямую, не
подсовывает выдуманные показатели. Всё, что появится на экране, Директор
посчитает сам по строкам в базе. Если остановить скрипт, картина застынет
на последнем настоящем состоянии, а не рассыплется.

Каждая запись помечена source='demo' — поэтому `--clean` убирает ровно то,
что скрипт добавил, и ничего больше.

    python tools/live_demo.py --business 1              # поток, шаг 12 с
    python tools/live_demo.py --business 1 --every 20   # медленнее
    python tools/live_demo.py --business 1 --clean      # убрать за собой
"""
import argparse
import datetime
import os
import sqlite3
import sys
import time

# Знак рубля не существует в cp1251, а Windows назначает эту кодировку, как
# только вывод уходит в файл или в конвейер. Без этой строки скрипт падает
# на первой же строке про деньги — но только при запуске в фоне, из-за чего
# ошибку и не видно в консоли.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MARK = "demo"


def _db():
    from database import DB_PATH  # путь берём у продукта, а не угадываем
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _ts(days_ago=0, hour=None):
    d = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    if hour is not None:
        d = d.replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _date(days_ago=0):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _rub(n):
    """Пробел между тысячами — и только там. Замена запятой во всей строке
    съедала запятую в названии тарифа: «Подписка, тариф» → «Подписка  тариф»."""
    return f"{n:,}".replace(",", " ") + " ₽"


# ── СЦЕНАРИЙ ────────────────────────────────────────────────────────────────
# Порядок не случайный: сначала обычная жизнь (числа шевелятся, поле отвечает
# волной), потом появляется срочный риск — Директор меняет набор решений, и
# поле стягивается. Потом риск снимается, и поле распускается обратно.
# Именно на этих трёх переходах и видно, что фон говорит о деле, а не живёт
# своей жизнью.
def step_client(con, bid, i):
    names = ["Пекарня «Мякиш»", "Студия «Контур»", "Кофейня «Пар»", "Мастерская «Обод»",
             "Салон «Ветка»", "Сервис «Ключ»", "Клиника «Плюс»", "Школа «Слог»"]
    name = names[i % len(names)]
    con.execute("INSERT INTO clients (business_id, name, created_at, source) VALUES (?,?,?,?)",
                (bid, name, _ts(), MARK))
    cid = con.execute("SELECT MAX(id) FROM clients WHERE business_id=?", (bid,)).fetchone()[0]
    return f"новый клиент: {name}", cid


def step_order(con, bid, i):
    row = con.execute(
        "SELECT id, name FROM clients WHERE business_id=? ORDER BY id DESC LIMIT 1", (bid,)).fetchone()
    texts = [("Подписка, тариф «Старт»", 9900), ("Подписка на квартал", 21000),
             ("Подключение под ключ", 39000), ("Подписка, тариф «Бизнес»", 29000)]
    text, amount = texts[i % len(texts)]
    con.execute(
        """INSERT INTO orders (business_id, client_id, text, amount, status, created_at, source)
           VALUES (?,?,?,?,'в работе',?,?)""",
        (bid, row["id"] if row else None, text, amount, _ts(), MARK))
    return f"заявка: {text} — {_rub(amount)}", None


def step_income(con, bid, i):
    amounts = [9900, 21000, 29000, 39000]
    a = amounts[i % len(amounts)]
    con.execute(
        """INSERT INTO finance_entries (business_id, kind, category, amount, note,
                                        op_date, created_at, source)
           VALUES (?,'income','подписки',?,?,?,?,?)""",
        (bid, a, "оплата подписки", _date(), _ts(), MARK))
    return f"пришли деньги: {_rub(a)}", None


def step_expense(con, bid, i):
    con.execute(
        """INSERT INTO finance_entries (business_id, kind, category, amount, note,
                                        op_date, created_at, source)
           VALUES (?,'expense','реклама',?,?,?,?,?)""",
        (bid, 34000, "закупка рекламы", _date(), _ts(), MARK))
    return "расход: реклама 34 000 ₽", None


def step_stale(con, bid, i):
    """Заявка, которая висит четвёртый день.

    Директор считает такие срочным риском (level='urgent'), меняет набор
    рекомендаций — и кабинет отвечает сразу двумя вещами: импульс «новый вывод»
    сходится к центру, а живое поле переходит в состояние «риск» и стягивается.
    """
    row = con.execute(
        "SELECT id FROM clients WHERE business_id=? ORDER BY id DESC LIMIT 1", (bid,)).fetchone()
    con.execute(
        """INSERT INTO orders (business_id, client_id, text, amount, status, created_at, source)
           VALUES (?,?,?,?,'новый',?,?)""",
        (bid, row["id"] if row else None, "Срочно: перенос базы на новый филиал",
         45000, _ts(4, 11), MARK))
    return "заявка висит 4-й день — Директор увидит срочный риск", None


def step_resolve(con, bid, i):
    """Зависшую заявку взяли в работу — риск снимается, поле распускается."""
    row = con.execute(
        "SELECT id FROM orders WHERE business_id=? AND status='новый' AND source=? "
        "ORDER BY id DESC LIMIT 1", (bid, MARK)).fetchone()
    if not row:
        return None, None
    con.execute("UPDATE orders SET status='в работе' WHERE id=?", (row["id"],))
    return "зависшую заявку взяли в работу — риск снят", None


# Пара (шаг, «сколько ждать перед ним не меньше»). Обычные события идут с
# общим шагом --every; двум переходам задано отдельное время, и вот почему:
# кабинет опрашивает сервер раз в 30 секунд, поэтому состояние, которое живёт
# 40 секунд, попадает ровно в один опрос — мигнуло и пропало. Чтобы переход
# «поле стянулось» и «поле распустилось» можно было именно РАЗГЛЯДЕТЬ, каждое
# состояние держится не меньше трёх опросов.
HOLD = 100          # секунд — три опроса кабинета с запасом

SCRIPT = [
    (step_client, 0), (step_income, 0), (step_order, 0), (step_client, 0),
    (step_income, 0), (step_order, 0), (step_expense, 0), (step_client, 0),
    (step_stale, 0),                      # ← поле уходит в «риск»
    (step_income, 0), (step_client, 0), (step_order, 0),
    (step_resolve, HOLD),                 # ← риск держится, потом снимается
    (step_income, HOLD), (step_client, 0), (step_order, 0),
    (step_expense, 0), (step_income, 0),
]


def run(bid, every, steps):
    con = _db()
    if not con.execute("SELECT 1 FROM businesses WHERE id=?", (bid,)).fetchone():
        print(f"бизнеса id={bid} нет в базе")
        return 1
    name = con.execute("SELECT name FROM businesses WHERE id=?", (bid,)).fetchone()["name"]
    total = steps or len(SCRIPT)
    plan = sum(max(every, SCRIPT[i % len(SCRIPT)][1]) for i in range(1, total))
    print(f"поток для «{name}» (id={bid}): {total} событий, шаг {every} с, "
          f"всего около {plan // 60} мин {plan % 60} с")
    print("кабинет опрашивает сервер раз в 30 с — держите вкладку открытой\n")
    try:
        for i in range(total):
            fn, hold = SCRIPT[i % len(SCRIPT)]
            if i:
                time.sleep(max(every, hold))
            what, _ = fn(con, bid, i)
            con.commit()
            if what:
                print(f"  [{i + 1:>2}/{total}] {datetime.datetime.now():%H:%M:%S}  {what}")
    except KeyboardInterrupt:
        print("\nостановлено — база осталась в последнем настоящем состоянии")
    finally:
        con.close()
    print("\nготово. убрать за собой: python tools/live_demo.py --business "
          f"{bid} --clean")
    return 0


def clean(bid):
    con = _db()
    n = 0
    for t in ("orders", "clients", "finance_entries"):
        cur = con.execute(f"DELETE FROM {t} WHERE business_id=? AND source=?", (bid, MARK))
        n += cur.rowcount
    con.commit()
    con.close()
    print(f"убрано записей потока: {n}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Живой поток событий для показа VELOR")
    ap.add_argument("--business", type=int, required=True, help="id бизнеса")
    ap.add_argument("--every", type=int, default=12, help="секунд между событиями")
    ap.add_argument("--steps", type=int, default=0, help="сколько событий (0 — весь сценарий)")
    ap.add_argument("--clean", action="store_true", help="убрать всё, что добавил поток")
    a = ap.parse_args()
    return clean(a.business) if a.clean else run(a.business, a.every, a.steps)


if __name__ == "__main__":
    sys.exit(main())
