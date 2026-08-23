# -*- coding: utf-8 -*-
"""
ЮKassa — поступления и возвраты.

Владелец берёт данные в личном кабинете ЮKassa: Настройки → Магазин →
shopId, и там же выпускает секретный ключ (начинается на live_ или test_).

Что даёт VELOR: каждое успешное поступление становится доходом, каждый возврат —
расходом. То есть прибыль и структура выручки считаются по реальным деньгам,
а не по тому, что владелец успел вписать руками.
"""
import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, iso_z,
                             need, request, rub, since, to_stamp)

ID = "yookassa"
NAME = "ЮKassa"
GROUP = "Платежи и банк"
GIVES = ["поступления по дням", "возвраты", "способы оплаты"]
HOWTO = ("Личный кабинет ЮKassa → Настройки → Магазин: скопируйте shopId, "
         "затем выпустите секретный ключ и вставьте его сюда.")
FIELDS = [
    field("shop_id", "shopId", "Число из настроек магазина", placeholder="123456"),
    field("secret_key", "Секретный ключ", "Начинается на live_ или test_", secret=True),
]

API = "https://api.yookassa.ru/v3"


def _auth(creds):
    return (str(need(creds, "shop_id")), str(need(creds, "secret_key")))


def check(creds, meta):
    request("GET", f"{API}/payments", auth=_auth(creds), params={"limit": 1})
    return dict(meta or {}, shop_id=str(creds.get("shop_id")), verified=True)


def _page(creds, path, params):
    return request("GET", f"{API}/{path}", auth=_auth(creds), params=params)


def sync(bid, creds, meta, cursor):
    frm = iso_z(since(cursor))
    added, newest = 0, None

    # ---- поступления ----
    params = {"created_at.gte": frm, "limit": 100, "status": "succeeded"}
    fetched = 0
    while fetched < MAX_ITEMS:
        data = _page(creds, "payments", params)
        items = data.get("items") or []
        for p in items:
            pid = p.get("id")
            amount = rub((p.get("amount") or {}).get("value"))
            created = to_stamp(p.get("captured_at") or p.get("created_at"))
            desc = (p.get("description") or "Оплата").strip()[:160]
            method = ((p.get("payment_method") or {}).get("title")
                      or (p.get("payment_method") or {}).get("type") or "ЮKassa")
            _, is_new = database.upsert_external_finance(
                bid, pid, ID, "income", amount,
                category="Онлайн-оплаты", note=desc,
                counterparty=str(method)[:80], op_date=day_of(created))
            if is_new:
                added += 1
            if created and (newest is None or created > newest):
                newest = created
        fetched += len(items)
        nxt = data.get("next_cursor")
        if not nxt or not items:
            break
        params = dict(params, cursor=nxt)

    # ---- возвраты (уменьшают выручку) ----
    params = {"created_at.gte": frm, "limit": 100}
    data = _page(creds, "refunds", params)
    for r in (data.get("items") or [])[:MAX_ITEMS]:
        rid = r.get("id")
        amount = rub((r.get("amount") or {}).get("value"))
        created = to_stamp(r.get("created_at"))
        _, is_new = database.upsert_external_finance(
            bid, f"refund-{rid}", ID, "expense", amount,
            category="Возвраты покупателям",
            note=(r.get("description") or "Возврат")[:160],
            counterparty="ЮKassa", op_date=day_of(created))
        if is_new:
            added += 1
    return added, newest
