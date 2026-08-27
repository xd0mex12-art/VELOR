# -*- coding: utf-8 -*-
"""
Финансовое понимание: документ → предложение → операция в финансах.

Проверяем восемь видов денежных документов, связь операции с людьми из памяти
бизнеса, ручной ввод той же дорогой, что и разбор, правку с историей — и
арифметику, в которой ошибаться нельзя:

    прибыль = выручка − расходы
    маржа   = прибыль / выручка   (при нулевой выручке её НЕТ, а не ноль)
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
import server, database, entities, understanding

c = TestClient(server.app)
ok = fail = 0


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
    return d["business_id"], {"X-Auth": d["token"]}


def upload(H, name, text, mime="text/plain"):
    if isinstance(text, str):
        text = text.encode("utf-8")
    return c.post("/api/inbox/upload", headers=H,
                  files=[("files", (name, text, mime))]).json()["saved"][0]


def note(H, text):
    return c.post("/api/inbox", headers=H, json={"text": text}).json()["item"]


def acts(item):
    return {a["action"]: a for a in (item.get("result") or {}).get("suggested_actions", [])}


def summary(H):
    return c.get("/api/finance", headers=H).json()["summary"]


bid, H = reg("fin_main")

# ============================================================
print("\n== ВОСЕМЬ ВИДОВ ДЕНЕЖНЫХ ДОКУМЕНТОВ ==")
DOCS = [
    ("чек",         "чек доставка.txt",
     "КАССОВЫЙ ЧЕК\nООО «Ромашка»\nДоставка курьером 1 850 ₽\nИтого к оплате 1 850 ₽\nКассир: Петрова",
     "EXPENSE_DOCUMENT", "expense", "receipt"),
    ("банковский скрин", "перевод.txt",
     "Выписка по счёту\nДата операции 12.08.2026\nСписание 70 000 ₽\nНазначение платежа: перевод",
     "BANK_TRANSACTION", None, "bank"),
    ("счёт",        "счёт 154.txt",
     "СЧЁТ НА ОПЛАТУ № 154 от 12.08.2026\nПлательщик: ООО «Флоренция»\nИтого к оплате 24 500 руб",
     "INVOICE", "expense", "invoice"),
    ("накладная",   "накладная.txt",
     "ТОВАРНАЯ НАКЛАДНАЯ ТОРГ-12\nГрузополучатель: ООО «Флоренция»\nИтого 12 000 руб",
     "WAYBILL", "expense", "waybill"),
    ("зарплата",    "листок.txt",
     "РАСЧЁТНЫЙ ЛИСТОК за август\nНачислено к выплате 60 000 руб",
     "SALARY_PAYMENT", "expense", "salary"),
    ("доход",       "ордер.txt",
     "ПРИХОДНЫЙ КАССОВЫЙ ОРДЕР\nОплата от клиента 15 000 руб",
     "INCOME_DOCUMENT", "income", "income"),
    ("расход словами", None,
     "Сегодня заплатил за аренду 40 000 рублей",
     "FINANCIAL_TRANSACTION", "expense", None),
    ("возврат",     "возврат.txt",
     "ЧЕК ВОЗВРАТА\nВозврат средств покупателю 3 200 руб",
     "REFUND", "expense", "refund"),
]
made = {}
for label, fname, text, want_type, want_dir, want_doc in DOCS:
    item = upload(H, fname, text) if fname else note(H, text)
    res = item.get("result") or {}
    made[label] = item
    check(f"{label} — тип «{want_type}»", res.get("type") == want_type,
          (res.get("type"), res.get("summary")))
    ex = res.get("extracted_data") or {}
    if want_dir:
        check(f"{label} — направление {want_dir}", ex.get("direction") == want_dir, ex)
    if want_doc:
        check(f"{label} — документ {want_doc}", ex.get("doc_type") == want_doc, ex)
    check(f"{label} — сумма вытащена", bool(ex.get("amount")), ex)
    a = acts(item)
    check(f"{label} — предложена денежная операция",
          bool({"create_expense", "create_income"} & set(a)), list(a))
    if want_dir == "income":
        check(f"{label} — именно доход", "create_income" in a, list(a))
    check(f"{label} — но сам VELOR денег не двигает",
          not any(x.get("auto") for x in a.values() if x["action"].startswith("create_")), a)

print("\n-- дата из документа --")
inv = made["счёт"]["result"]["extracted_data"]
check("дата операции взята из счёта", inv.get("op_date") == "2026-08-12", inv)

print("\n-- предложение конкретное, а не «запишите расход» --")
a = acts(made["чек"])["create_expense"]
check("в заголовке сумма", "1 850" in a["title"], a["title"])
check("и категория", "доставка" in a["title"], a["title"])
check("кнопка подтверждения говорит, с чем соглашаются",
      "1 850" in (a.get("confirm_title") or ""), a.get("confirm_title"))

# ============================================================
print("\n== ПАМЯТЬ БИЗНЕСА ДЕЛАЕТ ПЕРЕВОД ЗАРПЛАТОЙ ==")
emp = c.post("/api/facts", headers=H,
             json={"kind": "employee", "title": "Иванов Пётр", "body": "курьер"}).json()["id"]
sup = c.post("/api/facts", headers=H,
             json={"kind": "supplier", "title": "ООО Флора", "body": "цветы оптом"}).json()["id"]

bank = note(H, "Перевод Иванову 70 000 ₽")
bres = bank["result"] or {}
bex = bres.get("extracted_data") or {}
check("сотрудник узнан по памяти", bex.get("employee_id") == emp, bex)
check("и операция стала зарплатой", bex.get("category") == "зарплата", bex)
check("документ помечен как зарплата", bex.get("doc_type") == "salary", bex)
check("контрагент — имя из памяти", bex.get("counterparty") == "Иванов Пётр", bex)
check("уверенность выше, чем у безымянного перевода",
      (bres.get("confidence") or 0) >= 0.5, bres.get("confidence"))
ba = acts(bank)
check("предложение называет зарплату",
      "зарплата" in (ba.get("create_expense", {}).get("title") or ""), ba)

unknown = note(H, "Перевод Сидорову 70 000 ₽")
check("незнакомое имя не превращается в зарплату",
      not (unknown["result"]["extracted_data"] or {}).get("employee_id"),
      unknown["result"]["extracted_data"])

sup_note = note(H, "Оплатил ООО Флора 18 000 рублей")
check("поставщик тоже узнаётся",
      (sup_note["result"]["extracted_data"] or {}).get("supplier_id") == sup,
      sup_note["result"]["extracted_data"])

# ============================================================
print("\n== ПОДТВЕРЖДЕНИЕ → ОПЕРАЦИЯ В ФИНАНСАХ ==")
r = c.post(f"/api/inbox/{bank['id']}/action", headers=H, json={"action": "create_expense"})
check("подтверждение принято", r.status_code == 200, r.text[:200])
entry_id = r.json()["entity_id"]
row = database.finance_row(entry_id, bid)
check("операция в finance_entries", row and row["kind"] == "expense", row)
check("сумма та самая", row["amount"] == 70000, row)
check("категория зарплата", row["category"] == "зарплата", row)
check("сотрудник привязан", row["employee_id"] == emp, row)
check("имя сотрудника подтягивается связью", row["employee_name"] == "Иванов Пётр", row)
check("документ — зарплата", row["doc_type"] == "salary", row)
check("источник — входящие", row["source"] == "inbox", row)
check("время записи есть", bool(row["created_at"]), row)

fin = c.get("/api/finance", headers=H).json()
check("операция видна в ленте финансов",
      any(e["id"] == entry_id for e in fin["entries"]), len(fin["entries"]))
mine = [e for e in fin["entries"] if e["id"] == entry_id][0]
check("в ленте показан человек", mine["who"] == "Иванов Пётр", mine)
check("и чем подтверждено", mine["doc_type_ru"] == "Зарплата", mine)

print("\n-- у операции есть история --")
d = c.get(f"/api/finance/{entry_id}", headers=H).json()
check("карточка операции открывается", d["id"] == entry_id, d.get("id"))
check("в истории есть запись о создании",
      any(h["event"] == "created" for h in d["history"]), d["history"])
check("видно, кто записал", d["history"][0]["actor_ru"] == "владелец", d["history"][0])
check("и из какого материала", d["history"][0].get("item", {}).get("id") == bank["id"],
      d["history"][0].get("item"))

# ============================================================
print("\n== РУЧНАЯ ОПЕРАЦИЯ — ТА ЖЕ СИСТЕМА ==")
r = c.post("/api/finance", headers=H, json={"kind": "income", "data": {
    "amount": "35 000", "category": "продажи", "note": "Свадебное оформление",
    "op_date": "2026-08-20", "counterparty": "Мария", "doc_type": "receipt"}})
check("ручной доход принят", r.status_code == 200, r.text[:200])
man_id = r.json()["id"]
man = database.finance_row(man_id, bid)
check("пробелы в сумме не помешали", man["amount"] == 35000, man)
check("дата сохранена", man["op_date"] == "2026-08-20", man)
check("контрагент сохранён", man["counterparty"] == "Мария", man)
check("источник — ручной ввод", man["source"] == "manual", man)
check("лежит в той же таблице, что и разбор",
      man["kind"] == "income" and database.get_finance_entry(entry_id, bid) is not None, man)
with database._connect() as conn:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    both = conn.execute(
        "SELECT COUNT(*) AS n FROM finance_entries WHERE business_id = ? AND id IN (?, ?)",
        (bid, entry_id, man_id)).fetchone()["n"]
# finance_imports — это журнал загрузок выписок, а не вторая касса.
check("операции ИИ и руками лежат в одной таблице", both == 2, both)
check("отдельной финансовой базы для ИИ не появилось",
      not [t for t in tables if "financ" in t and t not in ("finance_entries", "finance_imports")],
      tables)
check("у ручной операции тоже есть история",
      any(h["event"] == "created"
          for h in c.get(f"/api/finance/{man_id}", headers=H).json()["history"]))

r = c.post("/api/finance", headers=H,
           json={"kind": "expense", "category": "аренда", "amount": 30000, "note": "август"})
check("короткая форма ввода всё ещё работает", r.status_code == 200, r.text[:160])
rent_id = r.json()["id"]

# ============================================================
print("\n== АРИФМЕТИКА: ВЫРУЧКА − РАСХОДЫ = ПРИБЫЛЬ ==")
s = summary(H)
rows = database.finance_rows(bid, limit=500)
inc = sum(x["amount"] for x in rows if x["kind"] == "income")
exp = sum(x["amount"] for x in rows if x["kind"] == "expense")
check("выручка сходится с записями", s["income"] == inc, (s["income"], inc))
check("расходы сходятся с записями", s["expense"] == exp, (s["expense"], exp))
check("прибыль = выручка − расходы", s["profit"] == s["income"] - s["expense"], s)
check("маржа = прибыль / выручка",
      s["margin"] == round(s["profit"] * 100 / s["income"], 1), s)
check("маржа отрицательная, когда расходы больше", s["margin"] < 0 if s["profit"] < 0 else True, s)

print("\n-- правка суммы пересчитывает всё --")
before = summary(H)
r = c.post(f"/api/finance/{rent_id}", headers=H, json={"data": {"amount": "45000"}})
check("правка принята", r.status_code == 200, r.text[:200])
after = r.json()["summary"]
check("расходы выросли ровно на разницу",
      after["expense"] - before["expense"] == 15000, (before["expense"], after["expense"]))
check("прибыль пересчитана", after["profit"] == after["income"] - after["expense"], after)
check("маржа пересчитана",
      after["margin"] == round(after["profit"] * 100 / after["income"], 1), after)

print("\n-- удаление тоже пересчитывает --")
r = c.post(f"/api/finance/{rent_id}/delete", headers=H)
check("операция удалена", r.status_code == 200, r.text[:120])
gone = r.json()["summary"]
check("расходы уменьшились", gone["expense"] == after["expense"] - 45000, gone)
check("прибыль снова сходится", gone["profit"] == gone["income"] - gone["expense"], gone)
check("след удаления остался в истории",
      any(h["event"] == "removed"
          for h in database.memory_links(bid, "expense", rent_id)),
      database.memory_links(bid, "expense", rent_id))

# ============================================================
print("\n== ПРАВКА ВСЕХ ПОЛЕЙ ==")
r = c.post(f"/api/finance/{entry_id}", headers=H, json={"data": {
    "amount": "72000", "category": "зарплата и премия", "note": "август, с премией",
    "op_date": "2026-08-05", "counterparty": "Иванов П.", "source": "manual",
    "doc_type": "bank"}})
check("правка принята", r.status_code == 200, r.text[:200])
d = r.json()
for field, want in (("amount", 72000), ("category", "зарплата и премия"),
                    ("note", "август, с премией"), ("op_date", "2026-08-05"),
                    ("counterparty", "Иванов П."), ("source", "manual"),
                    ("doc_type", "bank")):
    check(f"поле «{field}» изменено", str(d["values"].get(field)) == str(want),
          (d["values"].get(field), want))
check("все семь правок в истории", len(d["changes"]) == 7, sorted(d["changes"]))
check("история пополнилась",
      any(h["event"] == "edited" for h in d["history"]), [h["event"] for h in d["history"]])
check("видно, что было и что стало",
      d["changes"]["amount"] == {"was": 70000, "now": 72000}, d["changes"]["amount"])

print("\n-- связь можно снять и поставить заново --")
r = c.post(f"/api/finance/{entry_id}", headers=H, json={"data": {"employee_id": ""}})
check("сотрудник отвязан", database.get_finance_entry(entry_id, bid)["employee_id"] is None,
      database.get_finance_entry(entry_id, bid))
r = c.post(f"/api/finance/{entry_id}", headers=H, json={"data": {"employee_id": str(emp)}})
check("и привязан обратно",
      database.get_finance_entry(entry_id, bid)["employee_id"] == emp, r.text[:160])

order_id = database.add_order(bid, "Букет 15 роз", amount=2500)
r = c.post(f"/api/finance/{man_id}", headers=H, json={"data": {"order_id": str(order_id)}})
check("операцию можно привязать к заявке",
      database.finance_row(man_id, bid)["order_id"] == order_id, r.text[:160])
check("и текст заявки виден в ленте",
      database.finance_row(man_id, bid)["order_text"] == "Букет 15 роз")

client_id, _ = database.upsert_external_client(bid, "t:1", "test", name="Мария")
r = c.post(f"/api/finance/{man_id}", headers=H, json={"data": {"client_id": str(client_id)}})
check("и к клиенту тоже",
      database.finance_row(man_id, bid)["client_name"] == "Мария", r.text[:160])

# ============================================================
print("\n== КРАЙНИЕ СЛУЧАИ ==")
bad = [
    ("нулевая сумма", {"amount": "0"}),
    ("отрицательная сумма", {"amount": "-500"}),
    ("сумма словами", {"amount": "много"}),
    ("пустая сумма", {"amount": ""}),
    ("кривая дата", {"amount": "100", "op_date": "31.02.2026"}),
]
for label, data in bad:
    r = c.post("/api/finance", headers=H, json={"kind": "expense", "data": data})
    check(f"{label} — отказ", r.status_code == 400, (r.status_code, r.text[:90]))

r = c.post("/api/finance", headers=H, json={"kind": "expense", "data": {"amount": "1 850,50"}})
check("копейки округляются, а не роняют запись", r.status_code == 200, r.text[:120])
kop = database.get_finance_entry(r.json()["id"], bid)
check("до целых рублей", kop["amount"] == 1851, kop)
c.post(f"/api/finance/{kop['id']}/delete", headers=H)

r = c.post("/api/finance", headers=H, json={"kind": "expense",
                                            "data": {"amount": "999999999"}})
check("очень большая сумма принимается", r.status_code == 200, r.text[:120])
big_id = r.json()["id"]
s = summary(H)
check("и не ломает прибыль", s["profit"] == s["income"] - s["expense"], s)
c.post(f"/api/finance/{big_id}/delete", headers=H)

print("\n-- маржа на границах --")
bid2, H2 = reg("fin_zero")
s = summary(H2)
check("пустые финансы: выручка 0", s["income"] == 0, s)
check("маржи не существует, а не «0%»", s["margin"] is None, s)
check("прибыль 0", s["profit"] == 0, s)

c.post("/api/finance", headers=H2, json={"kind": "expense", "data": {"amount": "5000"}})
s = summary(H2)
check("расход без выручки: прибыль отрицательная", s["profit"] == -5000, s)
check("маржи всё ещё нет — делить не на что", s["margin"] is None, s)

c.post("/api/finance", headers=H2, json={"kind": "income", "data": {"amount": "5000"}})
s = summary(H2)
check("вышли в ноль: прибыль 0", s["profit"] == 0, s)
check("маржа ровно 0%", s["margin"] == 0, s)

c.post("/api/finance", headers=H2, json={"kind": "expense", "data": {"amount": "2500"}})
s = summary(H2)
check("расходы больше выручки: прибыль минус", s["profit"] == -2500, s)
check("маржа отрицательная", s["margin"] == -50.0, s)

c.post("/api/finance", headers=H2, json={"kind": "income", "data": {"amount": "10000"}})
s = summary(H2)
check("маржа дробная считается точно",
      s["margin"] == round(s["profit"] * 100 / s["income"], 1), s)

print("\n-- возврат не выдаётся за выручку --")
bid3, H3 = reg("fin_refund")
c.post("/api/finance", headers=H3, json={"kind": "income", "data": {"amount": "10000"}})
ref = upload(H3, "возврат.txt", "ЧЕК ВОЗВРАТА\nВозврат средств покупателю 2 000 руб")
c.post(f"/api/inbox/{ref['id']}/action", headers=H3, json={"action": "create_expense"})
s = summary(H3)
check("возврат ушёл в расходы, а не в выручку", s["income"] == 10000, s)
check("прибыль уменьшилась на возврат", s["profit"] == 8000, s)
check("возвраты видно отдельной строкой", s["refunds"] == 2000, s)
check("арифметика не нарушена", s["profit"] == s["income"] - s["expense"], s)

# ============================================================
print("\n== ГРАНИЦЫ И ЧУЖОЕ ==")
r = c.post(f"/api/finance/{entry_id}", headers=H2, json={"data": {"amount": "1"}})
check("чужую операцию не изменить", r.status_code == 404, r.status_code)
check("и сумма цела", database.get_finance_entry(entry_id, bid)["amount"] == 72000)
check("чужую операцию не прочитать",
      c.get(f"/api/finance/{entry_id}", headers=H2).status_code == 404)
check("чужую операцию не удалить",
      c.post(f"/api/finance/{entry_id}/delete", headers=H2).status_code == 404)
r = c.post("/api/finance", headers=H2, json={"kind": "expense",
                                             "data": {"amount": "100", "employee_id": str(emp)}})
check("чужого сотрудника в операцию не вписать", r.status_code == 400, r.text[:120])

r = c.post(f"/api/inbox/{bank['id']}/action", headers=H, json={"action": "create_expense"})
check("подтвердить дважды нельзя", r.status_code == 409, r.status_code)

check("несуществующая операция — 404",
      c.get("/api/finance/999999", headers=H).status_code == 404)
check("разбор финансов по-прежнему доступен",
      c.get("/api/finance/insights", headers=H).status_code == 200)

print("\n== READ-ONLY ПОСЛЕ ТРИАЛА ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("вносить операции нельзя",
      c.post("/api/finance", headers=H, json={"kind": "expense",
                                              "data": {"amount": "100"}}).status_code == 402)
check("править нельзя",
      c.post(f"/api/finance/{entry_id}", headers=H,
             json={"data": {"amount": "1"}}).status_code == 402)
check("удалять нельзя",
      c.post(f"/api/finance/{entry_id}/delete", headers=H).status_code == 402)
check("но цифры видно", c.get("/api/finance", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
