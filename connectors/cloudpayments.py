# -*- coding: utf-8 -*-
"""
CloudPayments — эквайринг: платежи и возвраты.

Владелец берёт Public ID и API Secret в личном кабинете: Сайты → нужный сайт.
"""
import datetime

import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need,
                             request, rub, since, to_stamp)

ID = "cloudpayments"
NAME = "CloudPayments"
GROUP = "Платежи и банк"
GIVES = ["транзакции по дням", "возвраты", "выручку эквайринга"]
HOWTO = "Личный кабинет CloudPayments → Сайты → выберите сайт: Public ID и API Secret."
FIELDS = [
    field("public_id", "Public ID", "Идентификатор сайта", placeholder="pk_..."),
    field("api_secret", "API Secret", "Пароль для API", secret=True),
]

API = "https://api.cloudpayments.ru"


def _auth(creds):
    return (need(creds, "public_id"), need(creds, "api_secret"))


def check(creds, meta):
    data = request("POST", f"{API}/test", auth=_auth(creds), json={})
    if not (data or {}).get("Success"):
        raise ConnectorError("CloudPayments не принял ключи. Проверьте Public ID и API Secret.")
    return dict(meta or {}, verified=True)


def _day_list(creds, day):
    """Список транзакций за один день — так устроен их API (выгрузка посуточная)."""
    data = request("POST", f"{API}/payments/list", auth=_auth(creds),
                   json={"Date": day, "TimeZone": "MSK"})
    if not (data or {}).get("Success"):
        raise ConnectorError((data or {}).get("Message") or "CloudPayments отклонил запрос.")
    return data.get("Model") or []


def sync(bid, creds, meta, cursor):
    start = since(cursor, days=30).date()
    today = datetime.date.today()
    added, newest, seen = 0, None, 0

    day = start
    while day <= today and seen < MAX_ITEMS:
        for t in _day_list(creds, day.isoformat()):
            seen += 1
            tid = t.get("TransactionId")
            if not tid:
                continue
            amount = rub(t.get("Amount"))
            created = to_stamp(t.get("CreatedDateIso") or t.get("CreatedDate"))
            status = (t.get("Status") or "").lower()
            desc = (t.get("Description") or "Оплата картой")[:160]
            payer = (t.get("Email") or t.get("CardFirstSix") or "CloudPayments")

            if t.get("OperationType") == "Refund" or status == "refunded":
                _, is_new = database.upsert_external_finance(
                    bid, f"refund-{tid}", ID, "expense", amount,
                    category="Возвраты покупателям", note=desc,
                    counterparty=str(payer)[:80], op_date=day_of(created))
            elif status in ("completed", "authorized"):
                _, is_new = database.upsert_external_finance(
                    bid, tid, ID, "income", amount,
                    category="Онлайн-оплаты", note=desc,
                    counterparty=str(payer)[:80], op_date=day_of(created))
            else:
                continue                      # отклонённые платежи деньгами не являются
            if is_new:
                added += 1
            if created and (newest is None or created > newest):
                newest = created
        day += datetime.timedelta(days=1)
    return added, newest
