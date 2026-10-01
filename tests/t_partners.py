# -*- coding: utf-8 -*-
"""
Партнёрская программа: кто привёл клиента и сколько ему причитается.

Здесь проверяется то, из-за чего бывают ссоры с людьми и потеря денег:

  1. Привязка ставится ОДИН РАЗ и не переписывается. На этом держится
     обещание «доля, пока клиент платит».
  2. Начисление происходит на каждой оплате и НЕ происходит на триале.
     Бесплатный доступ — не деньги, и платить за него партнёру не за что.
  3. Смена доли не переписывает прошлое. Деньги задним числом не меняются.
  4. Партнёрская отчётность не отдаёт данных чужих компаний.
"""
import os, sys, tempfile, pathlib

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
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
os.environ["REGISTER_MAX"] = "200"
sys.stdout.reconfigure(encoding="utf-8")

ROOT = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, trial, plans, partners

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


c = TestClient(server.app)


def new_business(name, partner_code=None):
    """Регистрация через настоящую ручку — путь, которым идёт живой клиент."""
    body = {"name": name, "login": name.lower().replace(" ", ""),
            "password": "parol123", "consent": True}
    if partner_code is not None:
        body["partner_code"] = partner_code
    r = c.post("/api/register", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── 1. Справочник партнёров ────────────────────────────────────────────────
dima = partners.create("Дима", "Dima", share_pct=15, contact="@dima")
check("партнёр заведён", bool(dima and dima["id"]))
check("код хранится в нижнем регистре", dima["code"] == "dima", dima["code"])
check("доля по умолчанию 15", dima["share_pct"] == 15)

check("поиск по коду в другом регистре", (partners.by_code("DIMA") or {}).get("id") == dima["id"])
check("поиск по коду с пробелами", (partners.by_code("  dima ") or {}).get("id") == dima["id"])
check("неизвестный код — None", partners.by_code("нет-такого") is None)
check("пустой код — None", partners.by_code("") is None)

sleepy = partners.create("Выключенный", "sleepy")
partners.set_active(sleepy["id"], False)
check("выключенный партнёр не находится по коду", partners.by_code("sleepy") is None)

try:
    partners.create("Без кода", "")
    check("код обязателен", False, "исключения не было")
except ValueError:
    check("код обязателен", True)

try:
    partners.create("Кривая доля", "krivaya", share_pct=150)
    check("доля больше 100 не принимается", False, "исключения не было")
except ValueError:
    check("доля больше 100 не принимается", True)


# ── 2. Привязка клиента ────────────────────────────────────────────────────
b1 = new_business("Клиника Один", partner_code="dima")
check("клиент привязан по коду из формы",
      (partners.partner_of(b1["business_id"]) or {}).get("id") == dima["id"])

b2 = new_business("Клиника Два", partner_code="DIMA")
check("регистр кода при регистрации не важен",
      (partners.partner_of(b2["business_id"]) or {}).get("id") == dima["id"])

b3 = new_business("Клиника Три", partner_code="мусор-которого-нет")
check("неизвестный код не ломает регистрацию", bool(b3.get("business_id")))
check("неизвестный код не создаёт привязки", partners.partner_of(b3["business_id"]) is None)

b4 = new_business("Клиника Четыре")
check("без кода привязки нет", partners.partner_of(b4["business_id"]) is None)

vasya = partners.create("Вася", "vasya")
check("привязка не переписывается вторым партнёром",
      partners.attach(b1["business_id"], "vasya") is False)
check("первый партнёр остался",
      (partners.partner_of(b1["business_id"]) or {}).get("id") == dima["id"])

check("выключенный партнёр не привязывает",
      partners.attach(b4["business_id"], "sleepy") is False)

# Сам себя привести нельзя: у партнёра есть свой кабинет, и регистрация по
# собственной ссылке не должна превращаться в скидку самому себе.
own = new_business("Кабинет Васи")
database.update_partner(vasya["id"], own_business_id=own["business_id"])
check("сам себя привязать нельзя",
      partners.attach(own["business_id"], "vasya") is False)


# ── 3. Начисления ──────────────────────────────────────────────────────────
bid1 = b1["business_id"]

# Триал — не деньги.
trial.launch(bid1)
check("триал не начисляет ничего", len(partners.earnings(dima["id"])) == 0)

# Первая оплата BUSINESS на месяц.
trial.activate_subscription(bid1, plan="business", months=1)
rows = partners.earnings(dima["id"])
check("оплата дала одну строку начисления", len(rows) == 1, str(rows))
check("сумма — цена тарифа", rows[0]["amount"] == plans.price("business"), str(rows[0]))
check("комиссия 15% от 9900 = 1485", rows[0]["commission"] == 1485, str(rows[0]))
check("в строке записан тариф", rows[0]["plan"] == "business")

# Продление на три месяца — одна строка на тройную сумму.
trial.extend_subscription(bid1, months=3)
rows = partners.earnings(dima["id"])
check("продление дало вторую строку", len(rows) == 2, str(rows))
check("три месяца — тройная сумма", rows[0]["amount"] == plans.price("business") * 3,
      str(rows[0]))
check("комиссия с трёх месяцев", rows[0]["commission"] == 1485 * 3, str(rows[0]))

# Фактическая сумма важнее каталожной.
bid2 = b2["business_id"]
trial.activate_subscription(bid2, plan="business", months=1, amount=5000)
row = partners.earnings(dima["id"])[0]
check("названная сумма побеждает каталожную", row["amount"] == 5000, str(row))
check("комиссия считается с названной суммы", row["commission"] == 750, str(row))

# Клиент без партнёра не создаёт начислений никому.
before = len(partners.earnings())
trial.activate_subscription(b4["business_id"], plan="start", months=1)
check("клиент без партнёра не начисляет", len(partners.earnings()) == before)

# Смена доли не трогает прошлое.
old_rows = [dict(r) for r in partners.earnings(dima["id"])]
partners.set_share(dima["id"], 20)
new_rows = partners.earnings(dima["id"])
check("смена доли не переписала прошлые начисления",
      [r["commission"] for r in new_rows] == [r["commission"] for r in old_rows])

# ...а к новой оплате применяется уже новая доля.
partners.attach(b3["business_id"], "dima")
trial.activate_subscription(b3["business_id"], plan="start", months=1)
fresh = partners.earnings(dima["id"])[0]
check("новая доля записана в новую строку", fresh["share_pct"] == 20, str(fresh))
check("комиссия 20% от 4900 = 980", fresh["commission"] == 980, str(fresh))
partners.set_share(dima["id"], 15)


# ── 4. Итоги и выплаты ─────────────────────────────────────────────────────
t = partners.totals(dima["id"])
check("итог: клиентов трое", t["clients"] == 3, str(t))
check("итог: начислено больше нуля", t["accrued"] > 0, str(t))
check("итог: к выплате равно начисленному", t["due"] == t["accrued"], str(t))
check("итог: выплачено ноль", t["paid_out"] == 0, str(t))

ids = [r["id"] for r in partners.earnings(dima["id"], unpaid_only=True)]
closed = partners.mark_paid(ids)
check("выплата закрыла все строки", closed == len(ids), "%s из %s" % (closed, len(ids)))
t2 = partners.totals(dima["id"])
check("после выплаты долга нет", t2["due"] == 0, str(t2))
check("после выплаты выплачено = начислено", t2["paid_out"] == t2["accrued"], str(t2))
check("повторная отметка ничего не меняет", partners.mark_paid(ids) == 0)
check("пустой список выплат безопасен", partners.mark_paid([]) == 0)


# ── 5. Граница между компаниями ────────────────────────────────────────────
# Партнёрская отчётность не должна отдавать НИЧЕГО о самих компаниях: ни
# названия, ни логина, ни данных. Только номер и состояние подписки.
biz_rows = database.partner_businesses(dima["id"])
leaked = {k for r in biz_rows for k in r.keys()} - {"id", "subscription_status"}
check("отчётность не отдаёт полей компании", not leaked, str(leaked))

t3 = partners.totals(dima["id"])
check("в итогах только числа", all(isinstance(v, int) for v in t3.values()), str(t3))



# ── 6. Ручки владельца ─────────────────────────────────────────────────────
# Партнёрская отчётность — это чужие деньги и чужие клиенты. Без пропуска
# владельца отсюда не должно уходить НИЧЕГО.
OWNER = c.post("/api/login", json={"login": "testowner", "password": "s3cret-owner"})
check("владелец входит", OWNER.status_code == 200, OWNER.text)
H = {"x-auth": OWNER.json()["token"]}

closed_doors = [
    ("GET", "/api/admin/partners", None),
    ("POST", "/api/admin/partners", {"name": "Чужой", "code": "hacker"}),
    ("GET", "/api/admin/partners/%d/earnings" % dima["id"], None),
    ("POST", "/api/admin/partners/payout", {"ids": [1]}),
    ("POST", "/api/admin/businesses/%d/partner" % b4["business_id"], {"code": "dima"}),
]
for method, url, body in closed_doors:
    r = c.request(method, url, json=body) if body else c.request(method, url)
    check("без владельца закрыто: " + url, r.status_code in (401, 403), r.status_code)

r = c.get("/api/admin/partners", headers=H)
check("владелец видит список", r.status_code == 200, r.text)
lst = r.json()["partners"]
check("в списке есть итоги", all("totals" in p for p in lst), str(lst)[:200])

# Ни одного названия приведённой компании в ответе: отчётность про деньги,
# а не про чужие кабинеты.
raw = r.text
check("в списке партнёров нет названий компаний",
      "Клиника Один" not in raw and "Клиника Два" not in raw, raw[:200])

r = c.post("/api/admin/partners",
           json={"name": "Новый", "code": "novy", "share_pct": 10}, headers=H)
check("партнёр заводится через ручку", r.status_code == 200, r.text)
novy = r.json()["partner"]
check("доля из запроса сохранилась", novy["share_pct"] == 10, str(novy))

r = c.post("/api/admin/partners", json={"name": "Двойник", "code": "novy"}, headers=H)
check("повторный код не принимается", r.status_code == 409, r.text)

r = c.post("/api/admin/partners",
           json={"name": "Кривой", "code": "krivoy", "share_pct": 300}, headers=H)
check("кривая доля отбивается понятной ошибкой", r.status_code == 400, r.text)

r = c.post("/api/admin/partners/%d/share" % novy["id"], json={"share_pct": 25}, headers=H)
check("доля меняется",
      r.status_code == 200 and r.json()["partner"]["share_pct"] == 25, r.text)

# Привязка задним числом: b4 пока ничей.
r = c.post("/api/admin/businesses/%d/partner" % b4["business_id"],
           json={"code": "novy"}, headers=H)
check("привязка задним числом работает", r.status_code == 200, r.text)
r = c.post("/api/admin/businesses/%d/partner" % b4["business_id"],
           json={"code": "dima"}, headers=H)
check("переписать чужого клиента нельзя даже владельцу", r.status_code == 409, r.text)

r = c.get("/api/admin/partners/%d/earnings" % dima["id"], headers=H)
check("начисления отдаются", r.status_code == 200 and "earnings" in r.json(), r.text)
rows_api = r.json()["earnings"]
fields = set().union(*[set(e.keys()) for e in rows_api]) if rows_api else set()
check("в начислениях нет полей компании сверх её номера",
      not (fields - {"id", "partner_id", "business_id", "plan", "months", "amount",
                     "share_pct", "commission", "created_at", "paid_out_at"}),
      str(fields))



# ── 7. Правка начислений ───────────────────────────────────────────────────
# Возврат клиенту или ошибочно отмеченная оплата. Строки не удаляются никогда:
# правка добавляется отдельной записью, чтобы в истории было видно, что именно
# изменили.
t_before = partners.totals(dima["id"])
r = c.post("/api/admin/partners/%d/adjust" % dima["id"],
           json={"business_id": bid1, "commission": -1485}, headers=H)
check("правка принимается", r.status_code == 200, r.text)
t_after = partners.totals(dima["id"])
check("правка уменьшила начисленное",
      t_after["accrued"] == t_before["accrued"] - 1485, str(t_after))
check("правка видна отдельной строкой",
      partners.earnings(dima["id"])[0]["plan"] == partners.ADJUST)
check("старые строки не тронуты",
      t_after["payments"] == t_before["payments"] + 1, str(t_after))

r = c.post("/api/admin/partners/%d/adjust" % dima["id"],
           json={"business_id": bid1, "commission": 0}, headers=H)
check("правка на ноль отбивается", r.status_code == 400, r.text)

r = c.post("/api/admin/partners/999999/adjust",
           json={"business_id": bid1, "commission": -100}, headers=H)
check("правка несуществующему партнёру — 404", r.status_code == 404, r.text)

r = c.post("/api/admin/partners/%d/adjust" % dima["id"],
           json={"business_id": bid1, "commission": -100})
check("правка без владельца закрыта", r.status_code in (401, 403), r.status_code)



# ── 8. Оплата через ЮKassa начисляет долю ──────────────────────────────────
# Вторая дорога денег: не ручная отметка владельца, а подтверждённый платёж.
# Она обязана приводить в ту же точку начисления — иначе включение оплаты
# тихо оставит партнёров без комиссии.
import billing

bpay = new_business("Клиника Оплата", partner_code="dima")
pid_biz = bpay["business_id"]

# Платёж на сумму МЕНЬШЕ каталожной: так бывает при скидке и Founder Pilot.
# Доля обязана считаться от того, что человек заплатил на самом деле.
pay = database.create_payment(pid_biz, kind=billing.KIND_SUBSCRIPTION,
                              amount=7000, plan="business", months=1,
                              description="тест", provider="internal")
database.attach_provider_payment(pay["id"], "test:%d" % pay["id"])
check("платёж отмечен оплаченным", database.mark_payment_paid(pay["id"]) is True)

before = len(partners.earnings(dima["id"]))
billing.apply_paid(database.get_payment(pay["id"]))
rows_pay = partners.earnings(dima["id"])
check("оплата через billing начислила долю", len(rows_pay) == before + 1,
      "%s → %s" % (before, len(rows_pay)))
check("сумма взята фактическая, а не каталожная", rows_pay[0]["amount"] == 7000,
      str(rows_pay[0]))
check("комиссия 15% от 7000 = 1050", rows_pay[0]["commission"] == 1050,
      str(rows_pay[0]))

# Подписка при этом включилась — деньги клиента важнее нашей бухгалтерии.
check("подписка после оплаты активна",
      (database.get_business(pid_biz) or {}).get("subscription_status") == "active")


print("\nуспешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
