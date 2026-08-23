# -*- coding: utf-8 -*-
"""
Shopify — заказы магазина.

Владелец создаёт в своей админке приватное приложение: Settings → Apps and sales
channels → Develop apps → Create an app → Configure Admin API scopes
(`read_orders`, `read_customers`) → Install → Reveal token. Наше приложение
в Shopify регистрировать не нужно.
"""
import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need,
                             public_url, request, rub, since, to_stamp)

ID = "shopify"
NAME = "Shopify"
GROUP = "Магазины и маркетплейсы"
GIVES = ["заказы и суммы", "покупателей", "выручку и возвраты"]
HOWTO = ("Админка Shopify → Settings → Apps → Develop apps → создайте приложение "
         "с правами read_orders и read_customers, установите его и скопируйте "
         "Admin API access token (начинается на shpat_).")
FIELDS = [
    field("shop", "Домен магазина", "Например: my-store.myshopify.com",
          placeholder="my-store.myshopify.com"),
    field("token", "Admin API access token", "Начинается на shpat_", secret=True),
]

VERSION = "2024-10"

_STATUS = {"paid": "выполнен", "partially_paid": "принят", "pending": "новый",
           "refunded": "отменён", "voided": "отменён"}


def _base(creds):
    shop = need(creds, "shop").strip().lower()
    shop = shop.replace("https://", "").replace("http://", "").strip("/")
    if not shop.endswith(".myshopify.com"):
        raise ConnectorError("Домен должен выглядеть так: my-store.myshopify.com")
    public_url("https://" + shop)          # и что он действительно публичный
    return f"https://{shop}/admin/api/{VERSION}"


def _headers(creds):
    return {"X-Shopify-Access-Token": need(creds, "token"),
            "Content-Type": "application/json"}


def check(creds, meta):
    data = request("GET", f"{_base(creds)}/shop.json", headers=_headers(creds))
    shop = data.get("shop") or {}
    return dict(meta or {}, shop_name=shop.get("name"),
                currency=shop.get("currency"), verified=True)


def sync(bid, creds, meta, cursor):
    frm = since(cursor).strftime("%Y-%m-%dT%H:%M:%S")
    data = request("GET", f"{_base(creds)}/orders.json", headers=_headers(creds),
                   params={"status": "any", "updated_at_min": frm,
                           "limit": min(250, MAX_ITEMS)})
    added, newest = 0, None
    for o in (data.get("orders") or [])[:MAX_ITEMS]:
        oid = o.get("id")
        if not oid:
            continue
        items = o.get("line_items") or []
        text = ", ".join(f"{(i.get('title') or 'товар')[:50]}"
                         + (f" ×{i['quantity']}" if int(i.get("quantity") or 1) > 1 else "")
                         for i in items[:5]) or f"Заказ {o.get('name') or oid}"
        total = rub(o.get("total_price"))
        created = to_stamp(o.get("created_at"))
        status = _STATUS.get((o.get("financial_status") or "").lower(), "принят")
        if o.get("cancelled_at"):
            status = "отменён"

        client_id = None
        cust = o.get("customer") or {}
        if cust.get("id"):
            name = " ".join(x for x in (cust.get("first_name"), cust.get("last_name")) if x)
            client_id, _ = database.upsert_external_client(
                bid, cust["id"], ID, name=name or None,
                phone=cust.get("phone") or o.get("phone"))

        _, is_new = database.upsert_external_order(
            bid, oid, ID, text[:400], amount=total, status=status,
            client_id=client_id, phone=o.get("phone"), created_at=created)
        if is_new:
            added += 1
        if status == "выполнен" and total:
            database.upsert_external_finance(
                bid, f"order-{oid}", ID, "income", total,
                category="Продажи Shopify", note=text[:160],
                counterparty=(cust.get("email") or "Shopify"), op_date=day_of(created))
        if created and (newest is None or created > newest):
            newest = created
    return added, newest
