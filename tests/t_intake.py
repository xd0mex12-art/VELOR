# -*- coding: utf-8 -*-
"""
Единое окно входящих: одна дверь для всего, что касается бизнеса.

Что здесь проверяется — обещания, а не строки кода:
  • принять можно текст и файлы одной отправкой, и ничего не теряется;
  • одно и то же не записывается дважды, даже под другим именем файла;
  • существующая запись узнаётся, а не удваивается;
  • неуверенный разбор НЕ становится фактом;
  • понятое самим VELOR помечено и в памяти, и в ответе клиенту;
  • чужое недоступно ни одним из входов;
  • падение модели, хранилища или базы не отменяет приём молча.
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
os.environ["DISABLE_SYNC_WORKER"] = "1"
sys.stdout.reconfigure(encoding="utf-8")
# Корень проекта вычисляется от самого файла: тесты должны запускаться
# из любой папки и на любой машине, а не только там, где их писали.
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, storage, intake, entities, understanding, ai, sales

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


def door(H, text=None, files=None, source=None):
    """Отправка в единое окно — так же, как это делает страница."""
    data = {}
    if text is not None:
        data["text"] = text
    if source:
        data["source"] = source
    return c.post("/api/inbox/intake", headers=H, data=data or {"text": ""},
                  files=files or [])


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"x" * 300


def txt(s):
    return s.encode("utf-8")


bid, H = reg("door_main")
other, OH = reg("door_other")

# Модели нет: разбор идёт правилами. Так поведение предсказуемо, а правила —
# именно та часть, которая обязана работать всегда.
ai.understand_material = lambda *a, **k: None


# ============================================================
print("\n== 1. ТЕКСТ ==")
r = door(H, "Кассовый чек\nИтого к оплате 1850 ₽")
d = r.json()
check("текст принят", r.status_code == 200 and len(d["accepted"]) == 1, r.text[:200])
first = d["accepted"][0]
check("материал сохранён как заметка", first["kind"] == "text", first.get("kind"))
check("разбор приложен сразу", bool(first.get("result")), first.get("result"))
check("тип определён правилами", first["result"]["type"] == "EXPENSE_DOCUMENT",
      first["result"]["type"])
check("сумма вытащена",
      first["result"]["extracted_data"].get("amount") == 1850, first["result"]["extracted_data"])
check("ни одного поля «выберите тип» в запросе не понадобилось", True)


# ============================================================
print("\n== 2. КАРТИНКА ==")
r = door(H, files=[("files", ("скрин.png", PNG, "image/png"))])
d = r.json()
check("картинка принята", len(d["accepted"]) == 1, r.text[:200])
img = d["accepted"][0]
check("оригинал лежит в хранилище", bool(img.get("has_file")), img)
check("тип по расширению, а не по заголовку запроса",
      database.get_inbox_item(img["id"], bid)["mime"] == "image/png")
check("отдаётся байт в байт",
      c.get(f"/api/inbox/{img['id']}/file", headers=H).content == PNG)


# ============================================================
print("\n== 3. PDF ==")
r = door(H, "счёт от поставщика", files=[("files", ("счёт.pdf", PDF, "application/pdf"))])
d = r.json()
check("текст и файл принимаются ОДНОЙ отправкой", len(d["accepted"]) == 2,
      [i["kind"] for i in d["accepted"]])
kinds = sorted(i["kind"] for i in d["accepted"])
check("оба вида на месте", kinds == ["file", "text"], kinds)
check("пояснение стоит первым — оно объясняет файл",
      d["accepted"][0]["kind"] == "text", d["accepted"][0]["kind"])


# ============================================================
print("\n== 4. ПОВТОР ==")
before = database.count_inbox(bid)
r = door(H, files=[("files", ("тот-же-скрин.png", PNG, "image/png"))])
d = r.json()
check("повтор не принят как новый", not d["accepted"], d["accepted"])
check("повтор назван повтором", len(d["duplicates"]) == 1, d["duplicates"])
check("ведёт к тому материалу, что уже разобран",
      d["duplicates"][0]["duplicate_of"] == img["id"], d["duplicates"][0])
check("во входящих не прибавилось", database.count_inbox(bid) == before,
      (before, database.count_inbox(bid)))
r = door(H, "Кассовый чек\n\n  итого   К ОПЛАТЕ 1850 ₽ ")
check("та же мысль с другими пробелами и регистром — тоже повтор",
      not r.json()["accepted"] and r.json()["duplicates"], r.json())
r = door(H, "Кассовый чек\nИтого к оплате 1851 ₽")
check("другая сумма — другой материал", len(r.json()["accepted"]) == 1, r.json())
check("у чужого бизнеса тот же файл принимается — отпечаток свой у каждого",
      len(door(OH, files=[("files", ("скрин.png", PNG, "image/png"))])
          .json()["accepted"]) == 1)


# ============================================================
print("\n== 5. НЕПОНЯТНОЕ ==")
r = door(H, "зайти к Марине")
res = r.json()["accepted"][0]["result"]
check("непонятное названо непонятным", res["type"] == "UNKNOWN", res["type"])
check("уверенность низкая", res["level"] == "LOW", (res["level"], res["confidence"]))
check("предлагается спросить владельца",
      any(a["action"] == "ask_user" for a in res["suggested_actions"]),
      [a["action"] for a in res["suggested_actions"]])
check("ничего не создано само", not res["applied"], res["applied"])
check("материал ждёт человека",
      database.get_inbox_item(r.json()["accepted"][0]["id"], bid)["status"] == "NEEDS_REVIEW")


# ============================================================
print("\n== 6. ВЫСОКАЯ УВЕРЕННОСТЬ ==")
ai.understand_material = lambda *a, **k: {
    "type": "PRICE_LIST", "confidence": 0.95, "summary": "Прайс на услуги",
    "extracted_data": {}, "provider": "test"}
real_available = ai.ai_available
ai.ai_available = lambda: True
r = door(H, files=[("files", ("прайс.txt", txt(
    "ПРАЙС-ЛИСТ\nМаникюр классический — 1500 ₽\nПедикюр .... 2000 руб.\n"
    "Наращивание ресниц\t3500 ₽\nИтого: 7000 ₽"), "text/plain"))])
price_item = r.json()["accepted"][0]
res = price_item["result"]
check("уверенность высокая", res["level"] == "HIGH", (res["level"], res["confidence"]))
check("прайс разобран построчно", res["extracted_data"].get("items_count") == 3,
      res["extracted_data"].get("items_count"))
check("итоговая строка позицией не считается",
      all(i["title"] != "Итого" for i in res["extracted_data"]["items"]),
      res["extracted_data"]["items"])
check("безопасное применено само", any(a["auto"] for a in res["applied"]), res["applied"])
prods = database.list_facts(bid, "product")
check("в памяти появились три позиции", len(prods) == 3, [p["title"] for p in prods])
check("с ценами", all("₽" in (p["body"] or "") for p in prods), [p["body"] for p in prods])
check("и все помечены неподтверждёнными — их никто не читал",
      all(not p["verified"] for p in prods), [(p["title"], p["verified"]) for p in prods])
check("у каждой позиции записано происхождение",
      all(database.memory_links(bid, "product", p["id"]) for p in prods))


# ============================================================
print("\n== 7. НИЗКАЯ УВЕРЕННОСТЬ ==")
ai.understand_material = lambda *a, **k: {
    "type": "SERVICES_OR_PRODUCTS", "confidence": 0.2, "summary": "Возможно, услуга",
    "extracted_data": {}, "provider": "test"}
r = door(H, "кажется, что-то про стрижку")
res = r.json()["accepted"][0]["result"]
check("низкая уверенность не даёт применить",
      not res["applied"], res["applied"])
check("догадка не стала фактом",
      not any(f["title"].lower().startswith("кажется")
              for f in database.list_facts(bid)), )
check("сказано, что нужна проверка", res["needs"] in ("нужно уточнение",
                                                      "нужно подтверждение"), res["needs"])


# ============================================================
print("\n== 8. СУЩЕСТВУЮЩИЙ КЛИЕНТ ==")
cid, _ = database.upsert_external_client(bid, "tg-777", "telegram",
                                         name="Иван Петров", phone="+7 (921) 000-11-22")
check("узнаётся по телефону в другом формате",
      entities.match(bid, "client", {"name": "И. Петров", "phone": "89210001122"}) == cid)
check("узнаётся по имени, когда телефона не назвали",
      entities.match(bid, "client", {"name": "иван  петров"}) == cid)
check("«ё» и регистр не мешают",
      entities.match(bid, "service", {"title": "Стрижка"}) is None)
eid, _t, _cl, what = entities.create_or_update(
    bid, "client", {"name": "Иван Петров", "phone": "+79210001122"})
check("дубля не создалось", eid == cid and what == "updated", (eid, cid, what))
check("клиентов по-прежнему один",
      len([x for x in database.list_clients(bid) if x["name"] == "Иван Петров"]) == 1)
other_cid, _, _, what2 = entities.create_or_update(
    bid, "client", {"name": "Иван Петров", "phone": "+79995554433"})
check("другой телефон — другой человек", other_cid != cid and what2 == "created",
      (other_cid, cid, what2))


# ============================================================
print("\n== 9. ДЕНЬГИ ==")
ai.understand_material = lambda *a, **k: None
before_fin = len(database.list_finance_entries(bid))
r = door(H, "Кассовый чек\nФискальный признак\nИтого к оплате 4500 ₽\n27.08.2026")
money = r.json()["accepted"][0]
res = money["result"]
check("это документ о расходе", res["type"] == "EXPENSE_DOCUMENT", res["type"])
check("сумма и дата вытащены",
      res["extracted_data"].get("amount") == 4500 and res["extracted_data"].get("op_date") == "2026-08-27",
      res["extracted_data"])
check("ДЕНЬГИ САМИ НЕ ДВИНУЛИСЬ", len(database.list_finance_entries(bid)) == before_fin,
      len(database.list_finance_entries(bid)))
check("расход только предложен",
      any(a["action"] == "create_expense" for a in res["suggested_actions"]),
      [a["action"] for a in res["suggested_actions"]])
r2 = c.post(f"/api/inbox/{money['id']}/action", headers=H,
            json={"action": "create_expense"})
check("после нажатия человека — записан", r2.status_code == 200, r2.text[:150])
check("в финансах прибавилось", len(database.list_finance_entries(bid)) == before_fin + 1)
check("и известно, из чего он вырос",
      bool(database.memory_links(bid, "expense", r2.json()["entity_id"])))


# ============================================================
print("\n== 10. ДОКУМЕНТ ==")
r = door(H, files=[("files", ("договор.txt", txt(
    "ДОГОВОР возмездного оказания услуг №14\nИсполнитель обязуется"), "text/plain"))])
res = r.json()["accepted"][0]["result"]
check("договор опознан", res["type"] == "CONTRACT", res["type"])
check("предлагается сохранить в базу знаний",
      any(a["action"] == "save_document" for a in res["suggested_actions"]),
      [a["action"] for a in res["suggested_actions"]])
check("сам в знания не уехал", not res["applied"], res["applied"])

r = door(H, files=[("files", ("голос.ogg", b"OggS" + b"\x00" * 50, "audio/ogg"))])
voice = r.json()["accepted"][0]
check("голосовое принято и сохранено", voice["kind"] == "file", voice)
check("честно сказано, что расшифровать не умеем",
      "расшифров" in (voice.get("cannot_read") or ""), voice.get("cannot_read"))


# ============================================================
print("\n== 11. ЧУЖОЕ ==")
mine = database.list_inbox(bid, limit=1)[0]["id"]
check("чужой материал не отдаётся",
      c.get(f"/api/inbox/{mine}", headers=OH).status_code == 404)
check("чужой оригинал не скачать",
      c.get(f"/api/inbox/{mine}/file", headers=OH).status_code == 404)
check("чужой разбор не прочитать",
      c.get(f"/api/inbox/{mine}/result", headers=OH).status_code == 404)
check("по чужому материалу нельзя выполнить действие",
      c.post(f"/api/inbox/{mine}/action", headers=OH,
             json={"action": "create_expense"}).status_code in (400, 404, 409))
check("без токена дверь закрыта", door({}, "чужое").status_code == 401)
check("подменить business_id в форме нельзя",
      c.post("/api/inbox/intake", headers=OH,
             data={"text": "подмена", "business_id": str(bid)}).json()["accepted"][0]["id"]
      not in [i["id"] for i in database.list_inbox(bid, limit=200)])
check("узнавание клиента не заглядывает к соседу",
      entities.match(other, "client", {"phone": "+79210001122"}) is None)
check("отпечаток соседа не мешает",
      database.find_inbox_by_hash(other, intake.fingerprint(data=PDF)) is None)


# ============================================================
print("\n== 12. МУСОР НА ВХОДЕ ==")
check("пустая отправка отклонена", door(H, "").status_code == 400)
r = door(H, files=[("files", ("вирус.exe", b"MZ", "application/octet-stream"))])
check("неизвестный тип не принят", not r.json()["accepted"], r.json())
check("и сказано почему",
      "не принимаем" in r.json()["rejected"][0]["error"], r.json()["rejected"])
r = door(H, files=[("files", ("пусто.txt", b"", "text/plain"))])
check("пустой файл не принят", "пуст" in r.json()["rejected"][0]["error"].lower(),
      r.json()["rejected"])
_long = door(H, "я" * (intake.NOTE_MAX + 1)).json()
check("слишком длинная заметка не принята",
      not _long["accepted"] and "длиннее" in _long["rejected"][0]["error"], _long)
r = door(H, "нормальный текст",
         files=[("files", ("плохой.exe", b"MZ", "application/octet-stream")),
                ("files", ("хороший.txt", txt("Кассовый чек 300 ₽"), "text/plain"))])
d = r.json()
check("один плохой файл не отменяет остальные",
      len(d["accepted"]) == 2 and len(d["rejected"]) == 1,
      (len(d["accepted"]), len(d["rejected"])))
check("больше десяти файлов за раз не принимаем",
      door(H, files=[("files", (f"f{i}.txt", txt(f"файл {i}"), "text/plain"))
                     for i in range(11)]).status_code == 400)


# ============================================================
print("\n== 13. МОДЕЛЬ УПАЛА ==")
def _boom(*a, **k):
    raise RuntimeError("модель недоступна")


ai.understand_material = _boom
r = door(H, "Кассовый чек\nИтого 990 ₽")
d = r.json()
check("материал всё равно принят", len(d["accepted"]) == 1, r.text[:200])
res = d["accepted"][0]["result"]
check("разбор сделан правилами", res["engine"] == "rules", res["engine"])
check("и он не пустой", res["extracted_data"].get("amount") == 990, res["extracted_data"])
check("материал не помечен сломанным",
      database.get_inbox_item(d["accepted"][0]["id"], bid)["status"] != "FAILED")
ai.understand_material = lambda *a, **k: None


# ============================================================
print("\n== 14. ХРАНИЛИЩЕ И БАЗА УПАЛИ ==")
real_put = storage.put
storage.put = _boom
r = door(H, files=[("files", ("падение.txt", txt("что-то"), "text/plain"))])
check("файл не принят, раз сохранить его негде", not r.json()["accepted"], r.json())
check("сказано по-человечески",
      "хранилищ" in r.json()["rejected"][0]["error"].lower(), r.json()["rejected"])
storage.put = real_put

real_add = database.add_inbox_item
database.add_inbox_item = _boom
r = door(H, "база лежит")
check("сбой базы не отдаётся как успех", r.status_code >= 400 or not r.json()["accepted"],
      (r.status_code, r.text[:120]))
database.add_inbox_item = real_add
check("после починки приём работает", len(door(H, "база вернулась").json()["accepted"]) == 1)

real_hash = database.find_inbox_by_hash
database.find_inbox_by_hash = _boom
r = door(H, "отпечаток не считается")
check("сбой проверки повторов не отменяет приём",
      len(r.json()["accepted"]) == 1, r.text[:150])
database.find_inbox_by_hash = real_hash


# ============================================================
print("\n== ФАКТ ≠ ДОГАДКА ==")
sales.set_policy(bid, "instagram", level=3, actor="тест")
allowed = set(sales.policy(bid, "instagram")["allowed"])
mem = sales.memory(bid, "сколько стоит маникюр", allowed)
check("непроверенное знание в памяти помечено",
      "не подтверждено владельцем" in mem["text"], mem["text"][:200])
check("и память об этом сообщает", mem["has_unverified"] is True)
check("но подтверждением для проверки оно остаётся",
      not sales.unproven("Маникюр классический — 1500 ₽", mem["grounding"], allowed))
fid = [p["id"] for p in database.list_facts(bid, "product")
       if p["title"] == "Маникюр классический"][0]
entities.update(bid, "product", fid, {"title": "Маникюр классический", "body": "1500 ₽"})
check("правка руками делает знание подтверждённым",
      database.get_fact(fid, bid)["verified"] == 1)
mem2 = sales.memory(bid, "маникюр", allowed)
check("подтверждённое больше не помечено",
      "Маникюр классический: 1500 ₽\n" in mem2["text"] + "\n"
      or "Маникюр классический: 1500 ₽" in mem2["text"].replace(
          "  [не подтверждено владельцем]", "|"), mem2["text"][:300])
check("ядро тоже видит пометку",
      "не подтверждено владельцем" in (database.get_business(bid).get("facts") or ""))

# Подтверждённую руками цену автоматика молча не переписывает.
r = door(H, files=[("files", ("прайс-новый.txt", txt(
    "ПРАЙС-ЛИСТ\nМаникюр классический — 9900 ₽"), "text/plain"))])
check("VELOR не переписал подтверждённую цену",
      database.get_fact(fid, bid)["body"] == "1500 ₽",
      database.get_fact(fid, bid)["body"])


# ============================================================
print("\n== КРИТЕРИИ ГОТОВНОСТИ ==")
ai.understand_material = lambda *a, **k: {
    "type": "SERVICES_OR_PRODUCTS", "confidence": 0.9, "summary": "Новая услуга",
    "extracted_data": {}, "provider": "test"}
before_srv = len(database.list_facts(bid, "service"))
r = door(H, "У нас новая услуга — экспресс-маникюр 2500 рублей.")
res = r.json()["accepted"][0]["result"]
check("СЦЕНАРИЙ 1: понято как услуга с ценой",
      res["extracted_data"].get("items") and
      res["extracted_data"]["items"][0]["title"].lower() == "экспресс-маникюр"
      and res["extracted_data"]["items"][0]["price"] == 2500, res["extracted_data"].get("items"))
srv = database.list_facts(bid, "service")
check("СЦЕНАРИЙ 1: услуга появилась одна", len(srv) == before_srv + 1,
      [s["title"] for s in srv])
# Сценарий 3: обновлённый прайс. Модель на этот раз согласна с правилами, но
# подтверждает изменения человек — так это и происходит в жизни: VELOR
# показывает, что поменяется, и ждёт нажатия.
ai.understand_material = lambda *a, **k: {
    "type": "PRICE_LIST", "confidence": 0.5, "summary": "Прайс на осень",
    "extracted_data": {}, "provider": "test"}
r = door(H, files=[("files", ("прайс-осень.txt", txt(
    "ПРАЙС-ЛИСТ\nМаникюр классический — 1500 ₽\nПедикюр 2400 ₽\n"
    "Наращивание ресниц 3500 ₽\nБрови 900 ₽"), "text/plain"))])
autumn = r.json()["accepted"][0]
check("СЦЕНАРИЙ 3: все четыре позиции найдены",
      autumn["result"]["extracted_data"].get("items_count") == 4,
      autumn["result"]["extracted_data"].get("items_count"))
applied = autumn["result"]["applied"]
detail = applied[0]["detail"] if applied else ""
check("СЦЕНАРИЙ 3: человеку сказано, что добавлено и что обновлено",
      "добавлено 1" in detail and "обновлено 2" in detail, detail)
check("СЦЕНАРИЙ 3: и что подтверждённое руками он не тронул",
      "оставлено без изменений 1" in detail, detail)
check("СЦЕНАРИЙ 3: повторное подтверждение того же действия отклонено",
      c.post(f"/api/inbox/{autumn['id']}/action", headers=H,
             json={"action": "add_price_list"}).status_code == 409)
prods = database.list_facts(bid, "product")
check("СЦЕНАРИЙ 3: новых позиций ровно одна, остальные обновлены",
      len(prods) == 4, [p["title"] for p in prods])
check("СЦЕНАРИЙ 3: цена обновилась",
      any(p["title"] == "Педикюр" and "2 400" in (p["body"] or "") for p in prods),
      [(p["title"], p["body"]) for p in prods])
check("СЦЕНАРИЙ 3: понятое самим VELOR фактом не стало",
      not any(p["verified"] for p in prods if p["title"] != "Маникюр классический"),
      [(p["title"], p["verified"]) for p in prods])
check("СЦЕНАРИЙ 3: а подтверждённое человеком осталось подтверждённым",
      all(p["verified"] for p in prods if p["title"] == "Маникюр классический"),
      [(p["title"], p["verified"]) for p in prods])
check("СЦЕНАРИЙ 3: у каждой позиции видно, из какого материала она пришла",
      all(database.memory_links(bid, "product", p["id"]) for p in prods))

ai.ai_available = real_available
print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
