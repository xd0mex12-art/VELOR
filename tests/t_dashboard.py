# -*- coding: utf-8 -*-
"""
Проверка главной страницы: что /api/home отдаёт брифинг, а не набор нулей.

Главная обязана держать три состояния: пустой бизнес (честно сказать, что данных
нет), рабочий бизнес (настоящие цифры и мысли) и «всё разобрано» (не выдумывать
проблему на ровном месте). Здесь проверяются все три.
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


def texts(block):
    """Все тексты слотов блока «Что говорит VELOR» — для проверки на повторы."""
    return [v["text"] for v in block.values() if v]


print("\n== пустой бизнес ==")
r = c.post("/api/register", json={"name": "Пустая студия", "login": "empty1",
                                  "password": "pass123", "consent": True})
bid0 = r.json()["business_id"]
H0 = {"X-Auth": r.json()["token"]}
d = c.get("/api/home", headers=H0).json()

check("есть блок velor", isinstance(d.get("velor"), dict), d.get("velor"))
check("есть блок attention", isinstance(d.get("attention"), list), d.get("attention"))
check("есть сегодняшняя дата", bool(d.get("today")), d.get("today"))
check("наблюдение честно говорит, что данных нет",
      "пока нет" in (d["velor"]["observation"] or {}).get("text", "").lower(),
      d["velor"]["observation"])
check("проблема не выдумана", d["velor"]["problem"] is None, d["velor"]["problem"])
check("рекомендация ведёт к настройке",
      (d["velor"]["recommendation"] or {}).get("href") == "memory.html", d["velor"]["recommendation"])
check("возможностей нет", d["velor"]["opportunity"] is None)
check("внимание: пустая база знаний и нет бота",
      len(d["attention"]) == 2, d["attention"])
check("каждая строка внимания ведёт куда-то",
      all(i.get("href") for i in d["attention"]), d["attention"])
check("активных клиентов 0", d["clients"]["active"] == 0, d["clients"])
check("всего клиентов 0", d["clients"]["total"] == 0, d["clients"])

print("\n== рабочий бизнес ==")
r = c.post("/api/register", json={"name": "Салон", "login": "work1",
                                  "password": "pass123", "consent": True})
bid = r.json()["business_id"]
H = {"X-Auth": r.json()["token"]}
c.post("/api/business", headers=H, json={"knowledge": "Стрижка 1500 ₽, окрашивание 4500 ₽."})

cl = database.get_or_create_client(bid, 555001, "Анна")["id"]
database.add_order(bid, "Стрижка", client_id=cl, amount=1500)
database.add_order(bid, "Окрашивание", client_id=cl, amount=4500)
database.add_order(bid, "Без суммы", client_id=cl)
database.add_finance_entry(bid, "income", "Услуги", 6000)
database.add_finance_entry(bid, "expense", "Аренда", 2000)

d = c.get("/api/home", headers=H).json()
check("активный клиент найден по заказу", d["clients"]["active"] == 1, d["clients"])
check("всего клиентов 1", d["clients"]["total"] == 1, d["clients"])
check("заявок с суммой 2 из 3",
      d["orders"]["with_amount"] == 2 and d["orders"]["total"] == 3, d["orders"])
check("наблюдение говорит о деньгах",
      "прибыль" in (d["velor"]["observation"] or {}).get("text", "").lower(),
      d["velor"]["observation"])
check("в наблюдении настоящая прибыль",
      "4 000" in (d["velor"]["observation"] or {}).get("text", ""), d["velor"]["observation"])
check("рекомендация — разобрать заявки",
      (d["velor"]["recommendation"] or {}).get("href") == "orders.html", d["velor"]["recommendation"])
check("слоты не повторяют друг друга",
      len(texts(d["velor"])) == len(set(texts(d["velor"]))), texts(d["velor"]))

attn = {i["title"] for i in d["attention"]}
check("внимание: заявки без суммы",
      any("Без суммы" in t for t in attn), attn)
check("внимание: новые заявки",
      any("Ждёт разбора" in t for t in attn), attn)
check("база знаний заполнена — про неё не напоминаем",
      not any("База знаний" in t for t in attn), attn)
check("внимания не больше пяти строк", len(d["attention"]) <= 5, len(d["attention"]))
check("строки внимания отсортированы по важности",
      [i["level"] for i in d["attention"]] == sorted(
          [i["level"] for i in d["attention"]],
          key=lambda l: {"urgent": 0, "warn": 1, "info": 2}.get(l, 3)),
      [i["level"] for i in d["attention"]])

print("\n== активность считается и по перепискам ==")
cl2 = database.get_or_create_client(bid, 555002, "Борис")["id"]
database.save_message(bid, cl2, "user", "Здравствуйте, сколько стоит стрижка?")
check("клиент без заказа, но с сообщением — активен",
      database.active_clients(bid) == 2, database.active_clients(bid))

print("\n== ничего не горит ==")
for o in database.get_orders(bid, limit=50):
    database.update_order_status(o["id"], "выполнен", bid)
    if not o.get("amount"):
        database.set_order_amount(o["id"], bid, 1000)
database.update_business(bid, tg_bot_token="111:AAtest-token-for-dashboard")
d = c.get("/api/home", headers=H).json()
check("список внимания пуст", d["attention"] == [], d["attention"])
check("проблема не выдумана на ровном месте", d["velor"]["problem"] is None, d["velor"]["problem"])
check("наблюдение всё равно есть", d["velor"]["observation"] is not None)

print("\n== риск попадает и в слот, и во внимание ==")
database.save_risks(bid, [
    {"category": "клиенты", "title": "Один клиент даёт 80% заказов",
     "why": "Уйдёт — выручка рухнет.", "action": "Расширить базу", "level": 1},
    {"category": "деньги", "title": "Расходы растут быстрее дохода",
     "why": "Аренда +40% за месяц.", "action": "Пересмотреть расходы", "level": 2},
])
d = c.get("/api/home", headers=H).json()
check("проблема = самый опасный риск",
      (d["velor"]["problem"] or {}).get("text") == "Один клиент даёт 80% заказов", d["velor"]["problem"])
check("остальные риски — во внимании",
      any("Рисков без решения" in i["title"] for i in d["attention"]), d["attention"])

print("\n== главная не зовёт ИИ ==")
import ai
called = {"n": 0}
_real = ai._ask
ai._ask = lambda *a, **k: (called.__setitem__("n", called["n"] + 1), "ответ")[1]
c.get("/api/home", headers=H)
ai._ask = _real
check("ни одного обращения к модели", called["n"] == 0, called["n"])

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
