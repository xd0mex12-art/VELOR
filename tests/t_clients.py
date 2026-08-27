# -*- coding: utf-8 -*-
"""
Срезы базы клиентов: экран «Клиенты» обещает владельцу, что «Не возвращаются» —
это те, кто покупал и пропал, а не те, кто не покупал никогда. Проверяем, что
цифры на чипах и в карточках считаются по базе, а не по видимой странице.
"""
import os, sys, tempfile, pathlib

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
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
import server, database

c = TestClient(server.app)
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


def touch(sql, *params):
    """Подправить дату в базе — иначе «давно» не смоделировать."""
    with database._connect() as conn:
        conn.execute(sql, params)


print("\n== пустая база ==")
r = c.post("/api/register", json={"name": "Пустой", "login": "cl_empty",
                                  "password": "pass123", "consent": True})
bid0 = r.json()["business_id"]
H0 = {"X-Auth": r.json()["token"]}
d = c.get("/api/clients", headers=H0).json()
check("всего 0", d["all_total"] == 0, d)
check("живых 0", d["active"] == 0, d)
check("уснувших 0", d["sleeping"] == 0, d)
check("средний чек 0, а не деление на ноль", d["avg_check"] == 0, d)
check("срез по умолчанию — все", d["segment"] == "all", d)

print("\n== настоящая база ==")
r = c.post("/api/register", json={"name": "Салон", "login": "cl_work",
                                  "password": "pass123", "consent": True})
bid = r.json()["business_id"]
H = {"X-Auth": r.json()["token"]}

# свежий покупатель
fresh = database.get_or_create_client(bid, 700001, "Свежая Анна")["id"]
database.add_order(bid, "Стрижка", client_id=fresh, amount=2000)

# покупал, но давно — «уснул»
old = database.get_or_create_client(bid, 700002, "Уснувший Борис")["id"]
oid = database.add_order(bid, "Окрашивание", client_id=old, amount=4000)
touch("UPDATE orders SET created_at = date('now','-120 day') WHERE id = ?", oid)

# никогда не покупал и пришёл давно — не «уснувший», просто молчун
never = database.get_or_create_client(bid, 700003, "Молчун Вера")["id"]
touch("UPDATE clients SET created_at = date('now','-200 day') WHERE id = ?", never)

d = c.get("/api/clients", headers=H).json()
check("всего трое", d["all_total"] == 3, d["all_total"])
check("новых за 30 дней двое", d["new30"] == 2, d["new30"])
check("уснул ровно один", d["sleeping"] == 1, d["sleeping"])
check("средний чек по заказам с суммой", d["avg_check"] == 3000, d["avg_check"])
check("живой за 30 дней один", d["active"] == 1, d["active"])
check("порог сна отдан фронту", d["sleeping_days"] == 60, d)

print("\n== срез: покупали ==")
d = c.get("/api/clients?segment=buyers", headers=H).json()
names = {i["name"] for i in d["items"]}
check("в срезе двое", d["total"] == 2, d["total"])
check("молчуна нет", "Молчун Вера" not in names, names)
check("всего база всё равно 3", d["all_total"] == 3, d["all_total"])

print("\n== срез: не возвращаются ==")
d = c.get("/api/clients?segment=sleeping", headers=H).json()
names = {i["name"] for i in d["items"]}
check("только тот, кто покупал и пропал", names == {"Уснувший Борис"}, names)
check("счётчик среза совпадает со списком", d["total"] == len(d["items"]), d["total"])

print("\n== срез: новые ==")
d = c.get("/api/clients?segment=new", headers=H).json()
names = {i["name"] for i in d["items"]}
check("старый клиент не считается новым", "Молчун Вера" not in names, names)
check("новых двое", d["total"] == 2, d["total"])

print("\n== поиск работает внутри среза ==")
d = c.get("/api/clients?segment=buyers&q=борис", headers=H).json()
check("нашли одного покупателя", d["total"] == 1 and len(d["items"]) == 1, d["total"])
d = c.get("/api/clients?segment=sleeping&q=анна", headers=H).json()
check("свежая Анна в спящие не попала", d["total"] == 0, d["total"])

print("\n== мусор в параметре не ломает экран ==")
d = c.get("/api/clients?segment=%27%20OR%201=1--", headers=H).json()
check("неизвестный срез падает в «все»", d["segment"] == "all" and d["total"] == 3, d)

print("\n== пагинация внутри среза ==")
for i in range(5):
    cid = database.get_or_create_client(bid, 710000 + i, f"Покупатель {i}")["id"]
    database.add_order(bid, "Заказ", client_id=cid, amount=1000)
d = c.get("/api/clients?segment=buyers&limit=3&offset=0", headers=H).json()
check("страница обрезана лимитом", len(d["items"]) == 3, len(d["items"]))
check("итог считает всех в срезе, а не строк на странице", d["total"] == 7, d["total"])
d2 = c.get("/api/clients?segment=buyers&limit=3&offset=3", headers=H).json()
first = {i["id"] for i in d["items"]}
second = {i["id"] for i in d2["items"]}
check("вторая страница не повторяет первую", not (first & second), (first, second))

print("\n== чужие клиенты не видны ==")
d = c.get("/api/clients", headers=H0).json()
check("у соседнего бизнеса база пуста", d["all_total"] == 0, d["all_total"])

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
