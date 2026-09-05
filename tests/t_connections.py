# -*- coding: utf-8 -*-
"""
Подключения: одна дверь, пять состояний и ни одной фальшивой галочки.

Проверяем три вещи:
  1) единый интерфейс — у любого подключения один и тот же паспорт: provider,
     status, connected_at, permissions, configuration, last_sync, error;
  2) состояние собирается из фактов — «подключено» только после живой проверки
     доступа, «нужен доступ» отдельно от «ошибка», «синхронизация» видна;
  3) то, чего нет, честно показано как «не подключено» и отказывается
     подключаться. Фальшивая галочка хуже пустого места: по ней владелец
     решает, что данные идут.
"""
import os, sys, json, tempfile, pathlib, sqlite3

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
sys.stdout.reconfigure(encoding="utf-8")
# Корень проекта вычисляется от самого файла: тесты должны запускаться
# из любой папки и на любой машине, а не только там, где их писали.
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import requests
import server, database, connections, connectors

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


# ── поддельный внешний мир ─────────────────────────────────────────────────
class Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.headers = {}
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self):
        return self._p


MODE = {"ozon": "ok"}       # ok | fail | auth


def fake_request(method, url, **kw):
    if "ozon" in url:
        if MODE["ozon"] == "auth":
            return Resp({"message": "invalid token"}, 401)
        if MODE["ozon"] == "fail":
            return Resp({"message": "boom"}, 500)
        return Resp({"result": {"postings": [
            {"posting_number": "T-1", "status": "delivered",
             "in_process_at": "2026-08-01T10:00:00Z",
             "products": [{"name": "Кофе", "quantity": 1, "price": "900.00"}],
             "financial_data": {"products": [{"price": "900.00", "quantity": 1}]}}]}})
    return Resp({"error": "no route"}, 404)


requests.request = fake_request
import connectors.base as cbase
cbase.TRIES = 1                      # в тесте повторять нечего: ответ детерминирован

c = TestClient(server.app)
r = c.post("/api/register", json={"name": "Связь", "login": "connone",
                                  "password": "pass123", "consent": True})
bid, H = r.json()["business_id"], {"X-Auth": r.json()["token"]}
r2 = c.post("/api/register", json={"name": "Сосед", "login": "conntwo",
                                   "password": "pass123", "consent": True})
bid2, H2 = r2.json()["business_id"], {"X-Auth": r2.json()["token"]}


def catalog(h=None):
    return c.get("/api/connections/catalog", headers=h or H).json()


def item(provider, h=None):
    return c.get(f"/api/connections/state/{provider}", headers=h or H).json()


def all_items(d):
    return [i for cat in d["categories"] for i in cat["items"]]


PASSPORT = ("provider", "status", "connected_at", "permissions",
            "configuration", "last_sync", "error")

print("== ЕДИНЫЙ ИНТЕРФЕЙС ==")
d = catalog()
items = all_items(d)
# «Разум» идёт первым намеренно: без модели VELOR считает числа, но не делает
# выводов, а выводы и есть продукт. Пока этой категории не было, у владельца не
# было ни одного места в кабинете, где видно — отвечает модель или нет.
check("категории: разум, общение, данные",
      [x["key"] for x in d["categories"]] == ["mind", "communication", "data"],
      [x["key"] for x in d["categories"]])
check("модель — самое важное подключение и стоит первой",
      d["categories"][0]["items"][0]["provider"] == "model",
      [i["provider"] for i in d["categories"][0]["items"]])
_model = d["categories"][0]["items"][0]
check("состояние модели — не «есть ключ», а живой ответ",
      "_probe" in open(os.path.join(ROOT, "connections.py"), encoding="utf-8").read())

print("\n== БАЗОВЫЙ ИИ РАБОТАЕТ СРАЗУ, СВОЙ КЛЮЧ — ПО ЖЕЛАНИЮ ==")
import ai as _ai, secretbox as _sb, json as _json2
# Смысл всей конструкции: чтобы ИИ отвечал, бизнесу не нужно ничего вводить.
# Базовый ключ VELOR общий и лежит в .env на сервере.
# В тестовом окружении .env не загружается, и базового ключа тут нет — так и
# должно быть: проверки не имеют права зависеть от ключей рабочей машины.
# Поэтому проверяем УСТРОЙСТВО, а не наличие ключа.
_base = [n for n, _ in _ai._platform_providers()]
check("базовый ключ — понятие продукта, а не бизнеса",
      callable(_ai._platform_providers))
check("без своего ключа бизнес пользуется базовым",
      _ai.own_key(bid) is None
      and [n for n, _ in _ai._all_providers(bid)] == _base)
check("карточка объясняет, что настраивать ничего не надо",
      "базовом ключе" in (_model.get("howto") or ""), _model.get("howto"))
check("свой ключ можно подключить", bool(_model.get("fields")))
check("ключ в форме помечен секретным",
      any(f.get("secret") for f in _model["fields"] if f["key"] == "key"), _model["fields"])

# Негодный ключ не должен сохраняться: принять его — значит выключить бизнесу
# ИИ и не сказать об этом.
try:
    connections.connect(bid, "model", {"provider": "claude", "key": "sk-ant-api03-broken"})
    check("негодный ключ не принимается", False, "приняли")
except Exception:
    check("негодный ключ не принимается", True)
check("и в базе после отказа ничего не осталось",
      database.get_connection(bid, "model") is None)

# Дальше — с заведомо «рабочим» ключом в обход проверки: живого ключа в тестах
# нет, а проверить надо предпочтение и изоляцию, а не сеть.
database.save_connection(
    bid, "model",
    credentials_blob=_sb.seal(_json2.dumps({"provider": "gigachat", "key": "TESTKEY"})),
    status="connected", permissions=[], config={})
check("свой ключ читается обратно", _ai.own_key(bid) == ("gigachat", "TESTKEY"))
_mine = [n for n, _ in _ai._all_providers(bid)]
check("свой ключ идёт первым", _mine and _mine[0].startswith("свой"), _mine)
check("базовые остаются страховкой за ним", _mine[1:] == _base, (_mine, _base))
# Бизнес передаётся контекстом: зовущих модель мест больше сорока, и
# протаскивать business_id через каждую сигнатуру не стали.
with _ai.for_business(bid):
    check("контекст бизнеса подхватывается без аргументов",
          [n for n, _ in _ai._all_providers()][0].startswith("свой"))
check("вне контекста чужой ключ не подхватывается",
      [n for n, _ in _ai._all_providers()] == _base)

# Секрет наружу не отдаётся никогда — ни в каталоге, ни в состоянии.
_blob = _json2.dumps(connections.catalog(bid), ensure_ascii=False)
check("ключ не утекает в каталог", "TESTKEY" not in _blob)
_row = database.get_connection(bid, "model")
check("ключ не отдаётся вместе с записью подключения", "credentials" not in (_row or {}))
_card = connections.catalog(bid)["categories"][0]["items"][0]
check("владельцу показан только хвост ключа",
      "…TKEY" in _json2.dumps(_card.get("configuration"), ensure_ascii=False),
      _card.get("configuration"))

# Соседу чужой ключ не достаётся.
check("свой ключ не виден соседу", _ai.own_key(bid2) is None)

print("\n== СВОЙ КЛЮЧ — СВОЙ СЧЁТ, ЛИМИТ НЕ ЕГО ==")
# Лимит запросов защищает БАЗОВЫЙ ключ VELOR — тот, за который платит
# платформа. Кто спрашивает своим ключом, тратит свои запросы и свои деньги:
# считать их за него незачем.
import ratelimit as _rl, config as _cfg

# Ключ у бизнеса свой (сохранён выше) — лимит расходоваться не должен.
_before = len(_rl._events.get("ask:testclient", []))
for _ in range(_cfg.ASK_MAX + 3):
    r = c.post("/api/ask", headers=H, json={"question": "тест"})
    if r.status_code == 429:
        break
check("со своим ключом лимит не срабатывает", r.status_code != 429, r.status_code)

# А без своего ключа — срабатывает: базовый ключ надо беречь.
connections.disconnect(bid, "model")
_hit = None
for _ in range(_cfg.ASK_MAX + 5):
    r = c.post("/api/ask", headers=H, json={"question": "тест"})
    if r.status_code == 429:
        _hit = r
        break
check("без своего ключа лимит срабатывает", _hit is not None,
      "прошло без 429 за %d запросов" % (_cfg.ASK_MAX + 5))
if _hit is not None:
    check("и объясняет, сколько ждать", "Подождите" in _hit.json().get("detail", ""),
          _hit.json())

# Порядок проверок: сперва «кто спрашивает», потом лимит. Побочная польза —
# просроченный токен больше не съедает попытку, а сразу получает 401.
# Заголовки HTTP только ASCII — кириллица в токене падает ещё в httpx.
_stale = c.post("/api/ask", headers={"X-Auth": "broken.token.value"},
                json={"question": "тест"})
check("просроченный токен получает 401, а не 429", _stale.status_code == 401,
      _stale.status_code)

# Отключение возвращает на базовый — бизнес не остаётся без ИИ.
connections.disconnect(bid, "model")
check("после отключения бизнес возвращается на базовый",
      _ai.own_key(bid) is None
      and [n for n, _ in _ai._all_providers(bid)] == _base)
for want, cat in (("telegram", "communication"), ("instagram", "communication"),
                  ("website", "communication"), ("whatsapp", "communication"),
                  ("bank", "data"), ("google_drive", "data"), ("google_sheets", "data"),
                  ("excel", "data")):
    got = next((i for i in items if i["provider"] == want), None)
    check(f"{want} есть и лежит в «{cat}»", got and got["category"] == cat, got)
check("CRM представлена настоящими системами",
      {"bitrix24", "amocrm", "hubspot"} <= {i["provider"] for i in items},
      [i["provider"] for i in items])
for i in items:
    check(f"{i['provider']}: паспорт полный", all(k in i for k in PASSPORT),
          [k for k in PASSPORT if k not in i])
check("статусы — только из пяти разрешённых",
      {i["status"] for i in items} <= set(connections.STATUSES), {i["status"] for i in items})
check("каждый статус объяснён словами",
      all(s["label"] and s["means"] for s in d["statuses"]), d["statuses"])
check("все подключения — один и тот же интерфейс",
      all(isinstance(a, connections.Adapter) for a in connections.ADAPTERS.values()))
check("и одинаково отвечают на вопрос «что умеешь»",
      all(isinstance(a.can_connect(), bool) and isinstance(a.can_sync(), bool)
          for a in connections.ADAPTERS.values()))
check("неизвестное подключение — 404",
      c.get("/api/connections/state/dragon", headers=H).status_code == 404)

print("\n== НИЧЕГО НЕ ПРИТВОРЯЕТСЯ ПОДКЛЮЧЁННЫМ ==")
check("у нового бизнеса всё отключено",
      all(i["status"] == connections.DISCONNECTED for i in items),
      [(i["provider"], i["status"]) for i in items])
check("и подключённых ноль", d["summary"]["connected"] == 0, d["summary"])
check("зато видно, чего ещё нет", d["summary"]["planned"] >= 5, d["summary"])
for p in ("whatsapp", "website", "google_drive", "google_sheets", "bank"):
    r = c.post(f"/api/connections/{p}/connect", headers=H, json={"config": {"x": "1"}})
    check(f"{p}: подключиться нельзя", r.status_code == 422, r.status_code)
    check(f"{p}: и сказано почему",
          "ещё не написана" in (r.json().get("detail") or ""), r.json())
    check(f"{p}: остался отключённым", item(p)["status"] == connections.DISCONNECTED)
# Instagram написан, но включается ключами приложения Meta на стороне сервера.
# Здесь их нет — и отказ обязан назвать именно эту причину, а не «нет интеграции»:
# чинить владельцу нужно разные вещи.
r = c.post("/api/connections/instagram/connect", headers=H, json={"config": {"x": "1"}})
check("instagram: формой с полями не подключить", r.status_code == 422, r.status_code)
check("instagram: и причина — незаданные ключи приложения",
      "сервере" in (r.json().get("detail") or ""), r.json())
check("instagram: остался отключённым",
      item("instagram")["status"] == connections.DISCONNECTED)
check("instagram: вход идёт на стороне сервиса", item("instagram")["needs_login"],
      item("instagram"))
check("несуществующий сервис не подключить",
      c.post("/api/connections/dragon/connect", headers=H,
             json={"config": {}}).status_code == 422)

print("\n== ПРАВА ВИДНЫ ДО СОГЛАСИЯ ==")
for p in ("instagram", "ozon", "telegram"):
    check(f"{p}: права объявлены заранее", len(item(p)["permissions"]) >= 1, item(p))
check("у неподключённого настройка пуста", item("ozon")["configuration"] == {})

print("\n== TELEGRAM: СТАТУС ИЗ ФАКТА, А НЕ ИЗ ФЛАЖКА ==")
check("без токена — не подключено", item("telegram")["status"] == connections.DISCONNECTED)
database.update_business(bid, tg_bot_token="123456:AA-real-token-xyz")
tg = item("telegram")
check("с токеном — подключено", tg["status"] == connections.CONNECTED, tg["status"])
check("и видно, какой это бот — по хвосту токена",
      tg["configuration"].get("Бот") == "…en-xyz", tg["configuration"])
check("токен целиком наружу не уходит",
      "123456:AA-real-token-xyz" not in json.dumps(tg, ensure_ascii=False), tg["configuration"])
check("подключать/отключать здесь нельзя — это делается в разделе бота",
      not tg["can_connect"] and not tg["can_disconnect"], tg)
database.update_business(bid, tg_bot_token=None)
check("токен убрали — снова не подключено",
      item("telegram")["status"] == connections.DISCONNECTED)

print("\n== ЖИВОЕ ПОДКЛЮЧЕНИЕ ==")
MODE["ozon"] = "ok"
r = c.post("/api/connections/ozon/connect", headers=H,
           json={"config": {"client_id": "42", "api_key": "secret-key-value"}})
check("подключение прошло", r.status_code == 200, r.text[:200])
oz = item("ozon")
check("статус подключено", oz["status"] == connections.CONNECTED, oz["status"])
check("дата подключения записана", bool(oz["connected_at"]), oz)
check("права записаны в само подключение", "читать заказы" in oz["permissions"], oz["permissions"])
check("настройка показывает несекретное", oz["configuration"], oz["configuration"])
check("и НЕ содержит ключ",
      "secret-key-value" not in json.dumps(oz, ensure_ascii=False), oz["configuration"])
check("секретов нет и во всём каталоге",
      "secret-key-value" not in json.dumps(catalog(), ensure_ascii=False))
check("данные действительно пришли",
      c.get("/api/orders", headers=H).json()["items"][0]["external_id"] == "ozon:T-1",
      c.get("/api/orders", headers=H).json()["items"][:1])
check("время синхронизации проставлено", bool(oz["last_sync"]), oz)
check("ошибки нет", oz["error"] is None, oz["error"])

print("\n== ПЯТЬ СОСТОЯНИЙ ==")
MODE["ozon"] = "fail"
c.post("/api/connections/ozon/sync", headers=H)
bad = item("ozon")
check("сбой сервиса → ERROR", bad["status"] == connections.ERROR, bad["status"])
check("и причина показана", bool(bad["error"]), bad)
check("подключение при этом не исчезло", bool(bad["connected_at"]))

MODE["ozon"] = "auth"
c.post("/api/connections/ozon/sync", headers=H)
na = item("ozon")
check("отказ по ключу → REQUIRES_AUTH", na["status"] == connections.REQUIRES_AUTH, na["status"])
check("это не то же самое, что ошибка",
      na["status"] != connections.ERROR and "ключ" in (na["error"] or "").lower(), na["error"])
check("переподключиться предлагается", na["can_connect"] is True, na)

connectors._busy.add((bid, "ozon"))
check("идёт выгрузка → SYNCING", item("ozon")["status"] == connections.SYNCING)
connectors._busy.discard((bid, "ozon"))
MODE["ozon"] = "ok"
c.post("/api/connections/ozon/sync", headers=H)
check("после удачной синхронизации снова CONNECTED",
      item("ozon")["status"] == connections.CONNECTED, item("ozon")["status"])

print("\n== ПЛОХОЙ КЛЮЧ НЕ СОЗДАЁТ ПОДКЛЮЧЕНИЯ ==")
MODE["ozon"] = "auth"
r = c.post("/api/connections/ozon/disconnect", headers=H)
check("сначала отключаем", r.status_code == 200 and
      r.json()["state"]["status"] == connections.DISCONNECTED, r.text[:200])
r = c.post("/api/connections/ozon/connect", headers=H,
           json={"config": {"client_id": "42", "api_key": "bad"}})
check("подключение с неверным ключом отклонено", r.status_code == 400, r.status_code)
check("и ничего не сохранилось",
      item("ozon")["status"] == connections.DISCONNECTED
      and item("ozon")["connected_at"] is None, item("ozon"))
MODE["ozon"] = "ok"
c.post("/api/connections/ozon/connect", headers=H,
       json={"config": {"client_id": "42", "api_key": "secret-key-value"}})

print("\n== ОТКЛЮЧЕНИЕ ==")
orders_before = len(c.get("/api/orders", headers=H).json()["items"])
r = c.post("/api/connections/ozon/disconnect", headers=H)
check("отключено", r.json()["state"]["status"] == connections.DISCONNECTED, r.text[:200])
check("ошибка и дата подключения сброшены",
      item("ozon")["error"] is None and item("ozon")["connected_at"] is None, item("ozon"))
check("но загруженные данные остались",
      len(c.get("/api/orders", headers=H).json()["items"]) == orders_before)
check("что нельзя отключить — то и не отключается",
      c.post("/api/connections/telegram/disconnect", headers=H).status_code == 422)

print("\n== ФАЙЛОВЫЕ ИСТОЧНИКИ ЧЕСТНЫ ==")
st = item("statement")
check("выписка не притворяется подключением",
      st["status"] == connections.DISCONNECTED and not st["can_connect"], st["status"])
check("но путь показан", st["manage_href"] == "import.html", st)
conn = sqlite3.connect(str(TMP / "t.db"))
conn.execute("INSERT INTO finance_imports (business_id, filename, source, total, added, skipped) "
             "VALUES (?,?,?,?,?,?)", (bid, "vypiska.csv", "csv", 10, 9, 1))
conn.commit(); conn.close()
st = item("statement")
check("последняя загрузка видна как последняя синхронизация", bool(st["last_sync"]), st)
check("и сколько записей она принесла", st["items_total"] == 9, st)
check("и какой это был файл", "vypiska.csv" in (st["note"] or ""), st["note"])

print("\n== ЧУЖОЕ ==")
mine = {i["provider"]: i["status"] for i in all_items(catalog())}
theirs = {i["provider"]: i["status"] for i in all_items(catalog(H2))}
check("сосед не видит наших подключений",
      all(v == connections.DISCONNECTED for v in theirs.values()), theirs)
check("и его каталог того же размера", len(theirs) == len(mine))
# Возвращаем подключение, чтобы проверить главное: чужие действия его не трогают.
c.post("/api/connections/ozon/connect", headers=H,
       json={"config": {"client_id": "42", "api_key": "secret-key-value"}})
c.post("/api/connections/ozon/disconnect", headers=H2)      # у соседа его нет
check("чужое отключение не трогает наше подключение",
      item("ozon", H)["status"] == connections.CONNECTED, item("ozon", H)["status"])
check("и сосед по-прежнему видит у себя пусто",
      item("ozon", H2)["status"] == connections.DISCONNECTED, item("ozon", H2))

print("\n== READ-ONLY ПОСЛЕ ТРИАЛА ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("подключать нельзя",
      c.post("/api/connections/ozon/connect", headers=H,
             json={"config": {"client_id": "1", "api_key": "k"}}).status_code == 402)
check("отключать нельзя",
      c.post("/api/connections/ozon/disconnect", headers=H).status_code == 402)
check("но каталог виден",
      c.get("/api/connections/catalog", headers=H).status_code == 200)
check("и карточка тоже", c.get("/api/connections/state/ozon", headers=H).status_code == 200)

print("\n== СТАРЫЙ ЭКРАН НЕ СЛОМАН ==")
old = c.get("/api/connections", headers=H).json()
check("каталог источников по-прежнему отдаётся", len(old.get("items", [])) == 11, len(old.get("items", [])))
check("и в нём нет секретов", "secret-key-value" not in json.dumps(old, ensure_ascii=False))

print(f"\n=== ИТОГО: успешно {ok}, провалено {fail} ===")
sys.exit(1 if fail else 0)
