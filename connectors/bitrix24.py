# -*- coding: utf-8 -*-
"""
Bitrix24 — сделки и контакты через ВХОДЯЩИЙ ВЕБХУК.

Самый простой способ подключения из всех: владелец создаёт вебхук у себя в
портале и получает готовую ссылку вида
    https://company.bitrix24.ru/rest/1/abcdef123456/
Никаких OAuth-приложений, никакой регистрации на нашей стороне.

Что даёт VELOR: сделки становятся заказами (с суммой и стадией), контакты —
клиентами, выигранные сделки — доходом. Воронка клиента наконец видна ИИ.
"""
import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need,
                             public_url, request, rub, since, to_stamp)

ID = "bitrix24"
NAME = "Bitrix24"
GROUP = "CRM и клиенты"
GIVES = ["сделки и суммы", "контакты клиентов", "стадии воронки"]
HOWTO = ("Портал Bitrix24 → Приложения → Разработчикам → Другое → Входящий вебхук. "
         "Отметьте права CRM (crm) и скопируйте ссылку вида "
         "https://вашпортал.bitrix24.ru/rest/1/ключ/")
FIELDS = [
    field("webhook", "Ссылка входящего вебхука",
          "Целиком, вместе с /rest/…/ на конце", secret=True,
          placeholder="https://company.bitrix24.ru/rest/1/abcdef/"),
]

# Стадии Bitrix кодируются как C1:WON / C1:LOSE / NEW / PREPARATION…
_STAGE = {"NEW": "новый", "WON": "выполнен", "LOSE": "отменён", "APOLOGY": "отменён"}


def _base(creds):
    url = need(creds, "webhook").strip()
    if "/rest/" not in url:
        raise ConnectorError("Это не похоже на ссылку вебхука. Она должна содержать /rest/ "
                             "и заканчиваться косой чертой.")
    # Адрес портала вводит человек — проверяем, что он публичный (защита от SSRF).
    return public_url(url).rstrip("/") + "/"


def _call(creds, method, params=None):
    data = request("POST", _base(creds) + method, json=params or {})
    if isinstance(data, dict) and data.get("error"):
        raise ConnectorError(data.get("error_description")
                             or "Bitrix24 отклонил запрос. Проверьте права вебхука (нужен CRM).")
    return data


def check(creds, meta):
    data = _call(creds, "crm.deal.fields")
    if not isinstance(data, dict) or "result" not in data:
        raise ConnectorError("Вебхук ответил, но без данных CRM. Проверьте, что в правах "
                             "вебхука отмечен раздел CRM.")
    return dict(meta or {}, verified=True)


def _stage_to_status(stage_id: str) -> str:
    tail = (stage_id or "").split(":")[-1].upper()
    return _STAGE.get(tail, "принят")


def sync(bid, creds, meta, cursor):
    frm = since(cursor).strftime("%Y-%m-%dT%H:%M:%S")
    added, newest, start = 0, None, 0

    while start < MAX_ITEMS:
        data = _call(creds, "crm.deal.list", {
            "order": {"DATE_MODIFY": "DESC"},
            "filter": {">DATE_MODIFY": frm},
            "select": ["ID", "TITLE", "OPPORTUNITY", "STAGE_ID", "DATE_CREATE",
                       "CONTACT_ID", "CLOSED"],
            "start": start,
        })
        deals = data.get("result") or []
        if not deals:
            break
        for d in deals:
            did = d.get("ID")
            if not did:
                continue
            amount = rub(d.get("OPPORTUNITY"))
            created = to_stamp(d.get("DATE_CREATE"))
            status = _stage_to_status(d.get("STAGE_ID"))
            title = (d.get("TITLE") or f"Сделка №{did}")[:200]

            client_id = None
            if d.get("CONTACT_ID") and str(d["CONTACT_ID"]) != "0":
                client_id = _pull_contact(bid, creds, d["CONTACT_ID"])

            _, is_new = database.upsert_external_order(
                bid, did, ID, title, amount=amount, status=status,
                client_id=client_id, created_at=created)
            if is_new:
                added += 1
            if status == "выполнен" and amount:
                database.upsert_external_finance(
                    bid, f"deal-{did}", ID, "income", amount,
                    category="Продажи (CRM)", note=title[:160],
                    counterparty="Bitrix24", op_date=day_of(created))
            if created and (newest is None or created > newest):
                newest = created
        nxt = data.get("next")
        if nxt is None:
            break
        start = nxt
    return added, newest


def _pull_contact(bid, creds, contact_id):
    """Подтянуть контакт сделки в нашу CRM. Ошибку глотаем: заказ важнее контакта."""
    try:
        res = (_call(creds, "crm.contact.get", {"id": contact_id}) or {}).get("result") or {}
    except ConnectorError:
        return None
    if not res:
        return None
    name = " ".join(x for x in (res.get("NAME"), res.get("LAST_NAME")) if x).strip()
    phones = res.get("PHONE") or []
    phone = phones[0].get("VALUE") if phones and isinstance(phones[0], dict) else None
    cid, _ = database.upsert_external_client(bid, contact_id, ID,
                                             name=name or None, phone=phone)
    return cid
