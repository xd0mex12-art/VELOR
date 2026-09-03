# -*- coding: utf-8 -*-
"""
Временной ряд, средний чек и разделение клиентов — доказательства, а не картинки.

График в VELOR существует не для красоты: он объясняет число, которое стоит над
ним. Отсюда главная проверка этого файла — ИНВАРИАНТ СХОДИМОСТИ: сумма ряда за
период обязана совпадать с числом сводки до рубля. Линия, которая спорит с
цифрой, хуже отсутствующей линии: по ней тоже принимают решения.

Второе, что проверяем, — честность границ. Отменённая заявка не принесла денег и
не должна попадать в средний чек. Заявка без суммы ничего не говорит о чеке, и
сколько таких было — владелец обязан видеть. Вернувшийся клиент определяется
запросом к базе, а не мнением модели.
"""
import os, sys, tempfile, pathlib, sqlite3, datetime

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "testowner"
os.environ["OWNER_PASSWORD"] = "s3cret-owner"
os.environ["JWT_SECRET"] = "test-secret-xyz"
os.environ["SECRET_KEY"] = "test-box-key"
os.environ["DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
sys.stdout.reconfigure(encoding="utf-8")
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database

c = TestClient(server.app)
ok = fail = 0
DB = str(TMP / "t.db")


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


def biz(login):
    return database.create_business(login, login=login, password="x")


def day(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def raw(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def money(bid, kind, amount, days_ago, category="прочее", op=True):
    """Операция. op=False — запись внесли позже, чем потратили: проверка op_date."""
    raw("""INSERT INTO finance_entries
             (business_id, kind, category, amount, op_date, created_at, source)
           VALUES (?,?,?,?,?,?,'manual')""",
        (bid, kind, category, amount, day(days_ago) if op else None,
         day(days_ago if op else 0) + " 12:00:00"))


def client(bid, name, days_ago=40):
    raw("INSERT INTO clients (business_id, name, created_at) VALUES (?,?,?)",
        (bid, name, day(days_ago) + " 12:00:00"))
    conn = sqlite3.connect(DB)
    cid = conn.execute("SELECT MAX(id) FROM clients WHERE business_id = ?", (bid,)).fetchone()[0]
    conn.close()
    return cid


def order(bid, amount, days_ago, client_id=None, status="выполнен"):
    raw("""INSERT INTO orders (business_id, client_id, text, amount, status, created_at)
           VALUES (?,?,?,?,?,?)""",
        (bid, client_id, "заказ", amount, status, day(days_ago) + " 12:00:00"))


print("== РЯД СХОДИТСЯ СО СВОДКОЙ ==")
# Главный инвариант: то, что нарисовано, и то, что написано, — одно число.
b1 = biz("series-sum")
money(b1, "income", 10000, 3)
money(b1, "income", 5000, 12)
money(b1, "expense", 4000, 5)
money(b1, "expense", 1500, 25)
order(b1, 3000, 4)
order(b1, 7000, 9)
client(b1, "Аня", 6)

s30 = database.daily_series(b1, 30)
tot = database.money_period(b1, 30, 0)
check("длина ряда равна окну", len(s30) == 30, len(s30))
check("день ряда — это дата", all(len(r["day"]) == 10 for r in s30))
check("ряд идёт от старого к новому", s30[0]["day"] < s30[-1]["day"])
check("последний день ряда — сегодня", s30[-1]["day"] == day(0), s30[-1]["day"])
check("в периоде есть движение — иначе сходимость доказывает ноль",
      tot["income"] > 0 and tot["expense"] > 0, tot)
check("доход ряда сходится со сводкой",
      sum(r["income"] for r in s30) == tot["income"],
      (sum(r["income"] for r in s30), tot["income"]))
check("расход ряда сходится со сводкой",
      sum(r["expense"] for r in s30) == tot["expense"],
      (sum(r["expense"] for r in s30), tot["expense"]))
check("прибыль ряда сходится со сводкой",
      sum(r["profit"] for r in s30) == tot["profit"])
check("заявки ряда сходятся со сводкой",
      sum(r["orders"] for r in s30) == database.orders_period(b1, 30)["count"])
check("клиенты ряда сходятся со сводкой",
      sum(r["clients"] for r in s30) == database.counts_period(b1, "clients", 30))

# Пустые дни заполнены нулями, а не пропущены: дыра в ряду — это ложь о том,
# что между двумя точками ничего не происходило.
days_in_row = [r["day"] for r in s30]
check("дни идут подряд без пропусков",
      days_in_row == sorted(set(days_in_row)) and len(days_in_row) == 30)
check("день без движения — это ноль, а не пропуск",
      any(r["income"] == 0 and r["expense"] == 0 and r["orders"] == 0 for r in s30))

# Короткое окно не должно тянуть старые деньги.
s7 = database.daily_series(b1, 7)
check("окно 7 дней содержит только свои дни", len(s7) == 7)
check("операция 12-дневной давности в семидневку не попала",
      sum(r["income"] for r in s7) == 10000, sum(r["income"] for r in s7))

print("\n== ДЕНЬ ОПЕРАЦИИ, А НЕ ДЕНЬ ЗАПИСИ ==")
# Чек могли внести через неделю, но потратили деньги тогда, когда потратили.
b2 = biz("series-opdate")
money(b2, "expense", 2000, 10, op=True)      # op_date = 10 дней назад
raw("""INSERT INTO finance_entries (business_id, kind, category, amount, op_date, created_at, source)
       VALUES (?,?,?,?,?,?,'manual')""",
    (b2, "expense", "прочее", 3000, day(20), day(0) + " 12:00:00"))
s = database.daily_series(b2, 30)
by_day = {r["day"]: r for r in s}
check("операция встала на день операции, а не на день записи",
      by_day[day(20)]["expense"] == 3000, by_day[day(20)])
check("на дне записи её нет", by_day[day(0)]["expense"] == 0, by_day[day(0)])
check("ряд с op_date сходится со сводкой",
      sum(r["expense"] for r in s) == database.money_period(b2, 30)["expense"])

print("\n== СРЕДНИЙ ЧЕК СЧИТАЕТСЯ ЧЕСТНО ==")
b3 = biz("series-aov")
order(b3, 10000, 3)                       # считается
order(b3, 20000, 5)                       # считается
order(b3, 0, 6)                           # суммы нет — в чек не идёт
order(b3, 90000, 7, status="отменён")     # отменён — денег не принёс
o = database.orders_period(b3, 30)
check("заявок всего — все четыре", o["count"] == 4, o)
check("отменённая посчитана отдельно", o["cancelled"] == 1, o)
check("в деньги вошли только неотменённые с суммой", o["amount"] == 30000, o)
check("средний чек не завышен отменённой", o["avg"] == 15000, o)
check("база чека названа числом", o["with_amount"] == 2, o)

print("\n== НОВЫЕ И ВЕРНУВШИЕСЯ — РАСЧЁТ, А НЕ ДОГАДКА ==")
b4 = biz("series-clients")
c_old = client(b4, "Старый", 60)
c_new = client(b4, "Новый", 5)
c_can = client(b4, "Отменивший", 5)
order(b4, 5000, 45, client_id=c_old)      # заказ ДО периода
order(b4, 6000, 4, client_id=c_old)       # и в периоде → вернувшийся
order(b4, 7000, 3, client_id=c_new)       # только в периоде → новый
order(b4, 8000, 2, client_id=c_can, status="отменён")   # отмена — не покупка
w = database.clients_split(b4, 30)
check("вернувшийся определён по заказу раньше периода", w["returning"] == 1, w)
check("новый — тот, у кого раньше заказа не было", w["new"] == 1, w)
check("активных ровно двое", w["active"] == 2, w)
check("клиент только с отменой в активные не попал", w["active"] == 2, w)
check("новые и вернувшиеся в сумме дают активных",
      w["new"] + w["returning"] == w["active"], w)
# Тот же расчёт на тех же данных обязан дать тот же ответ.
check("расчёт детерминирован", database.clients_split(b4, 30) == w)

print("\n== ЭНДПОИНТ ГОВОРИТ, ЧЕГО НЕ ЗНАЕТ ==")
r = c.post("/api/register", json={"name": "ser", "login": "ser-api",
                                  "password": "pass123", "consent": True})
d = r.json()
bid, H = d["business_id"], {"X-Auth": d["token"]}
order(bid, 12000, 3)
order(bid, 0, 4)                          # без суммы
order(bid, 50000, 5, status="отменён")    # отменённая
money(bid, "income", 12000, 3)

r = c.get("/api/series?days=30", headers=H)
check("ряд отдаётся", r.status_code == 200, r.status_code)
j = r.json()
check("в ответе ровно 30 дней", len(j["series"]) == 30, len(j.get("series", [])))
check("средний чек посчитан по одной заявке", j["aov"]["value"] == 12000, j["aov"])
check("сказано, сколько заявок вошло в чек", j["aov"]["counted"] == 1, j["aov"])
check("сказано, сколько заявок всего", j["aov"]["orders"] == 3, j["aov"])
check("у среднего чека есть источник", "заяв" in j["aov"]["source"], j["aov"]["source"])
check("про заявки без суммы сказано прямо",
      any("сумма не проставлена" in g for g in j["gaps"]), j["gaps"])
check("про отменённые сказано прямо",
      any("Отменённые" in g for g in j["gaps"]), j["gaps"])
check("у разделения клиентов есть источник", "вернувшийся" in j["clients"]["source"])
check("окно ограничено сверху", c.get("/api/series?days=9999", headers=H).json()["days"] == 90)
check("окно ограничено снизу", c.get("/api/series?days=1", headers=H).json()["days"] == 7)

print("\n== ЧУЖОЙ РЯД НЕ ОТДАЁТСЯ ==")
r2 = c.post("/api/register", json={"name": "ser2", "login": "ser-api-2",
                                   "password": "pass123", "consent": True})
d2 = r2.json()
H2 = {"X-Auth": d2["token"]}
mine = c.get("/api/series?days=30", headers=H).json()
alien = c.get(f"/api/series?days=30&business_id={bid}", headers=H2).json()
check("ряд соседа пуст, даже если спросить его business_id",
      sum(x["income"] for x in alien["series"]) == 0
      and sum(x["income"] for x in mine["series"]) == 12000,
      (sum(x["income"] for x in alien["series"]), sum(x["income"] for x in mine["series"])))
check("без входа ряд не отдаётся", c.get("/api/series").status_code in (401, 403),
      c.get("/api/series").status_code)

print("\n== ПУСТОЙ БИЗНЕС НЕ ВЫДУМЫВАЕТ ==")
b5 = biz("series-empty")
s = database.daily_series(b5, 30)
check("ряд пустого бизнеса — тридцать нулей",
      len(s) == 30 and all(r["income"] == 0 and r["orders"] == 0 for r in s))
check("средний чек пустого бизнеса — None, а не ноль",
      database.orders_period(b5, 30)["avg"] is None)
check("клиенты пустого бизнеса — нули",
      database.clients_split(b5, 30) == {"active": 0, "returning": 0, "new": 0})

print(f"\nИТОГО: успешно {ok}, провалено {fail}")
sys.exit(1 if fail else 0)
