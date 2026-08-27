# -*- coding: utf-8 -*-
"""
Связи бизнеса: запись имеет смысл только в окружении.

Проверяем все минимальные цепочки — клиент → лид → заявка → услуга → выручка,
сотрудник → зарплата → расход, поставщик → счёт → расход, материал → разбор →
запись, переписка → клиент, деньги → заявка/клиент/сотрудник/поставщик, — и
два живых сценария: «хочу повторить прошлый заказ» и «к чему относится этот
расход».

Отдельно проверяем, что связей НЕ появляется там, где их нет: выдуманная связь
хуже отсутствующей, потому что на неё будут опираться.
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
import server, database, entities, graph, context_engine

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


def create(H, kind, data):
    r = c.post("/api/memory/entity/" + kind, headers=H, json={"data": data})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def rel(H, kind, eid):
    return c.get(f"/api/graph/entity/{kind}/{eid}", headers=H)


def group(data, key):
    return next((g for g in (data.get("groups") or []) if g["key"] == key), None)


def titles(data, key):
    g = group(data, key)
    return [i["title"] for i in (g or {}).get("items", [])]


bid, H = reg("graph_one")
bid2, H2 = reg("graph_two")

print("== ЗАЯВКА УЗНАЁТ СВОИ УСЛУГИ ==")
svc = create(H, "service", {"title": "Букет на заказ", "body": "от 3500 ₽"})
hall = create(H, "service", {"title": "Оформление зала", "body": "от 15000 ₽"})
vase = create(H, "product", {"title": "Ваза стеклянная", "body": "1200 ₽"})
emp = create(H, "employee", {"title": "Петрова Анна", "role": "флорист"})
sup = create(H, "supplier", {"title": "Цветбаза Юг", "terms": "отсрочка 14 дней"})

cid = create(H, "client", {"name": "Смирнов Олег", "phone": "+7 911 111-11-11",
                           "notes": "любит пионы"})
database.update_client(cid, bid, favorite="пионы, без упаковки")

# Клиент, который написал, но ещё ничего не купил, — это лид. Отдельной
# таблицы для него нет и не нужно: разница в наличии заявки.
d = graph.client_dossier(bid, cid)
check("клиент без заявок считается лидом", d["is_lead"] is True, d)
database.save_message(bid, cid, "user", "Здравствуйте, хочу букет")
check("обращение записано", database.count_client_messages(bid, cid) == 1)

oid = database.add_order(bid, "Букет 25 роз к пятнице", client_id=cid, amount=7500)
r = rel(H, "order", oid)
check("связи заявки отдаются", r.status_code == 200, r.status_code)
og = r.json()
check("заявка знает клиента", titles(og, "client") == ["Смирнов Олег"], titles(og, "client"))
check("и узнала услугу по своему тексту", "Букет на заказ" in titles(og, "services"),
      titles(og, "services"))
check("лишнего не приписала", "Ваза стеклянная" not in titles(og, "services")
      and "Оформление зала" not in titles(og, "services"), titles(og, "services"))
svc_item = next(i for i in group(og, "services")["items"] if i["title"] == "Букет на заказ")
check("связь помечена как машинная", svc_item.get("auto") is True, svc_item)
check("и с уверенностью", (svc_item.get("confidence") or 0) > 0, svc_item)
check("связь читается со стороны заявки", svc_item["via"] == "включает", svc_item["via"])

sg = rel(H, "service", svc).json()
check("услуга видит свою заявку", titles(sg, "orders") == ["Букет 25 роз к пятнице"],
      titles(sg, "orders"))
check("и выручку по ней", "7 500 ₽" in (group(sg, "orders")["hint"] or ""),
      group(sg, "orders"))
check("и кто её брал", titles(sg, "clients") == ["Смирнов Олег"], titles(sg, "clients"))
check("услуга, которую не брали, заявок не показывает",
      group(rel(H, "service", hall).json(), "orders") is None)

print("\n== КЛИЕНТ → ЗАЯВКИ → УСЛУГИ → ДЕНЬГИ ==")
inc = create(H, "income", {"amount": "7500", "category": "продажи",
                           "client_id": str(cid), "order_id": str(oid)})
cg = rel(H, "client", cid).json()
check("клиент показывает заявки", titles(cg, "orders") == ["Букет 25 роз к пятнице"])
check("и что он брал", titles(cg, "services") == ["Букет на заказ"], titles(cg, "services"))
check("и обращения", (group(cg, "talk") or {}).get("total") == 1, group(cg, "talk"))
check("и деньги", (group(cg, "money") or {}).get("money", {}).get("income") == 7500,
      group(cg, "money"))
check("клиент с заявкой уже не лид", graph.client_dossier(bid, cid)["is_lead"] is False)

print("\n== «ХОЧУ ПОВТОРИТЬ ПРОШЛЫЙ ЗАКАЗ» ==")
d = graph.client_dossier(bid, cid)
check("досье знает прошлый заказ", d["last_order"]["text"] == "Букет 25 роз к пятнице", d)
check("и сумму", d["last_order"]["amount"] == 7500)
check("и услуги в нём", d["last_order"]["services"] == ["Букет на заказ"])
check("и дату", len(d["last_order"]["date"]) == 10, d["last_order"])
check("и предпочтения", d["preferences"] == "пионы, без упаковки")
block = context_engine._client_block(bid, cid, with_messages=False)
check("контекст ассистента содержит прошлый заказ", "Букет 25 роз" in block)
check("с суммой", "7 500 ₽" in block, block[-300:])
check("и с услугой", "Букет на заказ" in block, block[-300:])
check("и с предпочтениями", "пионы" in block)
check("и запретом додумывать", "ничего не додумывая" in block)
api = c.get(f"/api/graph/client/{cid}", headers=H)
check("досье доступно владельцу", api.status_code == 200 and api.json()["spent"] == 7500,
      api.text[:200])
check("у пустого клиента досье не выдумывает заказов",
      graph.client_dossier(bid, create(H, "client", {"name": "Новенький"}))["orders"] == [])

print("\n== СОТРУДНИК → ЗАРПЛАТА → РАСХОД ==")
sal = create(H, "expense", {"amount": "40000", "category": "зарплата",
                            "employee_id": str(emp), "doc_type": "salary"})
eg = rel(H, "employee", emp).json()
check("сотрудник показывает выплаты", (group(eg, "money") or {}).get("total") == 1,
      group(eg, "money"))
check("и сумму выплат", group(eg, "money")["money"]["expense"] == 40000)
check("операция названа расходом", titles(eg, "money") == ["40 000 ₽"], titles(eg, "money"))

print("\n== ПОСТАВЩИК → СЧЁТ → РАСХОД ==")
inv = create(H, "expense", {"amount": "12000", "category": "закупка",
                            "supplier_id": str(sup), "doc_type": "invoice"})
pg = rel(H, "supplier", sup).json()
check("поставщик показывает оплаты", (group(pg, "money") or {}).get("total") == 1)
check("и считает счета", "счетов: 1" in (group(pg, "money")["hint"] or ""),
      group(pg, "money")["hint"])

print("\n== ДЕНЬГИ → С КЕМ И ПО ЧЕМУ ==")
mg = rel(H, "expense", sal).json()
check("выплата знает человека", titles(mg, "with") == ["Петрова Анна"], titles(mg, "with"))
check("и объясняет связь", group(mg, "with")["items"][0]["via"] == "выплата человеку")
ig = rel(H, "income", inc).json()
check("доход знает клиента и заявку",
      set(titles(ig, "with")) == {"Смирнов Олег", "Букет 25 роз к пятнице"},
      titles(ig, "with"))
check("и через заявку дотягивается до услуги",
      titles(ig, "services") == ["Букет на заказ"], titles(ig, "services"))

print("\n== К ЧЕМУ ОТНОСИТСЯ РАСХОД ==")
item = c.post("/api/inbox", headers=H,
              json={"text": f"Оплатил доставку 900 рублей по заявке {oid}"}).json()["item"]
res = item.get("result") or {}
check("VELOR понял, что это деньги", res.get("type") in
      ("FINANCIAL_TRANSACTION", "EXPENSE_DOCUMENT", "BANK_TRANSACTION"), res.get("type"))
check("и привязал к заявке", (res.get("extracted_data") or {}).get("order_id") == oid,
      res.get("extracted_data"))
why = res.get("relations") or []
check("и объяснил почему", any(w["field"] == "order_id" and "номер" in w["reason"]
                               for w in why), why)
item2 = c.post("/api/inbox", headers=H,
               json={"text": "Оплатил аренду 30000 рублей"}).json()["item"]
check("без повода заявку не приписывает",
      not (item2.get("result") or {}).get("extracted_data", {}).get("order_id"),
      (item2.get("result") or {}).get("extracted_data"))

print("\n== МАТЕРИАЛ → РАЗБОР → ЗАПИСЬ ==")
note = c.post("/api/inbox", headers=H,
              json={"text": "Наши правила: возврат в течение 24 часов"}).json()["item"]
r = c.post(f"/api/inbox/{note['id']}/action", headers=H, json={"action": "add_rules"})
rule_id = r.json().get("entity_id")
check("правило записано из материала", r.status_code == 200 and rule_id, r.text[:200])
rg = rel(H, "rule", rule_id).json()
check("правило помнит источник", (group(rg, "source") or {}).get("items"), rg["groups"])
check("источник открывать как запись нельзя",
      group(rg, "source")["items"][0]["open"] is False)

print("\n== СВЯЗАТЬ И РАЗВЯЗАТЬ РУКАМИ ==")
r = c.post("/api/graph/link", headers=H,
           json={"src_type": "order", "src_id": oid, "dst_type": "product",
                 "dst_id": vase, "kind": "includes"})
check("ручная связь ставится", r.status_code == 200, r.text[:200])
og = rel(H, "order", oid).json()
check("и видна в заявке", "Ваза стеклянная" in titles(og, "services"), titles(og, "services"))
hand = next(i for i in group(og, "services")["items"] if i["title"] == "Ваза стеклянная")
check("ручная связь не помечена машинной", not hand.get("auto"), hand)
check("связь записана в историю записи",
      any(h["event"] == "linked"
          for h in c.get(f"/api/memory/entity/order/{oid}", headers=H).json()["history"]))
check("связей видно в списке",
      any(i["id"] == oid and i["links"] >= 2
          for i in c.get("/api/memory/list?type=order", headers=H).json()["items"]))

# Пересборка после правки текста не должна трогать то, что связал человек.
c.post(f"/api/memory/entity/order/{oid}", headers=H,
       json={"data": {"text": "Оформление зала на свадьбу", "amount": "7500"}})
og = rel(H, "order", oid).json()
check("после правки текста состав пересобран",
      "Оформление зала" in titles(og, "services"), titles(og, "services"))
check("старая машинная связь ушла", "Букет на заказ" not in titles(og, "services"))
check("ручная связь осталась", "Ваза стеклянная" in titles(og, "services"))

link_id = next(i["link_id"] for i in group(og, "services")["items"]
               if i["title"] == "Ваза стеклянная")
check("развязать можно",
      c.post("/api/graph/unlink", headers=H, json={"link_id": link_id}).status_code == 200)
check("и связи больше нет",
      "Ваза стеклянная" not in titles(rel(H, "order", oid).json(), "services"))
check("а сама запись цела",
      c.get(f"/api/memory/entity/product/{vase}", headers=H).status_code == 200)

print("\n== ЧЕГО ДЕЛАТЬ НЕЛЬЗЯ ==")
check("сама с собой запись не связывается",
      c.post("/api/graph/link", headers=H,
             json={"src_type": "order", "src_id": oid, "dst_type": "order",
                   "dst_id": oid}).status_code == 400)
check("несуществующая запись — 404",
      c.post("/api/graph/link", headers=H,
             json={"src_type": "order", "src_id": oid, "dst_type": "service",
                   "dst_id": 999999}).status_code == 404)
check("неизвестный вид — 400",
      c.post("/api/graph/link", headers=H,
             json={"src_type": "order", "src_id": oid, "dst_type": "dragon",
                   "dst_id": 1}).status_code == 400)
check("чужую запись не связать",
      c.post("/api/graph/link", headers=H2,
             json={"src_type": "service", "src_id": svc, "dst_type": "client",
                   "dst_id": cid}).status_code == 404)
check("чужие связи не видны", rel(H2, "order", oid).status_code == 404)
check("несуществующая запись связей не имеет", rel(H, "order", 999999).status_code == 404)

print("\n== СВЯЗЬ НЕ ПЕРЕЖИВАЕТ ЗАПИСЬ ==")
tmp_order = database.add_order(bid, "Букет на заказ для теста", amount=1000)
check("связь появилась", "Букет на заказ" in titles(rel(H, "order", tmp_order).json(), "services"))
c.post(f"/api/memory/entity/order/{tmp_order}/delete", headers=H)
check("после удаления заявки услуга её не показывает",
      "Букет на заказ для теста" not in titles(rel(H, "service", svc).json(), "orders"),
      titles(rel(H, "service", svc).json(), "orders"))
check("рёбер тоже не осталось",
      database.entity_links_of(bid, "order", tmp_order) == [])

print("\n== АРХИВНОЕ НЕ СВЯЗЫВАЕТСЯ ==")
c.post(f"/api/memory/entity/service/{hall}/archive", headers=H, json={"on": True})
arch_order = database.add_order(bid, "Оформление зала на юбилей", amount=20000)
check("услуга из архива в новую заявку не попадает",
      "Оформление зала" not in titles(rel(H, "order", arch_order).json(), "services"),
      titles(rel(H, "order", arch_order).json(), "services"))

print("\n== READ-ONLY ПОСЛЕ ТРИАЛА ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("связывать нельзя",
      c.post("/api/graph/link", headers=H,
             json={"src_type": "order", "src_id": oid, "dst_type": "product",
                   "dst_id": vase}).status_code == 402)
check("развязывать нельзя",
      c.post("/api/graph/unlink", headers=H, json={"link_id": 1}).status_code == 402)
check("но связи видно", rel(H, "order", oid).status_code == 200)
check("и досье клиента тоже",
      c.get(f"/api/graph/client/{cid}", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
