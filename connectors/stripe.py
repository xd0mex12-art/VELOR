# -*- coding: utf-8 -*-
"""
Stripe — онлайн-платежи и возвраты.

Владелец берёт секретный ключ в дашборде: Developers → API keys → Secret key.
Достаточно ключа с правами только на чтение (restricted key) — так безопаснее,
и мы это прямо советуем в подсказке.
"""
import datetime

import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need,
                             request, rub, since, to_stamp)

ID = "stripe"
NAME = "Stripe"
GROUP = "Платежи и банк"
GIVES = ["поступления", "возвраты", "выручку по дням"]
HOWTO = ("Дашборд Stripe → Developers → API keys. Лучше создать Restricted key "
         "с правами только на чтение Charges и Refunds.")
FIELDS = [
    field("secret_key", "Секретный ключ", "sk_live_… или rk_live_…", secret=True),
]

API = "https://api.stripe.com/v1"


def _headers(creds):
    return {"Authorization": "Bearer " + need(creds, "secret_key")}


def check(creds, meta):
    data = request("GET", f"{API}/charges", headers=_headers(creds), params={"limit": 1})
    return dict(meta or {}, livemode=bool((data or {}).get("data") and
                                          data["data"][0].get("livemode")), verified=True)


def sync(bid, creds, meta, cursor):
    gte = int(since(cursor).timestamp())
    added, newest = 0, None
    params = {"limit": 100, "created[gte]": gte}
    fetched = 0
    while fetched < MAX_ITEMS:
        data = request("GET", f"{API}/charges", headers=_headers(creds), params=params)
        items = data.get("data") or []
        for ch in items:
            cid = ch.get("id")
            if not cid or not ch.get("paid") or ch.get("status") != "succeeded":
                continue
            # Stripe отдаёт суммы в минимальных единицах (копейки/центы).
            amount = rub(ch.get("amount"), cents=True)
            created = to_stamp(datetime.datetime.utcfromtimestamp(
                int(ch.get("created") or 0)).isoformat())
            desc = (ch.get("description") or "Оплата")[:160]
            payer = ((ch.get("billing_details") or {}).get("email")
                     or ch.get("receipt_email") or "Stripe")
            _, is_new = database.upsert_external_finance(
                bid, cid, ID, "income", amount, category="Онлайн-оплаты",
                note=desc, counterparty=str(payer)[:80], op_date=day_of(created))
            if is_new:
                added += 1
            refunded = rub(ch.get("amount_refunded"), cents=True)
            if refunded:
                database.upsert_external_finance(
                    bid, f"refund-{cid}", ID, "expense", refunded,
                    category="Возвраты покупателям", note=desc,
                    counterparty=str(payer)[:80], op_date=day_of(created))
            if created and (newest is None or created > newest):
                newest = created
        fetched += len(items)
        if not data.get("has_more") or not items:
            break
        params = dict(params, starting_after=items[-1]["id"])
    return added, newest
