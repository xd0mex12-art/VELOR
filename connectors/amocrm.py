# -*- coding: utf-8 -*-
"""
amoCRM — сделки и контакты.

Владелец создаёт интеграцию в своём аккаунте (Настройки → Интеграции →
Создать интеграцию → Внешняя) и берёт там долгоживущий токен. Регистрировать
наше приложение в amoCRM не нужно — токен выпускает сам клиент у себя.

Что даёт VELOR: сделки становятся заказами с суммой и стадией, контакты —
клиентами, успешные сделки — доходом. Именно этого не хватало ИИ, чтобы
говорить о воронке, а не только о заявках из Telegram.
"""
import datetime

import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need,
                             public_url, request, rub, since, to_stamp)

ID = "amocrm"
NAME = "amoCRM"
GROUP = "CRM и клиенты"
GIVES = ["сделки и суммы", "контакты клиентов", "стадии воронки"]
HOWTO = ("amoCRM → Настройки → Интеграции → Создать интеграцию («Внешняя») → "
         "вкладка «Ключи и доступы» → скопируйте долгосрочный токен. "
         "Поддомен — первое слово в адресе кабинета: company.amocrm.ru → company.")
FIELDS = [
    field("subdomain", "Поддомен", "Например: company (из company.amocrm.ru)",
          placeholder="company"),
    field("token", "Долгосрочный токен", "Длинная строка из «Ключи и доступы»", secret=True),
]


def _base(creds):
    sub = need(creds, "subdomain").strip().lower()
    sub = sub.replace("https://", "").replace("http://", "").split(".")[0].strip("/")
    if not sub:
        raise ConnectorError("Укажите поддомен: company из адреса company.amocrm.ru")
    host = f"https://{sub}.amocrm.ru"
    public_url(host)                     # адрес вводит человек — проверяем (SSRF)
    return host + "/api/v4"


def _headers(creds):
    return {"Authorization": "Bearer " + need(creds, "token"),
            "Content-Type": "application/json"}


def check(creds, meta):
    data = request("GET", f"{_base(creds)}/account", headers=_headers(creds))
    return dict(meta or {}, account=(data or {}).get("name"), verified=True)


def _statuses(creds):
    """Карта id стадии → наш статус. Успех/провал в amoCRM всегда 142/143."""
    out = {142: "выполнен", 143: "отменён"}
    try:
        data = request("GET", f"{_base(creds)}/leads/pipelines", headers=_headers(creds))
    except ConnectorError:
        return out
    for p in ((data.get("_embedded") or {}).get("pipelines") or []):
        for s in (((p.get("_embedded") or {}).get("statuses")) or []):
            sid = s.get("id")
            if sid in out:
                continue
            name = (s.get("name") or "").lower()
            out[sid] = "новый" if ("первич" in name or "неразобр" in name) else "принят"
    return out


def sync(bid, creds, meta, cursor):
    frm = int(since(cursor).timestamp())
    statuses = _statuses(creds)
    added, newest, page = 0, None, 1

    while (page - 1) * 250 < MAX_ITEMS:
        data = request("GET", f"{_base(creds)}/leads", headers=_headers(creds),
                       params={"page": page, "limit": 250,
                               "filter[updated_at][from]": frm,
                               "with": "contacts"})
        if not data:                     # amoCRM отдаёт 204 (пустое тело), когда данных нет
            break
        leads = (data.get("_embedded") or {}).get("leads") or []
        if not leads:
            break
        for l in leads:
            lid = l.get("id")
            if not lid:
                continue
            amount = rub(l.get("price"))
            created = (to_stamp(datetime.datetime.utcfromtimestamp(
                int(l["created_at"])).isoformat()) if l.get("created_at") else None)
            status = statuses.get(l.get("status_id"), "принят")
            title = (l.get("name") or f"Сделка №{lid}")[:200]

            client_id = None
            contacts = ((l.get("_embedded") or {}).get("contacts") or [])
            if contacts:
                client_id = _pull_contact(bid, creds, contacts[0].get("id"))

            _, is_new = database.upsert_external_order(
                bid, lid, ID, title, amount=amount, status=status,
                client_id=client_id, created_at=created)
            if is_new:
                added += 1
            if status == "выполнен" and amount:
                database.upsert_external_finance(
                    bid, f"lead-{lid}", ID, "income", amount,
                    category="Продажи (CRM)", note=title[:160],
                    counterparty="amoCRM", op_date=day_of(created))
            if created and (newest is None or created > newest):
                newest = created
        if not ((data.get("_links") or {}).get("next")):
            break
        page += 1
    return added, newest


def _pull_contact(bid, creds, contact_id):
    """Контакт сделки в нашу CRM. Ошибку глотаем — заказ важнее контакта."""
    if not contact_id:
        return None
    try:
        c = request("GET", f"{_base(creds)}/contacts/{contact_id}", headers=_headers(creds))
    except ConnectorError:
        return None
    if not c:
        return None
    phone = None
    for f in (c.get("custom_fields_values") or []):
        if (f.get("field_code") or "").upper() == "PHONE":
            vals = f.get("values") or []
            if vals:
                phone = vals[0].get("value")
            break
    cid, _ = database.upsert_external_client(bid, contact_id, ID,
                                             name=(c.get("name") or None), phone=phone)
    return cid
