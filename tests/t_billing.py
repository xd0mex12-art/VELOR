# -*- coding: utf-8 -*-
"""
Тарифы, подписка, пробный период и оплата.

Здесь проверяется то, чего нельзя проверить глазами и что ломается тихо:
цена, доступ и деньги. Четыре темы, и каждая отвечает на один вопрос.

  1. Каталог один. Цена, которую видит владелец, не зависит от того, на какую
     страницу он попал. Раньше каталогов было три, и они не совпадали.
  2. Доступ решает backend. Фронт можно обойти, отправив запрос руками, —
     значит фронт не защита, и проверять надо в эндпоинте.
  3. Пробный период считает сервер. Обновление страницы его не продлевает, а
     окончание не удаляет данные.
  4. Подписку включает только подтверждённая оплата. Ни возврат на сайт, ни
     повторный webhook не должны дать лишнего дня доступа.
"""
import os, sys, json, tempfile, pathlib, datetime

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
# Тесту нужно много компаний подряд; защита от массовой регистрации здесь
# только мешает — её проверяет t_auth, а не этот файл.
os.environ["REGISTER_MAX"] = "200"
os.environ["ASK_MAX"] = "500"
os.environ["YOOKASSA_SHOP_ID"] = "test-shop"
os.environ["YOOKASSA_SECRET_KEY"] = "test-key"
sys.stdout.reconfigure(encoding="utf-8")
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, trial, plans, billing

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


# ── поддельная ЮKassa ──────────────────────────────────────────────────────
# Настоящая сеть в тестах не участвует: проверяем СВОЮ логику — что мы просим,
# чему верим и что делаем с ответом.
YK = {"payments": {}, "next": 1, "created": [], "fetches": 0, "down": False}


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http %s" % self.status_code)


def fake_post(url, auth=None, json=None, timeout=None, headers=None):
    YK["created"].append({"body": json, "headers": headers or {}, "auth": auth})
    pid = "yk-%d" % YK["next"]; YK["next"] += 1
    YK["payments"][pid] = {"id": pid, "status": "pending", "paid": False,
                           "amount": json["amount"]}
    return Resp({"id": pid, "status": "pending",
                 "confirmation": {"confirmation_url": "https://pay.test/" + pid}})


def fake_get(url, auth=None, timeout=None):
    YK["fetches"] += 1
    if YK["down"]:
        raise RuntimeError("сеть недоступна")
    pid = url.rsplit("/", 1)[-1]
    if pid not in YK["payments"]:
        return Resp({"error": "not found"}, 404)
    return Resp(YK["payments"][pid])


def pay_at_provider(pid, amount=None):
    """Как будто человек оплатил на стороне ЮKassa."""
    p = YK["payments"][pid]
    p["status"] = "succeeded"
    p["paid"] = True
    if amount is not None:
        p["amount"] = {"value": "%d.00" % amount, "currency": "RUB"}


billing.requests.post = fake_post
billing.requests.get = fake_get

c = TestClient(server.app)


def new_business(login, name="Компания"):
    r = c.post("/api/register", json={"name": name, "login": login,
                                      "password": "pass123", "consent": True})
    d = r.json()
    return d["business_id"], {"X-Auth": d["token"]}


bid, H = new_business("payone", "Первый")
bid2, H2 = new_business("paytwo", "Сосед")
OWN = {"X-Auth": c.post("/api/login", json={"login": "testowner",
                                            "password": "s3cret-owner"}).json()["token"]}


def launch(b):
    """Запустить пробный период, как это делает кнопка в кабинете."""
    trial.launch(b)


def subscribe(b, key, days=30):
    trial.activate_subscription(b, plan=key, months=1)
    if days != 30:
        exp = datetime.datetime.utcnow() + datetime.timedelta(days=days)
        database.update_business(b, subscription_expires=exp.strftime("%Y-%m-%d %H:%M:%S"))


# ===========================================================================
print("== КАТАЛОГ ОДИН, И ОН НА СЕРВЕРЕ ==")
# Три каталога с разными ценами (database.PLANS, trial.PLANS, вёрстка
# plans.html) — это гарантированный спор о деньгах с первым же клиентом.
check("тарифов три: START, BUSINESS, NETWORK",
      plans.ORDER == ["start", "business", "network"], plans.ORDER)
check("цены заданы один раз",
      [plans.price(k) for k in plans.ORDER] == [4900, 9900, 19900],
      [plans.price(k) for k in plans.ORDER])
check("trial больше не держит свой каталог", trial.PLANS is plans.PLANS)
check("database больше не держит свой каталог",
      not hasattr(database, "PLANS"))
_html = open(os.path.join(ROOT, "web", "plans.html"), encoding="utf-8").read()
for _p in ("4900", "9900", "19900", "15000", "4 900", "9 900", "19 900"):
    check("во фронте нет зашитой цены «%s»" % _p, _p not in _html)
check("страница берёт тарифы с сервера", "/api/billing" in _html)

_st = c.get("/api/billing", headers=H).json()
check("сервер отдаёт каталог", [p["key"] for p in _st["plans"]] == plans.ORDER,
      [p["key"] for p in _st["plans"]])
check("и те же цены, что в каталоге",
      [p["price"] for p in _st["plans"]] == [4900, 9900, 19900])
check("основной тариф помечен популярным",
      [p["key"] for p in _st["plans"] if p["popular"]] == ["business"])
check("настройка — разовая услуга на 15 000",
      _st["setup"]["price"] == 15000 and "SETUP" in _st["setup"]["name"], _st["setup"])

print("\n== ЧЕСТНОСТЬ КАТАЛОГА ==")
# Продавать несуществующее — быстрый способ потерять первого же клиента.
_net = [p for p in _st["plans"] if p["key"] == "network"][0]
_multi = [f for f in _net["features"] if f["key"] == "multi_unit"]
check("несколько направлений объявлены", len(_multi) == 1)
check("и честно помечены как «готовится», раз кода за ними нет",
      _multi and _multi[0]["ready"] is False, _multi)
check("страница показывает такую возможность иначе", "soon-mark" in _html)
_ready = [f["key"] for p in _st["plans"] for f in p["features"] if f["ready"]]
check("всё остальное в каталоге — существующие возможности",
      set(_ready) <= {"analytics_basic", "ai_director", "insights_auto",
                      "analytics_advanced", "priority_support"}, sorted(set(_ready)))

print("\n== СТАРЫЕ ЗНАЧЕНИЯ ТАРИФА НЕ РОНЯЮТ АККАУНТ ==")
# В базе живут «Старт», starter, pro, пустые значения. Их владельцы ни в чём
# не виноваты, и падать на них нельзя.
for old, want in (("Старт", "start"), ("starter", "start"), ("business", "business"),
                  ("pro", "network"), ("enterprise", "network"), ("", "business"),
                  (None, "business"), ("чепуха", "business")):
    check("«%s» → %s" % (old, want), plans.normalize(old) == want, plans.normalize(old))
# Легаси-колонка businesses.plan заполнена у КАЖДОГО аккаунта («Старт» стоит
# в схеме по умолчанию) и не означает ни одной оплаты. Пока её читали как
# тариф, новый бизнес немедленно оказывался на START, и пробный период,
# обещанный как полный доступ, молча выдавал урезанный продукт.
database.update_business(bid, plan="Старт", subscription_plan="")
check("легаси-колонка тарифом не считается",
      database.plan_key(database.get_business(bid)) == "business",
      database.plan_key(database.get_business(bid)))
subscribe(bid, "start")
check("а оплаченная подписка считается",
      database.plan_key(database.get_business(bid)) == "start")
subscribe(bid, "business")

print("\n== ВОЗМОЖНОСТИ, А НЕ СРАВНЕНИЕ СТРОК ==")
check("START — базовая аналитика и Директор",
      plans.entitlements("start") == {"analytics_basic", "ai_director"},
      plans.entitlements("start"))
check("BUSINESS добавляет находки и расширенную аналитику",
      plans.entitlements("start") < plans.entitlements("business"))
check("NETWORK включает всё из BUSINESS",
      plans.entitlements("business") < plans.entitlements("network"))
check("лимиты растут вместе с тарифом",
      plans.limit("start", "sources") < plans.limit("business", "sources")
      and plans.limit("start", "history") < plans.limit("business", "history"))
check("у верхнего тарифа источники без ограничения",
      plans.limit("network", "sources") == 0)
_src = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
check("в сервере нет сравнений тарифа строкой",
      'plan == "business"' not in _src and "plan == 'business'" not in _src)

# ===========================================================================
print("\n== ДОСТУП РЕШАЕТ BACKEND, А НЕ КНОПКА ==")
subscribe(bid, "start")
r = c.get("/api/initiatives", headers=H)
check("START не получает автоматические находки", r.status_code == 403, r.status_code)
check("и ему объясняют, чего не хватает",
      "BUSINESS" in r.json().get("detail", ""), r.json())
check("расширенная аналитика тоже закрыта",
      c.get("/api/weekly", headers=H).status_code == 403)
check("совет директоров тоже", c.get("/api/board", headers=H).status_code == 403)
check("но базовая аналитика открыта",
      c.get("/api/home", headers=H).status_code == 200)
# Показывать «3 находки» и закрывать их тарифом — значит подразнить.
_home = c.get("/api/home", headers=H).json()
check("на главной у START нет счётчика находок",
      (_home.get("initiatives") or {}).get("open") == 0, _home.get("initiatives"))

subscribe(bid, "business")
check("BUSINESS получает находки",
      c.get("/api/initiatives", headers=H).status_code == 200)
check("BUSINESS получает расширенную аналитику",
      c.get("/api/weekly", headers=H).status_code == 200)
subscribe(bid, "network")
check("NETWORK получает всё то же самое",
      c.get("/api/initiatives", headers=H).status_code == 200
      and c.get("/api/weekly", headers=H).status_code == 200)

print("\n== ДВА РАЗНЫХ ОТКАЗА ==")
# 402 и 403 говорят о разном, и путать их нельзя: в первом случае истёк срок,
# во втором не хватает тарифа. Одинаковый код заставил бы владельца гадать.
subscribe(bid, "start")
check("нет возможности на тарифе — 403",
      c.get("/api/weekly", headers=H).status_code == 403)
check("и сказано, какой тариф нужен",
      "BUSINESS" in c.get("/api/weekly", headers=H).json()["detail"])

# Правило VELOR: после окончания срока данные ОСТАЮТСЯ ВИДНЫ, останавливаются
# операции. Иначе экран «ваши данные сохранены» — ложь: половина разделов
# перестала бы открываться. Гейт тарифа поэтому спрашивает тариф, а не срок.
subscribe(bid, "business")
trial.disable(bid)
check("после окончания срока разделы тарифа всё равно открываются",
      c.get("/api/weekly", headers=H).status_code == 200)
check("и находки тоже видно",
      c.get("/api/initiatives", headers=H).status_code == 200)
check("но менять данные нельзя — 402",
      c.post("/api/leads", headers=H,
             json={"title": "Кто-то"}).status_code == 402)
check("и запускать пересчёт тоже нельзя",
      c.post("/api/initiatives/scan", headers=H, json={}).status_code == 402)
subscribe(bid, "business")

print("\n== ЧУЖОЕ НЕДОСТУПНО ==")
subscribe(bid2, "business")
_p = database.create_payment(bid, kind="subscription", amount=9900, plan="business")
r = c.post("/api/billing/payments/%d/refresh" % _p["id"], headers=H2)
check("платёж соседа не виден", r.status_code == 404, r.status_code)
_state2 = c.get("/api/billing", headers=H2).json()
check("в истории соседа нет чужих платежей",
      all(x["id"] != _p["id"] for x in _state2["history"]))
database.cancel_payment(_p["id"])

# ===========================================================================
print("\n== ПРОБНЫЙ ПЕРИОД СЧИТАЕТ СЕРВЕР ==")
bid3, H3 = new_business("paythree", "Новичок")
_a = c.get("/api/trial", headers=H3).json()
check("новый бизнес — в онбординге, отсчёт ещё не идёт",
      _a["phase"] == "onboarding" and _a["needs_launch"], _a["phase"])
launch(bid3)
_a = c.get("/api/trial", headers=H3).json()
check("после запуска — пробный период", _a["phase"] == "trial", _a["phase"])
check("ровно 14 дней", _a["trial_days"] == 14 and trial.TRIAL_DAYS == 14)
check("во время триала работает полный доступ",
      c.get("/api/initiatives", headers=H3).status_code == 200)
check("и это тариф BUSINESS, а не урезанный",
      database.plan_key(database.get_business(bid3)) == "business")

_end = database.get_business(bid3)["trial_end"]
for _ in range(5):
    c.get("/api/trial", headers=H3)
    c.get("/api/home", headers=H3)
check("обновление страницы не продлевает триал",
      database.get_business(bid3)["trial_end"] == _end)

print("\n== ПОСЛЕ ТРИАЛА ДАННЫЕ ОСТАЮТСЯ ==")
c.post("/api/leads", headers=H3, json={"title": "Хочет букет"})
_before = database.count_leads(bid3)
check("возможность заведена", _before >= 1, _before)
trial.disable(bid3)
_a = c.get("/api/trial", headers=H3).json()
check("аккаунт в режиме просмотра", _a["read_only"] and _a["phase"] == "locked", _a["phase"])
check("данные никуда не делись", database.count_leads(bid3) == _before)
check("их по-прежнему видно", c.get("/api/leads", headers=H3).status_code == 200)
check("но создавать нельзя",
      c.post("/api/leads", headers=H3, json={"title": "Ещё"}).status_code == 402)
check("платных возможностей «прямо сейчас» нет", billing.entitlements(bid3) == set(),
      billing.entitlements(bid3))
# А тариф у аккаунта остаётся — именно поэтому его данные видно.
check("но тариф у аккаунта остался",
      billing.plan_entitlements(bid3) == plans.entitlements("business"))
check("человеку сказано, что делать",
      "Оформите подписку" in c.post("/api/leads", headers=H3,
                                    json={"title": "Ещё"}).json()["detail"])

# ===========================================================================
print("\n== ОПЛАТА: ЦЕНУ СЧИТАЕТ СЕРВЕР ==")
bid4, H4 = new_business("payfour", "Покупатель")
launch(bid4)
r = c.post("/api/billing/checkout", headers=H4,
           json={"kind": "subscription", "plan": "business", "months": 1})
check("платёж создан", r.status_code == 200, r.json())
pay = r.json()["payment"]
check("сумма — из каталога, а не от фронта", pay["amount"] == 9900, pay)
check("выдан адрес страницы оплаты", pay["confirm_url"].startswith("https://pay.test/"))
check("платёж ждёт оплаты", pay["status"] == "pending")
check("подписка ещё НЕ активна",
      trial.access(database.get_business(bid4))["phase"] == "trial")
_body = YK["created"][-1]["body"]
check("в ЮKassa ушла та же сумма", _body["amount"]["value"] == "9900.00", _body["amount"])
check("ключ идемпотентности передан",
      YK["created"][-1]["headers"].get("Idempotence-Key", "").startswith("velor-pay-"))
check("адрес возврата — наш собственный",
      _body["confirmation"]["return_url"].endswith("/plans.html?payment=%d" % pay["id"]),
      _body["confirmation"]["return_url"])

r = c.post("/api/billing/checkout", headers=H4,
           json={"kind": "subscription", "plan": "нет-такого"})
check("несуществующий тариф не купить", r.status_code == 400, r.status_code)

print("\n== ВОЗВРАТ НА САЙТ САМ ПО СЕБЕ НИЧЕГО НЕ ВКЛЮЧАЕТ ==")
# Возврат означает лишь, что браузер открыл наш адрес. Его может открыть кто
# угодно и сколько угодно раз.
r = c.post("/api/billing/payments/%d/refresh" % pay["id"], headers=H4)
check("перепроверка не нашла оплаты", r.json()["status"] == "pending", r.json())
check("подписка не появилась",
      trial.access(database.get_business(bid4))["phase"] == "trial")
_pid = database.get_payment(pay["id"])["provider_id"]
check("сервер спрашивал платёжную систему", YK["fetches"] > 0)

print("\n== WEBHOOK ВКЛЮЧАЕТ ПОДПИСКУ ==")
pay_at_provider(_pid)
r = c.post("/api/billing/webhook", json={"event": "payment.succeeded",
                                         "object": {"id": _pid}})
check("webhook принят", r.status_code == 200, r.status_code)
check("и применён", r.json().get("applied") is True, r.json())
_b4 = database.get_business(bid4)
check("платёж отмечен оплаченным",
      database.get_payment(pay["id"])["status"] == "paid")
check("подписка активна", trial.access(_b4)["phase"] == "subscribed")
check("тариф проставлен", _b4["subscription_plan"] == "business")
check("срок назначен", bool(_b4["subscription_expires"]))
check("доступ открылся",
      c.get("/api/initiatives", headers=H4).status_code == 200)

print("\n== ПОВТОРНЫЙ WEBHOOK НИЧЕГО НЕ ДЕЛАЕТ ==")
# Платёжная система присылает уведомление столько раз, сколько нужно ей.
_exp = database.get_business(bid4)["subscription_expires"]
for _ in range(4):
    rr = c.post("/api/billing/webhook", json={"object": {"id": _pid}})
    check("повтор принят без ошибки", rr.status_code == 200, rr.status_code)
check("срок подписки не сдвинулся",
      database.get_business(bid4)["subscription_expires"] == _exp)
check("второй подписки не появилось",
      len([x for x in database.list_payments(bid4) if x["status"] == "paid"]) == 1)
check("повтор честно назван повтором",
      c.post("/api/billing/webhook", json={"object": {"id": _pid}}).json().get("already") is True)

print("\n== ЗАМОК СТОИТ В БАЗЕ, А НЕ В ПРОВЕРКЕ ПЕРЕД НИМ ==")
# Ранний выход «платёж уже оплачен» ловит обычный повтор — но не одновременные
# уведомления: два webhook-а успевают прочитать статус до того, как любой из
# них его изменит, и оба идут продлевать подписку. Поэтому замок — условие
# внутри самого UPDATE, и проверяется он напрямую.
_lock = database.create_payment(bid2, kind="subscription", amount=9900, plan="business")
check("первый вызов переводит платёж в оплаченный",
      database.mark_payment_paid(_lock["id"]) is True)
check("второй вызов не делает НИЧЕГО и говорит об этом",
      database.mark_payment_paid(_lock["id"]) is False)
check("и ещё три тоже",
      not any(database.mark_payment_paid(_lock["id"]) for _ in range(3)))
check("платёж остался одним и оплаченным",
      database.get_payment(_lock["id"])["status"] == "paid")
check("оплаченный платёж отменить нельзя — деньги уже получены",
      database.cancel_payment(_lock["id"])["status"] == "paid")

# Второй замок — UNIQUE на provider_id: один платёж у провайдера не может
# оказаться привязан к двум нашим строкам.
database.attach_provider_payment(_lock["id"], "yk-один-и-тот-же")
_dup = database.create_payment(bid2, kind="subscription", amount=9900, plan="business")
try:
    database.attach_provider_payment(_dup["id"], "yk-один-и-тот-же")
    _ok = False
except Exception:
    _ok = True
check("один платёж провайдера нельзя привязать дважды", _ok)
check("и по нему находится ровно первая строка",
      database.get_payment_by_provider("yk-один-и-тот-же")["id"] == _lock["id"])
database.cancel_payment(_dup["id"])

print("\n== WEBHOOK НЕ ВЕРИТ ТЕЛУ ЗАПРОСА ==")
# ЮKassa не подписывает уведомления: тело POST может прислать кто угодно.
bid5, H5 = new_business("payfive", "Хитрец")
launch(bid5)
r5 = c.post("/api/billing/checkout", headers=H5,
            json={"kind": "subscription", "plan": "network"}).json()["payment"]
_pid5 = database.get_payment(r5["id"])["provider_id"]
r = c.post("/api/billing/webhook",
           json={"event": "payment.succeeded",
                 "object": {"id": _pid5, "status": "succeeded", "paid": True,
                            "amount": {"value": "19900.00"}}})
check("подделанное «оплачено» в теле не сработало",
      trial.access(database.get_business(bid5))["phase"] == "trial", r.json())
check("платёж остался неоплаченным",
      database.get_payment(r5["id"])["status"] == "pending")

print("\n== СУММА ДОЛЖНА СОВПАСТЬ ==")
pay_at_provider(_pid5, amount=1)      # оплатили рубль вместо 19 900
r = c.post("/api/billing/webhook", json={"object": {"id": _pid5}})
check("платёж на чужую сумму не включает подписку",
      trial.access(database.get_business(bid5))["phase"] == "trial", r.json())
check("и это видно в ответе", r.json().get("mismatch") is True, r.json())
pay_at_provider(_pid5, amount=19900)  # теперь всё сходится
c.post("/api/billing/webhook", json={"object": {"id": _pid5}})
check("правильная сумма включает подписку",
      trial.access(database.get_business(bid5))["phase"] == "subscribed")
check("и именно тот тариф, который покупали",
      database.get_business(bid5)["subscription_plan"] == "network")

print("\n== ПЛАТЁЖНАЯ СИСТЕМА МОЛЧИТ — ПРОСИМ ПОВТОРИТЬ ==")
bid6, H6 = new_business("paysix", "Терпеливый")
launch(bid6)
r6 = c.post("/api/billing/checkout", headers=H6,
            json={"kind": "subscription", "plan": "start"}).json()["payment"]
_pid6 = database.get_payment(r6["id"])["provider_id"]
pay_at_provider(_pid6)
YK["down"] = True
r = c.post("/api/billing/webhook", json={"object": {"id": _pid6}})
check("не смогли проверить — отвечаем «пришлите снова»", r.status_code == 503, r.status_code)
check("и ничего не включили",
      trial.access(database.get_business(bid6))["phase"] == "trial")
YK["down"] = False
c.post("/api/billing/webhook", json={"object": {"id": _pid6}})
check("после повтора подписка включилась",
      trial.access(database.get_business(bid6))["phase"] == "subscribed")

print("\n== ЧУЖОЙ И НЕИЗВЕСТНЫЙ ПЛАТЁЖ ==")
r = c.post("/api/billing/webhook", json={"object": {"id": "yk-неизвестный"}})
check("неизвестный платёж принят без ошибки", r.status_code == 200, r.status_code)
check("и честно назван неизвестным", "ignored" in r.json(), r.json())
r = c.post("/api/billing/webhook", json={})
check("пустое уведомление не ломает адрес", r.status_code == 200)

print("\n== ПРОДЛЕНИЕ, А НЕ ПЕРЕЗАПУСК ==")
# Оплатил второй месяц заранее — должен получить два, а не потерять остаток.
_was = database.get_business(bid4)["subscription_expires"]
r = c.post("/api/billing/checkout", headers=H4,
           json={"kind": "subscription", "plan": "business"}).json()["payment"]
_pid7 = database.get_payment(r["id"])["provider_id"]
pay_at_provider(_pid7)
c.post("/api/billing/webhook", json={"object": {"id": _pid7}})
check("срок сдвинулся вперёд",
      database.get_business(bid4)["subscription_expires"] > _was,
      (_was, database.get_business(bid4)["subscription_expires"]))

print("\n== FOUNDER PILOT — УСЛОВИЕ, А НЕ ТАРИФ ==")
check("пилота нет в публичном каталоге",
      all(p["key"] != "founder" and "пилот" not in p["name"].lower()
          for p in _st["plans"]))
bid7, H7 = new_business("payseven", "Пилот")
launch(bid7)
r = c.post("/api/admin/businesses/%d/founder-pilot" % bid7, headers=OWN,
           json={"founder_pilot": True})
check("владелец VELOR включает пилот", r.status_code == 200 and r.json()["founder_pilot"])
check("посторонний включить не может",
      c.post("/api/admin/businesses/%d/founder-pilot" % bid7, headers=H7,
             json={"founder_pilot": True}).status_code in (401, 403))
_amount, _label = billing.quote(bid7, "subscription", "business")
check("первый месяц BUSINESS по цене START", _amount == 4900, _amount)
check("и это названо в описании платежа", "Founder Pilot" in _label, _label)
check("настройка входит в условия", billing.quote(bid7, "setup")[0] == 0)
r = c.post("/api/billing/checkout", headers=H7, json={"kind": "setup"}).json()["payment"]
check("бесплатная настройка проводится сразу", r["status"] == "paid", r)
check("и видна в истории строкой на ноль",
      any(x["kind"] == "setup" and x["amount"] == 0
          for x in c.get("/api/billing", headers=H7).json()["history"]))
r = c.post("/api/billing/checkout", headers=H7,
           json={"kind": "subscription", "plan": "business"}).json()["payment"]
check("первый платёж пилота — 4 900", r["amount"] == 4900, r)
_pid8 = database.get_payment(r["id"])["provider_id"]
pay_at_provider(_pid8)
c.post("/api/billing/webhook", json={"object": {"id": _pid8}})
check("пилот получает обычный BUSINESS, а не урезанный",
      plans.entitlements(database.plan_key(database.get_business(bid7)))
      == plans.entitlements("business"))
check("второй месяц уже по обычной цене",
      billing.quote(bid7, "subscription", "business")[0] == 9900,
      billing.quote(bid7, "subscription", "business"))

print("\n== НАСТРОЙКА — РАЗОВАЯ УСЛУГА ==")
bid8, H8 = new_business("payeight", "Настройка")
launch(bid8)
r = c.post("/api/billing/checkout", headers=H8, json={"kind": "setup"}).json()["payment"]
check("настройка стоит 15 000", r["amount"] == 15000, r)
_pid9 = database.get_payment(r["id"])["provider_id"]
pay_at_provider(_pid9)
c.post("/api/billing/webhook", json={"object": {"id": _pid9}})
check("оплата настройки отмечена", bool(database.get_business(bid8)["setup_paid"]))
check("но подписку она НЕ включает — это разные вещи",
      trial.access(database.get_business(bid8))["phase"] == "trial")

print("\n== БЕЗ КЛЮЧЕЙ ПЛАТЁЖКИ НЕ ВРЁМ ==")
_shop = server.config.YOOKASSA_SHOP_ID
server.config.YOOKASSA_SHOP_ID = None
check("оплата недоступна", not billing.configured())
r = c.post("/api/billing/checkout", headers=H8,
           json={"kind": "subscription", "plan": "business"})
check("покупка отклонена понятной ошибкой", r.status_code == 400, r.status_code)
check("и человеку сказано, что делать",
      "вручную" in r.json()["detail"], r.json())
check("состояние страницы об этом знает",
      c.get("/api/billing", headers=H8).json()["payments_enabled"] is False)
server.config.YOOKASSA_SHOP_ID = _shop

print("\n== СТРАНИЦА ТАРИФА ГОВОРИТ ПРАВДУ ==")
_s = c.get("/api/billing", headers=H4).json()
check("показан оплаченный тариф", _s["paid_plan"] == "business", _s["paid_plan"])
check("во время триала оплаченного тарифа нет",
      c.get("/api/billing", headers=H3).json()["paid_plan"] is None)
check("возможности перечислены",
      set(_s["entitlements"]) == plans.entitlements("business"), _s["entitlements"])
check("лимиты показаны", _s["limits"]["sources"] == plans.limit("business", "sources"))
check("история платежей есть", len(_s["history"]) >= 1)
check("в истории нет чужих полей",
      all("provider_id" not in x for x in _s["history"]))

print("\nИТОГО: успешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
