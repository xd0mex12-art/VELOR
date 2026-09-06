# -*- coding: utf-8 -*-
"""
Instagram как настоящий канал VELOR.

Проверяем ровно то, о чём легко соврать:
  1) вход — только на стороне Instagram, state подписан, чужой не подойдёт;
  2) токен хранится зашифрованным и наружу не выходит ни одним ответом API;
  3) вебхук без подписи не принимается, повторная доставка не рождает дублей;
  4) сообщение проходит весь путь: клиент → переписка → память → заявка;
  5) ограничения Instagram соблюдаются, а не изображаются: окно ответа,
     запрет писать первым, тег «пишет человек» только на ответ человека;
  6) передача разговора человеку работает в обе стороны и слышит эхо из
     приложения Instagram;
  7) Telegram от всего этого не изменился.
"""
import base64
import os, sys, json, time, hmac, hashlib, datetime, tempfile, pathlib, sqlite3

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
os.environ.pop("INSTAGRAM_APP_ID", None)
os.environ.pop("INSTAGRAM_APP_SECRET", None)
os.environ.pop("INSTAGRAM_VERIFY_TOKEN", None)
os.environ.pop("INSTAGRAM_REDIRECT_URI", None)
os.environ.pop("PUBLIC_URL", None)
os.environ.pop("RENDER_EXTERNAL_URL", None)
sys.stdout.reconfigure(encoding="utf-8")
# Корень проекта вычисляется от самого файла: тесты должны запускаться
# из любой папки и на любой машине, а не только там, где их писали.
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, connections, instagram, botcore, ai, trial

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


# ── поддельная сторона Meta ────────────────────────────────────────────────
TOKEN_LONG = "IGAA-LONG-LIVED-SECRET-TOKEN-777"
TOKEN_SHORT = "IGAA-short-1h"
IG_ID = "17841400000000000"
CALLS = []              # что именно мы отправили в Meta
SUB_FULL_OK = {"v": True}
OUT_N = {"n": 0}


class Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.headers = {}
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self):
        return self._p


def fake_request(method, url, **kw):
    params = kw.get("params") or {}
    data = kw.get("data") or {}
    CALLS.append({"method": method, "url": url, "params": dict(params), "data": dict(data)})

    if url.endswith("api.instagram.com/oauth/access_token"):
        if (data.get("code") or "") == "bad-code":
            return Resp({"error_type": "OAuthException", "code": 400,
                         "error_message": "Invalid authorization code"}, 400)
        return Resp({"access_token": TOKEN_SHORT, "user_id": int(IG_ID)})
    if "/access_token" in url and params.get("grant_type") == "ig_exchange_token":
        return Resp({"access_token": TOKEN_LONG, "expires_in": 5184000})
    if "/refresh_access_token" in url:
        return Resp({"access_token": TOKEN_LONG + "-R", "expires_in": 5184000})
    if url.endswith("/me") :
        return Resp({"user_id": IG_ID, "username": "florencia_flowers",
                     "name": "Флоренция", "account_type": "BUSINESS"})
    if url.endswith("/me/subscribed_apps"):
        if method.upper() == "DELETE":
            return Resp({"success": True})
        fields = params.get("subscribed_fields") or ""
        if not SUB_FULL_OK["v"] and "message_echoes" in fields:
            return Resp({"error": {"message": "Field message_echoes is not available",
                                   "code": 100}}, 400)
        return Resp({"success": True})
    if url.endswith("/me/messages"):
        if TOKEN_REVOKED["v"]:
            return Resp({"error": {"message": "Session has expired", "code": 190}}, 400)
        if WINDOW_CLOSED["v"] and not (data.get("tag") == "HUMAN_AGENT"):
            return Resp({"error": {"message": "outside window", "code": 10,
                                   "error_subcode": 2534022}}, 400)
        OUT_N["n"] += 1
        return Resp({"recipient_id": "x", "message_id": f"mid.out-{OUT_N['n']}"})
    if url.endswith("/me/conversations"):
        return Resp({"data": [{"id": "conv-1", "updated_time": "2026-08-20T10:00:00+0000"}]})
    if url.endswith("/conv-1"):
        return Resp({"messages": {"data": [
            {"id": "mid.hist-2", "created_time": "2026-08-20T10:05:00+0000",
             "from": {"id": IG_ID, "username": "florencia_flowers"},
             "to": {"data": [{"id": "9001"}]}, "message": "Добрый день!"},
            {"id": "mid.hist-1", "created_time": "2026-08-20T10:00:00+0000",
             "from": {"id": "9001", "username": "old_client"},
             "to": {"data": [{"id": IG_ID}]}, "message": "Здравствуйте, есть пионы?"},
        ]}})
    if "/17841" in url or url.rstrip("/").split("/")[-1].isdigit():
        return Resp({"name": "Мария Петрова", "username": "maria_p",
                     "profile_pic": "https://cdn/ava.jpg", "is_verified_user": False})
    return Resp({"error": {"message": "no route", "code": 100}}, 404)


TOKEN_REVOKED = {"v": False}
WINDOW_CLOSED = {"v": False}
instagram.requests.request = fake_request
instagram.TRIES = 1

c = TestClient(server.app)
r = c.post("/api/register", json={"name": "Флоренция", "login": "igone",
                                  "password": "pass123", "consent": True})
bid, H = r.json()["business_id"], {"X-Auth": r.json()["token"]}
r2 = c.post("/api/register", json={"name": "Сосед", "login": "igtwo",
                                   "password": "pass123", "consent": True})
bid2, H2 = r2.json()["business_id"], {"X-Auth": r2.json()["token"]}


def card(h=None):
    return c.get("/api/connections/state/instagram", headers=h or H).json()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(instagram.app_secret().encode(), body,
                                hashlib.sha256).hexdigest()


def push(entry_id, messaging, h_ok=True, biz=None):
    """Прислать событие так, как это делает Meta."""
    payload = {"object": "instagram", "entry": [
        {"id": entry_id, "time": int(time.time() * 1000), "messaging": messaging}]}
    raw = json.dumps(payload).encode()
    head = {"X-Hub-Signature-256": sign(raw) if h_ok else "sha256=deadbeef",
            "Content-Type": "application/json"}
    return c.post("/api/instagram/webhook", content=raw, headers=head)


def incoming(igsid, text, mid, attachments=None):
    m = {"mid": mid}
    if text is not None:
        m["text"] = text
    if attachments:
        m["attachments"] = attachments
    return [{"sender": {"id": igsid}, "recipient": {"id": IG_ID},
             "timestamp": int(time.time() * 1000), "message": m}]


def echo(igsid, text, mid):
    return [{"sender": {"id": IG_ID}, "recipient": {"id": igsid},
             "timestamp": int(time.time() * 1000),
             "message": {"mid": mid, "text": text, "is_echo": True, "is_self": True}}]


def msgs_of(business_id, client_id):
    with sqlite3.connect(os.environ["DB_PATH"]) as db:
        db.row_factory = sqlite3.Row
        return [dict(x) for x in db.execute(
            "SELECT * FROM messages WHERE business_id=? AND client_id=? ORDER BY id",
            (business_id, client_id))]


def raw_credentials(business_id):
    with sqlite3.connect(os.environ["DB_PATH"]) as db:
        row = db.execute("SELECT credentials FROM connections "
                         "WHERE business_id=? AND provider='instagram'",
                         (business_id,)).fetchone()
    return row[0] if row else ""


# ═══════════════════════════════════════════════════════════════════════════
print("== КАНАЛ ВЫКЛЮЧЕН: ЧЕСТНЫЙ ОТКАЗ, А НЕ ПОДДЕЛЬНЫЙ ВХОД ==")
check("приложение Meta не настроено", not instagram.configured())
st = instagram.setup_state()
check("и сказано, чего именно не хватает",
      not st["ready"] and "INSTAGRAM_APP_ID" in st["missing"]
      and "INSTAGRAM_APP_SECRET" in st["missing"], st)
d = card()
check("карточка честно отключена", d["status"] == connections.DISCONNECTED, d["status"])
check("подключиться нельзя", not d["can_connect"], d)
check("и причина названа словами", "выключено" in (d["note"] or ""), d["note"])
check("вход не выдаёт ссылку в никуда",
      c.post("/api/instagram/login", headers=H).status_code == 422)
r = c.post("/api/connections/instagram/connect", headers=H, json={"config": {"token": "1"}})
check("форма с полями тоже не проходит", r.status_code == 422, r.status_code)
check("и объясняет, что настройка серверная",
      "сервере" in (r.json().get("detail") or ""), r.json())
check("вебхук без секрета приложения не принимает ничего",
      push(IG_ID, incoming("9", "привет", "mid.zero")).status_code == 403)

# ── включаем приложение ────────────────────────────────────────────────────
os.environ["INSTAGRAM_APP_ID"] = "1122334455"
os.environ["INSTAGRAM_APP_SECRET"] = "app-secret-value-abc"
os.environ["PUBLIC_URL"] = "https://velor.example.com"

print("\n== НАСТРОЙКА ПОЯВИЛАСЬ — КНОПКА ОЖИЛА ==")
check("приложение настроено", instagram.configured())
st = instagram.setup_state()
check("готово к подключению", st["ready"], st)
check("адрес возврата собран из публичного адреса",
      st["redirect_uri"] == "https://velor.example.com/api/instagram/callback", st)
check("адрес вебхука показан",
      st["webhook_url"] == "https://velor.example.com/api/instagram/webhook", st)
check("проверочное слово стабильно",
      instagram.verify_token() == instagram.verify_token() and len(instagram.verify_token()) >= 16)
d = card()
check("карточка разрешает подключение", d["can_connect"], d)
check("и говорит, что вход на стороне сервиса", d["needs_login"], d)
check("права объявлены до согласия", len(d["permissions"]) >= 2, d["permissions"])
check("ограничения канала названы заранее",
      "первым не даёт" in (d["note"] or "") and "20" in (d["note"] or ""), d["note"])

print("\n== ВХОД ТОЛЬКО НА СТОРОНЕ INSTAGRAM ==")
r = c.post("/api/instagram/login", headers=H)
check("вход выдаёт ссылку", r.status_code == 200, r.text)
url = r.json()["url"]
check("ссылка ведёт на настоящий Instagram",
      url.startswith("https://www.instagram.com/oauth/authorize?"), url)
for part in ("client_id=1122334455", "response_type=code",
             "instagram_business_basic", "instagram_business_manage_messages",
             "redirect_uri=", "state="):
    check(f"в ссылке есть {part}", part in url, url)
check("лишних прав не просим",
      "content_publish" not in url and "manage_comments" not in url, url)

state = url.split("state=")[1].split("&")[0]
check("state разворачивается в свой бизнес", instagram.read_state(state) == bid)
# Подделываем ПОДПИСЬ, а не символ строки.
#
# Прошлые две редакции этой проверки правили последний символ base64 — и обе
# оказались негодными. У base64 длина в байтах не всегда делится на 3, и тогда
# последний символ несёт «неважные» биты: несколько разных символов
# декодируются в одни и те же байты. Изменённая строка иногда оказывалась тем
# же самым state, проверка падала без всякой дыры, а причину было не видно.
#
# Правим байт подписи после декодирования — тогда подделка отличается от
# оригинала всегда, и проверяется ровно то, что хотели: несовпадение подписи.
_pad = "=" * (-len(state) % 4)
_blob = bytearray(base64.urlsafe_b64decode(state + _pad))
_blob[-1] ^= 0x5A
forged = base64.urlsafe_b64encode(bytes(_blob)).decode().rstrip("=")
check("подделка действительно другая", forged != state, (state, forged))
check("подделан именно байт подписи",
      base64.urlsafe_b64decode(forged + "=" * (-len(forged) % 4))[:-instagram.MAC_LEN]
      == base64.urlsafe_b64decode(state + _pad)[:-instagram.MAC_LEN])
check("подделанный state не проходит", instagram.read_state(forged) is None)
check("пустой state не проходит", instagram.read_state("") is None)
old = instagram._sign_state(bid, int(time.time()) - instagram.STATE_TTL - 10)
check("просроченный state не проходит", instagram.read_state(old) is None)
check("state соседа открывает только соседа",
      instagram.read_state(instagram._sign_state(bid2, int(time.time()))) == bid2)
# Подпись — случайные байты, и в них попадается тот же символ, что и разделитель.
# Разбор «по последней точке» отклонял бы примерно каждый шестнадцатый вход без
# всякой причины, и поймать это на одном примере невозможно.
bad = [b for b in range(2000)
       if instagram.read_state(instagram._sign_state(b, int(time.time()))) != b]
check("state разбирается одинаково для любого бизнеса", not bad, bad[:5])

print("\n== ВОЗВРАТ ОТ META ==")
r = c.get("/api/instagram/callback", params={"error": "access_denied",
          "error_description": "Владелец отказал"}, follow_redirects=False)
check("отказ владельца возвращает в кабинет", r.status_code == 303, r.status_code)
check("и не создаёт подключения", card()["status"] == connections.DISCONNECTED)
r = c.get("/api/instagram/callback", params={"code": "x", "state": "garbage"},
          follow_redirects=False)
check("чужой state отбит", r.status_code == 303 and "ig=err" in r.headers["location"],
      r.headers.get("location"))
check("подключения по-прежнему нет", card()["status"] == connections.DISCONNECTED)

r = c.get("/api/instagram/callback", params={"code": "good-code", "state": state},
          follow_redirects=False)
check("правильный возврат принят", r.status_code == 303, r.status_code)
check("и сказал, что всё получилось", "ig=ok" in r.headers["location"],
      r.headers["location"])
d = card()
check("статус стал «Подключено»", d["status"] == connections.CONNECTED, d["status"])
check("видно, чей это аккаунт",
      d["configuration"].get("Аккаунт") == "@florencia_flowers", d["configuration"])
check("видно, до какого дня действует доступ",
      "Доступ действует до" in d["configuration"], d["configuration"])
check("видно, на какие события подписаны",
      "message_echoes" in (d["configuration"].get("События") or ""), d["configuration"])
check("дата подключения записана", bool(d["connected_at"]), d)

print("\n== ТОКЕН: ЗАШИФРОВАН И НАРУЖУ НЕ ВЫХОДИТ ==")
check("токен на месте и его видит только сервер", instagram.token_of(bid) == TOKEN_LONG)
check("в базе он лежит зашифрованным", TOKEN_LONG not in raw_credentials(bid),
      raw_credentials(bid)[:40])
check("короткий токен нигде не сохранён", TOKEN_SHORT not in raw_credentials(bid))
for path in ("/api/connections/catalog", "/api/connections/state/instagram",
             "/api/instagram/threads", "/api/instagram/setup"):
    body = c.get(path, headers=H).text
    check(f"{path}: токена в ответе нет", TOKEN_LONG not in body)
    check(f"{path}: секрета приложения в ответе нет", "app-secret-value-abc" not in body)
check("арендатор не видит общего проверочного слова",
      c.get("/api/instagram/setup", headers=H).json()["verify_token"] == "")
own = c.post("/api/login", json={"login": "testowner", "password": "s3cret-owner"})
if own.status_code == 200:
    OH = {"X-Auth": own.json()["token"]}
    check("а владелец VELOR видит — ему настраивать Meta",
          c.get("/api/instagram/setup", params={"business_id": bid},
                headers=OH).json()["verify_token"] != "")

print("\n== ВЕБХУК: ПОДПИСЬ ОБЯЗАТЕЛЬНА ==")
check("верификация адреса отвечает числом Meta",
      c.get("/api/instagram/webhook", params={
          "hub.mode": "subscribe", "hub.verify_token": instagram.verify_token(),
          "hub.challenge": "1158201444"}).text == "1158201444")
check("с чужим словом — отказ",
      c.get("/api/instagram/webhook", params={
          "hub.mode": "subscribe", "hub.verify_token": "чужое",
          "hub.challenge": "1"}).status_code == 403)
check("событие без подписи не принимается",
      c.post("/api/instagram/webhook", json={"object": "instagram", "entry": []}
             ).status_code == 403)
check("событие с неверной подписью не принимается",
      push(IG_ID, incoming("50999", "тест", "mid.bad"), h_ok=False).status_code == 403)
check("и ничего не создало", database.ig_thread(bid, "50999") is None)

print("\n== ПУТЬ СООБЩЕНИЯ: КЛИЕНТ → ПЕРЕПИСКА → ПАМЯТЬ → ЗАЯВКА ==")
# Отвечает AI-продавец: чтобы он вообще мог назвать цену, она должна лежать в
# памяти бизнеса, а каналу должно быть разрешено оформлять заявки. Иначе он
# честно промолчит — что проверяется отдельно в t_ai_sales.py.
import sales
ai.ai_available = lambda: True
database.add_fact(bid, "service", "Букет пионов", "4500 ₽, собираем за 2 часа")
sales.set_policy(bid, "instagram", level=3, actor="owner")
ai._ask = lambda system, messages, max_tokens=1024: (
    "Пионы есть, соберём букет к пятнице за 4500 ₽.\n"
    'VELOR: {"customer": {"phone": "+79990001122"}, '
    '"order": {"text": "Букет пионов", "amount": 4500, "phone": "+79990001122"}}')

before_orders = len(database.list_orders(bid)) if hasattr(database, "list_orders") else None
r = push(IG_ID, incoming("50001", "Здравствуйте! Хочу букет пионов", "mid.in-1"))
check("событие принято", r.status_code == 200, r.text)
th = database.ig_thread(bid, "50001")
check("переписка заведена", th is not None, th)
check("и привязана к клиенту VELOR", th and th["client_id"], th)
cl = database.get_client(th["client_id"], bid)
check("клиент назван так, как в Instagram", cl["name"] == "Мария Петрова", cl)
check("клиент помечен каналом", cl["source"] == "instagram", cl)
check("и не задвоится: у него внешний ключ канала",
      cl["external_id"] == "instagram:50001", cl)
check("@-логин сохранён", th["username"] == "maria_p", th)
mm = msgs_of(bid, th["client_id"])
check("сообщение клиента записано", any(m["role"] == "user" and "пионов" in m["content"]
                                        for m in mm), mm)
check("ответ VELOR записан", any(m["role"] == "assistant" and "Пионы" in m["content"]
                                 for m in mm), mm)
check("у обоих проставлен канал", all(m["channel"] == "instagram" for m in mm), mm)
check("ответ действительно ушёл в Instagram",
      any(x["url"].endswith("/me/messages") for x in CALLS[-6:]), CALLS[-3:])
sent = [x for x in CALLS if x["url"].endswith("/me/messages")][-1]
check("на автоответе тега «пишет человек» нет", "tag" not in sent["data"], sent["data"])
check("и отправлено именно тому, кто написал",
      json.loads(sent["data"]["recipient"])["id"] == "50001", sent["data"])
orders = database.get_client_orders(th["client_id"], bid)
check("заявка оформлена", len(orders) == 1, orders)
check("с суммой — это и есть выручка", orders and orders[0]["amount"] == 4500, orders)
check("и помечена каналом", orders and orders[0]["source"] == "instagram", orders)
check("телефон из разговора попал в карточку",
      (database.get_client(th["client_id"], bid) or {}).get("phone") == "+79990001122")
check("окно ответа посчитано от последнего входящего",
      instagram.window_state(bid, "50001")["hours_left"] > 23)

print("\n== ПОВТОРНАЯ ДОСТАВКА И СВОЁ ЭХО ==")
n_before = len(msgs_of(bid, th["client_id"]))
push(IG_ID, incoming("50001", "Здравствуйте! Хочу букет пионов", "mid.in-1"))
check("тот же mid второй раз ничего не добавил",
      len(msgs_of(bid, th["client_id"])) == n_before, n_before)
out_mid = json.loads('"' + f"mid.out-{OUT_N['n']}" + '"')
push(IG_ID, echo("50001", "Пионы есть, соберём букет к пятнице.", out_mid))
check("своё же эхо не принято за вмешательство человека",
      not database.ig_thread_paused(bid, "50001"))
check("и не задвоило ответ", len(msgs_of(bid, th["client_id"])) == n_before)

print("\n== ЧУЖОЙ АККАУНТ И ЧУЖАЯ КОМПАНИЯ ==")
push("99999999999", incoming("50002", "привет", "mid.alien"))
check("событие чужого аккаунта не создало переписку",
      database.ig_thread(bid, "50002") is None and database.ig_thread(bid2, "50002") is None)
check("у соседа переписок нет",
      c.get("/api/instagram/threads", headers=H2).json()["threads"] == [])
check("и чужую он не откроет",
      c.get("/api/instagram/thread/50001", headers=H2).status_code == 404)
check("а свою мы видим",
      len(c.get("/api/instagram/threads", headers=H).json()["threads"]) >= 1)

print("\n== ВЛОЖЕНИЕ БЕЗ ТЕКСТА: ЗОВЁМ ЧЕЛОВЕКА, А НЕ ВЫДУМЫВАЕМ ==")
sends_before = len([x for x in CALLS if x["url"].endswith("/me/messages")])
push(IG_ID, incoming("50003", None, "mid.pic",
                     attachments=[{"type": "image", "payload": {"url": "https://x/1.jpg"}}]))
th3 = database.ig_thread(bid, "50003")
check("переписка всё равно заведена", th3 is not None)
m3 = msgs_of(bid, th3["client_id"])
check("вложение записано словами", any("прислал фото" in m["content"] for m in m3), m3)
check("но ответа не выдумано",
      len([x for x in CALLS if x["url"].endswith("/me/messages")]) == sends_before)

print("\n== ПЕРЕДАЧА РАЗГОВОРА ЧЕЛОВЕКУ ==")
r = c.post("/api/instagram/thread/50001/handoff", headers=H, json={"paused": True})
check("разговор взят на себя", r.status_code == 200 and r.json()["paused"], r.text)
sends_before = len([x for x in CALLS if x["url"].endswith("/me/messages")])
push(IG_ID, incoming("50001", "А доставка есть?", "mid.in-2"))
check("сообщение сохранено",
      any("доставка" in m["content"] for m in msgs_of(bid, th["client_id"])))
check("но VELOR промолчал",
      len([x for x in CALLS if x["url"].endswith("/me/messages")]) == sends_before)
r = c.post("/api/instagram/thread/50001/reply", headers=H,
           json={"text": "Да, привезём в пятницу."})
check("человек ответил из кабинета", r.status_code == 200, r.text)
check("без тега — сутки ещё не прошли", not r.json()["tagged_human"], r.json())
check("ответ человека записан в ту же переписку",
      any(m["role"] == "assistant" and "пятницу" in m["content"]
          for m in msgs_of(bid, th["client_id"])))
r = c.post("/api/instagram/thread/50001/handoff", headers=H, json={"paused": False})
check("разговор возвращён VELOR", r.status_code == 200 and not r.json()["paused"])
sends_before = len([x for x in CALLS if x["url"].endswith("/me/messages")])
push(IG_ID, incoming("50001", "Отлично, беру", "mid.in-3"))
check("и он снова отвечает",
      len([x for x in CALLS if x["url"].endswith("/me/messages")]) == sends_before + 1)
check("переключатель на несуществующей переписке — 404",
      c.post("/api/instagram/thread/777777/handoff", headers=H,
             json={"paused": True}).status_code == 404)

print("\n== ЭХО ИЗ САМОГО ПРИЛОЖЕНИЯ INSTAGRAM ==")
push(IG_ID, echo("50001", "Здравствуйте, это Арина, отвечу лично", "mid.human-1"))
check("ответ человека из приложения записан",
      any("это Арина" in m["content"] for m in msgs_of(bid, th["client_id"])))
check("и VELOR сам замолчал в этой переписке",
      database.ig_thread_paused(bid, "50001"))
sends_before = len([x for x in CALLS if x["url"].endswith("/me/messages")])
push(IG_ID, incoming("50001", "Хорошо!", "mid.in-4"))
check("следующее сообщение он не перехватывает",
      len([x for x in CALLS if x["url"].endswith("/me/messages")]) == sends_before)
c.post("/api/instagram/thread/50001/handoff", headers=H, json={"paused": False})

print("\n== ОКНО ОТВЕТА — ПРАВИЛО INSTAGRAM, А НЕ НАШЕ ==")
def set_last_in(hours_ago):
    when = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours_ago)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(os.environ["DB_PATH"]) as db:
        db.execute("UPDATE ig_threads SET last_in_at=? WHERE business_id=? AND igsid=?",
                   (when, bid, "50001"))

set_last_in(1)
w = instagram.window_state(bid, "50001")
check("свежая переписка: отвечает кто угодно",
      w["can_reply"] and not w["needs_human_tag"] and w["hours_left"] > 22, w)
set_last_in(30)
w = instagram.window_state(bid, "50001")
check("после суток автоответ уже нельзя", w["needs_human_tag"], w)
check("но человеку ещё можно", w["can_reply"] and w["human_hours_left"] > 100, w)
WINDOW_CLOSED["v"] = True          # Meta теперь отвергает всё без тега
r = c.post("/api/instagram/thread/50001/reply", headers=H, json={"text": "Ещё здесь?"})
check("человек ответил и Meta приняла", r.status_code == 200, r.text)
check("потому что ушёл тег «пишет человек»", r.json()["tagged_human"], r.json())
tagged = [x for x in CALLS if x["url"].endswith("/me/messages")][-1]
check("тег именно HUMAN_AGENT", tagged["data"].get("tag") == "HUMAN_AGENT", tagged["data"])
check("и тип сообщения — MESSAGE_TAG",
      tagged["data"].get("messaging_type") == "MESSAGE_TAG", tagged["data"])
WINDOW_CLOSED["v"] = False
set_last_in(24 * 8)
w = instagram.window_state(bid, "50001")
check("через восемь суток отвечать нельзя никому", not w["can_reply"], w)
check("и сказано почему", "семи суток" in w["why"], w)
r = c.post("/api/instagram/thread/50001/reply", headers=H, json={"text": "Ау"})
check("кабинет не пускает отправку", r.status_code == 400, r.status_code)
check("с человеческим объяснением", "семи суток" in (r.json().get("detail") or ""), r.json())
check("пустой ответ не отправляется",
      c.post("/api/instagram/thread/50001/reply", headers=H,
             json={"text": "   "}).status_code == 400)
set_last_in(1)

print("\n== ПЕРВЫМ ПИСАТЬ НЕЛЬЗЯ ==")
database.ig_thread_upsert(bid, "50009", th["client_id"])
w = instagram.window_state(bid, "50009")
check("клиенту, который не писал, ответить нельзя", not w["can_reply"], w)
check("и объяснено, что это запрет Instagram",
      "первым" in w["why"], w)

print("\n== ИСТОРИЯ: РОВНО ТО, ЧТО ОТДАЁТ INSTAGRAM ==")
res = instagram.pull_recent(bid)
check("выгрузка прошла", res["ok"], res)
check("и честно названа своим пределом", "20" in res["limit_note"], res)
old_th = database.ig_thread(bid, "9001")
check("старая переписка подтянулась", old_th is not None, old_th)
hist = msgs_of(bid, old_th["client_id"])
check("в ней обе стороны", {m["role"] for m in hist} == {"user", "assistant"}, hist)
check("время сообщений — их собственное, а не момент загрузки",
      any(str(m["created_at"]).startswith("2026-08-20") for m in hist), hist)
n_hist = len(hist)
instagram.pull_recent(bid)
check("повторная выгрузка не задваивает",
      len(msgs_of(bid, old_th["client_id"])) == n_hist, n_hist)

print("\n== ПЕРЕПИСКА В КАБИНЕТЕ ==")
d = c.get("/api/instagram/threads", headers=H).json()
check("список отдаётся вместе с состоянием подключения",
      d["connection"]["status"] == connections.CONNECTED, d["connection"]["status"])
check("у каждой переписки посчитано окно ответа",
      all("window" in t for t in d["threads"]), d["threads"][:1])
check("ограничения канала названы числами",
      d["limits"]["reply_window_hours"] == 24 and d["limits"]["history_limit"] == 20,
      d["limits"])
one = c.get("/api/instagram/thread/50001", headers=H).json()
check("одна переписка отдаёт историю", len(one["messages"]) >= 3, len(one["messages"]))
check("и карточку клиента", one["client"] and one["client"]["id"] == th["client_id"])
check("несуществующая — 404",
      c.get("/api/instagram/thread/nope", headers=H).status_code == 404)

print("\n== ДОСТУП ОТОЗВАЛИ ==")
TOKEN_REVOKED["v"] = True
sends_before = len([x for x in CALLS if x["url"].endswith("/me/messages")])
push(IG_ID, incoming("50001", "Ещё вопрос", "mid.in-5"))
check("сообщение всё равно сохранено",
      any("Ещё вопрос" in m["content"] for m in msgs_of(bid, th["client_id"])))
d = card()
check("статус стал «Нужен доступ», а не «Ошибка»",
      d["status"] == connections.REQUIRES_AUTH, d["status"])
check("и сказано, что делать", "заново" in (d["error"] or ""), d["error"])
TOKEN_REVOKED["v"] = False
database.mark_connection_synced(bid, "instagram", added=0)
check("после успешной выгрузки статус вернулся",
      card()["status"] == connections.CONNECTED, card()["status"])

print("\n== СРОК ЖИЗНИ ДОСТУПА ==")
row = database.get_connection(bid, "instagram")
meta = dict(row["meta"])
meta["token_expires_at"] = "2020-01-01 00:00:00"
database.save_connection(bid, "instagram", None, meta,
                         permissions=row["permissions"], config=row["config"])
check("протухший доступ показан как «Нужен доступ», хотя ошибки не было",
      card()["status"] == connections.REQUIRES_AUTH, card()["status"])
meta["token_expires_at"] = (datetime.datetime.utcnow()
                            + datetime.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
database.save_connection(bid, "instagram", None, meta,
                         permissions=row["permissions"], config=row["config"])
fresh = instagram.ensure_fresh_token(bid)
check("доступ на исходе продлевается заранее", fresh == TOKEN_LONG + "-R", fresh)
check("и новый срок записан",
      instagram.token_expires_at(bid) > datetime.datetime.utcnow().strftime("%Y-%m-%d"),
      instagram.token_expires_at(bid))

print("\n== ПОДПИСКА НА СОБЫТИЯ: ЧТО ЕСТЬ, ТО И ПИШЕМ ==")
SUB_FULL_OK["v"] = False
sub = instagram.subscribe(TOKEN_LONG)
check("при отказе откатились к обязательному minimum", sub["fields"] == ["messages"], sub)
SUB_FULL_OK["v"] = True
check("иначе подписываемся на всё нужное",
      instagram.subscribe(TOKEN_LONG)["fields"] == instagram.WEBHOOK_FIELDS)

print("\n== ТРИАЛ ЗАКОНЧИЛСЯ ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
sends_before = len([x for x in CALLS if x["url"].endswith("/me/messages")])
push(IG_ID, incoming("50001", "Здравствуйте ещё раз", "mid.in-6"))
check("сообщение сохранено — данные бизнеса не теряем",
      any("ещё раз" in m["content"] for m in msgs_of(bid, th["client_id"])))
check("но отвечать VELOR перестал",
      len([x for x in CALLS if x["url"].endswith("/me/messages")]) == sends_before)
check("и ручной ответ закрыт",
      c.post("/api/instagram/thread/50001/reply", headers=H,
             json={"text": "привет"}).status_code == 402)
check("и подключить канал нельзя",
      c.post("/api/instagram/login", headers=H).status_code == 402)
check("а читать переписку можно — это его данные",
      c.get("/api/instagram/threads", headers=H).status_code == 200)
database.update_business(bid, trial_start=None, trial_end=None,
                         subscription_status="trial", trial_used=0)

print("")
print("== ЗВОНОК META: ОТКЛЮЧЕНИЕ И УДАЛЕНИЕ ==")
# Meta требует эти два адреса обязательным полем, но проверяем мы не букву
# требования. Без первого владелец убирает VELOR у себя в Instagram, а кабинет
# продолжает писать «Подключено» над мёртвым токеном — и первым о поломке
# узнаёт клиент, которому никто не ответил.


def signed(payload, secret=None, algo="HMAC-SHA256"):
    """Собрать signed_request так же, как его собирает Meta."""
    body = dict(payload)
    body["algorithm"] = algo
    raw = base64.urlsafe_b64encode(
        json.dumps(body).encode()).decode().rstrip("=")
    key = (secret if secret is not None else instagram.app_secret()).encode()
    sig = base64.urlsafe_b64encode(
        hmac.new(key, raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return sig + "." + raw


def deauth(sr):
    return c.post("/api/instagram/deauthorize", data={"signed_request": sr})


# Подпись — единственное, что отделяет звонок Meta от звонка постороннего.
# А по этому звонку мы отключаем бизнесу канал продаж.
alive_before = instagram.token_of(bid)
check("токен на месте до проверок", alive_before == TOKEN_LONG + "-R", alive_before)
for name, bad in [("мусор вместо запроса", "не-подпись"),
                  ("подпись чужим секретом", signed({"user_id": IG_ID}, secret="чужой")),
                  ("чужой алгоритм подписи", signed({"user_id": IG_ID}, algo="PLAINTEXT")),
                  ("пустое поле", "")]:
    r = deauth(bad)
    check("не отключаемся: " + name,
          r.status_code == 200 and instagram.token_of(bid) is not None, r.status_code)

check("подделку разбор отвергает сам",
      instagram.read_signed_request(signed({"user_id": IG_ID}, secret="чужой")) is None)
check("а настоящий запрос читает",
      (instagram.read_signed_request(signed({"user_id": IG_ID})) or {}).get("user_id") == IG_ID)

# Звонок про аккаунт, которого у нас нет, не должен ни падать, ни задевать чужой
# канал: вебхук у приложения Meta один на всех, ошибиться тут — отключить не ту
# компанию.
r = deauth(signed({"user_id": "17841499999999999"}))
check("звонок про незнакомый аккаунт принят и никого не задел",
      r.status_code == 200 and instagram.token_of(bid) is not None)

msgs_kept_meta = len(msgs_of(bid, th["client_id"]))
r = deauth(signed({"user_id": IG_ID}))
check("настоящий звонок принят", r.status_code == 200, r.text)
check("доступ отозван — токена больше нет", instagram.token_of(bid) is None)
check("и кабинет говорит «Не подключено», а не «Подключено»",
      card()["status"] == connections.DISCONNECTED, card()["status"])
check("переписка при этом цела — это записи бизнеса",
      len(msgs_of(bid, th["client_id"])) == msgs_kept_meta, msgs_kept_meta)
check("и клиент цел", database.get_client(th["client_id"], bid) is not None)

# ── запрос на удаление данных ──────────────────────────────────────────────
r = c.post("/api/instagram/data-deletion", data={"signed_request": "мусор"})
check("удаление без верной подписи отклонено", r.status_code == 400, r.status_code)

r = c.post("/api/instagram/data-deletion", data={"signed_request": signed({"user_id": IG_ID})})
check("на запрос об удалении отвечаем", r.status_code == 200, r.text)
dj = r.json() if r.status_code == 200 else {}
check("Meta получает адрес состояния", "/api/instagram/deletion-status" in (dj.get("url") or ""), dj)
check("и код подтверждения", len(dj.get("confirmation_code") or "") == 16, dj)
r2 = c.post("/api/instagram/data-deletion", data={"signed_request": signed({"user_id": IG_ID})})
check("повторный запрос того же человека даёт тот же код",
      r2.json().get("confirmation_code") == dj.get("confirmation_code"))
check("а другому аккаунту — другой",
      instagram.deletion_code("17841400000000001") != dj.get("confirmation_code"))
check("сам id аккаунта в адрес не попадает", IG_ID not in (dj.get("url") or ""), dj.get("url"))

page = c.get("/api/instagram/deletion-status?code=" + (dj.get("confirmation_code") or ""))
check("страница состояния открывается", page.status_code == 200, page.status_code)
check("и показывает код", (dj.get("confirmation_code") or "") in page.text)
# Код приходит из адресной строки, то есть от кого угодно. Печатать его на
# странице как есть — обычный способ пустить чужой скрипт на свой домен.
bad = c.get("/api/instagram/deletion-status?code=<script>alert(1)</script>")
check("посторонний код на страницу не проходит",
      "<script>alert" not in bad.text, bad.text[:120])

# ── адреса для кабинета Meta ───────────────────────────────────────────────
stm = instagram.setup_state()
check("кабинету показан адрес отключения",
      stm["deauthorize_url"].endswith("/api/instagram/deauthorize"), stm)
check("и адрес запроса на удаление",
      stm["deletion_url"].endswith("/api/instagram/data-deletion"), stm)

# Возвращаем канал на место: следующие проверки идут с живым подключением.
instagram.save_token(bid, TOKEN_LONG, 60 * 24 * 3600, {"ig_id": IG_ID, "username": "flowers"})
check("канал восстановлен для дальнейших проверок",
      instagram.token_of(bid) == TOKEN_LONG)

print("\n== ОТКЛЮЧЕНИЕ ==")
msgs_kept = len(msgs_of(bid, th["client_id"]))
r = c.post("/api/connections/instagram/disconnect", headers=H)
check("отключение прошло", r.status_code == 200, r.text)
check("от событий отписались",
      any(x["method"].upper() == "DELETE" and x["url"].endswith("/me/subscribed_apps")
          for x in CALLS[-4:]), CALLS[-2:])
check("статус снова «Не подключено»", card()["status"] == connections.DISCONNECTED)
check("токена больше нет", instagram.token_of(bid) is None)
check("но переписка осталась — она принадлежит бизнесу",
      len(msgs_of(bid, th["client_id"])) == msgs_kept, msgs_kept)
check("и клиент остался", database.get_client(th["client_id"], bid) is not None)
check("событие после отключения не обрабатывается",
      push(IG_ID, incoming("50001", "эй", "mid.after")).status_code == 200
      and not any("эй" == m["content"] for m in msgs_of(bid, th["client_id"])))

print("\n== TELEGRAM НЕ СЛОМАЛСЯ ==")
tg_client = database.get_or_create_client(bid2, tg_user_id=777, name="Иван")
reply = botcore.handle_message(bid2, 777, "Иван", "Здравствуйте, нужен букет")
check("бот отвечает как прежде", bool(reply), reply)
tm = msgs_of(bid2, tg_client["id"])
check("и помечает канал телеграмом",
      all(m["channel"] == "telegram" for m in tm), tm)
check("заявка из телеграма помечена своим каналом",
      all(o["source"] == "telegram" for o in database.get_client_orders(tg_client["id"], bid2)),
      database.get_client_orders(tg_client["id"], bid2))

print("\n== УДАЛЕНИЕ БИЗНЕСА УНОСИТ И КАНАЛ ==")
database.delete_business(bid)
with sqlite3.connect(os.environ["DB_PATH"]) as db:
    left_t = db.execute("SELECT COUNT(*) FROM ig_threads WHERE business_id=?", (bid,)).fetchone()[0]
    left_s = db.execute("SELECT COUNT(*) FROM ig_seen WHERE business_id=?", (bid,)).fetchone()[0]
check("переписок не осталось", left_t == 0, left_t)
check("отметок сообщений не осталось", left_s == 0, left_s)

print(f"\nИТОГО: {ok} зелёных, {fail} упавших")
sys.exit(1 if fail else 0)
