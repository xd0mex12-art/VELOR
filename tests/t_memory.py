# -*- coding: utf-8 -*-
"""
Память бизнеса: знание всегда знает, откуда оно взялось.

Проверяем четыре дороги, которыми знание попадает в память (фото меню →
услуги, PDF правил → правила, текст владельца → цель, документ сотрудника →
сотрудник), и главное — что у каждой записи остаётся цепочка «источник → что
VELOR понял → запись», оригинал доступен, правку видно в истории, а чужой
бизнес ничего этого не видит.
"""
import io as _io
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


def upload(H, name, data, mime="text/plain"):
    if isinstance(data, str):
        data = data.encode("utf-8")
    r = c.post("/api/inbox/upload", headers=H, files=[("files", (name, data, mime))])
    return r.json()["saved"][0]


def note(H, text):
    return c.post("/api/inbox", headers=H, json={"text": text}).json()["item"]


def confirm(H, item_id, action, data=None):
    body = {"action": action}
    if data is not None:
        body["data"] = data
    return c.post(f"/api/inbox/{item_id}/action", headers=H, json=body)


def actions_of(item):
    return {a["action"] for a in (item.get("result") or {}).get("suggested_actions", [])}


def applied_of(item):
    return {a["action"] for a in (item.get("result") or {}).get("applied", [])}


def make_pdf(lines):
    """Настоящий PDF с кириллицей — иначе проверка «PDF правил» ничего не проверяет."""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    try:
        pdfmetrics.registerFont(TTFont("MemA", r"C:\Windows\Fonts\arial.ttf"))
        font = "MemA"
    except Exception:
        font = "Helvetica"
    buf = _io.BytesIO()
    cv = canvas.Canvas(buf)
    cv.setFont(font, 12)
    for i, line in enumerate(lines):
        cv.drawString(60, 800 - i * 20, line)
    cv.save()
    return buf.getvalue()


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"

bid, H = reg("mem_main")

# ============================================================
print("\n== 1. ФОТО МЕНЮ → УСЛУГИ ==")
menu = upload(H, "меню зала.jpg", JPEG, "image/jpeg")
res = (menu.get("result") or {})
check("материал принят", menu["id"] > 0, menu)
check("картинку без распознавания VELOR честно не понимает",
      res.get("level") == "LOW", res.get("level"))
check("тип угадан только по имени файла, и это видно по уверенности",
      (res.get("confidence") or 1) <= 0.5, res.get("confidence"))
check("сам ничего не применил", not applied_of(menu), applied_of(menu))

r = c.post(f"/api/inbox/{menu['id']}/classify", headers=H,
           json={"type": "SERVICES_OR_PRODUCTS"})
check("владелец уточнил, что это меню", r.status_code == 200, r.text[:120])
menu2 = r.json()
check("после уточнения предложено добавить в услуги",
      "add_services" in {a["action"] for a in menu2["result"]["suggested_actions"]},
      menu2["result"]["suggested_actions"])

r = confirm(H, menu["id"], "add_services",
            {"title": "Букет «Весна»", "body": "3 500 ₽, сборка 20 минут"})
check("услуга создана", r.status_code == 200 and r.json()["entity_type"] == "service",
      r.text[:160])
service_id = r.json()["entity_id"]

lst = c.get("/api/memory/list?type=service", headers=H).json()
mine = [i for i in lst["items"] if i["id"] == service_id]
check("услуга видна в памяти бизнеса", len(mine) == 1, lst["items"])
check("и у неё записан источник — то самое фото",
      mine and mine[0]["source"] and mine[0]["source"]["item"]["title"] == "меню зала.jpg",
      mine[0]["source"] if mine else None)

# ============================================================
print("\n== 2. PDF ПРАВИЛ → ПРАВИЛА ==")
pdf = make_pdf(["РЕГЛАМЕНТ РАБОТЫ", "Правила работы с клиентами.",
                "Предоплата 50 процентов.", "Порядок оформления заказа.",
                "Условия работы курьера."])
rules_item = upload(H, "правила.pdf", pdf, "application/pdf")
rres = rules_item.get("result") or {}
check("PDF прочитан и разобран", rres.get("type") == "BUSINESS_RULES", rres.get("type"))
check("уверенность не выдумана (не 100% без модели)",
      0 < (rres.get("confidence") or 0) <= 0.9, rres.get("confidence"))

if "add_rules" in applied_of(rules_item):
    rule_id = [a for a in rres["applied"] if a["action"] == "add_rules"][0]["entity_id"]
    check("правило записано самим VELOR (безопасное действие)", bool(rule_id), rule_id)
else:
    check("добавить в правила предложено", "add_rules" in actions_of(rules_item),
          actions_of(rules_item))
    r = confirm(H, rules_item["id"], "add_rules",
                {"title": "Предоплата 50%", "body": "Из регламента"})
    check("правило создано", r.status_code == 200, r.text[:160])
    rule_id = r.json()["entity_id"]

rl = c.get("/api/memory/list?type=rule", headers=H).json()
rrow = [i for i in rl["items"] if i["id"] == rule_id]
check("правило видно в памяти", len(rrow) == 1, rl["items"])
check("источник правила — сам PDF",
      rrow and rrow[0]["source"] and rrow[0]["source"]["item"]["title"] == "правила.pdf",
      rrow[0]["source"] if rrow else None)

# ============================================================
print("\n== 3. ТЕКСТ ВЛАДЕЛЬЦА → ЦЕЛЬ ==")
goal_note = note(H, "Хочу выйти на 500 тысяч выручки в месяц к концу года")
gres = goal_note.get("result") or {}
check("VELOR понял, что это цель, а не трата",
      gres.get("type") == "GOAL_STATEMENT", gres.get("type"))
check("сумму взял как цель, а не как расход",
      gres.get("extracted_data", {}).get("target") == 500000,
      gres.get("extracted_data"))
check("показатель угадан по словам",
      gres.get("extracted_data", {}).get("metric") == "income",
      gres.get("extracted_data"))
check("в финансы ничего не предложено",
      not ({"create_expense", "create_income"} & actions_of(goal_note)),
      actions_of(goal_note))
check("предложено поставить цель", "add_goal" in actions_of(goal_note),
      actions_of(goal_note))

r = confirm(H, goal_note["id"], "add_goal")
check("цель поставлена", r.status_code == 200 and r.json()["entity_type"] == "goal",
      r.text[:160])
goal_id = r.json()["entity_id"]
check("деньги не тронуты", database.finance_summary(bid)["expense"] == 0,
      database.finance_summary(bid))
gl = c.get("/api/memory/list?type=goal", headers=H).json()
grow = [i for i in gl["items"] if i["id"] == goal_id]
check("цель видна в памяти с источником-заметкой",
      grow and grow[0]["source"] and grow[0]["source"]["item"], grow)

# ============================================================
print("\n== 4. ДОКУМЕНТ СОТРУДНИКА → СВЕДЕНИЯ О СОТРУДНИКЕ ==")
EMP = ("ТРУДОВОЙ ДОГОВОР\n"
       "Сотрудник: Петрова Анна Ивановна\n"
       "Должность: старший флорист\n"
       "Оклад: 60000\n"
       "Телефон: +7 999 111-22-33\n"
       "Испытательный срок 3 месяца. Принят на работу с 1 марта.")
emp_item = upload(H, "договор Петрова.txt", EMP)
eres = emp_item.get("result") or {}
check("VELOR понял, что это про сотрудника",
      eres.get("type") == "EMPLOYEE_INFORMATION", eres.get("type"))
ex = eres.get("extracted_data") or {}
check("имя вытащено", ex.get("person") == "Петрова Анна Ивановна", ex)
check("должность вытащена", ex.get("role") == "старший флорист", ex)
check("телефон вытащен", "999" in str(ex.get("phone") or ""), ex)
check("предложено добавить сотрудника", "add_employee" in actions_of(emp_item),
      actions_of(emp_item))
check("но сам VELOR его не завёл — это личные данные",
      "add_employee" not in applied_of(emp_item), applied_of(emp_item))

r = confirm(H, emp_item["id"], "add_employee")
check("сотрудник записан", r.status_code == 200 and r.json()["entity_type"] == "employee",
      r.text[:200])
emp_id = r.json()["entity_id"]

fact = database.get_fact(emp_id, bid)
check("лежит в общей памяти фактов, а не в новой таблице",
      fact and fact["kind"] == "employee", fact)
check("структурные поля сохранены отдельно",
      fact and fact["data"].get("role") == "старший флорист", fact)
biz = database.get_business(bid)
check("и ядро видит сотрудника среди знаний",
      "Петрова Анна Ивановна" in (biz.get("facts") or ""), (biz.get("facts") or "")[:200])
check("вместе с должностью",
      "старший флорист" in (biz.get("facts") or ""), (biz.get("facts") or "")[:200])

# ============================================================
print("\n== ЦЕПОЧКА: ИСТОЧНИК → ЧТО ПОНЯЛ → ЗАПИСЬ ==")
r = c.get(f"/api/memory/entity/employee/{emp_id}", headers=H)
check("карточка знания открывается", r.status_code == 200, r.text[:160])
card = r.json()
check("значения полей на месте", card["values"]["title"] == "Петрова Анна Ивановна", card["values"])
check("описание формы приехало вместе с записью",
      bool(card["schema"]["fields"]) and card["schema"]["can_edit"], card["schema"])
src = card["source"]
check("источник — материал во входящих", src and src["item"]["id"] == emp_item["id"], src)
check("видно дату", bool(src.get("created_at")), src)
check("видно уверенность", src.get("confidence") is not None, src)
check("видно автора", src.get("actor_ru") == "владелец", src)
check("видно, что именно VELOR извлёк",
      bool((src.get("result") or {}).get("extracted")) or
      bool((src.get("decision") or {}).get("original")), src)
check("подпись записи собрана", bool(card["label"]["title"]), card["label"])

r = c.get(f"/api/inbox/{emp_item['id']}/file", headers=H)
check("оригинал документа по-прежнему открывается", r.status_code == 200, r.status_code)
check("и это тот самый файл", "Петрова" in r.content.decode("utf-8"), r.content[:60])

# ============================================================
print("\n== ПРАВКА И ИСТОРИЯ ==")
r = c.post(f"/api/memory/entity/employee/{emp_id}", headers=H,
           json={"data": {"role": "флорист-декоратор"}})
check("правка принята", r.status_code == 200, r.text[:200])
d = r.json()
check("изменение зафиксировано",
      d["changes"].get("role", {}).get("was") == "старший флорист" and
      d["changes"]["role"]["now"] == "флорист-декоратор", d["changes"])
check("остальные поля не стёрлись", d["values"]["title"] == "Петрова Анна Ивановна",
      d["values"])
check("в истории два события", len(d["history"]) == 2,
      [h["event"] for h in d["history"]])
check("первое — как записали", d["history"][0]["event"] == "created", d["history"][0])
check("второе — что поправили", d["history"][1]["event"] == "edited", d["history"][1])
check("и кто поправил", d["history"][1]["actor_ru"] == "владелец", d["history"][1])
check("исходное предложение ИИ не переписано",
      d["history"][0].get("decision", {}).get("corrected", {}).get("role")
      == "старший флорист", d["history"][0].get("decision"))

r = c.post(f"/api/memory/entity/employee/{emp_id}", headers=H,
           json={"data": {"title": ""}})
check("без обязательного поля не сохранить", r.status_code == 400, r.status_code)
r = c.post(f"/api/memory/entity/employee/{emp_id}", headers=H,
           json={"data": {"title": "Петрова Анна Ивановна", "лишнее": "х"}})
check("лишнее поле отброшено, а не записано",
      r.status_code == 200 and "лишнее" not in r.json()["values"], r.text[:160])

r = c.post(f"/api/memory/entity/expense/{service_id}", headers=H, json={"data": {"amount": 1}})
check("нельзя править запись под чужим видом", r.status_code == 400, r.status_code)
r = c.get("/api/memory/entity/employee/999999", headers=H)
check("несуществующая запись — 404", r.status_code == 404, r.status_code)
r = c.get("/api/memory/entity/выдумка/1", headers=H)
check("несуществующий вид — 404", r.status_code == 404, r.status_code)

print("\n-- правка суммы меняет и сами финансы --")
r = confirm(H, note(H, "Заплатил Иванову 12 000 рублей за доставку")["id"], "create_expense")
exp_id = r.json()["entity_id"]
c.post(f"/api/memory/entity/expense/{exp_id}", headers=H, json={"data": {"amount": "9000"}})
check("расход исправлен в самих финансах",
      database.get_finance_entry(exp_id, bid)["amount"] == 9000,
      database.get_finance_entry(exp_id, bid))

# ============================================================
print("\n== ОБРАТНЫЙ ВЗГЛЯД: ЧТО ВЫРОСЛО ИЗ МАТЕРИАЛА ==")
r = c.get(f"/api/memory/source/{emp_item['id']}", headers=H)
check("материал показывает, что из него получилось", r.status_code == 200, r.text[:120])
grown = r.json()["entities"]
check("именно эта запись", any(g["id"] == emp_id and g["entity"] == "employee" for g in grown),
      grown)
check("с человеческим названием вида",
      any(g["entity_title"] == "Сотрудник" for g in grown), grown)
check("оригинал материала рядом", r.json()["item"]["id"] == emp_item["id"], r.json()["item"])

# ============================================================
print("\n== КАРТА ПАМЯТИ ==")
m = c.get("/api/memory/map", headers=H).json()
check("разделы собраны", len(m["groups"]) >= 5, [g["key"] for g in m["groups"]])
kinds = {t["type"]: t for g in m["groups"] for t in g["types"]}
check("все виды знаний на карте",
      {"company", "service", "product", "employee", "supplier", "rule",
       "client", "order", "expense", "income", "goal", "document"} <= set(kinds),
      sorted(kinds))
check("сотрудник посчитан", kinds["employee"]["count"] == 1, kinds["employee"])
check("и он подтверждён документом", kinds["employee"]["documented"] == 1, kinds["employee"])
check("всего знаний больше нуля", m["total"] > 0, m["total"])
check("часть подтверждена документами",
      0 < m["documented"] <= m["total"], (m["documented"], m["total"]))
check("ручные записи не выдаются за подтверждённые документом",
      m["documented"] <= m["linked"], (m["documented"], m["linked"]))
check("последние события памяти показаны", len(m["recent"]) > 0, m["recent"])

# ============================================================
print("\n== РУЧНЫЕ ЗАПИСИ ТОЖЕ ИМЕЮТ ИСТОЧНИК ==")
r = c.post("/api/facts", headers=H, json={"kind": "supplier", "title": "ООО «Флора»",
                                          "body": "Цветы оптом"})
check("поставщик заведён вручную", r.status_code == 200, r.text[:120])
sup_id = r.json()["id"]
sl = c.get("/api/memory/list?type=supplier", headers=H).json()
srow = [i for i in sl["items"] if i["id"] == sup_id]
check("и в памяти он подписан как внесённый вручную",
      srow and srow[0]["source"] and srow[0]["source"]["source_kind"] == "manual", srow)

r = c.post("/api/finance", headers=H, json={"kind": "expense", "category": "аренда",
                                            "amount": 30000})
check("ручной расход записан", r.status_code == 200, r.text[:120])
fl = c.get("/api/memory/list?type=expense", headers=H).json()
check("и у него тоже отмечен источник",
      any(i["source"] and i["source"]["source_kind"] == "manual" for i in fl["items"]),
      fl["items"][:2])

r = c.post("/api/documents/upload", headers=H,
           files=[("file", ("инструкция.txt", "Как принимать заказы. Шаг первый.".encode("utf-8"),
                            "text/plain"))])
check("документ загружен", r.json().get("ok"), r.text[:160])
dl = c.get("/api/memory/list?type=document", headers=H).json()
check("документ виден в памяти", dl["total"] >= 1, dl)
check("с указанием, что его загрузили вручную",
      any(i["source"] and i["source"]["source_kind"] == "manual" for i in dl["items"]),
      dl["items"][:2])
doc_id = dl["items"][0]["id"]
r = c.post(f"/api/memory/entity/document/{doc_id}", headers=H,
           json={"data": {"filename": "инструкция по заказам.txt"}})
check("документ можно переименовать", r.status_code == 200, r.text[:160])
check("название изменилось",
      database.get_document(doc_id, bid)["filename"] == "инструкция по заказам.txt",
      database.get_document(doc_id, bid))

# ============================================================
print("\n== ПОРЯДОК И ГРАНИЦЫ ==")
check("страницы вида ограничены разумным пределом",
      c.get("/api/memory/list?type=service&limit=9999", headers=H).status_code == 200)
check("пустой вид отдаёт пустой список, а не ошибку",
      c.get("/api/memory/list?type=product", headers=H).json()["items"] == [])
check("неизвестный вид — 404",
      c.get("/api/memory/list?type=никакой", headers=H).status_code == 404)
check("без вида — тоже 404", c.get("/api/memory/list", headers=H).status_code == 404)

print("\n-- чужой бизнес --")
bid2, H2 = reg("mem_other")
check("у соседа память пуста", c.get("/api/memory/map", headers=H2).json()["total"] == 0)
check("чужую запись не открыть",
      c.get(f"/api/memory/entity/employee/{emp_id}", headers=H2).status_code == 404)
check("чужую запись не изменить",
      c.post(f"/api/memory/entity/employee/{emp_id}", headers=H2,
             json={"data": {"title": "Взлом"}}).status_code == 400)
check("имя не изменилось",
      database.get_fact(emp_id, bid)["title"] == "Петрова Анна Ивановна",
      database.get_fact(emp_id, bid))
check("чужой материал не показывает свою цепочку",
      c.get(f"/api/memory/source/{emp_item['id']}", headers=H2).status_code == 404)

print("\n-- ядро опирается на память как на источник правды --")
import context_engine
block = context_engine._memory_source_block(bid)
check("в контекст ушло, откуда взяты знания", "договор Петрова.txt" in block, block[:200])
check("и что именно оттуда взяли", "сотрудник" in block.lower(), block[:200])

print("\n== READ-ONLY ПОСЛЕ ТРИАЛА ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("править память нельзя",
      c.post(f"/api/memory/entity/employee/{emp_id}", headers=H,
             json={"data": {"title": "Иванов"}}).status_code == 402)
check("но смотреть можно",
      c.get(f"/api/memory/entity/employee/{emp_id}", headers=H).status_code == 200)
check("и карта открывается", c.get("/api/memory/map", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
