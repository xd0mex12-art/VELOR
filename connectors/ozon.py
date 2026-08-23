# -*- coding: utf-8 -*-
"""
Ozon Seller API — заказы и выручка магазина.

Владелец берёт Client-Id и Api-Key в личном кабинете продавца:
Настройки → Seller API → «Сгенерировать ключ». Никаких приложений
регистрировать не нужно — поэтому источник работает сразу.

Что даёт VELOR: отправления (заказы) с составом и суммой, а оплаченные —
ещё и как доход в общие финансы. Дальше их видит вся система: CRM, оборот,
средний чек, цели, AI-директор.
"""
import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need,
                             request, rub, since, stamp, to_stamp)

ID = "ozon"
NAME = "Ozon"
GROUP = "Магазины и маркетплейсы"
GIVES = ["заказы и их состав", "выручку по дням", "статусы и отмены"]
HOWTO = ("Личный кабинет продавца Ozon → Настройки → Seller API → «Сгенерировать ключ». "
         "Скопируйте Client-Id и Api-Key.")
FIELDS = [
    field("client_id", "Client-Id", "Число из раздела Seller API", placeholder="123456"),
    field("api_key", "Api-Key", "Длинный ключ, показывается один раз", secret=True),
]

API = "https://api-seller.ozon.ru"
# Версия v2 этого метода снята с обслуживания и отвечает «404 page not found»
# на любой ключ — из-за неё подключение выглядело как «неверный адрес» даже
# при правильных данных. Рабочая версия — v3 (проверено живым запросом).
POSTINGS = "/v3/posting/fbs/list"

# Статусы Ozon → наши четыре. Всё, что не разобрали, считаем принятым в работу.
_STATUS = {
    "awaiting_packaging": "новый", "awaiting_registration": "новый",
    "awaiting_approve": "новый", "acceptance_in_progress": "принят",
    "awaiting_deliver": "принят", "arbitration": "принят",
    "client_arbitration": "принят", "delivering": "принят",
    "driver_pickup": "принят", "delivered": "выполнен",
    "cancelled": "отменён", "not_accepted": "отменён",
}


def _headers(creds):
    client_id, api_key = need(creds, "client_id", "api_key")
    return {"Client-Id": str(client_id), "Api-Key": str(api_key),
            "Content-Type": "application/json"}


def check(creds, meta):
    """Живая проверка ключей: запрашиваем один заказ за узкое окно.

    Берём тот же эндпоинт, что и синхронизация: если он ответил — значит и
    данные пойдут. Проверять другим методом смысла нет: ключ может иметь права
    на справочники, но не на заказы, и «подключено» окажется обманом.
    """
    request("POST", f"{API}{POSTINGS}", headers=_headers(creds),
            json={"dir": "DESC",
                  "filter": {"since": "2024-01-01T00:00:00.000Z",
                             "to": "2024-01-02T00:00:00.000Z"},
                  "limit": 1, "offset": 0, "translit": True})
    return dict(meta or {}, verified=True)


def _postings(creds, frm, to, offset):
    body = {
        "dir": "DESC",
        "filter": {"since": frm, "to": to},
        "limit": 100, "offset": offset,
        "with": {"analytics_data": False, "financial_data": True},
        "translit": True,
    }
    data = request("POST", f"{API}{POSTINGS}", headers=_headers(creds), json=body)
    result = data.get("result") or {}
    if isinstance(result, list):          # у части аккаунтов ответ — сразу список
        return result
    return result.get("postings") or []


def sync(bid, creds, meta, cursor):
    frm = since(cursor)
    frm_iso = frm.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to_iso = stamp().replace(" ", "T") + ".000Z"

    added, offset, newest = 0, 0, None
    while offset < MAX_ITEMS:
        batch = _postings(creds, frm_iso, to_iso, offset)
        if not batch:
            break
        for p in batch:
            number = p.get("posting_number") or p.get("order_number")
            if not number:
                continue
            products = p.get("products") or []
            text = ", ".join(
                f"{(it.get('name') or 'товар')[:60]}"
                + (f" ×{it['quantity']}" if int(it.get("quantity") or 1) > 1 else "")
                for it in products[:5]) or f"Заказ Ozon {number}"

            # Сумма: сначала финансовые данные (там цена с учётом скидок),
            # иначе считаем по позициям. Ничего не «прикидываем».
            fin = (p.get("financial_data") or {}).get("products") or []
            total = sum(rub(x.get("price")) * int(x.get("quantity") or 1) for x in fin)
            if not total:
                total = sum(rub(it.get("price")) * int(it.get("quantity") or 1) for it in products)

            created = to_stamp(p.get("in_process_at") or p.get("created_at"))
            status = _STATUS.get((p.get("status") or "").lower(), "принят")
            _, is_new = database.upsert_external_order(
                bid, number, ID, text[:400], amount=total, status=status,
                created_at=created)
            if is_new:
                added += 1
            # Доход признаём только по доставленным: отменённое не выручка.
            if status == "выполнен" and total:
                database.upsert_external_finance(
                    bid, f"order-{number}", ID, "income", total,
                    category="Продажи Ozon", note=text[:160],
                    counterparty="Ozon", op_date=day_of(created))
            if created and (newest is None or created > newest):
                newest = created
        if len(batch) < 100:
            break
        offset += 100
    return added, newest
