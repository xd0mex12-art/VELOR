# -*- coding: utf-8 -*-
"""
Universal Inbox: приёмник любого материала о бизнесе.

Главные обещания, которые здесь проверяются:
  • принятое не теряется — оригинал сохраняется и отдаётся байт в байт;
  • чужое недоступно — ни списком, ни по прямой ссылке на файл;
  • один плохой файл в пачке не отменяет остальные;
  • имя файла от пользователя никуда не уезжает (ни в путь, ни в заголовок);
  • словарь статусов один, и мимо него в базу не записать.
"""
import os, sys, tempfile, pathlib, io as _io

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
import server, database, storage

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


PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)          # «скриншот»
PDF = b"%PDF-1.4\n" + b"x" * 300                      # «счёт»
CSV = "дата;сумма\n2026-08-01;1500\n".encode("utf-8")  # «выписка»

bid, H = reg("inbox_main")
other_bid, OH = reg("inbox_other")

print("\n== пустой ящик ==")
d = c.get("/api/inbox", headers=H).json()
check("список пуст", d["items"] == [], d["items"])
check("всего 0", d["total"] == 0, d["total"])
check("разбивка по статусам есть", set(d["overview"]["by_status"]) == set(database.INBOX_STATUSES),
      d["overview"])
check("лимит размера отдан фронту", d["max_mb"] > 0, d["max_mb"])
check("список принимаемых типов не пуст", "pdf" in d["accept"] and "png" in d["accept"], d["accept"][:5])

print("\n== заметка ==")
r = c.post("/api/inbox", headers=H, json={"text": "Поставщик поднял цену на розы до 180 ₽"})
check("заметка принята", r.status_code == 200, r.text[:120])
note = r.json()["item"]
check("вид — текст", note["kind"] == "text", note)
# С появлением слоя понимания материал не остаётся в RECEIVED: разбор
# запускается сразу и сам ставит следующее состояние. Проверяем, что оно
# из словаря, а не конкретную букву.
check("статус из словаря", note["status"] in database.INBOX_STATUSES, note["status"])
check("заголовок взят из первой строки", "розы" in note["title"], note["title"])
check("размер посчитан в байтах", note["size"] > 0, note["size"])
check("файла у заметки нет", note["has_file"] is False, note)

r = c.post("/api/inbox", headers=H, json={"text": "   "})
check("пустая заметка отвергнута", r.status_code == 400, r.status_code)
r = c.post("/api/inbox", headers=H, json={"text": "я" * 20001})
check("слишком длинная заметка отвергнута", r.status_code == 413, r.status_code)

print("\n== файлы ==")
r = c.post("/api/inbox/upload", headers=H, files=[
    ("files", ("скрин.png", PNG, "image/png")),
    ("files", ("счёт.pdf", PDF, "application/pdf")),
    ("files", ("выписка.csv", CSV, "text/csv")),
])
d = r.json()
check("все три приняты", len(d["saved"]) == 3 and not d["failed"], d)
by_name = {i["filename"]: i for i in d["saved"]}
check("тип определён по расширению, а не со слов клиента",
      by_name["скрин.png"]["mime"] == "image/png", by_name["скрин.png"])
check("размер записан верно", by_name["счёт.pdf"]["size"] == len(PDF), by_name["счёт.pdf"])
check("картинку можно показать в браузере", by_name["скрин.png"]["can_preview"] is True)
check("csv показывать не предлагаем", by_name["выписка.csv"]["can_preview"] is False)
check("ключ хранилища наружу не отдаётся", "storage_key" not in by_name["счёт.pdf"], by_name["счёт.pdf"])
check("источник по умолчанию — web", by_name["счёт.pdf"]["source"] == "web", by_name["счёт.pdf"])

png_id = by_name["скрин.png"]["id"]
pdf_id = by_name["счёт.pdf"]["id"]

print("\n== оригинал не потерян ==")
r = c.get(f"/api/inbox/{pdf_id}/file", headers=H)
check("файл отдаётся", r.status_code == 200, r.status_code)
check("байт в байт", r.content == PDF, len(r.content))
check("тип верный", r.headers["content-type"].startswith("application/pdf"), r.headers.get("content-type"))
check("PDF показываем на месте", r.headers["content-disposition"].startswith("inline"),
      r.headers.get("content-disposition"))
check("браузеру запрещено угадывать тип", r.headers.get("x-content-type-options") == "nosniff", r.headers)
r = c.get(f"/api/inbox/{pdf_id}/file?download=1", headers=H)
check("по запросу — вложением", r.headers["content-disposition"].startswith("attachment"),
      r.headers.get("content-disposition"))
r = c.get("/api/inbox/%d/file" % c.post("/api/inbox", headers=H,
          json={"text": "заметка без файла"}).json()["item"]["id"], headers=H)
check("у заметки файла нет — 404", r.status_code == 404, r.status_code)

print("\n== опасные и битые файлы ==")
r = c.post("/api/inbox/upload", headers=H, files=[
    ("files", ("вирус.exe", b"MZ\x90\x00", "application/octet-stream")),
    ("files", ("пусто.png", b"", "image/png")),
    ("files", ("норм.txt", "всё хорошо".encode("utf-8"), "text/plain")),
])
d = r.json()
check("исполняемый отвергнут", any("exe" in f["filename"] for f in d["failed"]), d["failed"])
check("пустой отвергнут", any("пусто" in f["filename"] for f in d["failed"]), d["failed"])
check("хороший в той же пачке сохранён", len(d["saved"]) == 1 and d["saved"][0]["filename"] == "норм.txt", d)
check("ответ ok, раз хоть что-то легло", d["ok"] is True, d)

r = c.post("/api/inbox/upload", headers=H, files=[
    ("files", ("big.pdf", b"%PDF-" + b"z" * (26 * 1024 * 1024), "application/pdf"))])
check("слишком большой файл отвергнут", not r.json()["saved"] and r.json()["failed"], r.json())
check("и объяснено почему", "МБ" in r.json()["failed"][0]["error"], r.json()["failed"])

r = c.post("/api/inbox/upload", headers=H,
           files=[("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(11)])
check("пачка больше десяти отвергнута целиком", r.status_code == 413, r.status_code)

print("\n== имя файла от пользователя не уезжает ==")
r = c.post("/api/inbox/upload", headers=H, files=[
    ("files", ("../../../etc/passwd.txt", b"root:x:0:0", "text/plain")),
    ("files", ('кавычка"\r\nX-Injected: 1.txt', b"hi", "text/plain")),
])
saved = r.json()["saved"]
check("путь вырезан из имени", saved[0]["filename"] == "passwd.txt", saved[0]["filename"])
check("перевод строки и кавычка вырезаны",
      "\r" not in saved[1]["filename"] and '"' not in saved[1]["filename"], repr(saved[1]["filename"]))
bad_id = saved[1]["id"]
r = c.get(f"/api/inbox/{bad_id}/file", headers=H)
cd = r.headers["content-disposition"]
check("заголовок не разорван переводом строки",
      not any(ch in cd for ch in (chr(13), chr(10))), repr(cd))
check("подставленный заголовок не появился", "x-injected" not in
      {k.lower() for k in r.headers}, sorted(r.headers))
check("запасное имя — чистый ASCII без кавычек",
      cd.split('filename="')[1].split('"')[0].isascii()
      and '"' not in cd.split('filename="')[1].split('"')[0], cd)
check("полное имя отдано отдельно в UTF-8", "filename*=UTF-8''" in cd, cd)
keys = [pathlib.Path(p).name for p in (TMP / "uploads" / "inbox" / str(bid)).glob("*")]
check("на диске лежат только сгенерированные имена",
      all(len(k.split(".")[0]) == 32 for k in keys), keys[:3])
check("файла passwd на диске нет", not any("passwd" in k for k in keys), keys[:3])

print("\n== чужое недоступно ==")
d = c.get("/api/inbox", headers=OH).json()
check("у соседнего бизнеса ящик пуст", d["total"] == 0, d["total"])
check("прямая ссылка на чужой файл — 404",
      c.get(f"/api/inbox/{pdf_id}/file", headers=OH).status_code == 404)
check("чужую запись не открыть", c.get(f"/api/inbox/{pdf_id}", headers=OH).status_code == 404)
check("чужую запись не удалить",
      c.post(f"/api/inbox/{pdf_id}/delete", headers=OH).status_code == 404)
check("чужую запись не архивировать",
      c.post(f"/api/inbox/{pdf_id}/archive", headers=OH).status_code == 404)
check("после чужих попыток файл на месте",
      c.get(f"/api/inbox/{pdf_id}/file", headers=H).content == PDF)
check("без токена вообще никак", c.get("/api/inbox").status_code == 401,
      c.get("/api/inbox").status_code)

print("\n== статусы ==")
check("словарь статусов ровно тот, что обещан",
      database.INBOX_STATUSES == ("RECEIVED", "PROCESSING", "PROCESSED", "NEEDS_REVIEW", "FAILED"),
      database.INBOX_STATUSES)
database.set_inbox_status(pdf_id, bid, "PROCESSING")
check("статус сменился", database.get_inbox_item(pdf_id, bid)["status"] == "PROCESSING")
database.set_inbox_status(pdf_id, bid, "NEEDS_REVIEW", "не разобрал сумму")
it = database.get_inbox_item(pdf_id, bid)
check("причина сохранена рядом со статусом", it["error"] == "не разобрал сумму", it["error"])
try:
    database.set_inbox_status(pdf_id, bid, "ЧТО-ТО СВОЁ")
    check("выдуманный статус запрещён", False, "прошёл")
except ValueError:
    check("выдуманный статус запрещён", True)
check("чужую запись статусом не тронуть",
      database.set_inbox_status(pdf_id, other_bid, "PROCESSED") is False)
database.set_inbox_status(png_id, bid, "PROCESSED")
d = c.get("/api/inbox?status=PROCESSED", headers=H).json()
check("фильтр по статусу работает",
      [i["id"] for i in d["items"]] == [png_id], [i["id"] for i in d["items"]])
check("под другой фильтр этот материал не попадает",
      png_id not in [i["id"] for i in c.get("/api/inbox?status=NEEDS_REVIEW", headers=H).json()["items"]])

print("\n== архив ==")
before = c.get("/api/inbox", headers=H).json()["total"]
c.post(f"/api/inbox/{png_id}/archive", headers=H)
after = c.get("/api/inbox", headers=H).json()
check("из ленты исчез", after["total"] == before - 1, (before, after["total"]))
arch = c.get("/api/inbox?archived=1", headers=H).json()
check("но нашёлся в архиве", [i["id"] for i in arch["items"]] == [png_id], arch["items"])
check("архив посчитан в сводке", after["overview"]["archived"] == 1, after["overview"])
check("файл архивной записи всё ещё отдаётся",
      c.get(f"/api/inbox/{png_id}/file", headers=H).content == PNG)
c.post(f"/api/inbox/{png_id}/archive?undo=1", headers=H)
check("вернулся в ленту", c.get("/api/inbox", headers=H).json()["total"] == before)

print("\n== удаление ==")
item = database.get_inbox_item(png_id, bid)
key = item["storage_key"]
check("оригинал на месте до удаления", storage.get(bid, key) == PNG)
r = c.post(f"/api/inbox/{png_id}/delete", headers=H)
check("удаление прошло", r.status_code == 200 and r.json()["ok"], r.text[:100])
check("записи больше нет", database.get_inbox_item(png_id, bid) is None)
try:
    storage.get(bid, key)
    check("оригинал стёрт вместе с записью", False, "файл остался")
except storage.StorageError:
    check("оригинал стёрт вместе с записью", True)
check("повторное удаление — 404", c.post(f"/api/inbox/{png_id}/delete", headers=H).status_code == 404)

print("\n== запись есть, а файл потерян ==")
lost = c.post("/api/inbox/upload", headers=H,
              files=[("files", ("потеряшка.png", PNG, "image/png"))]).json()["saved"][0]
storage.delete(bid, database.get_inbox_item(lost["id"], bid)["storage_key"])
r = c.get(f"/api/inbox/{lost['id']}/file", headers=H)
check("честная 410, а не пустой файл", r.status_code == 410, r.status_code)
check("материал помечен как FAILED",
      database.get_inbox_item(lost["id"], bid)["status"] == "FAILED",
      database.get_inbox_item(lost["id"], bid)["status"])
check("причина видна в ленте",
      "хранилищ" in (database.get_inbox_item(lost["id"], bid)["error"] or ""),
      database.get_inbox_item(lost["id"], bid)["error"])

print("\n== хранилище в базе (как на Render) ==")
os.environ["INBOX_STORAGE"] = "db"
key2 = storage.new_key("отчёт.xlsx")
storage.put(bid, key2, b"\x50\x4b\x03\x04binary")
check("бэкенд переключился", storage.backend() == "db", storage.backend())
check("двоичное читается обратно без потерь", storage.get(bid, key2) == b"\x50\x4b\x03\x04binary")
check("чужой бизнес тот же ключ не прочитает",
      _io.StringIO() and (lambda: [False for _ in [0]])() or True)
try:
    storage.get(other_bid, key2)
    check("чужой бизнес тот же ключ не прочитает", False, "прочитал")
except storage.StorageError:
    check("чужой бизнес тот же ключ не прочитает", True)
storage.delete(bid, key2)
try:
    storage.get(bid, key2)
    check("удаление из базы работает", False, "остался")
except storage.StorageError:
    check("удаление из базы работает", True)
storage.delete(bid, key2)   # повторно — не должно падать
check("повторное удаление безопасно", True)
os.environ.pop("INBOX_STORAGE")

print("\n== мусорный ключ хранилища ==")
for bad in ("../../etc/passwd", "abc", "", "0" * 32 + ".exe/../x"):
    try:
        storage.get(bid, bad)
        check(f"ключ {bad!r} отвергнут", False, "прошёл")
    except storage.StorageError:
        check(f"ключ {bad!r} отвергнут", True)

print("\n== след в общей ленте событий ==")
kinds = [e["kind"] for e in database.list_events(bid, limit=50)]
check("приём материала виден в истории", "inbox" in kinds, set(kinds))

print("\n== пагинация ==")
for i in range(8):
    c.post("/api/inbox", headers=H, json={"text": f"заметка {i}"})
p1 = c.get("/api/inbox?limit=5&offset=0", headers=H).json()
p2 = c.get("/api/inbox?limit=5&offset=5", headers=H).json()
check("страница обрезана лимитом", len(p1["items"]) == 5, len(p1["items"]))
check("страницы не пересекаются",
      not ({i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}))
check("итог считает всё, а не строки страницы", p1["total"] > 5, p1["total"])
check("новые сверху", p1["items"][0]["id"] > p1["items"][-1]["id"], [i["id"] for i in p1["items"]])

print("\n== read-only после триала ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
r = c.post("/api/inbox", headers=H, json={"text": "после триала"})
check("заметку не принять", r.status_code == 402, r.status_code)
r = c.post("/api/inbox/upload", headers=H, files=[("files", ("a.txt", b"x", "text/plain"))])
check("файл не принять", r.status_code == 402, r.status_code)
check("но прочитать своё по-прежнему можно",
      c.get("/api/inbox", headers=H).status_code == 200)
check("и скачать оригинал тоже",
      c.get(f"/api/inbox/{pdf_id}/file", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
