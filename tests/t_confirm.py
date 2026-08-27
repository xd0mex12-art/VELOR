# -*- coding: utf-8 -*-
"""
Слой подтверждения человеком.

Смысл слоя в одном: последнее слово за владельцем, и это слово сохраняется.
Проверяется весь путь — загрузка → разбор → уверенность → просмотр → правка →
подтверждение → появление записи → история, — а также то, что нельзя обойти:
подтвердить непредложенное, подтвердить дважды, дописать чужое поле, создать
запись без обязательного значения, тронуть чужой материал.
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
    r = c.post("/api/inbox/upload", headers=H,
               files=[("files", (name, text.encode("utf-8"), mime))])
    return r.json()["saved"][0]


bid, H = reg("conf_main")

# ============================================================
print("\n== ПУТЬ ЦЕЛИКОМ: загрузка → разбор → правка → подтверждение → запись ==")
CHECK = ("КАССОВЫЙ ЧЕК\nООО «Ромашка» ИНН 7701234567\n"
         "Доставка курьером 1 850 ₽\nИтого к оплате 1 850 ₽\nКассир: Петрова")

print("\n-- 1. загрузка --")
it = upload(H, "чек доставка.txt", CHECK)
check("материал принят", it["id"] > 0, it)

print("\n-- 2. разбор пришёл вместе с материалом --")
res = it["result"]
check("разбор есть сразу", res is not None, it)
check("тип определён", res["type"] == "EXPENSE_DOCUMENT", res["type"])

print("\n-- 3. уверенность и что она означает --")
check("уровень средний", res["level"] == "MEDIUM", (res["level"], res["confidence"]))
check("сказано, что нужно подтверждение", res["needs"] == "нужно подтверждение", res["needs"])
check("материал ждёт человека",
      database.get_inbox_item(it["id"], bid)["status"] == "NEEDS_REVIEW")

print("\n-- 4. просмотр: пришла готовая форма --")
act = [a for a in res["suggested_actions"] if a["action"] == "create_expense"][0]
check("у действия указан вид записи", act["entity"] == "expense", act)
check("у действия есть поля формы", len(act["fields"]) >= 2, act.get("fields"))
byname = {f["name"]: f for f in act["fields"]}
check("сумма подставлена из разбора", byname["amount"]["value"] == 1850, byname["amount"])
check("категория подставлена", byname["category"]["value"] == "доставка", byname["category"])
check("сумма помечена обязательной", byname["amount"]["required"] is True, byname["amount"])
check("сумма — число", byname["amount"]["type"] == "int", byname["amount"])
check("известно, где потом искать запись", act["where"] == "finance.html", act)

print("\n-- 5. правка: человек меняет сумму и категорию --")
before = database.finance_summary(bid)["expense"]
r = c.post(f"/api/inbox/{it['id']}/action", headers=H, json={
    "action": "create_expense",
    "data": {"amount": 1900, "category": "курьер", "note": "чек от 24.08"}})
check("подтверждение принято", r.status_code == 200, r.text[:150])
d = r.json()

print("\n-- 6. запись создана из ИСПРАВЛЕННЫХ значений --")
after = database.finance_summary(bid)["expense"]
check("в финансах ровно исправленная сумма", after - before == 1900, (before, after))
entry = database.list_finance_entries(bid)[0]
check("категория тоже исправленная", entry["category"] == "курьер", entry)
check("заметка человека сохранена", entry["note"] == "чек от 24.08", entry)
check("ответ говорит, что создано", d["entity_type"] == "expense" and d["entity_id"],
      (d.get("entity_type"), d.get("entity_id")))
check("материал разобран",
      database.get_inbox_item(it["id"], bid)["status"] == "PROCESSED")

print("\n-- 7. история: что предлагал ИИ и что сделал человек --")
h = c.get(f"/api/inbox/{it['id']}/history", headers=H).json()
check("история отдаётся", h["history"], h)
rec = h["history"][-1]
check("решение помечено как правка", rec["decision"] == "edited", rec["decision"])
check("сохранено предложение ИИ", rec["original"].get("amount") == 1850, rec["original"])
check("сохранено значение человека", rec["corrected"].get("amount") == 1900, rec["corrected"])
check("видно, что именно изменилось",
      rec["changes"]["amount"] == {"was": 1850, "now": 1900}, rec["changes"])
check("категория тоже в изменениях",
      rec["changes"]["category"]["now"] == "курьер", rec["changes"])
check("кто исправил", rec["actor"] == "business", rec["actor"])
check("когда исправил", bool(rec["created_at"]), rec)
check("на что ссылается разбор", rec["result_id"] == res["id"], (rec["result_id"], res["id"]))
check("что создано", (rec["entity_type"], bool(rec["entity_id"])) == ("expense", True), rec)
check("статистика правок посчитана", h["stats"]["edited"] >= 1, h["stats"])

print("\n== ПОДТВЕРЖДЕНИЕ БЕЗ ПРАВОК ==")
it2 = upload(H, "чек2.txt", "Кассовый чек\nИтого к оплате 640 ₽")
r = c.post(f"/api/inbox/{it2['id']}/action", headers=H, json={"action": "create_expense"})
check("подтверждено как есть", r.status_code == 200, r.text[:120])
rec = c.get(f"/api/inbox/{it2['id']}/history", headers=H).json()["history"][-1]
check("решение — подтверждение, а не правка", rec["decision"] == "confirmed", rec["decision"])
check("изменений нет", rec["changes"] == {}, rec["changes"])
check("сумма ИИ и сумма записи совпали",
      rec["original"].get("amount") == rec["corrected"].get("amount") == 640, rec)

print("\n== «НЕ УЧИТЫВАТЬ» ==")
it3 = upload(H, "чек3.txt", "Кассовый чек\nИтого к оплате 100 ₽")
money_before = database.finance_summary(bid)["expense"]
r = c.post(f"/api/inbox/{it3['id']}/dismiss", headers=H, json={"reason": "это личная покупка"})
check("отклонение принято", r.status_code == 200, r.text[:120])
check("в финансы ничего не попало",
      database.finance_summary(bid)["expense"] == money_before)
check("материал больше не ждёт",
      database.get_inbox_item(it3["id"], bid)["status"] == "PROCESSED")
check("сам материал не удалён", database.get_inbox_item(it3["id"], bid) is not None)
check("оригинал на месте",
      c.get(f"/api/inbox/{it3['id']}/file", headers=H).status_code == 200)
rec = c.get(f"/api/inbox/{it3['id']}/history", headers=H).json()["history"][-1]
check("отказ записан в историю", rec["decision"] == "dismissed", rec["decision"])
check("причина сохранена", rec["note"] == "это личная покупка", rec["note"])
check("разбор ИИ рядом с отказом сохранён",
      c.get(f"/api/inbox/{it3['id']}/result", headers=H).json()["result"] is not None)

print("\n== НИЗКАЯ УВЕРЕННОСТЬ: человек уточняет сам ==")
it4 = upload(H, "непонятно.txt", "зфывафыв qwe rty 123")
res4 = c.get(f"/api/inbox/{it4['id']}/result", headers=H).json()["result"]
check("сначала не понял", res4["type"] == "UNKNOWN" and res4["level"] == "LOW", res4["type"])
check("список видов предложен для уточнения",
      any(t["type"] == "PRICE_LIST" for t in res4["types"]), res4["types"][:2])
check("UNKNOWN выбрать нельзя",
      all(t["type"] != "UNKNOWN" for t in res4["types"]), res4["types"])
r = c.post(f"/api/inbox/{it4['id']}/classify", headers=H, json={"type": "PRICE_LIST"})
check("уточнение принято", r.status_code == 200, r.text[:120])
res4b = r.json()["result"]
check("тип стал тем, что назвал человек", res4b["type"] == "PRICE_LIST", res4b["type"])
check("уверенность полная", res4b["confidence"] == 1.0, res4b["confidence"])
check("движок — человек", res4b["engine"] == "human", res4b["engine"])
check("появились подходящие действия",
      "add_price_list" in [a["action"] for a in res4b["suggested_actions"]],
      res4b["suggested_actions"])
check("выдуманный тип отклонён",
      c.post(f"/api/inbox/{it4['id']}/classify", headers=H,
             json={"type": "МОЙ_ТИП"}).status_code == 400)
check("UNKNOWN как уточнение отклонён",
      c.post(f"/api/inbox/{it4['id']}/classify", headers=H,
             json={"type": "UNKNOWN"}).status_code == 400)
hist = c.get(f"/api/inbox/{it4['id']}/history", headers=H).json()["history"]
rec = hist[-1]
check("уточнение в истории", rec["decision"] == "classified", rec["decision"])
check("видно, что ИИ говорил UNKNOWN", rec["original"]["type"] == "UNKNOWN", rec["original"])
check("и что человек сказал PRICE_LIST", rec["corrected"]["type"] == "PRICE_LIST", rec["corrected"])
with database._connect() as conn:
    n = conn.execute("SELECT COUNT(*) AS n FROM inbox_results WHERE item_id = ?",
                     (it4["id"],)).fetchone()["n"]
check("разбор ИИ сохранён рядом с ответом человека", n >= 2, n)

print("\n== УНИВЕРСАЛЬНОСТЬ: восемь видов записей ==")
# Реестр растёт (память бизнеса добавила сотрудников, поставщиков, компанию и
# документы) — проверяем не точный список, а что базовые восемь на месте.
check("реестр знает все восемь",
      {"expense", "income", "client", "order",
       "service", "product", "rule", "goal"} <= set(entities.ENTITIES),
      sorted(entities.ENTITIES))
for name in sorted(entities.ENTITIES):
    sch = entities.schema(name)
    check(f"у «{name}» есть форма и место", bool(sch["fields"]) and bool(sch["where"]), sch)

print("\n-- создание каждого вида одной дверью --")
made = {}
made["expense"] = entities.create(bid, "expense", {"amount": 500, "category": "тест"})
made["income"] = entities.create(bid, "income", {"amount": 700, "category": "продажи"})
made["client"] = entities.create(bid, "client", {"name": "Пётр", "phone": "+79990001122"})
made["order"] = entities.create(bid, "order", {"text": "Букет 15 роз", "amount": 2500})
made["service"] = entities.create(bid, "service", {"title": "Стрижка", "body": "1500 ₽"})
made["product"] = entities.create(bid, "product", {"title": "Шампунь", "body": "800 ₽"})
made["rule"] = entities.create(bid, "rule", {"title": "Предоплата 50%"})
made["goal"] = entities.create(bid, "goal", {"title": "Выручка 500к", "target": 500000,
                                             "metric": "income"})
check("все восемь созданы", all(v[0] for v in made.values()),
      {k: v[0] for k, v in made.items()})
check("клиент попал в базу клиентов",
      any(x["name"] == "Пётр" for x in database.list_clients(bid)), None)
check("заявка попала в заказы",
      any(o["text"] == "Букет 15 роз" for o in database.get_orders(bid, limit=20)), None)
check("сумма заявки записана",
      [o for o in database.get_orders(bid, limit=20) if o["text"] == "Букет 15 роз"][0]["amount"] == 2500)
check("цель попала в цели",
      any(g["title"] == "Выручка 500к" for g in database.list_goals(bid)), None)
check("услуга и товар легли в память",
      {"Стрижка", "Шампунь"} <= {x["title"] for x in database.list_facts(bid)}, None)
check("повторный клиент не задвоился",
      entities.create(bid, "client", {"name": "Пётр", "phone": "+79990001122"})[0]
      == made["client"][0])

print("\n-- проверки формы --")
try:
    entities.create(bid, "expense", {"category": "без суммы"})
    check("без обязательного поля не создаётся", False, "создалось")
except entities.EntityError as e:
    check("без обязательного поля не создаётся", "Сумма" in str(e), str(e))
try:
    entities.create(bid, "expense", {"amount": "много"})
    check("не-число отклонено", False, "создалось")
except entities.EntityError as e:
    check("не-число отклонено", "число" in str(e), str(e))
try:
    entities.create(bid, "expense", {"amount": -5})
    check("отрицательная сумма отклонена", False, "создалось")
except entities.EntityError as e:
    check("отрицательная сумма отклонена", "отрицательн" in str(e), str(e))
try:
    entities.create(bid, "goal", {"title": "Цель", "target": 10, "deadline": "31 декабря"})
    check("кривая дата отклонена", False, "создалось")
except entities.EntityError as e:
    check("кривая дата отклонена", "ГГГГ" in str(e), str(e))
try:
    entities.create(bid, "выдуманное", {})
    check("выдуманный вид отклонён", False, "создалось")
except entities.EntityError:
    check("выдуманный вид отклонён", True)

print("\n-- «1 850» и «1 850 ₽» из формы приводятся к числу --")
_id, _txt, clean = entities.create(bid, "expense", {"amount": "2 300", "category": "х"})
check("пробелы в сумме не мешают", clean["amount"] == 2300, clean)

print("\n== ЧЕГО НЕЛЬЗЯ ==")
it5 = upload(H, "чек5.txt", "Кассовый чек\nИтого к оплате 300 ₽")
check("нельзя подтвердить непредложенное",
      c.post(f"/api/inbox/{it5['id']}/action", headers=H,
             json={"action": "create_income"}).status_code == 400)
c.post(f"/api/inbox/{it5['id']}/action", headers=H, json={"action": "create_expense"})
check("нельзя подтвердить дважды",
      c.post(f"/api/inbox/{it5['id']}/action", headers=H,
             json={"action": "create_expense"}).status_code == 409)

it6 = upload(H, "прайс.txt", "ПРАЙС-ЛИСТ\nСтрижка 1500 ₽")
r = c.post(f"/api/inbox/{it6['id']}/action", headers=H, json={
    "action": "add_price_list",
    "data": {"title": "Прайс", "body": "текст", "kind": "rule", "business_id": 999}})
check("лишние поля из формы отброшены", r.status_code == 200, r.text[:120])
rec = c.get(f"/api/inbox/{it6['id']}/history", headers=H).json()["history"][-1]
check("в аудит попали только описанные поля",
      set(rec["corrected"]) <= {"title", "body"}, rec["corrected"])
check("вид записи не подменён", rec["entity_type"] == "product", rec["entity_type"])

print("\n== ЧУЖОЕ ==")
other_bid, OH = reg("conf_other")
check("чужую историю не прочитать",
      c.get(f"/api/inbox/{it['id']}/history", headers=OH).status_code == 404)
check("чужое не подтвердить",
      c.post(f"/api/inbox/{it['id']}/action", headers=OH,
             json={"action": "create_expense"}).status_code == 409)
check("чужое не отклонить",
      c.post(f"/api/inbox/{it['id']}/dismiss", headers=OH, json={}).status_code == 404)
check("чужое не переклассифицировать",
      c.post(f"/api/inbox/{it['id']}/classify", headers=OH,
             json={"type": "CONTRACT"}).status_code == 404)
check("без токена ничего",
      c.get(f"/api/inbox/{it['id']}/history").status_code == 401)
check("в чужих финансах пусто", database.finance_summary(other_bid)["expense"] == 0)

print("\n== АВТОПРИМЕНЕНИЕ ТОЖЕ ПОПАДАЕТ В ИСТОРИЮ ==")
import ai
real = ai.ai_available
ai.ai_available = lambda: True
ai.understand_material = lambda *a, **k: {
    "type": "PRICE_LIST", "confidence": 0.9, "summary": "Прайс на услуги",
    "extracted_data": {}, "provider": "test"}
it7 = upload(H, "прайс-авто.txt", "ПРАЙС-ЛИСТ\nЦены на услуги\nСтрижка 1500 ₽")
ai.ai_available = real
res7 = c.get(f"/api/inbox/{it7['id']}/result", headers=H).json()["result"]
check("применено само", any(a["auto"] for a in res7["applied"]), res7["applied"])
hist7 = c.get(f"/api/inbox/{it7['id']}/history", headers=H).json()["history"]
check("автодействие видно в истории",
      any(x["decision"] == "auto" for x in hist7), [x["decision"] for x in hist7])
auto = [x for x in hist7 if x["decision"] == "auto"][0]
check("исполнитель — VELOR", auto["actor"] == "velor", auto["actor"])
check("указано, что создано", auto["entity_type"] == "product" and auto["entity_id"], auto)

print("\n== ЛЕНТА ЗНАЕТ О РЕШЕНИЯХ ==")
d = c.get("/api/inbox?limit=50", headers=H).json()
withd = [i for i in d["items"] if i.get("decisions")]
check("в ленте видно число решений", len(withd) >= 4, len(withd))

print("\n== READ-ONLY ПОСЛЕ ТРИАЛА ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
it8 = database.list_inbox(bid, limit=1)[0]
check("подтвердить нельзя",
      c.post(f"/api/inbox/{it8['id']}/action", headers=H,
             json={"action": "create_expense"}).status_code in (402, 409))
check("отклонить нельзя",
      c.post(f"/api/inbox/{it8['id']}/dismiss", headers=H, json={}).status_code == 402)
check("уточнить нельзя",
      c.post(f"/api/inbox/{it8['id']}/classify", headers=H,
             json={"type": "CONTRACT"}).status_code == 402)
check("но историю посмотреть можно",
      c.get(f"/api/inbox/{it8['id']}/history", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
