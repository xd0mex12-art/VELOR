# -*- coding: utf-8 -*-
"""
HubSpot — сделки и контакты через Private App token.

Владелец создаёт приватное приложение у себя: Settings → Integrations →
Private Apps → Create → scopes `crm.objects.deals.read`,
`crm.objects.contacts.read` → Create app → скопировать токен (pat-…).
"""
import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need,
                             request, rub, since, to_stamp)

ID = "hubspot"
NAME = "HubSpot"
GROUP = "CRM и клиенты"
GIVES = ["сделки и суммы", "контакты", "стадии воронки"]
HOWTO = ("HubSpot → Settings → Integrations → Private Apps → Create private app. "
         "Дайте права crm.objects.deals.read и crm.objects.contacts.read, "
         "скопируйте токен (начинается на pat-).")
FIELDS = [
    field("token", "Private App token", "Начинается на pat-", secret=True),
]

API = "https://api.hubapi.com"

_STAGE = {"closedwon": "выполнен", "closedlost": "отменён",
          "appointmentscheduled": "новый", "qualifiedtobuy": "принят"}


def _headers(creds):
    return {"Authorization": "Bearer " + need(creds, "token"),
            "Content-Type": "application/json"}


def check(creds, meta):
    request("GET", f"{API}/crm/v3/objects/deals", headers=_headers(creds),
            params={"limit": 1})
    return dict(meta or {}, verified=True)


def _status(stage: str) -> str:
    return _STAGE.get((stage or "").lower(), "принят")


def sync(bid, creds, meta, cursor):
    frm = since(cursor)
    frm_ms = int(frm.timestamp() * 1000)
    added, newest, after = 0, None, None
    fetched = 0

    while fetched < MAX_ITEMS:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": frm_ms}]}],
            "properties": ["dealname", "amount", "dealstage", "createdate"],
            "limit": 100,
        }
        if after:
            body["after"] = after
        data = request("POST", f"{API}/crm/v3/objects/deals/search",
                       headers=_headers(creds), json=body)
        results = data.get("results") or []
        if not results:
            break
        for d in results:
            did = d.get("id")
            props = d.get("properties") or {}
            if not did:
                continue
            amount = rub(props.get("amount"))
            created = to_stamp(props.get("createdate"))
            status = _status(props.get("dealstage"))
            title = (props.get("dealname") or f"Сделка {did}")[:200]

            _, is_new = database.upsert_external_order(
                bid, did, ID, title, amount=amount, status=status, created_at=created)
            if is_new:
                added += 1
            if status == "выполнен" and amount:
                database.upsert_external_finance(
                    bid, f"deal-{did}", ID, "income", amount,
                    category="Продажи (CRM)", note=title[:160],
                    counterparty="HubSpot", op_date=day_of(created))
            if created and (newest is None or created > newest):
                newest = created
        fetched += len(results)
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break

    _pull_contacts(bid, creds, frm_ms)
    return added, newest


def _pull_contacts(bid, creds, frm_ms):
    """Контакты отдельно: в HubSpot они не приходят вместе со сделкой."""
    try:
        data = request("POST", f"{API}/crm/v3/objects/contacts/search",
                       headers=_headers(creds),
                       json={"filterGroups": [{"filters": [
                                 {"propertyName": "lastmodifieddate",
                                  "operator": "GTE", "value": frm_ms}]}],
                             "properties": ["firstname", "lastname", "phone", "email"],
                             "limit": 100})
    except ConnectorError:
        return                                    # контакты не критичны для сделок
    for c in (data.get("results") or []):
        p = c.get("properties") or {}
        name = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x).strip()
        database.upsert_external_client(bid, c.get("id"), ID,
                                        name=name or p.get("email"), phone=p.get("phone"))
