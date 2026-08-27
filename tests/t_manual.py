# -*- coding: utf-8 -*-
"""
Ручное управление: последнее слово за владельцем.

VELOR разбирает, предлагает и записывает сам — но всё, что он записал, должно
быть открываемо, правимо, убираемо и восстановимо руками. Проверяем это для
всех десяти видов записей сразу: доход, расход, клиент, сотрудник, услуга,
товар, заявка, правило, цель, документ.

Отдельно — три вещи, которые легко потерять:
  1) исправление владельца не должно стирать то, что предлагал ИИ;
  2) архив — не удаление: запись уходит из работы и из знаний ядра, но цела;
  3) после правки денег аналитика обязана пересчитаться сразу.
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
import server, database, entities, signals, understanding

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
    return c.post("/api/memory/entity/" + kind, headers=H, json={"data": data})


def card(H, kind, eid):
    return c.get(f"/api/memory/entity/{kind}/{eid}", headers=H)


def edit(H, kind, eid, data):
    return c.post(f"/api/memory/entity/{kind}/{eid}", headers=H, json={"data": data})


def archive(H, kind, eid, on=True):
    return c.post(f"/api/memory/entity/{kind}/{eid}/archive", headers=H, json={"on": on})


def remove(H, kind, eid):
    return c.post(f"/api/memory/entity/{kind}/{eid}/delete", headers=H)


def listing(H, kind, archived=False):
    return c.get(f"/api/memory/list?type={kind}" + ("&archived=1" if archived else ""),
                 headers=H).json()


def events(H, kind, eid):
    return [h["event"] for h in card(H, kind, eid).json().get("history", [])]


bid, H = reg("manual_one")
bid2, H2 = reg("manual_two")

# Все виды, которые владелец должен уметь вести руками, и чем их заполнять.
SAMPLE = {
    "company":  {"title": "Режим работы", "body": "с 9 до 21 без выходных"},
    "service":  {"title": "Букет на заказ", "body": "от 3500 ₽, сборка 40 минут"},
    "product":  {"title": "Ваза стеклянная", "body": "1200 ₽, в наличии 4"},
    "employee": {"title": "Петрова Анна", "role": "флорист", "contact": "@anna",
                 "body": "смены пн-ср"},
    "supplier": {"title": "Цветбаза Юг", "contact": "+7 900 000-00-00",
                 "terms": "отсрочка 14 дней"},
    "rule":     {"title": "Возврат в течение 24 часов", "body": "если букет не забрали"},
    "client":   {"name": "Смирнов Олег", "phone": "+7 911 111-11-11", "notes": "любит пионы"},
    "order":    {"text": "Букет 25 роз", "amount": "7500", "phone": "+7 911 222-22-22"},
    "expense":  {"amount": "4300", "category": "закупка", "note": "розы"},
    "income":   {"amount": "12000", "category": "продажи", "note": "букеты на 8 марта"},
    "goal":     {"title": "Выйти на 500 тысяч выручки", "target": "500000",
                 "metric": "income"},
}
REQUIRED = ("income", "expense", "client", "employee", "service", "product",
            "order", "rule", "goal", "document")

print("== ЧТО МОЖНО С КАЖДЫМ ВИДОМ ==")
for t in REQUIRED:
    sch = entities.schema(t)
    check(f"{t}: смотреть и править", sch["can_edit"] and entities.ENTITIES[t].get("read"))
    check(f"{t}: убирается (архив или удаление)",
          sch["can_archive"] or sch["can_delete"],
          sch)
check("документ формой не заводится — он появляется файлом",
      entities.schema("document")["can_create"] is False)
check("остальные девять заводятся руками",
      all(entities.schema(t)["can_create"] for t in REQUIRED if t != "document"))
# Деньги и заявки — события: их либо не было, либо они были. Спрятанный, но
# посчитанный расход врал бы в прибыли, поэтому архива у них нет.
check("у денег архива нет — только удаление",
      not entities.schema("expense")["can_archive"]
      and not entities.schema("income")["can_archive"]
      and entities.schema("expense")["can_delete"])
check("у заявки архива нет — у неё статус",
      not entities.schema("order")["can_archive"] and entities.schema("order")["can_delete"])
check("сотрудник, услуга, клиент, документ, цель — архивируются",
      all(entities.schema(t)["can_archive"]
          for t in ("employee", "service", "client", "document", "goal", "rule",
                    "product", "supplier", "company")))

print("\n== СОЗДАНИЕ РУКАМИ ==")
MADE = {}
for t, data in SAMPLE.items():
    r = create(H, t, data)
    okk = r.status_code == 200 and r.json().get("id")
    check(f"{t}: записано", okk, r.status_code if not okk else "")
    if okk:
        MADE[t] = r.json()["id"]
check("документ так завести нельзя",
      create(H, "document", {"filename": "x.pdf"}).status_code == 400)
check("неизвестный вид — 404", create(H, "dragon", {"title": "x"}).status_code == 404)
r = create(H, "service", {"body": "без названия"})
check("без обязательного поля не сохранится", r.status_code == 400, r.status_code)
check("и объясняет, какого поля не хватает",
      "Название" in (r.json().get("detail") or ""), r.json())

d = card(H, "employee", MADE["employee"]).json()
check("в карточке видно текущие данные", d["values"]["title"] == "Петрова Анна", d["values"])
check("и должность не потерялась", d["values"].get("role") == "флорист", d["values"])
check("автор записан — владелец", (d["source"] or {}).get("actor_ru") == "владелец", d["source"])
check("источник — внесено вручную", (d["source"] or {}).get("source_kind") == "manual")
check("уверенность у ручной записи полная", (d["source"] or {}).get("confidence") == 1.0)
check("история начинается с создания", events(H, "employee", MADE["employee"]) == ["created"])
check("исправлять пока нечего", d["corrections"] == [], d["corrections"])

print("\n== ПРАВКА ==")
r = edit(H, "employee", MADE["employee"], dict(SAMPLE["employee"], role="старший флорист"))
check("правка сохранилась", r.status_code == 200 and
      r.json()["values"]["role"] == "старший флорист", r.text[:200])
ch = r.json()["changes"]
check("что было и что стало — в истории",
      ch.get("role", {}).get("was") == "флорист" and ch["role"]["now"] == "старший флорист", ch)
check("событие правки записано", events(H, "employee", MADE["employee"]) == ["created", "edited"])
check("правку видно в списке",
      any(i["id"] == MADE["employee"] and i["edits"] == 1
          for i in listing(H, "employee")["items"]))
r = edit(H, "employee", MADE["employee"], dict(SAMPLE["employee"], role="старший флорист"))
check("повторная правка без изменений историю не засоряет",
      len(events(H, "employee", MADE["employee"])) == 2)

print("\n== ИИ ЗАПИСАЛ → ВЛАДЕЛЕЦ ПОПРАВИЛ ==")
item = c.post("/api/inbox", headers=H,
              json={"text": "Перевёл Петровой Анне зарплату 40000 рублей"}).json()["item"]
res = (item.get("result") or {})
check("VELOR понял, что это движение денег",
      res.get("type") in ("FINANCIAL_TRANSACTION", "SALARY_PAYMENT", "EXPENSE_DOCUMENT"),
      res.get("type"))
check("и нашёл сотрудника в памяти бизнеса",
      str((res.get("extracted_data") or {}).get("employee_id") or "") == str(MADE["employee"]),
      res.get("extracted_data"))
r = c.post(f"/api/inbox/{item['id']}/action", headers=H,
           json={"action": "create_expense"})
check("владелец подтвердил — операция создана", r.status_code == 200, r.text[:200])
ai_eid = r.json().get("entity_id") or r.json().get("id")
check("операция нашлась", bool(ai_eid), r.json())

before = c.get("/api/finance", headers=H).json()["summary"]
op = c.get(f"/api/finance/{ai_eid}", headers=H).json()
check("в карточке видно, что операцию предложил VELOR",
      op["origin"]["by"] == "confirmed", op["origin"])
check("и что подтвердил её человек",
      op["origin"]["by_ru"] == "VELOR предложил, вы подтвердили", op["origin"])
check("и с какой уверенностью", (op["origin"]["confidence"] or 0) > 0.5, op["origin"])
check("в списке операций та же метка",
      any(e["id"] == ai_eid and e["origin"]["by"] == "confirmed"
          for e in c.get("/api/finance", headers=H).json()["entries"]))

r = c.post(f"/api/finance/{ai_eid}", headers=H,
           json={"data": {"amount": "35000", "category": "зарплата"}})
check("владелец исправил сумму", r.status_code == 200 and r.json()["values"]["amount"] == 35000,
      r.text[:200])
fixes = {x["field"]: x for x in r.json()["corrections"]}
check("предложение ИИ не стёрлось", "amount" in fixes, r.json()["corrections"])
check("видно оба значения: ИИ и владельца",
      str(fixes["amount"]["ai"]) == "40000" and str(fixes["amount"]["now"]) == "35000",
      fixes.get("amount"))
hist = [h["event"] for h in r.json()["history"]]
check("история хранит и запись ИИ, и правку человека", hist == ["created", "edited"], hist)
src = [h["by"] for h in r.json()["history"]]
check("и происхождение разное: разбор и рука человека",
      src == ["confirmed", "manual"], src)

after = c.get("/api/finance", headers=H).json()["summary"]
check("аналитика пересчиталась сразу",
      after["expense"] == before["expense"] - 5000,
      (before["expense"], after["expense"]))
check("прибыль = доход − расход",
      after["profit"] == after["income"] - after["expense"], after)
check("маржа = прибыль / выручка",
      after["margin"] == round(after["profit"] * 100 / after["income"], 1), after)
check("модули помечены на пересборку", signals.is_dirty(bid, "forecast"), "forecast")
check("Директор тоже", signals.is_dirty(bid, "board"), "board")

# Та же цепочка, но глазами памяти бизнеса: карточка расхода должна знать,
# что предлагал ИИ, независимо от того, с какой страницы её открыли.
mc = card(H, "expense", ai_eid).json()
check("память бизнеса показывает то же исправление",
      any(x["field"] == "amount" for x in mc["corrections"]), mc["corrections"])

print("\n== АРХИВ: УБРАТЬ, НЕ ПОТЕРЯВ ==")
facts_before = database.get_business(bid)["facts"]
check("сотрудник в знаниях ядра", "Петрова Анна" in facts_before)
r = archive(H, "employee", MADE["employee"])
check("убрали в архив", r.status_code == 200 and r.json()["archived"], r.text[:200])
check("из рабочего списка исчез",
      all(i["id"] != MADE["employee"] for i in listing(H, "employee")["items"]))
check("в архиве — нашёлся",
      any(i["id"] == MADE["employee"] for i in listing(H, "employee", archived=True)["items"]))
check("ядро его больше не знает", "Петрова Анна" not in database.get_business(bid)["facts"])
check("но запись цела", card(H, "employee", MADE["employee"]).json()["values"]["title"]
      == "Петрова Анна")
check("и история цела", "archived" in events(H, "employee", MADE["employee"]))
check("в старой выплате имя осталось",
      (c.get(f"/api/finance/{ai_eid}", headers=H).json()["entry"].get("employee_name") or "")
      == "Петрова Анна")
check("счётчик действующих упал", entities.count(bid, "employee") == 0)
check("счётчик архива вырос", entities.count(bid, "employee", archived=True) == 1)
check("на карте видно, сколько убрано",
      any(k["type"] == "employee" and k["archived"] == 1
          for g in c.get("/api/memory/map", headers=H).json()["groups"] for k in g["types"]))
r = archive(H, "employee", MADE["employee"], on=False)
check("вернули в работу", r.status_code == 200 and not r.json()["archived"])
check("ядро снова знает", "Петрова Анна" in database.get_business(bid)["facts"])
check("возврат тоже записан", "restored" in events(H, "employee", MADE["employee"]))
check("деньги в архив не убираются", archive(H, "expense", ai_eid).status_code == 400)
check("заявка тоже", archive(H, "order", MADE["order"]).status_code == 400)

# Архивный сотрудник не должен подставляться в новые разборы: VELOR не может
# предлагать выплату человеку, которого владелец убрал из работы.
archive(H, "employee", MADE["employee"])
it2 = c.post("/api/inbox", headers=H,
             json={"text": "Перевёл Петровой Анне 15000"}).json()["item"]
check("архивный сотрудник в новые предложения не попадает",
      not (it2.get("result") or {}).get("extracted_data", {}).get("employee_id"),
      (it2.get("result") or {}).get("extracted_data"))
archive(H, "employee", MADE["employee"], on=False)

print("\n== АРХИВ ДОКУМЕНТА ==")
r = c.post("/api/documents/upload", headers=H,
           files=[("file", ("price.txt", "Пионы стоят 450 рублей за штуку".encode("utf-8"),
                            "text/plain"))])
doc_id = r.json().get("doc_id")
check("документ загружен", bool(doc_id), r.json())
check("ядро цитирует документ", any("450" in x for x in database.search_chunks(bid, "пионы")))
check("документ убирается в архив", archive(H, "document", doc_id).status_code == 200)
check("и перестаёт отвечать на вопросы", database.search_chunks(bid, "пионы") == [])
check("но файл на месте", card(H, "document", doc_id).status_code == 200)
archive(H, "document", doc_id, on=False)
check("вернули — снова цитируется", any("450" in x for x in database.search_chunks(bid, "пионы")))

print("\n== УДАЛЕНИЕ И СВЯЗИ ==")
r = remove(H, "employee", MADE["employee"])
check("сотрудника с выплатой удалить нельзя", r.status_code == 400, r.status_code)
check("и сказано почему", "архив" in (r.json().get("detail") or "").lower(), r.json())
check("запись на месте", card(H, "employee", MADE["employee"]).status_code == 200)
check("карточка предупреждает заранее",
      bool(card(H, "employee", MADE["employee"]).json().get("blocked")))
c.post(f"/api/finance/{ai_eid}", headers=H, json={"data": {"employee_id": ""}})
r = remove(H, "employee", MADE["employee"])
check("после отвязки удаляется", r.status_code == 200, r.text[:200])
check("в ответе сказано, что именно удалено",
      "Петрова Анна" in (r.json().get("detail") or ""), r.json())
check("из списка исчез",
      all(i["id"] != MADE["employee"] for i in listing(H, "employee")["items"]))
check("след удаления в истории остался",
      any(l["event"] == "removed"
          for l in database.memory_links(bid, "employee", MADE["employee"])))

oid = c.post("/api/orders", headers=H,
             json={"text": "Композиция на стол", "amount": 5000}).json().get("id")
cl = create(H, "client", {"name": "Постоянный клиент"}).json()["id"]
database.update_order(oid, bid, client_id=cl) if False else None
check("клиента без истории удалить можно", remove(H, "client", cl).status_code == 200)

exp2 = create(H, "expense", {"amount": "900", "category": "доставка",
                             "order_id": str(MADE["order"])}).json()["id"]
r = remove(H, "order", MADE["order"])
check("заявку с привязанной операцией удалить нельзя", r.status_code == 400, r.status_code)
check("и объяснено, что делать", "Финанс" in (r.json().get("detail") or ""), r.json())
c.post(f"/api/finance/{exp2}", headers=H, json={"data": {"order_id": ""}})
check("после отвязки заявка удаляется", remove(H, "order", MADE["order"]).status_code == 200)

sum_before = c.get("/api/finance", headers=H).json()["summary"]
check("удаление операции пересчитывает деньги",
      c.post(f"/api/finance/{exp2}/delete", headers=H).json()["summary"]["expense"]
      == sum_before["expense"] - 900)

print("\n== ЧУЖОЕ ==")
check("сосед не видит записей", listing(H2, "service")["items"] == [])
check("чужую карточку не открыть", card(H2, "service", MADE["service"]).status_code == 404)
check("чужую запись не изменить",
      edit(H2, "service", MADE["service"], {"title": "подмена"}).status_code == 400)
check("чужую не заархивировать", archive(H2, "service", MADE["service"]).status_code == 400)
check("чужую не удалить", remove(H2, "service", MADE["service"]).status_code == 400)
check("и она цела", card(H, "service", MADE["service"]).json()["values"]["title"]
      == "Букет на заказ")
check("несуществующая — 404", card(H, "service", 999999).status_code == 404)
check("удалить несуществующую нельзя", remove(H, "service", 999999).status_code == 400)

print("\n== READ-ONLY ПОСЛЕ ТРИАЛА ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("заводить нельзя", create(H, "rule", {"title": "новое"}).status_code == 402)
check("править нельзя",
      edit(H, "service", MADE["service"], {"title": "другое"}).status_code == 402)
check("архивировать нельзя", archive(H, "service", MADE["service"]).status_code == 402)
check("удалять нельзя", remove(H, "rule", MADE["rule"]).status_code == 402)
check("но смотреть можно", card(H, "service", MADE["service"]).status_code == 200)
check("и историю тоже", len(card(H, "service", MADE["service"]).json()["history"]) >= 1)
check("и список", c.get("/api/memory/list?type=service", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
