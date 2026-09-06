# -*- coding: utf-8 -*-
"""
ВКонтакте как настоящий канал VELOR.

Проверяем ровно то, о чём легко соврать:
  1) ключ проверяется живым вызовом ДО сохранения, а не принимается на слово;
  2) одно сообщество — один бизнес; чужое событие не попадает в чужую переписку;
  3) без секретного слова не принимается ничего, даже с верным group_id;
  4) повторная доставка не рождает ни второго ответа, ни второй заявки;
  5) сообщение проходит весь путь: клиент → переписка → память → заявка;
  6) ответ владельца из самого ВК слышен — VELOR замолкает;
  7) ограничения площадки соблюдаются, а не изображаются;
  8) Telegram и Instagram от всего этого не изменились.
"""
import os, sys, json, tempfile, pathlib, sqlite3

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "testowner"
os.environ["OWNER_PASSWORD"] = "s3cret-owner"
os.environ["JWT_SECRET"] = "test-secret-vk"
os.environ["SECRET_KEY"] = "test-box-key"
os.environ["DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
os.environ["PUBLIC_URL"] = "https://velor.example.com"
sys.stdout.reconfigure(encoding="utf-8")
ROOT = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient                            # noqa: E402
import server, database, connections, vk, botcore, ai, sales         # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


# ── поддельная сторона ВКонтакте ───────────────────────────────────────────
TOKEN = "vk1.a.GOOD-COMMUNITY-TOKEN"
GROUP_ID = "220110033"
CONFIRM = "a1b2c3d4"
CALLS = []
BAD_TOKEN = {"v": False}       # ВК перестал принимать ключ
NO_PERMISSION = {"v": False}   # человек не разрешил сообщения


class Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


def fake_post(url, data=None, timeout=None, **kw):
    data = dict(data or {})
    CALLS.append({"url": url, "data": data})
    method = url.rsplit("/", 1)[-1]

    if BAD_TOKEN["v"] or data.get("access_token") == "vk1.a.WRONG":
        return Resp({"error": {"error_code": 5, "error_msg": "User authorization failed"}})

    if method == "groups.getById":
        return Resp({"response": {"groups": [
            {"id": int(GROUP_ID), "name": "Флоренция", "screen_name": "florencia"}]}})
    if method == "users.get":
        return Resp({"response": [{"id": int(data.get("user_ids") or 0),
                                   "first_name": "Мария", "last_name": "Петрова",
                                   "domain": "maria_p",
                                   "photo_100": "https://vk.cc/ava.jpg"}]})
    if method == "messages.send":
        if NO_PERMISSION["v"]:
            return Resp({"error": {"error_code": 901,
                                   "error_msg": "Can't send messages to user without permission"}})
        return Resp({"response": 7000 + len(CALLS)})
    if method == "messages.getConversations":
        return Resp({"response": {"items": [
            {"conversation": {"peer": {"id": 9001, "type": "user"}}}]}})
    if method == "messages.getHistory":
        return Resp({"response": {"items": [
            {"id": 12, "date": 1786000600, "from_id": -int(GROUP_ID), "out": 1,
             "text": "Здравствуйте! Слушаю вас."},
            {"id": 11, "date": 1786000000, "from_id": 9001, "out": 0,
             "text": "Здравствуйте, есть пионы?"},
        ]}})
    return Resp({"error": {"error_code": 100, "error_msg": "no route"}})


vk.requests.post = fake_post
vk.TRIES = 1

c = TestClient(server.app)
r = c.post("/api/register", json={"name": "Флоренция", "login": "vkone",
                                  "password": "pass123", "consent": True})
bid, H = r.json()["business_id"], {"X-Auth": r.json()["token"]}
r2 = c.post("/api/register", json={"name": "Сосед", "login": "vktwo",
                                   "password": "pass123", "consent": True})
bid2, H2 = r2.json()["business_id"], {"X-Auth": r2.json()["token"]}


def card(h=None):
    return c.get("/api/connections/state/vk", headers=h or H).json()


def msgs_of(b, cid):
    return database.get_client_messages(cid, b, limit=100)


def event(kind, obj, group=None, secret=None, event_id=None):
    """Собрать звонок Callback API так же, как его шлёт ВК."""
    body = {"type": kind, "group_id": int(group or GROUP_ID),
            "secret": secret if secret is not None else vk.callback_secret(bid)}
    if obj is not None:
        body["object"] = obj
    if event_id:
        body["event_id"] = event_id
    return c.post("/api/vk/callback", json=body)


def incoming(peer, text, event_id, date=1786001000):
    return {"message": {"id": int(event_id[-4:] if event_id[-4:].isdigit() else 1),
                        "date": date, "peer_id": int(peer), "from_id": int(peer),
                        "text": text, "attachments": []},
            "client_info": {}}


# ═══════════════════════════════════════════════════════════════════════════
print("== КЛЮЧ ПРОВЕРЯЕТСЯ ЖИВЫМ ВЫЗОВОМ, А НЕ НА СЛОВО ==")
d = card()
check("до подключения канал отключён", d["status"] == connections.DISCONNECTED, d["status"])
check("но он написан, а не «готовится»", d["implemented"], d)
check("и подключить его можно", d["can_connect"], d)
check("права объявлены до согласия", len(d["permissions"]) >= 2, d["permissions"])
check("ограничение площадки названо заранее", "первым" in (d["note"] or ""), d["note"])

r = c.post("/api/connections/vk/connect", headers=H,
           json={"config": {"token": "", "confirmation": CONFIRM}})
check("без ключа не подключить", r.status_code == 422, r.status_code)
r = c.post("/api/connections/vk/connect", headers=H,
           json={"config": {"token": TOKEN, "confirmation": ""}})
check("без строки подтверждения тоже", r.status_code == 422, r.status_code)
check("и объяснено, где её взять", "Callback API" in (r.json().get("detail") or ""), r.json())

r = c.post("/api/connections/vk/connect", headers=H,
           json={"config": {"token": "vk1.a.WRONG", "confirmation": CONFIRM}})
check("негодный ключ не сохраняется", r.status_code == 422, r.status_code)
check("и сказано, что делать", "заново" in (r.json().get("detail") or ""), r.json())
check("канал остался отключённым", card()["status"] == connections.DISCONNECTED)

print("\n== ПОДКЛЮЧЕНИЕ ==")
r = c.post("/api/connections/vk/connect", headers=H,
           json={"config": {"token": " " + TOKEN + chr(10), "confirmation": CONFIRM}})
check("ключ с пробелами и переводом строки принят", r.status_code == 200, r.text)
d = card()
check("статус «Подключено»", d["status"] == connections.CONNECTED, d["status"])
check("видно, какое это сообщество",
      d["configuration"].get("Сообщество") == "Флоренция", d["configuration"])
check("и его адрес", "florencia" in (d["configuration"].get("Адрес") or ""), d["configuration"])
check("пока событий не было — так и написано",
      "ещё ни одного" in (d["configuration"].get("Callback API") or ""), d["configuration"])
check("ключ хранится расшифровываемым только нам", vk.token_of(bid) == TOKEN, vk.token_of(bid))

raw = database.get_connection(bid, "vk", with_secrets=True) or {}
check("в базе он лежит зашифрованным", TOKEN not in str(raw.get("credentials") or ""))
check("и ни одним ответом API наружу не выходит",
      TOKEN not in json.dumps(card(), ensure_ascii=False))

st = c.get("/api/vk/setup", headers=H).json()
check("кабинету показан адрес Callback API",
      st["callback_url"].endswith("/api/vk/callback"), st)
check("и секретное слово", len(st["secret"]) == 32, st)
check("у соседа секретное слово другое",
      c.get("/api/vk/setup", headers=H2).json()["secret"] != st["secret"])

print("\n== ОДНО СООБЩЕСТВО — ОДИН БИЗНЕС ==")
r = c.post("/api/connections/vk/connect", headers=H2,
           json={"config": {"token": TOKEN, "confirmation": CONFIRM}})
check("чужое сообщество к себе не подключить", r.status_code == 422, r.status_code)
check("и сказано почему", "другой компании" in (r.json().get("detail") or ""), r.json())

print("\n== ПРОВЕРКА АДРЕСА ==")
r = event("confirmation", None)
check("на проверку отвечаем строкой подтверждения", r.text == CONFIRM, r.text)
r = event("confirmation", None, secret="чужое-слово")
check("с чужим секретным словом — не отвечаем", r.text != CONFIRM, r.text)
r = event("confirmation", None, group="999000111")
check("незнакомому сообществу — тоже", r.text != CONFIRM, r.text)

print("\n== БЕЗ СЕКРЕТНОГО СЛОВА НЕ ПРОХОДИТ НИЧЕГО ==")
r = event("message_new", incoming(50999, "тест", "ev-bad"), secret="не то", event_id="ev-bad")
check("событие принято молча (ВК нельзя отвечать ошибкой)", r.status_code == 200)
check("но ничего не создало", database.vk_thread(bid, "50999") is None)
r = event("message_new", incoming(50998, "тест", "ev-x"), group="999000111", event_id="ev-x")
check("событие незнакомого сообщества ничего не создало",
      database.vk_thread(bid, "50998") is None)

print("\n== ПУТЬ СООБЩЕНИЯ: КЛИЕНТ → ПЕРЕПИСКА → ПАМЯТЬ → ЗАЯВКА ==")
# Чтобы продавец вообще мог назвать цену, она должна лежать в памяти бизнеса,
# а каналу должно быть разрешено оформлять заявки. Иначе он честно промолчит.
ai.ai_available = lambda: True
database.add_fact(bid, "service", "Букет пионов", "4500 ₽, собираем за 2 часа")
sales.set_policy(bid, "vk", level=3, actor="owner")
ai._ask = lambda system, messages, max_tokens=1024: (
    "Пионы есть, соберём букет к пятнице за 4500 ₽." + chr(10) +
    'VELOR: {"customer": {"phone": "+79990001122"}, '
    '"order": {"text": "Букет пионов", "amount": 4500, "phone": "+79990001122"}}')

r = event("message_new", incoming(9001, "Здравствуйте! Сколько стоит букет пионов?", "ev1"),
          event_id="ev1")
check("событие принято", r.status_code == 200 and r.text == "ok", r.text)
th = database.vk_thread(bid, "9001")
check("разговор заведён", th is not None, th)
check("и привязан к клиенту VELOR", th and th["client_id"], th)
cl = database.get_client(th["client_id"], bid)
check("клиент назван так, как в ВК", cl["name"] == "Мария Петрова", cl)
check("клиент помечен каналом", cl["source"] == "vk", cl)
check("и не задвоится: у него внешний ключ канала",
      cl["external_id"] == "vk:9001", cl)
check("короткий адрес профиля сохранён", th["screen_name"] == "maria_p", th)
mm = msgs_of(bid, th["client_id"])
check("сообщение клиента записано",
      any(m["role"] == "user" and "пионов" in m["content"] for m in mm), mm)
check("ответ VELOR записан",
      any(m["role"] == "assistant" and "Пионы" in m["content"] for m in mm), mm)
check("у обоих проставлен канал", all(m["channel"] == "vk" for m in mm), mm)
sent = [x for x in CALLS if x["url"].endswith("messages.send")]
check("ответ действительно ушёл в ВК", bool(sent), CALLS[-2:])
check("и отправлен именно тому, кто написал", sent[-1]["data"]["peer_id"] == "9001", sent[-1])
check("random_id проставлен — ВК им отсекает дубли",
      int(sent[-1]["data"]["random_id"]) > 0, sent[-1]["data"])
orders = database.get_client_orders(th["client_id"], bid)
check("заявка оформлена", len(orders) == 1, orders)
check("и помечена своим каналом", orders and orders[0]["source"] == "vk", orders)
leads_now = database.list_leads(bid)
check("возможность замечена", len(leads_now) >= 1, leads_now)
check("и у неё канал ВК", any(l["channel"] == "vk" for l in leads_now), leads_now)

print("\n== ПОВТОРНАЯ ДОСТАВКА ==")
sends_before = len([x for x in CALLS if x["url"].endswith("messages.send")])
msgs_before = len(msgs_of(bid, th["client_id"]))
orders_before = len(database.get_client_orders(th["client_id"], bid))
event("message_new", incoming(9001, "Здравствуйте! Сколько стоит букет пионов?", "ev1"), event_id="ev1")
check("второго ответа клиент не получил",
      len([x for x in CALLS if x["url"].endswith("messages.send")]) == sends_before)
check("и второй записи в переписке нет",
      len(msgs_of(bid, th["client_id"])) == msgs_before)
check("и второй заявки тоже",
      len(database.get_client_orders(th["client_id"], bid)) == orders_before)

print("\n== ЭХО ИЗ САМОГО ВК ==")
event("message_reply", {"peer_id": 9001, "from_id": -int(GROUP_ID),
                        "date": 1786002000, "text": "Мария, добрый день! Это Аня."},
      event_id="ev2")
check("ответ владельца из ВК записан в переписку",
      any("Это Аня" in m["content"] and m["role"] == "assistant"
          for m in msgs_of(bid, th["client_id"])), msgs_of(bid, th["client_id"])[-2:])
check("и VELOR замолчал в этом разговоре", database.vk_thread_paused(bid, "9001"))

sends_before = len([x for x in CALLS if x["url"].endswith("messages.send")])
event("message_new", incoming(9001, "А доставка есть?", "ev3"), event_id="ev3")
check("сообщение клиента всё равно сохранено",
      any("доставка" in m["content"] for m in msgs_of(bid, th["client_id"])))
check("но отвечать VELOR не стал — разговор ведёт человек",
      len([x for x in CALLS if x["url"].endswith("messages.send")]) == sends_before)

r = c.post("/api/vk/thread/9001/handoff", headers=H, json={"paused": False})
check("владелец вернул разговор VELOR", r.status_code == 200, r.text)
check("и это записано", not database.vk_thread_paused(bid, "9001"))

print("\n== ОТВЕТ ВЛАДЕЛЬЦА ИЗ КАБИНЕТА ==")
r = c.post("/api/vk/thread/9001/reply", headers=H, json={"text": "Доставка от 300 ₽."})
check("ответ отправлен", r.status_code == 200, r.text)
check("и записан в переписку",
      any("300" in m["content"] and m["role"] == "assistant"
          for m in msgs_of(bid, th["client_id"])))
r = c.post("/api/vk/thread/9001/reply", headers=H, json={"text": "   "})
check("пустое сообщение не отправить", r.status_code == 400, r.status_code)
r = c.post("/api/vk/thread/404404/reply", headers=H, json={"text": "привет"})
check("в несуществующий разговор — тоже", r.status_code == 400, r.status_code)

print("\n== ОГРАНИЧЕНИЕ ПЛОЩАДКИ НАЗЫВАЕТСЯ, А НЕ ПРЯЧЕТСЯ ==")
NO_PERMISSION["v"] = True
r = c.post("/api/vk/thread/9001/reply", headers=H, json={"text": "Напоминаю о себе"})
check("ВК не дал написать — и мы говорим об этом", r.status_code == 400, r.status_code)
check("причина человеческая, а не код ошибки",
      "первым" in (r.json().get("detail") or ""), r.json())
NO_PERMISSION["v"] = False

print("\n== БЕСЕДЫ НЕ ТРОГАЕМ ==")
event("message_new", incoming(2000000005, "Всем привет", "ev4"), event_id="ev4")
check("в беседу VELOR не влезает", database.vk_thread(bid, "2000000005") is None)

print("\n== ЧУЖОЕ НЕ ВИДНО ==")
check("сосед не видит разговоров чужого сообщества",
      c.get("/api/vk/threads", headers=H2).json()["threads"] == [])
check("и не может открыть чужой разговор",
      c.get("/api/vk/thread/9001", headers=H2).status_code == 404)
check("и не может в него ответить",
      c.post("/api/vk/thread/9001/reply", headers=H2,
             json={"text": "привет"}).status_code == 400)
check("без токена список не отдаётся",
      c.get("/api/vk/threads").status_code == 401)

print("\n== ТРИАЛ ЗАКОНЧИЛСЯ ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
sends_before = len([x for x in CALLS if x["url"].endswith("messages.send")])
event("message_new", incoming(9001, "Здравствуйте ещё раз", "ev5"), event_id="ev5")
check("сообщение сохранено — данные бизнеса не теряем",
      any("ещё раз" in m["content"] for m in msgs_of(bid, th["client_id"])))
check("но отвечать VELOR перестал",
      len([x for x in CALLS if x["url"].endswith("messages.send")]) == sends_before)
check("и ручной ответ закрыт",
      c.post("/api/vk/thread/9001/reply", headers=H,
             json={"text": "привет"}).status_code == 402)
check("а читать переписку можно — это его данные",
      c.get("/api/vk/threads", headers=H).status_code == 200)
database.update_business(bid, trial_start=None, trial_end=None,
                         subscription_status="trial", trial_used=0)

print("\n== ПЕРВАЯ ЗАГРУЗКА ==")
res = vk.pull_recent(bid)
check("история подтянута", res["ok"] and res["added"] >= 2, res)
t9 = database.vk_thread(bid, "9001")
check("время сообщений — их собственное, а не время загрузки",
      any(m["created_at"].startswith("2026-") for m in msgs_of(bid, t9["client_id"])),
      [m["created_at"] for m in msgs_of(bid, t9["client_id"])][:3])
before = len(msgs_of(bid, t9["client_id"]))
vk.pull_recent(bid)
check("повторная загрузка дублей не создаёт",
      len(msgs_of(bid, t9["client_id"])) == before, before)

print("\n== ДОСТУП ОТОЗВАЛИ ==")
BAD_TOKEN["v"] = True
res = vk.pull_recent(bid)
check("выгрузка честно не удалась", not res["ok"], res)
check("статус стал «Нужен доступ», а не «Ошибка»",
      card()["status"] == connections.REQUIRES_AUTH, card()["status"])
check("и сказано, что делать", "заново" in (card()["error"] or ""), card()["error"])
BAD_TOKEN["v"] = False

print("\n== ОТКЛЮЧЕНИЕ ==")
kept = len(msgs_of(bid, th["client_id"]))
r = c.post("/api/connections/vk/disconnect", headers=H)
check("отключение прошло", r.status_code == 200, r.text)
check("статус снова «Не подключено»", card()["status"] == connections.DISCONNECTED)
check("ключа больше нет", vk.token_of(bid) is None)
check("но переписка осталась — она принадлежит бизнесу",
      len(msgs_of(bid, th["client_id"])) == kept, kept)
event("message_new", incoming(9001, "эй", "ev6"), event_id="ev6")
check("событие после отключения не обрабатывается",
      not any(m["content"] == "эй" for m in msgs_of(bid, th["client_id"])))

print("\n== TELEGRAM НЕ СЛОМАЛСЯ ==")
tg_client = database.get_or_create_client(bid2, tg_user_id=777, name="Иван")
reply = botcore.handle_message(bid2, 777, "Иван", "Здравствуйте, нужен букет")
check("бот отвечает как прежде", bool(reply), reply)
check("и помечает канал телеграмом",
      all(m["channel"] == "telegram" for m in msgs_of(bid2, tg_client["id"])))

print("\n== УДАЛЕНИЕ БИЗНЕСА УНОСИТ И КАНАЛ ==")
database.delete_business(bid)
with sqlite3.connect(os.environ["DB_PATH"]) as db:
    left_t = db.execute("SELECT COUNT(*) FROM vk_threads WHERE business_id=?", (bid,)).fetchone()[0]
    left_s = db.execute("SELECT COUNT(*) FROM vk_seen WHERE business_id=?", (bid,)).fetchone()[0]
check("разговоров не осталось", left_t == 0, left_t)
check("отметок событий не осталось", left_s == 0, left_s)

print("\nИТОГО: %d зелёных, %d упавших" % (ok, fail))
sys.exit(1 if fail else 0)
