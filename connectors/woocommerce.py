# -*- coding: utf-8 -*-
"""
WooCommerce — заказы магазина на WordPress.

Владелец создаёт ключи в своей админке: WooCommerce → Настройки → Дополнительно
→ REST API → Добавить ключ (права «Чтение»). Получает Consumer key и
Consumer secret — их и вставляет.
"""
import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need,
                             public_url, request, rub, since, to_stamp)

ID = "woocommerce"
NAME = "WooCommerce"
GROUP = "Магазины и маркетплейсы"
GIVES = ["заказы и суммы", "покупателей", "выручку"]
HOWTO = ("WooCommerce → Настройки → Дополнительно → REST API → Добавить ключ "
         "с правами «Чтение». Скопируйте Consumer key и Consumer secret.")
FIELDS = [
    field("url", "Адрес сайта", "Например: https://moy-magazin.ru",
          placeholder="https://moy-magazin.ru"),
    field("key", "Consumer key", "Начинается на ck_", secret=True),
    field("secret", "Consumer secret", "Начинается на cs_", secret=True),
]

_STATUS = {"completed": "выполнен", "processing": "принят", "on-hold": "принят",
           "pending": "новый", "cancelled": "отменён", "refunded": "отменён",
           "failed": "отменён"}


def _base(creds):
    # Адрес магазина вводит человек: требуем публичный https (защита от SSRF
    # и от передачи ключей открытым текстом).
    url = public_url(need(creds, "url")).rstrip("/")
    return url + "/wp-json/wc/v3"


def _auth(creds):
    return (need(creds, "key"), need(creds, "secret"))


def check(creds, meta):
    data = request("GET", f"{_base(creds)}/system_status", auth=_auth(creds))
    env = (data or {}).get("environment") or {}
    return dict(meta or {}, currency=((data or {}).get("settings") or {}).get("currency"),
                site=env.get("site_url"), verified=True)


def sync(bid, creds, meta, cursor):
    after = since(cursor).strftime("%Y-%m-%dT%H:%M:%S")
    data = request("GET", f"{_base(creds)}/orders", auth=_auth(creds),
                   params={"after": after, "per_page": 100, "orderby": "date",
                           "order": "desc"})
    if not isinstance(data, list):
        raise ConnectorError("Магазин вернул неожиданный ответ. Проверьте, что REST API "
                             "включён и ключи имеют права на чтение.")
    added, newest = 0, None
    for o in data[:MAX_ITEMS]:
        oid = o.get("id")
        if not oid:
            continue
        items = o.get("line_items") or []
        text = ", ".join(f"{(i.get('name') or 'товар')[:50]}"
                         + (f" ×{i['quantity']}" if int(i.get("quantity") or 1) > 1 else "")
                         for i in items[:5]) or f"Заказ №{oid}"
        total = rub(o.get("total"))
        created = to_stamp(o.get("date_created"))
        status = _STATUS.get((o.get("status") or "").lower(), "принят")

        bill = o.get("billing") or {}
        client_id = None
        if o.get("customer_id"):
            name = " ".join(x for x in (bill.get("first_name"), bill.get("last_name")) if x)
            client_id, _ = database.upsert_external_client(
                bid, o["customer_id"], ID, name=name or None, phone=bill.get("phone"))

        _, is_new = database.upsert_external_order(
            bid, oid, ID, text[:400], amount=total, status=status,
            client_id=client_id, phone=bill.get("phone"), created_at=created)
        if is_new:
            added += 1
        if status == "выполнен" and total:
            database.upsert_external_finance(
                bid, f"order-{oid}", ID, "income", total,
                category="Продажи в магазине", note=text[:160],
                counterparty=bill.get("email") or "WooCommerce", op_date=day_of(created))
        if created and (newest is None or created > newest):
            newest = created
    return added, newest
