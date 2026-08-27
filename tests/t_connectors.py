# -*- coding: utf-8 -*-
"""
Проверка коннекторов без выхода в интернет.

Подменяем requests.request фальшивым сервером, который отдаёт такие же ответы,
как настоящие API. Так проверяется то, что мы реально написали: разбор ответов,
пересчёт сумм, статусы, дедупликация, шифрование ключей, обработка ошибок.
"""
import os, sys, json, tempfile, pathlib

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
os.environ["CONNECTOR_HOST_GAP"] = "0"   # без пауз между запросами: сервер поддельный
sys.stdout.reconfigure(encoding="utf-8")
# Корень проекта вычисляется от самого файла: тесты должны запускаться
# из любой папки и на любой машине, а не только там, где их писали.
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import requests
import server, database, connectors, secretbox

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  OK  ", name)
    else:    fail += 1; print("  FAIL", name, extra)


# ---------- поддельный внешний мир ----------
class Resp:
    def __init__(self, payload, status=200, headers=None):
        self._p = payload; self.status_code = status
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    def json(self): return self._p

ROUTES = {}          # (метод, кусок URL) -> ответ или функция
CALLS = []

KW = []

def fake_request(method, url, **kw):
    CALLS.append((method, url)); KW.append(kw)
    for (m, frag), payload in ROUTES.items():
        if m == method and frag in url:
            return Resp(payload() if callable(payload) else payload)
    return Resp({"error": "no route"}, 404)

requests.request = fake_request

# safeurl резолвит домены через DNS — в тесте подменяем на «всё публичное»,
# кроме заведомо внутренних адресов (их проверяем отдельно).
import safeurl
_real_normalize = safeurl.normalize
def fake_normalize(url, require_https=False):
    low = (url or "").lower()
    for bad in ("127.0.0.1", "localhost", "169.254.", "192.168.", "10.0."):
        if bad in low:
            raise safeurl.UnsafeUrl("Этот адрес ведёт во внутреннюю сеть.")
    if "://" not in low:
        url = "https://" + url
    if require_https and url.lower().startswith("http://"):
        raise safeurl.UnsafeUrl("Нужен адрес по https://")
    return url
safeurl.normalize = fake_normalize
import connectors.base as cbase
cbase.safeurl = safeurl

c = TestClient(server.app)
r = c.post("/api/register", json={"name":"Тест-магазин","login":"shop1",
                                  "password":"pass123","consent":True})
bid = r.json()["business_id"]
H = {"X-Auth": r.json()["token"]}

print("\n== каталог источников ==")
cat = c.get("/api/connections", headers=H).json()
ids = [i["id"] for i in cat["items"]]
check("11 коннекторов в каталоге", len(ids) == 11, ids)
check("секреты не утекают в каталог",
      all("credentials" not in i for i in cat["items"]))
check("у каждого есть поля формы", all(i["fields"] for i in cat["items"]))
check("ничего не подключено", all(not i["connected"] for i in cat["items"]))

print("\n== Ozon: подключение и разбор заказов ==")
ROUTES[("POST", "/v3/posting/fbs/list")] = {"result": {"postings": [
    {"posting_number": "0001-1", "status": "delivered",
     "in_process_at": "2026-08-01T10:00:00Z",
     "products": [{"name": "Кофе в зёрнах", "quantity": 2, "price": "900.00"}],
     "financial_data": {"products": [{"price": "900.00", "quantity": 2}]}},
    {"posting_number": "0001-2", "status": "cancelled",
     "in_process_at": "2026-08-02T10:00:00Z",
     "products": [{"name": "Чайник", "quantity": 1, "price": "3000.00"}],
     "financial_data": {"products": [{"price": "3000.00", "quantity": 1}]}},
]}}
r = c.post("/api/connections/ozon", headers=H,
           json={"credentials": {"client_id": "123", "api_key": "secret-key"}})
check("подключение прошло", r.status_code == 200, r.text[:200])
check("сразу загружено 2 заказа", r.json()["synced"]["added"] == 2, r.json())

orders = c.get("/api/orders", headers=H).json()
check("заказы в общей таблице", orders["total"] == 2, orders)
check("оборот 1800 (отменённый не в счёт)", orders["turnover"] == 1800 + 3000, orders["turnover"])
texts = [o["text"] for o in orders["items"]]
check("состав заказа разобран", any("Кофе в зёрнах ×2" in t for t in texts), texts)
statuses = {o["external_id"]: o["status"] for o in orders["items"]}
check("статус доставлен -> выполнен", statuses.get("ozon:0001-1") == "выполнен", statuses)
check("статус отменён -> отменён", statuses.get("ozon:0001-2") == "отменён", statuses)

fin = c.get("/api/finance", headers=H).json()
check("доход только по доставленному = 1800", fin["summary"]["income"] == 1800, fin["summary"])

print("\n== повторная синхронизация не создаёт дублей ==")
r = c.post("/api/connections/ozon/sync", headers=H).json()
check("добавлено 0", r["added"] == 0, r)
orders2 = c.get("/api/orders", headers=H).json()
check("заказов по-прежнему 2", orders2["total"] == 2, orders2["total"])
check("доход не задвоился", c.get("/api/finance", headers=H).json()["summary"]["income"] == 1800)

print("\n== смена статуса подтягивается ==")
ROUTES[("POST", "/v3/posting/fbs/list")] = {"result": {"postings": [
    {"posting_number": "0001-2", "status": "delivered",
     "in_process_at": "2026-08-02T10:00:00Z",
     "products": [{"name": "Чайник", "quantity": 1, "price": "3000.00"}],
     "financial_data": {"products": [{"price": "3000.00", "quantity": 1}]}},
]}}
c.post("/api/connections/ozon/sync", headers=H)
statuses = {o["external_id"]: o["status"] for o in c.get("/api/orders", headers=H).json()["items"]}
check("отменённый стал выполненным", statuses.get("ozon:0001-2") == "выполнен", statuses)
check("доход пересчитан до 4800",
      c.get("/api/finance", headers=H).json()["summary"]["income"] == 4800)

print("\n== ключи хранятся зашифрованно ==")
row = database.get_connection(bid, "ozon", with_secrets=True)
blob = row["credentials"]
check("в базе не открытый текст", "secret-key" not in blob, blob[:40])
check("расшифровывается обратно",
      json.loads(secretbox.open_(blob))["api_key"] == "secret-key")
check("наружу не отдаётся", "credentials" not in c.get("/api/connections", headers=H).json()["items"][0])

print("\n== Wildberries: продажи и возвраты ==")
ROUTES[("GET", "statistics-api.wildberries.ru")] = [
    {"saleID": "S001", "date": "2026-08-03T12:00:00", "subject": "Футболка",
     "supplierArticle": "ART-1", "forPay": 1200},
    {"saleID": "R002", "date": "2026-08-04T12:00:00", "subject": "Футболка",
     "supplierArticle": "ART-1", "forPay": -1200},
]
r = c.post("/api/connections/wildberries", headers=H,
           json={"credentials": {"token": "wb-token"}})
check("WB подключён", r.status_code == 200, r.text[:200])
fin = c.get("/api/finance", headers=H).json()["summary"]
check("продажа +1200 в доход", fin["income"] == 4800 + 1200, fin)
check("возврат -1200 в расход", fin["expense"] == 1200, fin)

print("\n== ЮKassa: платежи ==")
ROUTES[("GET", "api.yookassa.ru/v3/payments")] = {"items": [
    {"id": "pay-1", "amount": {"value": "2500.00"}, "captured_at": "2026-08-05T09:00:00Z",
     "description": "Заказ на сайте", "payment_method": {"title": "Банковская карта"}},
]}
ROUTES[("GET", "api.yookassa.ru/v3/refunds")] = {"items": []}
r = c.post("/api/connections/yookassa", headers=H,
           json={"credentials": {"shop_id": "999", "secret_key": "live_x"}})
check("ЮKassa подключена", r.status_code == 200, r.text[:200])
check("платёж стал доходом",
      c.get("/api/finance", headers=H).json()["summary"]["income"] == 6000 + 2500)

print("\n== Bitrix24: сделки и контакты ==")
ROUTES[("POST", "/rest/1/hook/crm.deal.fields")] = {"result": {"TITLE": {}}}
ROUTES[("POST", "/rest/1/hook/crm.deal.list")] = {"result": [
    {"ID": "10", "TITLE": "Ремонт квартиры", "OPPORTUNITY": "150000",
     "STAGE_ID": "C1:WON", "DATE_CREATE": "2026-08-06T10:00:00+03:00", "CONTACT_ID": "77"},
]}
ROUTES[("POST", "/rest/1/hook/crm.contact.get")] = {"result": {
    "ID": "77", "NAME": "Иван", "LAST_NAME": "Петров",
    "PHONE": [{"VALUE": "+79990001122"}]}}
r = c.post("/api/connections/bitrix24", headers=H,
           json={"credentials": {"webhook": "https://co.bitrix24.ru/rest/1/hook/"}})
check("Bitrix подключён", r.status_code == 200, r.text[:200])
clients = c.get("/api/clients", headers=H).json()
names = [x["name"] for x in clients["items"]]
check("контакт попал в CRM", "Иван Петров" in names, names)
check("телефон подтянулся",
      any(x.get("phone") == "+79990001122" for x in clients["items"]), clients["items"])
check("выигранная сделка = доход 150000",
      c.get("/api/finance", headers=H).json()["summary"]["income"] == 8500 + 150000)

print("\n== ошибки показываются по-человечески ==")
ROUTES[("GET", "api.stripe.com/v1/charges")] = None
def bad_request(method, url, **kw):
    CALLS.append((method, url))
    if "stripe" in url:
        return Resp({"error": {"message": "Invalid API Key"}}, 401)
    return fake_request(method, url, **kw)
requests.request = bad_request
r = c.post("/api/connections/stripe", headers=H, json={"credentials": {"secret_key": "sk_bad"}})
check("неверный ключ -> 400", r.status_code == 400, r.status_code)
check("текст понятен человеку", "Ключ доступа не подошёл" in r.json().get("detail",""),
      r.json().get("detail"))
check("подключение не сохранилось", database.get_connection(bid, "stripe") is None)
requests.request = fake_request

print("\n== SSRF в коннекторах ==")
for bad in ["http://127.0.0.1:8080/rest/1/x/", "https://169.254.169.254/rest/1/x/"]:
    r = c.post("/api/connections/bitrix24", headers=H, json={"credentials": {"webhook": bad}})
    check(f"вебхук заблокирован {bad[:30]}", r.status_code == 400, r.status_code)
r = c.post("/api/connections/woocommerce", headers=H,
           json={"credentials": {"url": "http://192.168.0.5", "key": "ck", "secret": "cs"}})
check("адрес магазина во внутренней сети заблокирован", r.status_code == 400, r.status_code)

print("\n== ИИ видит подключённые системы ==")
import context_engine
block = context_engine._sources_block(bid)
check("Ozon в контексте", "Ozon" in block, block[:120])
check("Bitrix24 в контексте", "Bitrix24" in block, block[:160])
snap = server._biz_snapshot(bid)
check("снимок учитывает импортированные заказы", "заказов всего 4" in snap, snap)

print("\n== отключение источника ==")
r = c.post("/api/connections/ozon/delete", headers=H)
check("отключено", r.status_code == 200)
check("подключения нет", database.get_connection(bid, "ozon") is None)
check("но заказы остались", c.get("/api/orders", headers=H).json()["total"] == 4)

print("\n== устойчивость связи ==")
import connectors.base as cb

WB_SALE = [{"saleID": "S-9001", "forPay": 1000, "date": "2026-08-01T10:00:00",
            "subject": "Платье", "supplierArticle": "A1"}]


def _sequence(*responses):
    """Поддельный сервер, который отдаёт заготовленные ответы по очереди."""
    box = list(responses)
    seen = []

    def handler(method, url, **kw):
        seen.append((method, url, kw))
        item = box.pop(0) if box else Resp(WB_SALE)
        if isinstance(item, Exception):
            raise item
        return item
    handler.seen = seen
    return handler


# 429 с Retry-After: раньше это роняло весь прогон, теперь просто ждём и повторяем.
h = _sequence(Resp({"message": "too many"}, 429, {"Retry-After": "0"}), Resp(WB_SALE))
requests.request = h
try:
    got = cb.request("GET", "https://statistics-api.wildberries.ru/api/v1/supplier/sales")
    check("429 -> повтор и успех", got == WB_SALE, got)
    check("повтор был ровно один", len(h.seen) == 2, len(h.seen))
except Exception as e:
    check("429 -> повтор и успех", False, e)

# 5xx у сервиса — тоже рябь, а не поломка настройки.
h = _sequence(Resp({"m": "oops"}, 502, {"Retry-After": "0"}),
              Resp({"m": "oops"}, 500, {"Retry-After": "0"}), Resp(WB_SALE))
requests.request = h
try:
    got = cb.request("GET", "https://statistics-api.wildberries.ru/api/v1/supplier/sales")
    check("5xx -> повтор до успеха", got == WB_SALE, got)
except Exception as e:
    check("5xx -> повтор до успеха", False, e)

# Обрыв связи — повторяем.
h = _sequence(requests.Timeout("timeout"), Resp(WB_SALE))
requests.request = h
try:
    got = cb.request("GET", "https://statistics-api.wildberries.ru/api/v1/supplier/sales")
    check("таймаут -> повтор и успех", got == WB_SALE, got)
except Exception as e:
    check("таймаут -> повтор и успех", False, e)

# А вот неверный ключ повторять бессмысленно — ключ не станет верным.
h = _sequence(Resp({"m": "denied"}, 401), Resp(WB_SALE))
requests.request = h
try:
    cb.request("GET", "https://statistics-api.wildberries.ru/api/v1/supplier/sales")
    check("401 не повторяется", False, "ошибки не было")
except cb.ConnectorError:
    check("401 не повторяется", len(h.seen) == 1, len(h.seen))

# Если сервис просит ждать дольше, чем человек готов терпеть, — не держим его.
h = _sequence(Resp({"m": "later"}, 429, {"Retry-After": "600"}), Resp(WB_SALE))
requests.request = h
try:
    cb.request("GET", "https://statistics-api.wildberries.ru/api/v1/supplier/sales")
    check("слишком долгая пауза -> честная ошибка", False, "ошибки не было")
except cb.ConnectorError as e:
    check("слишком долгая пауза -> честная ошибка", len(h.seen) == 1, len(h.seen))
    check("текст про ожидание", "подожд" in str(e).lower(), str(e))

requests.request = fake_request

print("\n== очередь к сервисам с редким лимитом ==")
_gap_was = cb.HOST_GAP
cb.HOST_GAP = 0.2
cb._next_ok.clear()
h = _sequence(Resp(WB_SALE), Resp(WB_SALE))
requests.request = h
import time as _t
_t0 = _t.monotonic()
cb.request("GET", "https://example-shop-api.test/orders")
cb.request("GET", "https://example-shop-api.test/orders")
check("подряд идущие запросы разведены паузой", _t.monotonic() - _t0 >= 0.15,
      round(_t.monotonic() - _t0, 3))

# Хост с редким лимитом: человек не должен стоять минуту у спиннера.
cb._next_ok["slow-api.test"] = _t.monotonic() + 600
try:
    cb.request("GET", "https://slow-api.test/x")
    check("нетерпеливый поток не ждёт минутами", False, "ошибки не было")
except cb.ConnectorError as e:
    check("нетерпеливый поток не ждёт минутами", "через минуту" in str(e), str(e))
cb.HOST_GAP = _gap_was
cb._next_ok.clear()
requests.request = fake_request

print("\n== проверка ключа Wildberries дешёвая ==")
KW.clear()
ROUTES[("GET", "/supplier/sales")] = WB_SALE
r = c.post("/api/connections/wildberries", headers=H, json={"credentials": {"token": "wb-token"}})
check("подключилось", r.status_code == 200, r.text[:200])
first = (KW[0].get("params") or {}) if KW else {}
check("на проверку ключа не заказывается вся история", first.get("flag") == 1, first)

print("\n== ключ с невидимым мусором ==")
r = c.post("/api/connections/wildberries", headers=H,
           json={"credentials": {"token": "  wb-token\n"}})
check("пробелы и перенос строки не мешают", r.status_code == 200, r.text[:200])
row = database.get_connection(bid, "wildberries", with_secrets=True)
check("ключ сохранён без мусора", connectors._creds(row).get("token") == "wb-token",
      repr(connectors._creds(row).get("token")))
r = c.post("/api/connections/wildberries", headers=H,
           json={"credentials": {"token": "wb-token" + chr(160)}})
row = database.get_connection(bid, "wildberries", with_secrets=True)
check("неразрывный пробел вычищен", connectors._creds(row).get("token") == "wb-token",
      repr(connectors._creds(row).get("token")))

print("\n== два обновления одного источника разом ==")
connectors._busy.add((bid, "wildberries"))
res = connectors.sync(bid, "wildberries")
check("второй прогон не запускается", res.get("busy") is True, res)
check("и это не ошибка", res.get("ok") is True and not res.get("error"), res)
connectors._busy.discard((bid, "wildberries"))
res = connectors.sync(bid, "wildberries")
check("после освобождения работает", res.get("ok") and not res.get("busy"), res)
check("замок отпущен", (bid, "wildberries") not in connectors._busy)

r = c.post("/api/connections/wildberries/sync", headers=H)
check("кнопка «обновить» отвечает", r.status_code == 200 and r.json().get("ok"), r.text[:160])

print("\n== подключение не ждёт первую выгрузку ==")
os.environ.pop("DISABLE_SYNC_WORKER", None)
slow = {"n": 0}


def slow_sales(method, url, **kw):
    slow["n"] += 1
    _t.sleep(0.6)                      # как будто у продавца большая история
    return Resp(WB_SALE)


requests.request = slow_sales
database.delete_connection(bid, "wildberries")
_t0 = _t.monotonic()
r = c.post("/api/connections/wildberries", headers=H, json={"credentials": {"token": "wb-token"}})
elapsed = _t.monotonic() - _t0
d = r.json()
check("ответ пришёл сразу", elapsed < 1.4, round(elapsed, 2))
check("сказано, что данные ещё едут", (d.get("synced") or {}).get("pending") is True, d)
check("подключение уже сохранено", d.get("connection") is not None)
for _ in range(40):
    _t.sleep(0.1)
    if (database.get_connection(bid, "wildberries") or {}).get("last_sync_at"):
        break
row = database.get_connection(bid, "wildberries") or {}
check("фоновая выгрузка отработала следом", bool(row.get("last_sync_at")), row.get("last_sync_at"))
check("и без ошибки", not row.get("last_error"), row.get("last_error"))
os.environ["DISABLE_SYNC_WORKER"] = "1"
requests.request = fake_request


print("\n== read-only: подключать и синхронизировать нельзя ==")
import trial
trial.launch(bid); trial.disable(bid)
r = c.post("/api/connections/wildberries/sync", headers=H)
check("402 на синхронизацию", r.status_code == 402, r.status_code)
r = c.post("/api/connections/ozon", headers=H, json={"credentials": {"client_id":"1","api_key":"2"}})
check("402 на подключение", r.status_code == 402, r.status_code)
check("список источников читается", c.get("/api/connections", headers=H).status_code == 200)

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
