# -*- coding: utf-8 -*-
"""
Слой понимания: что VELOR понял про присланное.

Проверяется не «модель угадала», а то, ради чего слой существует:
  • тип определяется и без ИИ — правилами, которые не выдумывают;
  • выдуманная моделью сумма не попадает в финансы;
  • деньги не двигаются сами ни при какой уверенности;
  • низкая уверенность останавливает автоматику, а не ускоряет её;
  • оригинал переживает любой исход разбора.
"""
import os, sys, tempfile, pathlib, json

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
os.environ["DISABLE_SYNC_WORKER"] = "1"     # разбор считается на месте
sys.stdout.reconfigure(encoding="utf-8")
# Корень проекта вычисляется от самого файла: тесты должны запускаться
# из любой папки и на любой машине, а не только там, где их писали.
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, storage, understanding, ai

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


def note(H, text):
    return c.post("/api/inbox", headers=H, json={"text": text}).json()["item"]


def upload(H, name, data, mime="application/octet-stream"):
    r = c.post("/api/inbox/upload", headers=H, files=[("files", (name, data, mime))])
    d = r.json()
    return (d["saved"][0] if d["saved"] else None), d["failed"]


def txt(sample):
    return sample.encode("utf-8")


bid, H = reg("und_main")

# ============================================================
print("\n== 1. ЧЕК ==")
CHECK = """КАССОВЫЙ ЧЕК
ООО «Ромашка»  ИНН 7701234567
Доставка курьером          1 850 ₽
Итого к оплате             1 850 ₽
НДС 20%                      308 ₽
Кассир: Петрова А.
"""
it, _ = upload(H, "чек доставка.txt", txt(CHECK), "text/plain")
res = c.get(f"/api/inbox/{it['id']}/result", headers=H).json()["result"]
check("тип — документ о расходе", res["type"] == "EXPENSE_DOCUMENT", res["type"])
check("сумма взята итоговая, а не НДС", res["extracted_data"].get("amount") == 1850,
      res["extracted_data"])
check("валюта рубли", res["extracted_data"].get("currency") == "RUB", res["extracted_data"])
check("категория — доставка", res["extracted_data"].get("category") == "доставка",
      res["extracted_data"])
check("направление — расход", res["extracted_data"].get("direction") == "expense",
      res["extracted_data"])
acts = [a["action"] for a in res["suggested_actions"]]
check("предложено записать расход", "create_expense" in acts, acts)
check("запись расхода помечена небезопасной",
      all(not a["safe"] for a in res["suggested_actions"] if a["action"] == "create_expense"),
      res["suggested_actions"])
check("расход НЕ записан сам", res["applied"] == [], res["applied"])
check("уровень уверенности назван", res["level"] in ("HIGH", "MEDIUM", "LOW"), res["level"])
check("есть человеческая подпись типа", res["type_ru"] == "Документ о расходе", res["type_ru"])
check("разобрано правилами, ИИ не подключён", res["engine"] == "rules", res["engine"])

print("\n-- подтверждение человеком записывает расход --")
before = database.finance_summary(bid)["expense"]
r = c.post(f"/api/inbox/{it['id']}/action", headers=H, json={"action": "create_expense"})
check("действие выполнено", r.status_code == 200, r.text[:120])
after = database.finance_summary(bid)["expense"]
check("расход появился в финансах", after - before == 1850, (before, after))
check("материал стал разобранным",
      database.get_inbox_item(it["id"], bid)["status"] == "PROCESSED",
      database.get_inbox_item(it["id"], bid)["status"])
check("повторное нажатие отклонено",
      c.post(f"/api/inbox/{it['id']}/action", headers=H,
             json={"action": "create_expense"}).status_code == 409)
check("непредложенное действие отклонено",
      c.post(f"/api/inbox/{it['id']}/action", headers=H,
             json={"action": "create_income"}).status_code == 400)
check("выдуманное действие отклонено",
      c.post(f"/api/inbox/{it['id']}/action", headers=H,
             json={"action": "выдать_себе_премию"}).status_code == 400)
check("оригинал на месте после разбора",
      c.get(f"/api/inbox/{it['id']}/file", headers=H).content == txt(CHECK))

# ============================================================
print("\n== 2. БАНКОВСКАЯ ОПЕРАЦИЯ ==")
BANK = """Выписка по счёту 40802810000000012345
Дата операции: 12.08.2026
Назначение платежа: оплата аренды помещения
Списание: 45 000 ₽
Остаток по счёту: 312 400 ₽
БИК 044525225
"""
it2, _ = upload(H, "выписка.txt", txt(BANK), "text/plain")
res2 = c.get(f"/api/inbox/{it2['id']}/result", headers=H).json()["result"]
check("тип — банковская операция", res2["type"] == "BANK_TRANSACTION", res2["type"])
check("категория — аренда", res2["extracted_data"].get("category") == "аренда",
      res2["extracted_data"])
check("взята бо́льшая сумма из двух",
      res2["extracted_data"].get("amount") in (45000, 312400), res2["extracted_data"])
check("отмечено, что сумм несколько",
      res2["extracted_data"].get("amounts_found", 0) >= 2, res2["extracted_data"])
check("деньги сами не записаны", res2["applied"] == [], res2["applied"])

# ============================================================
print("\n== 3. PDF С ПРАВИЛАМИ ==")
try:
    from pypdf import PdfWriter
    import io as _io
    w = PdfWriter(); w.add_blank_page(width=200, height=200)
    buf = _io.BytesIO(); w.write(buf)
    pdf_bytes = buf.getvalue()
    have_pdf = True
except Exception as e:
    pdf_bytes, have_pdf = b"%PDF-1.4\n", False
    print("   (pypdf недоступен: %s)" % e)

it3, _ = upload(H, "регламент работы с клиентами.pdf", pdf_bytes, "application/pdf")
res3 = c.get(f"/api/inbox/{it3['id']}/result", headers=H).json()["result"]
check("тип по имени файла — правила работы", res3["type"] == "BUSINESS_RULES", res3["type"])
check("уверенность не высокая: текст не прочитан",
      res3["level"] in ("MEDIUM", "LOW"), (res3["level"], res3["confidence"]))
check("правила не добавлены в память сами",
      not any(a["action"] == "add_rules" for a in res3["applied"]), res3["applied"])
check("предложено добавить в правила",
      "add_rules" in [a["action"] for a in res3["suggested_actions"]], res3["suggested_actions"])
check("PDF-оригинал цел", c.get(f"/api/inbox/{it3['id']}/file", headers=H).content == pdf_bytes)

print("\n-- прайс и меню --")
it3b, _ = upload(H, "прайс-лист.txt", txt("ПРАЙС-ЛИСТ\nСтрижка 1500 ₽\nОкрашивание 4500 ₽"), "text/plain")
r3b = c.get(f"/api/inbox/{it3b['id']}/result", headers=H).json()["result"]
check("прайс распознан", r3b["type"] == "PRICE_LIST", r3b["type"])
it3c, _ = upload(H, "меню.txt", txt("МЕНЮ\nКапучино 250\nЛатте 280\nАссортимент десертов"), "text/plain")
r3c = c.get(f"/api/inbox/{it3c['id']}/result", headers=H).json()["result"]
check("меню — это услуги/товары", r3c["type"] == "SERVICES_OR_PRODUCTS", r3c["type"])
it3d, _ = upload(H, "договор.txt",
                 txt("ДОГОВОР №14\nНастоящий договор заключён между Заказчиком и Исполнителем.\nРеквизиты сторон"),
                 "text/plain")
r3d = c.get(f"/api/inbox/{it3d['id']}/result", headers=H).json()["result"]
check("договор распознан", r3d["type"] == "CONTRACT", r3d["type"])

# ============================================================
print("\n== 4. ОБЫЧНЫЙ ТЕКСТ ==")
n = note(H, "Сегодня заплатил Иванову 70 тысяч зарплаты")
res4 = c.get(f"/api/inbox/{n['id']}/result", headers=H).json()["result"]
check("тип — движение денег", res4["type"] == "FINANCIAL_TRANSACTION", res4["type"])
check("«70 тысяч» превратились в 70000", res4["extracted_data"].get("amount") == 70000,
      res4["extracted_data"])
check("категория — зарплата", res4["extracted_data"].get("category") == "зарплата",
      res4["extracted_data"])
check("направление — расход", res4["extracted_data"].get("direction") == "expense",
      res4["extracted_data"])
check("получатель распознан", res4["extracted_data"].get("counterparty") == "Иванову",
      res4["extracted_data"])
check("деньги не списаны сами", res4["applied"] == [], res4["applied"])

n2 = note(H, "Получили выручку 128 500 ₽ за выходные")
res4b = c.get(f"/api/inbox/{n2['id']}/result", headers=H).json()["result"]
check("доход отличается от расхода", res4b["extracted_data"].get("direction") == "income",
      res4b["extracted_data"])
check("для дохода предложен доход, а не расход",
      "create_income" in [a["action"] for a in res4b["suggested_actions"]],
      res4b["suggested_actions"])
check("сумма с пробелами разобрана", res4b["extracted_data"].get("amount") == 128500,
      res4b["extracted_data"])

n3 = note(H, "Надо не забыть поздравить Марину с днём рождения")
res4c = c.get(f"/api/inbox/{n3['id']}/result", headers=H).json()["result"]
check("заметка без денег не стала финансовой",
      res4c["type"] != "FINANCIAL_TRANSACTION", res4c["type"])

# ============================================================
print("\n== 5. НЕИЗВЕСТНЫЙ ДОКУМЕНТ ==")
it5, _ = upload(H, "qwerty.txt", txt("зфывафыв\nqwe rty\n123 456"), "text/plain")
res5 = c.get(f"/api/inbox/{it5['id']}/result", headers=H).json()["result"]
check("честно UNKNOWN", res5["type"] == "UNKNOWN", res5["type"])
check("уверенность низкая", res5["level"] == "LOW", (res5["level"], res5["confidence"]))
check("предложено уточнить у владельца",
      [a["action"] for a in res5["suggested_actions"]] == ["ask_user"], res5["suggested_actions"])
check("ничего не применено само", res5["applied"] == [], res5["applied"])
check("материал ждёт человека",
      database.get_inbox_item(it5["id"], bid)["status"] == "NEEDS_REVIEW",
      database.get_inbox_item(it5["id"], bid)["status"])
check("подпись «нужно уточнение»", res5["needs"] == "нужно уточнение", res5["needs"])

print("\n-- голосовое --")
it5b, _ = upload(H, "голосовое.ogg", b"OggS\x00\x02" + b"\x00" * 200, "audio/ogg")
check("голосовое принято", it5b is not None, it5b)
res5b = c.get(f"/api/inbox/{it5b['id']}/result", headers=H).json()["result"]
check("тип — голосовое сообщение", res5b["type"] == "VOICE_INFORMATION", res5b["type"])
check("расшифровки нет — предложено уточнить",
      "ask_user" in [a["action"] for a in res5b["suggested_actions"]], res5b["suggested_actions"])

# ============================================================
print("\n== 6. ПОВРЕЖДЁННЫЙ ФАЙЛ ==")
it6, _ = upload(H, "битый счёт.pdf", b"%PDF-1.4\n" + os.urandom(400), "application/pdf")
res6 = c.get(f"/api/inbox/{it6['id']}/result", headers=H).json()["result"]
check("разбор не упал, а вернул результат", res6 is not None, res6)
check("уверенность не высокая", res6["level"] != "HIGH", (res6["level"], res6["confidence"]))
check("оригинал битого файла сохранён",
      c.get(f"/api/inbox/{it6['id']}/file", headers=H).status_code == 200)

print("\n-- оригинал пропал из хранилища --")
it6b, _ = upload(H, "пропажа.txt", txt("ПРАЙС-ЛИСТ\nУслуга 100 ₽"), "text/plain")
storage.delete(bid, database.get_inbox_item(it6b["id"], bid)["storage_key"])
r = c.post(f"/api/inbox/{it6b['id']}/process", headers=H)
check("повторный разбор отвечает, а не падает", r.status_code == 200, r.status_code)
res6b = r.json()["result"]
check("тип UNKNOWN — читать было нечего", res6b["type"] == "UNKNOWN", res6b["type"])
check("причина записана", "хранилищ" in (res6b["error"] or ""), res6b["error"])
check("материал помечен сбоем",
      database.get_inbox_item(it6b["id"], bid)["status"] == "FAILED",
      database.get_inbox_item(it6b["id"], bid)["status"])

# ============================================================
print("\n== 7. НИЗКАЯ УВЕРЕННОСТЬ ОСТАНАВЛИВАЕТ АВТОМАТИКУ ==")
check("порог высокой уверенности задан", understanding.HIGH_AT > understanding.MEDIUM_AT,
      (understanding.HIGH_AT, understanding.MEDIUM_AT))
check("0.95 — высокая", understanding.level_of(0.95) == "HIGH")
check("0.7 — средняя", understanding.level_of(0.70) == "MEDIUM")
check("0.3 — низкая", understanding.level_of(0.30) == "LOW")
check("деньги помечены небезопасными",
      not understanding.ACTIONS["create_expense"]["safe"]
      and not understanding.ACTIONS["create_income"]["safe"])
try:
    understanding.apply_action(bid, it["id"], "create_expense", auto=True)
    check("VELOR не может записать расход сам", False, "выполнил")
except understanding.ActionError as e:
    check("VELOR не может записать расход сам", "сам не выполняет" in str(e), str(e))

print("\n-- модель выдумала сумму --")
real = ai.ai_available
fake = {"type": "EXPENSE_DOCUMENT", "confidence": 0.97,
        "summary": "Расход на доставку",
        "extracted_data": {"amount": 999999, "currency": "RUB", "category": "доставка"},
        "provider": "test"}
ai.ai_available = lambda: True
ai.understand_material = lambda *a, **k: dict(fake)
it7, _ = upload(H, "чек2.txt", txt("Кассовый чек\nИтого к оплате 2 400 ₽"), "text/plain")
res7 = c.get(f"/api/inbox/{it7['id']}/result", headers=H).json()["result"]
check("выдуманная сумма заменена на настоящую",
      res7["extracted_data"].get("amount") == 2400, res7["extracted_data"])
check("уверенность снижена после подлога",
      res7["confidence"] <= 0.55, res7["confidence"])
check("уровень перестал быть высоким", res7["level"] != "HIGH", res7["level"])
check("небезопасное действие всё равно не выполнено само",
      not any(not understanding.ACTIONS[x["action"]]["safe"] for x in res7["applied"]),
      res7["applied"])
check("сохранение в базу знаний не делается само на каждый чек",
      not any(x["action"] == "save_document" for x in res7["applied"]), res7["applied"])

print("\n-- модель назвала сумму, которой в тексте нет вовсе --")
ai.understand_material = lambda *a, **k: {
    "type": "FINANCIAL_TRANSACTION", "confidence": 0.99, "summary": "Оплата",
    "extracted_data": {"amount": 55000, "currency": "RUB"}, "provider": "test"}
n7 = note(H, "Заплатил за парковку мелочью")
res7b = c.get(f"/api/inbox/{n7['id']}/result", headers=H).json()["result"]
check("сумма выброшена — её нет в тексте",
      "amount" not in res7b["extracted_data"], res7b["extracted_data"])
check("запись в финансы больше не предлагается",
      "create_expense" not in [a["action"] for a in res7b["suggested_actions"]],
      res7b["suggested_actions"])
r = c.post(f"/api/inbox/{n7['id']}/action", headers=H, json={"action": "create_expense"})
check("и напрямую записать нельзя", r.status_code == 400, r.status_code)

print("\n-- модель и правила разошлись --")
ai.understand_material = lambda *a, **k: {
    "type": "CONTRACT", "confidence": 0.95, "summary": "Договор",
    "extracted_data": {}, "provider": "test"}
it7c, _ = upload(H, "чек3.txt", txt("Кассовый чек\nФискальный признак\nИтого 500 ₽"), "text/plain")
res7c = c.get(f"/api/inbox/{it7c['id']}/result", headers=H).json()["result"]
check("при разногласии уверенность обрезана", res7c["confidence"] <= 0.70, res7c["confidence"])

print("\n-- модель согласилась с правилами --")
ai.understand_material = lambda *a, **k: {
    "type": "PRICE_LIST", "confidence": 0.9, "summary": "Прайс на услуги",
    "extracted_data": {}, "provider": "test"}
it7d, _ = upload(H, "прайс2.txt", txt("ПРАЙС-ЛИСТ\nСтрижка 1500 ₽\nЦены на услуги"), "text/plain")
res7d = c.get(f"/api/inbox/{it7d['id']}/result", headers=H).json()["result"]
check("согласие поднимает уверенность", res7d["level"] == "HIGH",
      (res7d["level"], res7d["confidence"]))
check("безопасное действие выполнено само",
      any(a["auto"] and a["action"] == "add_price_list" for a in res7d["applied"]),
      res7d["applied"])
# Раньше прайс сохранялся ОДНОЙ записью с именем файла — «прайс2.txt» в памяти
# бизнеса не отвечает ни на один вопрос. Теперь список разбирается построчно, и
# в памяти появляется сама позиция с ценой.
_prods = database.list_facts(bid, "product")
check("в памяти бизнеса появилась позиция прайса, а не имя файла",
      any(f["title"] == "Стрижка" for f in _prods), _prods[:2])
check("у позиции сохранена цена",
      any(f["title"] == "Стрижка" and "1 500" in (f["body"] or "") for f in _prods),
      _prods[:2])
check("позиция помечена неподтверждённой — её никто не читал",
      all(not f["verified"] for f in _prods if f["title"] == "Стрижка"), _prods[:2])
check("материал считается разобранным",
      database.get_inbox_item(it7d["id"], bid)["status"] == "PROCESSED",
      database.get_inbox_item(it7d["id"], bid)["status"])
check("сводка взята у модели", res7d["summary"] == "Прайс на услуги", res7d["summary"])
check("движок помечен как llm", res7d["engine"] == "llm", res7d["engine"])

ai.ai_available = real

# ============================================================
print("\n== история разборов и доступ ==")
r = c.post(f"/api/inbox/{it5['id']}/process", headers=H)
check("переразбор прошёл", r.status_code == 200, r.status_code)
with database._connect() as conn:
    n_res = conn.execute("SELECT COUNT(*) AS n FROM inbox_results WHERE item_id = ?",
                         (it5["id"],)).fetchone()["n"]
check("прошлый разбор не затёрт", n_res >= 2, n_res)

other_bid, OH = reg("und_other")
check("чужой разбор не отдаётся",
      c.get(f"/api/inbox/{it['id']}/result", headers=OH).status_code == 404)
check("чужой материал не переразобрать",
      c.post(f"/api/inbox/{it['id']}/process", headers=OH).status_code == 404)
check("чужое действие не выполнить",
      c.post(f"/api/inbox/{it['id']}/action", headers=OH,
             json={"action": "create_expense"}).status_code == 409)
check("без токена разбор недоступен",
      c.get(f"/api/inbox/{it['id']}/result").status_code == 401)

print("\n== разбор виден прямо в ленте ==")
d = c.get("/api/inbox?limit=50", headers=H).json()
withres = [i for i in d["items"] if i.get("result")]
check("у материалов ленты есть разбор", len(withres) >= 5, len(withres))
check("в ленте видно тип и уверенность",
      all(r["result"].get("type") and r["result"].get("level") for r in withres), withres[:1])

print("\n== read-only после триала ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("переразбор запрещён",
      c.post(f"/api/inbox/{it5['id']}/process", headers=H).status_code == 402)
check("действие запрещено",
      c.post(f"/api/inbox/{it3b['id']}/action", headers=H,
             json={"action": "add_price_list"}).status_code == 402)
check("но разбор всё ещё видно",
      c.get(f"/api/inbox/{it['id']}/result", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
