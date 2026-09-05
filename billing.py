# -*- coding: utf-8 -*-
"""
BILLING — деньги и подписка.

Один принцип, из которого следует всё остальное:

    ПОДПИСКУ ВКЛЮЧАЕТ ТОЛЬКО ПОДТВЕРЖДЁННАЯ ОПЛАТА.

Не возвращение человека на сайт, не нажатие кнопки, не редирект с
`?success=1` — только ответ платёжной системы. Возврат на страницу говорит
ровно одно: браузер открыл наш адрес. Его может открыть кто угодно и сколько
угодно раз, вручную набрав ссылку, — и если бы подписка включалась там, VELOR
раздавал бы доступ бесплатно всем, кто прочитал адресную строку.

Порядок:

    выбор тарифа → payment (pending) → страница оплаты ЮKassa → оплата
      → webhook → ПЕРЕПРОВЕРКА платежа через API ЮKassa → paid → подписка

ПОЧЕМУ ПЕРЕПРОВЕРКА. ЮKassa не подписывает webhook. Тело POST-запроса — это
просто JSON, присланный кем угодно на открытый адрес; поверить ему значит
отдать подписку любому, кто узнал структуру уведомления. Поэтому из тела мы
берём ровно одно — идентификатор платежа, — и спрашиваем состояние у API
ЮKassa по нашим ключам. Отвечает там уже сама платёжная система.

ИДЕМПОТЕНТНОСТЬ. Webhook приходит столько раз, сколько нужно платёжной
системе: сеть моргнула, наш ответ не дошёл, сработал ретрай по расписанию.
Замок стоит в базе: `payments.provider_id` UNIQUE, а `mark_payment_paid`
переводит строку в paid условием `status <> 'paid'` внутри самого UPDATE и
сообщает, сделал ли это он. Продление вызывается ТОЛЬКО когда сделал он —
второй webhook находит платёж уже оплаченным и молча отвечает «ок».
"""
import json
import logging
import uuid

import requests

import config
import database
import plans
import trial

log = logging.getLogger("velor.billing")

API = "https://api.yookassa.ru/v3"
TIMEOUT = 20

KIND_SUBSCRIPTION = "subscription"
KIND_SETUP = "setup"


class BillingError(Exception):
    """Понятная владельцу причина, по которой платёж не создался."""


# ---------- НАСТРОЙКА ----------
def configured():
    """Есть ли ключи ЮKassa. Без них платить нельзя — и врать об этом нельзя."""
    return bool(getattr(config, "YOOKASSA_SHOP_ID", None)
                and getattr(config, "YOOKASSA_SECRET_KEY", None))


def _auth():
    return (str(config.YOOKASSA_SHOP_ID), str(config.YOOKASSA_SECRET_KEY))


# ---------- ЦЕНА ----------
def quote(bid, kind, plan_key=None, months=1):
    """
    Сколько стоит эта покупка именно для этого бизнеса.

    Цену считает сервер и только сервер. Присланная фронтом сумма не
    используется нигде: иначе достаточно было бы отправить свой запрос с
    `amount: 1`, и BUSINESS обошёлся бы в рубль.
    """
    b = database.get_business(bid) or {}
    pilot = bool(b.get("founder_pilot"))
    months = max(1, int(months or 1))

    if kind == KIND_SETUP:
        if pilot:
            # Founder Pilot: настройка входит в условия.
            return 0, plans.SETUP["name"] + " (Founder Pilot)"
        return plans.SETUP["price"], plans.SETUP["name"]

    key = plans.normalize(plan_key)
    p = plans.get(key)

    if pilot and key == plans.FOUNDER_PILOT["plan"] and not _paid_subscription_before(bid):
        # Первый месяц пилота — по цене START. Дальше обычная цена: условие
        # разовое, и продлевать его молча нельзя.
        return (plans.FOUNDER_PILOT["first_month_price"] * months,
                p["name"] + " — первый месяц (Founder Pilot)")

    label = p["name"] if months == 1 else "%s, %d мес." % (p["name"], months)
    return p["price"] * months, label


def _paid_subscription_before(bid):
    return any(x["status"] == "paid" and x["kind"] == KIND_SUBSCRIPTION
               for x in database.list_payments(bid, limit=100))


# ---------- СОЗДАНИЕ ПЛАТЕЖА ----------
def start(bid, kind, plan_key=None, months=1, base_url=None):
    """
    Завести платёж и получить адрес страницы оплаты.

    Сначала пишем строку в свою базу, потом идём в ЮKassa: если сеть отвалится
    на втором шаге, у нас останется запись о намерении, а не потерянный платёж.

    base_url — origin нашего же сервиса; адрес возврата собирается ИЗ НЕГО, а не
    из того, что прислал фронт. Присланный адрес — открытый редирект: ЮKassa
    послушно увела бы человека куда угодно, и ссылка «оплатить VELOR» стала бы
    ссылкой на чужой сайт.
    """
    if kind not in (KIND_SUBSCRIPTION, KIND_SETUP):
        raise BillingError("Неизвестный тип покупки.")
    if kind == KIND_SUBSCRIPTION and plans.normalize(plan_key) != (plan_key or "").strip().lower():
        # normalize терпим к истории, но выбор тарифа человеком — не история.
        raise BillingError("Такого тарифа нет.")

    amount, label = quote(bid, kind, plan_key, months)

    # Бесплатная покупка не идёт в платёжку: там нечего проводить. Это
    # единственный случай, когда доступ включается без денег, и он должен быть
    # виден в истории платежей как строка на 0 ₽, а не как молчаливая правка.
    if amount <= 0:
        row = database.create_payment(bid, kind=kind, amount=0,
                                      plan=plan_key, months=months,
                                      description=label, provider="internal")
        database.attach_provider_payment(row["id"], "internal:%d" % row["id"])
        if database.mark_payment_paid(row["id"]):
            apply_paid(database.get_payment(row["id"]))
        return database.get_payment(row["id"])

    if not configured():
        raise BillingError(
            "Приём оплаты ещё не подключён. Напишите нам — оформим вручную.")

    row = database.create_payment(bid, kind=kind, amount=amount,
                                  plan=plan_key, months=months,
                                  description=label)
    ret = None
    if base_url:
        ret = "%s/plans.html?payment=%d" % (str(base_url).rstrip("/"), row["id"])
    try:
        yk = _create_remote(row, label, ret)
    except BillingError:
        database.cancel_payment(row["id"])
        raise
    except Exception as e:
        database.cancel_payment(row["id"])
        log.exception("ЮKassa не приняла платёж (biz %s)", bid)
        raise BillingError("Платёжная система не ответила: " + str(e)[:120])

    confirm = ((yk.get("confirmation") or {}).get("confirmation_url"))
    return database.attach_provider_payment(row["id"], yk["id"], confirm)


def _create_remote(row, label, return_url):
    """POST /payments в ЮKassa.

    Idempotence-Key — требование ЮKassa: при повторе того же запроса она вернёт
    тот же платёж, а не создаст второй. Ключ берём от нашей строки, чтобы повтор
    был повтором, а не новой попыткой.
    """
    body = {
        "amount": {"value": "%d.00" % int(row["amount"]), "currency": "RUB"},
        "capture": True,
        "description": label[:128],
        "metadata": {"payment_id": str(row["id"]),
                     "business_id": str(row["business_id"]),
                     "kind": row["kind"], "plan": row["plan"] or ""},
    }
    if return_url:
        body["confirmation"] = {"type": "redirect", "return_url": return_url}
    r = requests.post(API + "/payments", auth=_auth(), json=body, timeout=TIMEOUT,
                      headers={"Idempotence-Key": "velor-pay-%s" % row["id"]})
    if r.status_code >= 400:
        raise BillingError("ЮKassa отказала: %s" % _reason(r))
    return r.json()


def _reason(r):
    try:
        d = r.json()
        return (d.get("description") or d.get("code") or r.text)[:160]
    except Exception:
        return ("код %s" % r.status_code)


# ---------- WEBHOOK ----------
def handle_webhook(payload):
    """
    Уведомление от ЮKassa.

    Возвращает (http_status, dict). 200 значит «принято, больше не присылайте»;
    503 — «мы не смогли проверить, пришлите ещё раз». Ошибку 4xx на непонятное
    уведомление не отдаём: ЮKassa сочтёт адрес сломанным и перестанет слать.

    Из тела берём ТОЛЬКО идентификатор. Всё остальное — статус, сумма — читаем
    из ответа API ЮKassa: тело POST-запроса не подписано и доверять ему нельзя.
    """
    obj = (payload or {}).get("object") or {}
    pid = obj.get("id")
    if not pid:
        return 200, {"ok": True, "ignored": "нет идентификатора платежа"}

    row = database.get_payment_by_provider(pid)
    if not row:
        # Не наш платёж или мы его ещё не записали. Отвечаем 200: повторы
        # ничего не изменят, а «ошибка» заставила бы ЮKassa стучаться вечно.
        return 200, {"ok": True, "ignored": "платёж не найден"}

    if row["status"] == "paid":
        return 200, {"ok": True, "already": True}   # обычный повтор, всё в порядке

    try:
        remote = fetch(pid)
    except Exception as e:
        log.warning("Не удалось перепроверить платёж %s: %s", pid, e)
        return 503, {"ok": False, "retry": True}    # пусть пришлют снова

    status = (remote.get("status") or "").lower()
    if status == "canceled":
        database.cancel_payment(row["id"])
        return 200, {"ok": True, "canceled": True}
    if status != "succeeded" or not remote.get("paid"):
        return 200, {"ok": True, "pending": True}

    # Сумма должна совпасть с той, которую назначили мы. Не совпала — это не
    # оплата нашего счёта, и доступ по ней не включается.
    try:
        got = int(float((remote.get("amount") or {}).get("value") or 0))
    except (TypeError, ValueError):
        got = -1
    if got != int(row["amount"]):
        log.error("Сумма платежа %s не совпала: ждали %s, пришло %s",
                  pid, row["amount"], got)
        return 200, {"ok": False, "mismatch": True}

    # Замок: продлеваем только если в paid перевёл именно этот вызов.
    if not database.mark_payment_paid(row["id"]):
        return 200, {"ok": True, "already": True}

    apply_paid(database.get_payment(row["id"]))
    return 200, {"ok": True, "applied": True}


def fetch(provider_id):
    """Состояние платежа у ЮKassa — источник истины об оплате."""
    r = requests.get("%s/payments/%s" % (API, provider_id), auth=_auth(),
                     timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------- ЧТО ДЕЛАЕТ ОПЛАТА ----------
def apply_paid(row):
    """
    Оплаченный платёж → доступ. Единственное место, где включается подписка.

    Продление, а не переустановка: если человек оплатил второй месяц заранее,
    он должен получить два месяца, а не потерять остаток первого.
    """
    bid = row["business_id"]
    if row["kind"] == KIND_SETUP:
        database.update_business(bid, setup_paid=row.get("paid_at") or "")
        database.log_event(bid, "plan", "Настройка VELOR оплачена",
                           "Мы свяжемся и начнём настройку.", level="important")
        return

    key = plans.normalize(row.get("plan"))
    months = max(1, int(row.get("months") or 1))
    b = database.get_business(bid) or {}
    st = trial.access(b)

    if st["phase"] == "subscribed" and plans.normalize(b.get("subscription_plan")) == key:
        trial.extend_subscription(bid, months=months)
    else:
        trial.activate_subscription(bid, plan=key, months=months)

    database.log_event(bid, "plan", "Подписка активна",
                       "Тариф «%s». Доступ открыт." % plans.name(key),
                       level="important")


# ---------- СОСТОЯНИЕ ДЛЯ КАБИНЕТА ----------
def state(bid):
    """Всё, что нужно странице тарифа: подписка, каталог, история платежей."""
    b = database.get_business(bid) or {}
    st = trial.access(b)
    key = database.plan_key(b)
    return {
        "access": st,
        "plan": key,
        "plan_name": plans.name(key),
        # Тариф считается выбранным, только если он оплачен. Во время триала
        # человек пользуется BUSINESS, но не покупал его, и подсвечивать
        # карточку как «ваш тариф» было бы обманом.
        "paid_plan": plans.normalize(b.get("subscription_plan"))
                     if b.get("subscription_plan") else None,
        "plans": plans.public(b.get("subscription_plan")),
        "setup": dict(plans.SETUP,
                      price=quote(bid, KIND_SETUP)[0],
                      paid=bool(b.get("setup_paid"))),
        "founder_pilot": bool(b.get("founder_pilot")),
        "payments_enabled": configured(),
        "entitlements": sorted(entitlements(bid)),
        "limits": plans.limits(key),
        "history": [
            {"id": x["id"], "kind": x["kind"], "plan": x["plan"],
             "amount": x["amount"], "status": x["status"],
             "created_at": x["created_at"], "paid_at": x["paid_at"],
             "description": x["description"]}
            for x in database.list_payments(bid, limit=20)
        ],
    }


# ---------- ДОСТУП ----------
def plan_entitlements(bid, business=None):
    """
    Что даёт ТАРИФ бизнеса. Про срок здесь ничего не спрашивается.

    Именно это отвечает на вопрос «можно ли смотреть»: после окончания триала
    VELOR оставляет данные видимыми — блокируются операции, а не чтение. Если
    бы срок учитывался здесь, экран «ваши данные сохранены» врал бы: половина
    данных переставала бы открываться.
    """
    b = business if business is not None else (database.get_business(bid) or {})
    return plans.entitlements(database.plan_key(b)) if b else set()


def entitlements(bid, business=None):
    """
    Что бизнесу доступно ПРЯМО СЕЙЧАС — с учётом срока.

    Отличается от plan_entitlements ровно одним: в режиме просмотра здесь
    пусто. Годится для показа («что у меня есть») и для решения, стоит ли
    вообще заводить разговор о платной находке; для ответа «пускать ли к
    странице» — нет, см. выше.
    """
    b = business if business is not None else (database.get_business(bid) or {})
    if not b:
        return set()
    if trial.access(b)["read_only"]:
        return set()
    return plan_entitlements(bid, business=b)


def allowed(bid, entitlement, business=None):
    """Доступно сейчас (с учётом срока)."""
    return entitlement in entitlements(bid, business=business)


def plan_allows(bid, entitlement, business=None):
    """Даёт ли это ТАРИФ (без учёта срока) — гейт страниц спрашивает так."""
    return entitlement in plan_entitlements(bid, business=business)
