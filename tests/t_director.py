# -*- coding: utf-8 -*-
"""
AI Director: ни одного вывода без цифры и ни одной цифры без источника.

Проверяем три вещи, в таком порядке важности:
  1) арифметика сходится и совпадает с базой — прибыль, маржа, проценты;
  2) у каждого числа и каждого вывода написано, из чего он посчитан;
  3) там, где данных мало, Директор МОЛЧИТ и говорит «Недостаточно данных
     для вывода», а не показывает красивый ноль.

Третье — главное. Пустая аналитика опаснее отсутствующей: по ней принимают
решения.
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
# Корень проекта вычисляется от самого файла: тесты должны запускаться
# из любой папки и на любой машине, а не только там, где их писали.
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, director, entities, ai

c = TestClient(server.app)
ok = fail = 0
DB = str(TMP / "t.db")


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


def reg(login):
    r = c.post("/api/register", json={"name": login, "login": login,
                                      "password": "pass123", "consent": True})
    d = r.json()
    assert "token" in d, d
    return d["business_id"], {"X-Auth": d["token"]}


def biz(login):
    """
    Ещё один бизнес — прямо в базе.

    Регистрация через API ограничена по частоте (и правильно: это защита от
    перебора). Сценариям Директора нужен десяток разных бизнесов, а не проверка
    регистрации, поэтому заводим их напрямую.
    """
    return database.create_business(login, login=login, password="x"), None


def day(n):
    """Дата n дней назад в формате базы."""
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def raw(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def money(bid, kind, amount, category, days_ago, doc_type=None, employee_id=None,
          client_id=None, order_id=None):
    raw("""INSERT INTO finance_entries
             (business_id, kind, category, amount, op_date, created_at, doc_type,
              employee_id, client_id, order_id, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,'manual')""",
        (bid, kind, category, amount, day(days_ago), day(days_ago) + " 12:00:00",
         doc_type, employee_id, client_id, order_id))


def order(bid, text, amount, days_ago, client_id=None, status="выполнен"):
    raw("""INSERT INTO orders (business_id, client_id, text, amount, status, created_at)
           VALUES (?,?,?,?,?,?)""",
        (bid, client_id, text, amount, status, day(days_ago) + " 12:00:00"))
    conn = sqlite3.connect(DB)
    oid = conn.execute("SELECT MAX(id) FROM orders WHERE business_id = ?", (bid,)).fetchone()[0]
    conn.close()
    return oid


def client(bid, name, days_ago):
    raw("INSERT INTO clients (business_id, name, created_at) VALUES (?,?,?)",
        (bid, name, day(days_ago) + " 12:00:00"))
    conn = sqlite3.connect(DB)
    cid = conn.execute("SELECT MAX(id) FROM clients WHERE business_id = ?", (bid,)).fetchone()[0]
    conn.close()
    return cid


def metric(b, key):
    return next(m for m in b["metrics"] if m["key"] == key)


def find(b, section, key):
    return next((f for f in b[section] if f["key"] == key), None)


# ── ПУСТОЙ БИЗНЕС: ЧЕСТНОЕ МОЛЧАНИЕ ────────────────────────────────────────
print("== ПУСТОЙ БИЗНЕС ==")
empty_bid, EH = reg("dir_empty")
b = director.briefing(empty_bid)
check("сводка не притворяется готовой", b["ready"] is False, b["headline"])
check("прямо сказано, что данных нет", "Данных пока нет" in b["headline"], b["headline"])
check("и что выдумывать их не станут", "придумывать" in b["headline"].lower(), b["headline"])
for key in ("revenue", "expenses", "profit", "margin"):
    m = metric(b, key)
    check(f"{key}: не ноль, а «недостаточно данных»",
          m["value"] is None and m["display"] == director.NOT_ENOUGH, m["display"])
check("разделы пусты", not (b["changed"] or b["risks"] or b["opportunities"]
                            or b["recommendations"]))
check("и объяснено почему", any(director.NOT_ENOUGH in g for g in b["gaps"]), b["gaps"])

# ── БИЗНЕС С ИСТОРИЕЙ ──────────────────────────────────────────────────────
print("\n== ИСПОЛНИТЕЛЬНАЯ СВОДКА ==")
bid, H = reg("dir_main")
bid2, H2 = reg("dir_other")

# Текущие 30 дней: выручка 482 000, расходы 291 400.
for i, amount in enumerate((200000, 180000, 102000)):
    money(bid, "income", amount, "продажи", 5 + i * 5)
money(bid, "expense", 120000, "закупка", 6)
money(bid, "expense", 58400, "аренда", 8)
money(bid, "expense", 40000, "зарплата", 9, doc_type="salary")
# Доставка: текущее окно 14 дней — 41 400 ₽ …
money(bid, "expense", 25400, "доставка", 3)
money(bid, "expense", 16000, "доставка", 10)
# … предыдущее окно 14 дней — 31 600 ₽. Рост ровно 31%.
money(bid, "expense", 25600, "доставка", 20)
money(bid, "expense", 6000, "доставка", 22)

# Предыдущие 30 дней — чтобы было с чем сравнивать.
money(bid, "income", 400000, "продажи", 40)
money(bid, "income", 30000, "продажи", 45)
money(bid, "expense", 200000, "закупка", 41)
money(bid, "expense", 60000, "аренда", 44)

for i in range(24):
    client(bid, f"Клиент {i + 1}", 2 + i % 25)
for i in range(10):
    client(bid, f"Старый {i + 1}", 40 + i)

for i in range(47):
    order(bid, "Заказ " + str(i + 1), 10000 if i % 2 == 0 else 0, 1 + i % 28)
for i in range(30):
    order(bid, "Прошлый заказ", 9000, 35 + i % 20)

b = director.briefing(bid)
m = {x["key"]: x for x in b["metrics"]}
check("выручка посчитана", m["revenue"]["value"] == 482000, m["revenue"]["value"])
check("расходы посчитаны", m["expenses"]["value"] == 291400, m["expenses"]["value"])
check("прибыль = выручка − расходы",
      m["profit"]["value"] == 482000 - 291400 == 190600, m["profit"]["value"])
check("маржа = прибыль / выручка",
      m["margin"]["value"] == round(190600 * 100 / 482000, 1) == 39.5, m["margin"]["value"])
check("новые клиенты сосчитаны", m["clients_new"]["value"] == 24, m["clients_new"]["value"])
check("новые заказы сосчитаны", m["orders_new"]["value"] == 47, m["orders_new"]["value"])
check("числа показаны по-человечески",
      m["revenue"]["display"] == "482 000 ₽" and m["margin"]["display"] == "39.5%",
      (m["revenue"]["display"], m["margin"]["display"]))

print("\n== У КАЖДОЙ ЦИФРЫ ЕСТЬ ИСТОЧНИК ==")
for x in b["metrics"]:
    check(f"{x['key']}: источник указан", bool((x["source"] or "").strip()), x)
check("источник выручки называет число операций",
      "3 операции" in m["revenue"]["source"], m["revenue"]["source"])
check("источник прибыли показывает саму формулу",
      "482 000 ₽ − 291 400 ₽" in m["profit"]["source"], m["profit"]["source"])
check("источник маржи показывает деление",
      "190 600 ₽ / 482 000 ₽" in m["margin"]["source"], m["margin"]["source"])
check("источник заказов говорит про суммы",
      "сумма проставлена у 24 из 47" in m["orders_new"]["source"], m["orders_new"]["source"])
check("основание сводки названо", "Считано по:" in b["why"], b["why"])
ops = database.money_period(bid, 30)["entries"]
check("и в нём число операций и заявок",
      f"{ops} операций" in b["why"] and "47 заявок" in b["why"], (b["why"], ops))

print("\n== ЧТО ИЗМЕНИЛОСЬ ==")
ch = {f["key"]: f for f in b["changed"]}
check("выручка сравнена с прошлым периодом", "revenue" in ch, list(ch))
check("и процент верный",
      ch["revenue"]["numbers"]["change"] == round((482000 - 430000) * 100 / 430000),
      ch["revenue"]["numbers"])
check("в тексте — обе суммы",
      "482 000 ₽" in ch["revenue"]["detail"] and "430 000 ₽" in ch["revenue"]["detail"],
      ch["revenue"]["detail"])
cat = ch.get("category")
check("категория расходов разобрана отдельно", cat is not None, list(ch))
check("это доставка", cat and cat["numbers"]["category"] == "доставка", cat)
check("рост посчитан за 14 дней",
      cat and cat["numbers"]["days"] == 14 and cat["numbers"]["change"] == 31,
      cat and cat["numbers"])
check("формулировка ровно та, что нужна владельцу",
      cat and cat["title"] == "Расходы на «доставка» выросли на 31% за последние 14 дней",
      cat and cat["title"])
check("и источник объясняет, что с чем сравнивали",
      cat and "2 операции" in cat["source"] and "Финансы" in cat["source"], cat and cat["source"])
check("суммы в тексте совпадают с базой",
      cat and cat["numbers"]["now"] == 41400 and cat["numbers"]["was"] == 31600,
      cat and cat["numbers"])
for f in b["changed"]:
    check(f"«{f['title'][:34]}…»: есть источник", bool((f["source"] or "").strip()))

print("\n== РИСКИ ==")
check("заявки без суммы замечены", find(b, "risks", "no_amount") is not None,
      [r["key"] for r in b["risks"]])
na = find(b, "risks", "no_amount")
check("и посчитаны точно", na["numbers"] == {"without": 23, "total": 47}, na["numbers"])
check("у риска есть источник", bool(na["source"]))
check("зависимость от одного клиента не выдумана",
      find(b, "risks", "one_client") is None, "клиентов у заказов нет — доли быть не может")

# Убыточный бизнес: тот же расчёт, другой ответ.
loss_bid, LH = biz("dir_loss")
money(loss_bid, "income", 50000, "продажи", 5)
money(loss_bid, "expense", 90000, "закупка", 6)
money(loss_bid, "expense", 30000, "аренда", 7)
money(loss_bid, "income", 60000, "продажи", 40)
money(loss_bid, "expense", 50000, "закупка", 41)
lb = director.briefing(loss_bid)
loss = find(lb, "risks", "loss")
check("минус назван минусом", loss is not None, [r["key"] for r in lb["risks"]])
check("и с точной разницей", loss and "70 000 ₽" in loss["detail"], loss and loss["detail"])
check("уровень тревоги высокий", loss and loss["level"] == "urgent")
check("ножницы «расходы против выручки» замечены",
      find(lb, "risks", "scissors") is not None, [r["key"] for r in lb["risks"]])
sc = find(lb, "risks", "scissors")
check("и проценты в них настоящие",
      sc and sc["numbers"] == {"income_change": -17, "expense_change": 140},
      sc and sc["numbers"])

# Зарплатная нагрузка.
pay_bid, PH = biz("dir_pay")
emp = database.add_fact(pay_bid, "employee", "Петрова Анна")
money(pay_bid, "income", 200000, "продажи", 5)
money(pay_bid, "expense", 120000, "зарплата", 6, doc_type="salary", employee_id=emp)
money(pay_bid, "expense", 40000, "аренда", 7)
money(pay_bid, "income", 150000, "продажи", 40)
pb = director.briefing(pay_bid)
pr = find(pb, "risks", "payroll")
check("зарплатная нагрузка видна", pr is not None, [r["key"] for r in pb["risks"]])
check("доля посчитана верно", pr and pr["numbers"]["share"] == 75, pr and pr["numbers"])
check("и сказано, откуда взята", pr and "зарплата" in pr["source"], pr and pr["source"])

print("\n== ВОЗМОЖНОСТИ ==")
svc_bid, SH = biz("dir_svc")
svc = database.add_fact(svc_bid, "service", "Оформление свадеб")
o1 = order(svc_bid, "Оформление свадеб — зал", 120000, 5)
o2 = order(svc_bid, "Оформление свадеб — арка", 80000, 9)
for oid in (o1, o2):
    database.add_entity_link(svc_bid, "order", oid, "service", svc, kind="includes",
                             source="auto", confidence=1.0)
money(svc_bid, "income", 200000, "продажи", 6)
money(svc_bid, "income", 100000, "продажи", 40)
sb = director.briefing(svc_bid)
top = find(sb, "opportunities", "top_service")
check("топовая услуга найдена по связям", top is not None,
      [o["key"] for o in sb["opportunities"]])
check("сумма по ней — сумма её заявок",
      top and top["numbers"]["amount"] == 200000, top and top["numbers"])
check("и сказано, что это связь заявок с услугой",
      top and "заявка включает услугу" in top["source"], top and top["source"])
check("без связей такого вывода нет",
      find(b, "opportunities", "top_service") is None
      and any("не связаны с услугами" in g for g in b["gaps"]), b["gaps"])

print("\n== РЕКОМЕНДАЦИИ ИДУТ ОТ ФАКТОВ ==")
for f in lb["recommendations"]:
    check(f"«{f['title'][:30]}…»: указано основание", f["source"].startswith("Основание:"),
          f["source"])
keys = {f["key"] for f in lb["recommendations"]}
check("совет по убытку опирается на убыток", "do_loss" in keys, keys)
rec = find(lb, "recommendations", "do_loss")
check("и повторяет ту же цифру", rec and "70 000 ₽" in rec["detail"], rec and rec["detail"])
check("советов не больше четырёх", len(lb["recommendations"]) <= 4)
check("без риска и возможности совета не появляется",
      not director.briefing(empty_bid)["recommendations"])

print("\n== ЗАЩИТА ОТ ВЫДУМОК ==")
young_bid, YH = biz("dir_young")
money(young_bid, "income", 5000, "продажи", 1)
money(young_bid, "expense", 3000, "закупка", 2)
yb = director.briefing(young_bid)
check("у молодого бизнеса трендов нет", yb["changed"] == [], yb["changed"])
check("и сказано, почему",
      any("Сравнить период не с чем" in g for g in yb["gaps"]), yb["gaps"])
check("но сами цифры показаны", metric(yb, "revenue")["value"] == 5000)
check("проценты не выдуманы", metric(yb, "revenue")["delta"] is None)

tiny_bid, TH = biz("dir_tiny")
money(tiny_bid, "expense", 600, "мелочь", 3)
money(tiny_bid, "expense", 300, "мелочь", 20)
money(tiny_bid, "income", 1000, "продажи", 40)
raw("UPDATE businesses SET created_at = ? WHERE id = ?", (day(90), tiny_bid))
tb = director.briefing(tiny_bid)
check("рост с 300 до 600 ₽ трендом не считается",
      not any("мелочь" in (f["numbers"].get("category") or "") for f in tb["changed"]),
      tb["changed"])
check("одна операция не даёт вывода о расходах",
      find(tb, "changed", "expenses") is None, tb["changed"])

zero_bid, ZH = biz("dir_zero")
money(zero_bid, "expense", 15000, "аренда", 5)
money(zero_bid, "expense", 12000, "закупка", 6)
money(zero_bid, "expense", 9000, "реклама", 7)
zb = director.briefing(zero_bid)
check("при нулевой выручке маржи нет, а не 0%",
      metric(zb, "margin")["value"] is None
      and metric(zb, "margin")["display"] == director.NOT_ENOUGH,
      metric(zb, "margin"))
check("и объяснено, почему её нет",
      "делить не на что" in metric(zb, "margin")["source"], metric(zb, "margin")["source"])
check("расходы при этом показаны честно", metric(zb, "expenses")["value"] == 36000)
check("и минус назван минусом", find(zb, "risks", "loss") is not None,
      [r["key"] for r in zb["risks"]])

print("\n== БЕЗ МОДЕЛИ ==")
called = {"n": 0}
_real = ai._ask
ai._ask = lambda *a, **k: (called.__setitem__("n", called["n"] + 1), "ответ")[1]
director.briefing(bid)
c.get("/api/director", headers=H)
ai._ask = _real
check("ни одного обращения к модели", called["n"] == 0, called["n"])

print("\n== ЧЕРЕЗ API ==")
r = c.get("/api/director", headers=H)
check("сводка отдаётся", r.status_code == 200, r.status_code)
api = r.json()
check("те же числа, что и внутри", metric(api, "revenue")["value"] == 482000)
check("период по умолчанию — 30 дней", api["days"] == 30)
check("неразумный период приводится к разумному",
      c.get("/api/director?days=999", headers=H).json()["days"] == 90
      and c.get("/api/director?days=1", headers=H).json()["days"] == 7)
home = c.get("/api/home", headers=H).json()
check("главная получает сводку одним запросом",
      (home.get("director") or {}).get("metrics"), list(home)[:5])
check("и отвечает на вопрос страницы",
      "Прибыль" in home["director"]["headline"], home["director"]["headline"])
check("сосед видит свою пустую сводку",
      c.get("/api/director", headers=H2).json()["ready"] is False)
check("и ни одной чужой цифры",
      metric(c.get("/api/director", headers=H2).json(), "revenue")["value"] is None)

database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("после триала сводку по-прежнему видно",
      c.get("/api/director", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
