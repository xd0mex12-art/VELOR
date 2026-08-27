# -*- coding: utf-8 -*-
"""
AI SALES CHANNEL: продавец, который не умеет выдумывать.

Проверяем то, на чём канал продаж ломается в реальной жизни:
  1) уровни автономии не выдают опасных прав — ни один, включая четвёртый;
  2) права меняют не текст инструкции, а то, что модель видит;
  3) любая цифра из ответа сверяется с памятью бизнеса, и неподтверждённый
     ответ клиенту НЕ уходит;
  4) действия (контакт, лид, заявка) делаются только с разрешения;
  5) во всех случаях неуверенности разговор уходит человеку, а владелец видит
     причину и черновик;
  6) канал Instagram и Telegram от этого не сломались.
"""
import os, sys, json, time, hmac, hashlib, tempfile, pathlib, sqlite3

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
os.environ["CONNECTOR_HOST_GAP"] = "0"
os.environ["INSTAGRAM_APP_ID"] = "1122334455"
os.environ["INSTAGRAM_APP_SECRET"] = "app-secret-value-abc"
os.environ["PUBLIC_URL"] = "https://velor.example.com"
sys.stdout.reconfigure(encoding="utf-8")
# Корень проекта вычисляется от самого файла: тесты должны запускаться
# из любой папки и на любой машине, а не только там, где их писали.
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, sales, instagram, ai, botcore

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


# ── поддельная модель ──────────────────────────────────────────────────────
SCRIPT = []          # что модель ответит на следующий вопрос
ASKED = []           # какие системные промпты она получила


def fake_ask(system, messages, max_tokens=1024):
    ASKED.append(system)
    return SCRIPT.pop(0) if SCRIPT else "Хорошо."


ai.ai_available = lambda: True
ai._ask = fake_ask

# ── поддельный Instagram ───────────────────────────────────────────────────
IG_ID = "17841400000000000"
SENT = []
OUT_N = {"n": 0}


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.headers = payload, status, {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


def fake_request(method, url, **kw):
    data = kw.get("data") or {}
    if url.endswith("/me/messages"):
        OUT_N["n"] += 1
        SENT.append(json.loads(data.get("message") or "{}").get("text") or "")
        return Resp({"recipient_id": "x", "message_id": f"mid.out-{OUT_N['n']}"})
    if url.endswith("/me/conversations"):
        return Resp({"data": []})
    return Resp({"name": "Мария Петрова", "username": "maria_p"})


instagram.requests.request = fake_request
instagram.TRIES = 1

c = TestClient(server.app)
r = c.post("/api/register", json={"name": "Флоренция", "login": "salesone",
                                  "password": "pass123", "consent": True})
bid, H = r.json()["business_id"], {"X-Auth": r.json()["token"]}
r2 = c.post("/api/register", json={"name": "Сосед", "login": "salestwo",
                                   "password": "pass123", "consent": True})
bid2, H2 = r2.json()["business_id"], {"X-Auth": r2.json()["token"]}

# ── память бизнеса: то, что продавцу разрешено знать ───────────────────────
database.add_fact(bid, "service", "Букет пионов", "от 4500 ₽, собираем за 2 часа")
database.add_fact(bid, "service", "Оформление зала", "от 30000 ₽, нужен выезд замерщика")
database.add_fact(bid, "product", "Ваза стеклянная", "1200 ₽, есть в наличии")
database.add_fact(bid, "rule", "Доставка", "по городу 3 часа, бесплатно от 5000 ₽")
database.add_fact(bid, "rule", "Скидка постоянным", "10% при пятом заказе")
database.add_fact(bid, "rule", "Возврат", "возврат срезанных цветов невозможен")
database.add_fact(bid, "company", "Режим работы", "с 9 до 21 без выходных")

CAT = "\n".join(["— Букет пионов: от 4500 ₽, собираем за 2 часа",
                 "— Ваза стеклянная: 1200 ₽, есть в наличии"])


def pol(level=None, grants=None, h=None):
    body = {"channel": "instagram"}
    if level is not None:
        body["level"] = level
    if grants is not None:
        body["grants"] = grants
    return c.post("/api/ai/policy", headers=h or H, json=body)


def client_of(name="Клиент"):
    cid, _ = database.upsert_external_client(bid, "T" + str(time.time_ns()),
                                             "instagram", name=name)
    return database.get_client(cid, bid)


# ═══════════════════════════════════════════════════════════════════════════
print("== УРОВНИ АВТОНОМИИ ==")
check("уровней ровно четыре", sorted(sales.LEVELS) == [1, 2, 3, 4], list(sales.LEVELS))
L = {n: set(m["grants"]) for n, m in sales.LEVELS.items()}
check("уровень 1 — только отвечает", L[1] == {sales.ANSWER_FAQ}, L[1])
check("уровень 2 — плюс лиды и контакты",
      L[2] == {sales.ANSWER_FAQ, sales.SEND_PRICE, sales.RECOMMEND_SERVICE,
               sales.COLLECT_CUSTOMER, sales.CREATE_LEAD}, L[2])
check("уровень 3 — плюс заявки", L[3] == L[2] | {sales.CREATE_ORDER}, L[3])
check("уровень 4 не шире третьего по обычным правам", L[4] == L[3], L[4])
check("уровни вложены друг в друга", L[1] < L[2] < L[3])
DANG = set(sales.DANGEROUS)
check("опасных прав ровно три",
      DANG == {"discount", "refund", "modify_price"}, DANG)
for n in sales.LEVELS:
    check(f"уровень {n} НЕ выдаёт ни одного опасного права", not (L[n] & DANG), L[n] & DANG)
check("по умолчанию заявки не оформляются сами",
      sales.CREATE_ORDER not in L[sales.DEFAULT_LEVEL], sales.DEFAULT_LEVEL)

print("\n== ОПАСНОЕ ВКЛЮЧАЕТСЯ ТОЛЬКО ПОИМЁННО ==")
p = sales.policy(bid)
check("новый бизнес начинает со второго уровня", p["level"] == 2, p["level"])
check("и без единого опасного права", p["dangerous_on"] == [], p)
pol(level=4)
p = sales.policy(bid)
check("даже на четвёртом уровне скидок нет", "discount" not in p["allowed"], p["allowed"])
check("и возвратов нет", "refund" not in p["allowed"], p["allowed"])
check("и цены мимо прайса нет", "modify_price" not in p["allowed"], p["allowed"])
pol(grants=["discount"])
p = sales.policy(bid)
check("включённое поимённо — работает", "discount" in p["allowed"], p["allowed"])
check("и видно отдельно от уровня", p["dangerous_on"] == ["discount"], p)
pol(level=2)
p = sales.policy(bid)
check("смена уровня не сбивает выданное право", "discount" in p["allowed"], p["allowed"])
check("но уровень поменялся", p["level"] == 2, p["level"])
pol(grants=[])
check("право отзывается", "discount" not in sales.policy(bid)["allowed"])
check("несуществующее право не принимается",
      pol(grants=["fly_to_mars"]).status_code == 400)
check("несуществующий уровень не принимается", pol(level=9).status_code == 400)

print("\n== ИЗМЕНЕНИЯ ЗАПИСАНЫ В ИСТОРИЮ ==")
pol(grants=["refund"])
events = database.list_events(bid, limit=60)
titles = " | ".join((e.get("title") or "") for e in events)
check("включение опасного права попало в историю", "Возврат" in titles or "возврат" in titles.lower(),
      titles[:200])
check("и помечено как важное",
      any(e.get("level") == "important" and "азреш" in (e.get("title") or "")
          for e in events),
      [(e.get("title"), e.get("level")) for e in events[:6]])
pol(grants=[])

print("\n== ПАМЯТЬ: ПРАВА МЕНЯЮТ ТО, ЧТО МОДЕЛЬ ВИДИТ ==")
full = sales.memory(bid, "сколько стоит букет", set(sales.LEVELS[3]["grants"]))
check("с правом на цены каталог приходит с ценами", "4500" in full["text"], full["text"][:200])
check("и правила работы тоже", "3 часа" in full["text"], full["text"][:300])
no_price = sales.memory(bid, "сколько стоит букет",
                        {sales.ANSWER_FAQ, sales.RECOMMEND_SERVICE})
check("без права на цены цен в промпте НЕТ", "4500" not in no_price["text"],
      no_price["text"][:200])
check("но названия услуг остались", "Букет пионов" in no_price["text"])
only_faq = sales.memory(bid, "сколько стоит букет", {sales.ANSWER_FAQ})
check("без права подбирать услуги каталога нет вовсе",
      "Букет пионов" not in only_faq["text"], only_faq["text"][:200])
check("правило о скидке спрятано без разрешения",
      "10%" not in full["text"], full["text"])
with_disc = sales.memory(bid, "скидка", set(sales.LEVELS[3]["grants"]) | {"discount"})
check("и появляется вместе с разрешением", "10%" in with_disc["text"])
check("правило о возврате спрятано без разрешения",
      "срезанных" not in full["text"], full["text"])
check("память соседа пуста — она не смешивается",
      sales.memory(bid2, "букет", set(sales.LEVELS[3]["grants"]))["empty"])

print("\n== ПОЛНОТА ПАМЯТИ ВИДНА ЗАРАНЕЕ ==")
h = sales.memory_health(bid)
check("услуги посчитаны", h["services"] == 2, h)
check("цены найдены", h["priced"] == 3 and h["ready"], h)
h2 = sales.memory_health(bid2)
check("у пустого бизнеса продавать нечего", not h2["ready"], h2)
check("и сказано, чего не хватает",
      any("нет ни услуг" in g for g in h2["gaps"]), h2["gaps"])

print("\n== ПРОВЕРКА НА ВЫДУМКУ ==")
ALL = set(sales.LEVELS[3]["grants"])
G = CAT + "\nДоставка: по городу 3 часа, бесплатно от 5000 ₽"


def bad(reply, grounding=G, allowed=ALL):
    return sales.unproven(reply, grounding, allowed)


check("цена из каталога проходит", not bad("Букет пионов — от 4500 ₽."))
check("та же цена с пробелом проходит", not bad("Букет пионов — от 4 500 ₽."))
check("цена из каталога в другом написании проходит",
      not bad("Ваза стоит 1200 руб."))
check("ВЫДУМАННАЯ цена не проходит", bad("Букет обойдётся в 3800 ₽."))
check("и сказано, какая именно", "3800" in bad("Букет обойдётся в 3800 ₽.")[0])
check("посчитанная в уме сумма не проходит",
      bad("Два букета — 9000 ₽."), "перемножил каталог")
check("срок из правил проходит", not bad("Доставим за 3 часа."))
check("ВЫДУМАННЫЙ срок не проходит", bad("Доставим за 30 минут."))
check("выдуманный процент не проходит", bad("Сделаем скидку 15%.", allowed=ALL | {"discount"}))
check("наличие из каталога проходит", not bad("Ваза есть в наличии."))
check("наличие, о котором в памяти молчок, не проходит",
      bad("Пионы есть в наличии.", grounding="— Букет пионов: от 4500 ₽"))
check("слова клиента — тоже подтверждение",
      not bad("Записал бюджет 7000 ₽.", grounding=G + "\nУ меня бюджет 7000"))
check("без права на цены любая цена не проходит",
      bad("Букет — 4500 ₽.", allowed={sales.ANSWER_FAQ}))
check("без права на скидки само слово не проходит",
      bad("Могу дать скидку.", allowed=ALL))
check("с правом на скидку записанная скидка проходит",
      not bad("Скидка 10% при пятом заказе.",
              grounding=G + "\nСкидка постоянным: 10% при пятом заказе",
              allowed=ALL | {"discount"}))
check("без права на возврат слово «возврат» не проходит",
      bad("Оформим возврат.", allowed=ALL))
check("«вернусь с ответом» — это не про возврат денег",
      not bad("Вернусь с ответом через час.",
              grounding=G + "\nотвечаем в течение 1 часа"))
check("обычный ответ без цифр проходит", not bad("Здравствуйте! Что вас интересует?"))
check("номер заявки не считается ценой", not bad("Ваша заявка №41 принята."))

print("\n== РАЗГОВОР ЦЕЛИКОМ ==")
pol(level=3)
cl = client_of("Мария")
SCRIPT.append('Букет пионов — от 4500 ₽, соберём за 2 часа.\n'
              'VELOR: {"lead": {"interest": "букет пионов"}}')
d = sales.answer(bid, cl, "Сколько стоит букет пионов?")
check("подтверждённый ответ уходит клиенту", d["reply"] and "4500" in d["reply"], d)
check("и человека не зовут", not d["handoff"], d)
check("лид записан", any(a["action"] == sales.CREATE_LEAD for a in d["actions"]), d["actions"])
check("интерес попал в карточку",
      "букет пионов" in (database.get_client(cl["id"], bid).get("notes") or ""),
      database.get_client(cl["id"], bid).get("notes"))
check("в промпт ушла память бизнеса", "4500" in ASKED[-1], ASKED[-1][-400:])

cl2 = client_of("Игорь")
SCRIPT.append('Такой букет стоит 3800 ₽, привезём за 30 минут.\nVELOR: {}')
d = sales.answer(bid, cl2, "А из роз?")
check("выдуманный ответ клиенту НЕ уходит", "3800" not in (d["reply"] or ""), d["reply"])
check("вместо него — честная фраза без цифр", d["reply"] == sales.HOLD_REPLY, d["reply"])
check("разговор передан человеку", d["handoff"], d)
check("черновик сохранён для владельца", "3800" in (d["draft"] or ""), d["draft"])
check("и названы обе выдумки", len(d["unproven"]) >= 2, d["unproven"])
check("причина написана словами", "не подтверждён" in d["reason"], d["reason"])

# Ответ заблокирован, но контакт называл сам клиент — терять его незачем.
cl2b = client_of("Без имени")
SCRIPT.append('Роза стоит 777 ₽. Записала вас, Вера.\n'
              'VELOR: {"customer": {"name": "Вера", "phone": "+79001112233"}, '
              '"lead": {"interest": "розы"}, '
              '"order": {"text": "Розы", "amount": 777}}')
d2 = sales.answer(bid, cl2b, "Я Вера, +79001112233, сколько розы?")
row2b = database.get_client(cl2b["id"], bid)
check("выдуманная цена всё так же не уходит", d2["reply"] == sales.HOLD_REPLY, d2["reply"])
check("но контакт клиента сохранён", row2b["phone"] == "+79001112233", row2b)
check("и имя тоже", row2b["name"] == "Вера", row2b["name"])
check("а заявка по непроверенной цене НЕ создана",
      not database.get_client_orders(cl2b["id"], bid),
      database.get_client_orders(cl2b["id"], bid))
check("причина осталась про непроверенный факт",
      "не подтверждён" in d2["reason"], d2["reason"])

cl3 = client_of("Анна")
SCRIPT.append('Уточню у флориста.\nVELOR: {"handoff": "Нестандартный заказ"}')
d = sales.answer(bid, cl3, "Нужен букет из ландышей в январе")
check("модель может позвать человека сама", d["handoff"], d)
check("и её ответ клиенту при этом уходит", d["reply"] == "Уточню у флориста.", d["reply"])
check("с её же причиной", d["reason"] == "Нестандартный заказ", d["reason"])

print("\n== КОНТАКТ, ЛИД, ЗАЯВКА ==")
cl4 = client_of("Гость")
SCRIPT.append('Записала, Ольга! Букет пионов — от 4500 ₽.\n'
              'VELOR: {"customer": {"name": "Ольга", "phone": "+79990001122"}, '
              '"lead": {"interest": "пионы"}, '
              '"order": {"text": "Букет пионов", "amount": 4500}}')
d = sales.answer(bid, cl4, "Меня зовут Ольга, +79990001122, беру пионы")
acts = {a["action"]: a for a in d["actions"]}
check("контакт записан", sales.COLLECT_CUSTOMER in acts, d["actions"])
row = database.get_client(cl4["id"], bid)
check("имя в карточке", row["name"] == "Ольга", row["name"])
check("телефон в карточке", row["phone"] == "+79990001122", row["phone"])
SCRIPT.append('Записала второй номер.\nVELOR: {"customer": {"phone": "+79995554433"}}')
sales.answer(bid, database.get_client(cl4["id"], bid), "Ещё мой номер +79995554433")
row2 = database.get_client(cl4["id"], bid)
check("подтверждённый телефон молча не затирается",
      row2["phone"] == "+79990001122", row2["phone"])
check("а второй номер уходит в заметку",
      "+79995554433" in (row2["notes"] or ""), row2["notes"])
check("заявка оформлена", sales.CREATE_ORDER in acts, d["actions"])
orders = database.get_client_orders(cl4["id"], bid)
check("с суммой из каталога", orders and orders[0]["amount"] == 4500, orders)
check("и с пометкой канала", orders and orders[0]["source"] == "instagram", orders)
links = database.memory_links(bid, "order", orders[0]["id"])
check("в истории записи видно, что её создал ИИ",
      any(l.get("source_kind") == "ai" for l in links), links)
check("и что это был не человек",
      any(l.get("actor") == "velor" for l in links), links)

print("\n== БЕЗ ПРАВА — НЕ ДЕЛАЕТ ==")
pol(level=2)
cl5 = client_of("Пётр")
SCRIPT.append('Отлично, оформляю!\n'
              'VELOR: {"order": {"text": "Букет пионов", "amount": 4500}}')
d = sales.answer(bid, cl5, "Беру букет пионов")
check("заявка НЕ создана", not database.get_client_orders(cl5["id"], bid),
      database.get_client_orders(cl5["id"], bid))
check("и сказано, что не хватило разрешения",
      any(a.get("skipped") for a in d["actions"]), d["actions"])
check("готового клиента передали человеку", d["handoff"], d)
check("с внятной причиной", "разрешения" in d["reason"], d["reason"])

pol(level=1)
cl6 = client_of("Лена")
SCRIPT.append('Букет пионов — от 4500 ₽.\nVELOR: {}')
d = sales.answer(bid, cl6, "Сколько стоит?")
check("на первом уровне цена не уходит клиенту", d["reply"] == sales.HOLD_REPLY, d["reply"])
check("и это названо прямо",
      any("называть цены" in u for u in d["unproven"]), d["unproven"])
check("в промпт первого уровня каталог не попал",
      "4500" not in ASKED[-1], ASKED[-1][-300:])

print("\n== ВСЕГДА ЗОВЁМ ЧЕЛОВЕКА ==")
pol(level=3)
n_before = len(SCRIPT)
d = sales.answer(bid, client_of(), "Позовите менеджера, пожалуйста")
check("прямую просьбу о человеке не отдаём модели вовсе", len(SCRIPT) == n_before)
check("и сразу зовём", d["handoff"] and "попросил живого" in d["reason"], d)

SCRIPT.append('Сделаем скидку 10%.\nVELOR: {}')
d = sales.answer(bid, client_of(), "Дадите скидку?")
check("скидка без разрешения не уходит клиенту", d["reply"] == sales.HOLD_REPLY, d["reply"])
check("и разговор идёт человеку", d["handoff"], d)

ai.ai_available = lambda: False
d = sales.answer(bid, client_of(), "Привет")
check("без модели продавца нет — зовём человека", d["handoff"], d)
check("но клиент получает вежливый ответ", d["reply"] and "5" not in d["reply"], d["reply"])
ai.ai_available = lambda: True

print("\n== ПУСТАЯ ПАМЯТЬ ==")
SCRIPT.append('У нас есть букеты от 2000 ₽.\nVELOR: {}')
d = sales.answer(bid2, client_of.__wrapped__(bid2) if False else
                 dict(database.get_client(
                     database.upsert_external_client(bid2, "X1", "instagram",
                                                     name="Гость")[0], bid2)),
                 "Что у вас есть?")
check("на пустой памяти выдуманный ассортимент не уходит",
      d["reply"] == sales.HOLD_REPLY, d["reply"])
check("и модели прямо сказано, что памяти нет",
      "ПАМЯТЬ БИЗНЕСА ПУСТА" in ASKED[-1], ASKED[-1][-300:])

print("\n== ЧЕРЕЗ ЖИВОЙ ДИРЕКТ ==")
import secretbox
blob = secretbox.seal(json.dumps({"access_token": "T", "expires_at": "2030-01-01 00:00:00"}))
database.save_connection(bid, "instagram", blob,
                         {"ig_id": IG_ID, "username": "florencia",
                          "token_expires_at": "2030-01-01 00:00:00"},
                         permissions=instagram.PERMISSIONS_RU,
                         config={"Аккаунт": "@florencia"})
pol(level=3)


def push(igsid, text, mid):
    payload = {"object": "instagram", "entry": [{"id": IG_ID, "time": 1,
        "messaging": [{"sender": {"id": igsid}, "recipient": {"id": IG_ID},
                       "timestamp": int(time.time() * 1000),
                       "message": {"mid": mid, "text": text}}]}]}
    raw = json.dumps(payload).encode()
    return c.post("/api/instagram/webhook", content=raw, headers={
        "X-Hub-Signature-256": "sha256=" + hmac.new(
            instagram.app_secret().encode(), raw, hashlib.sha256).hexdigest(),
        "Content-Type": "application/json"})


SENT.clear()
SCRIPT.append('Букет пионов — от 4500 ₽.\nVELOR: {"lead": {"interest": "пионы"}}')
push("70001", "Сколько стоит букет пионов?", "mid.s1")
check("ответ ушёл в директ", SENT and "4500" in SENT[-1], SENT)
check("переписка не остановлена", not database.ig_thread_paused(bid, "70001"))

SENT.clear()
SCRIPT.append('Это будет 9900 ₽.\nVELOR: {}')
push("70002", "А свадебный букет?", "mid.s2")
check("выдуманная цена в директ не ушла", SENT and "9900" not in SENT[-1], SENT)
check("клиент получил честную фразу", SENT and SENT[-1] == sales.HOLD_REPLY, SENT)
check("переписка передана человеку", database.ig_thread_paused(bid, "70002"))
th = database.ig_thread(bid, "70002")
check("владелец видит причину", "не подтверждён" in (th.get("paused_why") or ""),
      th.get("paused_why"))
check("и черновик, который VELOR хотел отправить",
      "9900" in (th.get("ai_draft") or ""), th.get("ai_draft"))
one = c.get("/api/instagram/thread/70002", headers=H).json()
check("причина видна в кабинете", one["thread"]["paused_why"], one["thread"])
check("и политика канала рядом", one["policy"]["level"] == 3, one.get("policy"))
c.post("/api/instagram/thread/70002/handoff", headers=H, json={"paused": False})
th = database.ig_thread(bid, "70002")
check("возврат VELOR стирает старую причину", not th.get("paused_why"), th)
check("и старый черновик", not th.get("ai_draft"), th)

print("\n== ДОСТУП К НАСТРОЙКЕ ==")
d = c.get("/api/ai/policy", headers=H).json()
check("справочник прав отдаётся целиком", len(d["permissions"]) == 9, len(d["permissions"]))
check("опасные помечены", set(d["dangerous"]) == DANG, d["dangerous"])
check("каждое право объяснено словами",
      all(p["title"] and p["means"] for p in d["permissions"]))
check("уровни объяснены словами",
      all(l["title"] and l["means"] for l in d["levels"]))
check("полнота памяти приходит вместе с настройкой", d["memory"]["ready"], d["memory"])
check("сосед видит свою настройку, а не нашу",
      c.get("/api/ai/policy", headers=H2).json()["policy"]["level"] == sales.DEFAULT_LEVEL)
pol(level=4, h=H2)
check("и наша от этого не поменялась", sales.policy(bid)["level"] == 3, sales.policy(bid))
check("без входа настройку не посмотреть",
      c.get("/api/ai/policy").status_code == 401)

database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("после триала настройку не поменять", pol(level=1).status_code == 402)
check("но посмотреть можно", c.get("/api/ai/policy", headers=H).status_code == 200)
database.update_business(bid, trial_start=None, trial_end=None,
                         subscription_status="trial", trial_used=0)

print("\n== TELEGRAM НЕ ТРОНУТ ==")
tg = database.get_or_create_client(bid2, tg_user_id=555, name="Иван")
SCRIPT.append("Здравствуйте! Чем помочь?")
reply = botcore.handle_message(bid2, 555, "Иван", "Привет")
check("телеграм отвечает прежним ядром", bool(reply), reply)
check("и продавец его не перехватывает",
      database.ai_policy(bid2, "telegram") is None)

print("\n== УДАЛЕНИЕ БИЗНЕСА ==")
database.delete_business(bid)
with sqlite3.connect(os.environ["DB_PATH"]) as db:
    left = db.execute("SELECT COUNT(*) FROM ai_policy WHERE business_id=?",
                      (bid,)).fetchone()[0]
check("настройка автономии удалена вместе с бизнесом", left == 0, left)

print(f"\nИТОГО: {ok} зелёных, {fail} упавших")
sys.exit(1 if fail else 0)
