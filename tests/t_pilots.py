# -*- coding: utf-8 -*-
"""
ВИДИМОСТЬ ПО ПИЛОТАМ — чтобы триал не заканчивался в тишине.

Пилот не приходит и не говорит «мне не подошло». Он просто перестаёт заходить,
через две недели доступ закрывается, и мы узнаём об этом постфактум по пустому
счёту. Этот файл проверяет три ответа, без которых вмешаться невозможно:

  1. Дошёл ли человек до первого брифинга — и за сколько часов. Пока брифинга
     не было, продукт для него ещё не случился, чем бы ни был занят счётчик дней.
  2. Заходит ли вообще. Считаем РАЗНЫЕ дни, а не заходы: сорок открытий за один
     вечер — это один день, и притворяться, что это активность, нельзя.
  3. Не истекает ли триал молча. Это единственная строка, ради которой всё
     остальное считается.

Отдельно проверяется то, что легко сломать незаметно: активность засчитывается
владельцу КАБИНЕТА, а не владельцу VELOR, заглянувшему в чужой кабинет из
админки. Иначе в отчёте о клиентах мы видели бы собственное отражение.
"""
import os, sys, tempfile, pathlib, datetime

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "testowner"
os.environ["OWNER_PASSWORD"] = "s3cret-owner"
os.environ["JWT_SECRET"] = "test-secret-pilots"
os.environ["SECRET_KEY"] = "test-box-key"
os.environ["DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
os.environ["REGISTER_MAX"] = "200"
os.environ["ASK_MAX"] = "500"
sys.stdout.reconfigure(encoding="utf-8")
ROOT = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient                          # noqa: E402
import server, database, trial, plans                              # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


c = TestClient(server.app)


def new_business(login, name="Компания"):
    d = c.post("/api/register", json={"name": name, "login": login,
                                      "password": "pass123", "consent": True}).json()
    return d["business_id"], {"X-Auth": d["token"]}


def biz(bid):
    return database.get_business(bid) or {}


def seen_ago(bid, days):
    """Сдвинуть отметку захода в прошлое — как будто человек не заходил."""
    stamp = (datetime.datetime.utcnow() - datetime.timedelta(days=days)
             ).strftime("%Y-%m-%d %H:%M:%S")
    with database._connect() as conn:
        conn.execute("UPDATE businesses SET last_seen_at = ? WHERE id = ?", (stamp, bid))
    server._SEEN_CACHE.clear()          # кеш процесса не должен мешать проверке


def overview():
    d = c.get("/api/admin/trial-overview", headers=OWN).json()
    return {b["id"]: b for b in d["businesses"]}, d


bidA, HA = new_business("pilot-a", "Пилот А")
bidB, HB = new_business("pilot-b", "Пилот Б")
OWN = {"X-Auth": c.post("/api/login", json={"login": "testowner",
                                            "password": "s3cret-owner"}).json()["token"]}

print("== ЗАХОД В КАБИНЕТ ОТМЕЧАЕТСЯ ==")
check("до первого запроса заходов нет", (biz(bidA).get("active_days") or 0) == 0)
c.get("/api/orders", headers=HA)
check("после запроса из кабинета — один день", biz(bidA).get("active_days") == 1)
check("и отметка времени поставлена", bool(biz(bidA).get("last_seen_at")))

for _ in range(5):
    c.get("/api/orders", headers=HA)
check("пять запросов подряд не превращаются в пять дней",
      biz(bidA).get("active_days") == 1, biz(bidA).get("active_days"))

# Кеш процесса экономит записи, но считает дни не он: обнуляем его и убеждаемся,
# что счётчик держится на дате, а не на таймере.
server._SEEN_CACHE.clear()
c.get("/api/orders", headers=HA)
check("в тот же день день не добавляется даже без кеша",
      biz(bidA).get("active_days") == 1)

seen_ago(bidA, 2)
c.get("/api/orders", headers=HA)
check("заход в другой день добавляет день", biz(bidA).get("active_days") == 2)

print("\n== ЧУЖОЙ ЗАХОД НЕ ЗАСЧИТЫВАЕТСЯ ==")
# Владелец VELOR открывает кабинет пилота из админки. Это НЕ активность пилота:
# иначе брошенный аккаунт выглядел бы живым ровно потому, что мы в него смотрим.
before = biz(bidB).get("active_days") or 0
server._SEEN_CACHE.clear()
r = c.get("/api/orders?business_id=%d" % bidB, headers=OWN)
check("владелец видит чужой кабинет", r.status_code == 200)
check("но пилоту это в активность не пишется",
      (biz(bidB).get("active_days") or 0) == before)
check("и отметки времени у него не появилось", not biz(bidB).get("last_seen_at"))

print("\n== ПУТЬ К ЦЕННОСТИ ==")
trial.launch(bidA)
rows, _ = overview()
check("без брифинга ценность не наступила", rows[bidA]["hours_to_value"] is None)
check("и это видно отдельным полем", rows[bidA]["first_briefing_at"] is None)
check("брифингов ноль", rows[bidA]["briefings"] == 0)

database.save_briefing(bidA, datetime.date.today().isoformat(), '{"headline":"есть"}')
rows, _ = overview()
check("после первого брифинга время до ценности посчитано",
      isinstance(rows[bidA]["hours_to_value"], int), rows[bidA]["hours_to_value"])
check("и брифинг посчитан", rows[bidA]["briefings"] == 1)

print("\n== ТИХОЕ ИСТЕЧЕНИЕ ==")
# Три дня до конца — последний момент, когда ещё можно вмешаться.
trial.launch(bidB)
trial.set_trial_end(bidB, (datetime.date.today() + datetime.timedelta(days=2)).isoformat())
rows, d = overview()
check("триал заканчивается, а брифинга не было — тревога",
      rows[bidB]["silent_risk"] is True)
check("и сказано, почему именно", "брифинг" in rows[bidB]["silent_why"],
      rows[bidB]["silent_why"])
check("такие пилоты посчитаны отдельно", (d["counts"].get("silent") or 0) >= 1)

database.save_briefing(bidB, datetime.date.today().isoformat(), '{"headline":"есть"}')
server._SEEN_CACHE.clear()
c.get("/api/orders", headers=HB)          # зашёл сегодня
rows, _ = overview()
check("брифинг был и человек заходит — тревоги нет",
      rows[bidB]["silent_risk"] is False)

seen_ago(bidB, 5)
rows, _ = overview()
check("брифинг был, но пять дней тишины — снова тревога",
      rows[bidB]["silent_risk"] is True)
check("и причина другая", "заход" in rows[bidB]["silent_why"], rows[bidB]["silent_why"])

trial.set_trial_end(bidA, (datetime.date.today() + datetime.timedelta(days=11)).isoformat())
rows, _ = overview()
check("до конца ещё далеко — не тревожим", rows[bidA]["silent_risk"] is False)

trial.activate_subscription(bidB, "business", months=1)
rows, _ = overview()
check("оплативший клиент из тревоги выходит", rows[bidB]["silent_risk"] is False)

print("\n== СВОДКА — ТОЛЬКО ВЛАДЕЛЬЦУ VELOR ==")
check("бизнес чужую сводку не получает",
      c.get("/api/admin/trial-overview", headers=HA).status_code in (401, 403))
check("без токена — тоже нет",
      c.get("/api/admin/trial-overview").status_code == 401)

print("\n== ВИТРИНА И КАБИНЕТ НАЗЫВАЮТ ОДНУ ЦЕНУ ==")
# Лендинг видит человек ДО регистрации. Пока цены лежали в вёрстке, он обещал
# 1 990 ₽, а кабинет просил 4 900 — и правым оказывался тот, кто смотрел позже.
r = c.get("/api/public/plans")
check("каталог отдаётся без входа", r.status_code == 200)
pub = r.json()
check("три тарифа", len(pub["plans"]) == 3)
check("цены те же, что в каталоге",
      [p["price"] for p in pub["plans"]] == [plans.price(k) for k in plans.ORDER])
check("популярный отмечен один", sum(1 for p in pub["plans"] if p["popular"]) == 1)
check("разовая настройка тоже оттуда", pub["setup"]["price"] == plans.SETUP["price"])
check("про конкретный бизнес в публичной ручке ничего нет",
      all(p.get("current") is False for p in pub["plans"]))

land = pathlib.Path("web/index.html").read_text(encoding="utf-8")
check("лендинг берёт цены с сервера", "/api/public/plans" in land)
for stale in ("1 990", "4 990", "9 990 ₽ / мес"):
    check("на лендинге не осталось цены «%s»" % stale,
          stale not in land.split("<script>")[0], stale)
check("и цен в вёрстке карточек нет вовсе",
      "₽ / мес" not in land.split("<script>")[0])

print("\nИТОГО: успешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
