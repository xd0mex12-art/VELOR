# -*- coding: utf-8 -*-
"""
Реестр коннекторов и единая точка синхронизации.

Здесь живёт вся работа с секретами (шифрование/расшифровка) и с базой, а сами
коннекторы остаются чистыми: получили ключи — вернули данные. Так добавление
нового сервиса не требует править ни server.py, ни фронт.
"""
from __future__ import annotations

import json
import logging

import database
import secretbox
import signals
from connectors.base import ConnectorError

from connectors import (amocrm, bitrix24, cloudpayments, hubspot, ozon, shopify,
                        slack, stripe, wildberries, woocommerce, yookassa)

log = logging.getLogger("velor.connectors")

# Порядок = порядок появления в кабинете внутри своей группы.
MODULES = [ozon, wildberries, shopify, woocommerce,
           yookassa, cloudpayments, stripe,
           bitrix24, amocrm, hubspot, slack]

REGISTRY = {m.ID: m for m in MODULES}


def catalog() -> list[dict]:
    """Описание всех коннекторов для фронта: поля формы, подсказки, что даёт."""
    return [{
        "id": m.ID,
        "name": m.NAME,
        "group": m.GROUP,
        "gives": m.GIVES,
        "howto": m.HOWTO,
        "fields": m.FIELDS,
    } for m in MODULES]


def describe(provider: str) -> dict | None:
    m = REGISTRY.get(provider)
    if not m:
        return None
    return {"id": m.ID, "name": m.NAME, "group": m.GROUP, "gives": m.GIVES,
            "howto": m.HOWTO, "fields": m.FIELDS}


def _creds(conn_row: dict) -> dict:
    """Расшифровать ключи подключения."""
    raw = secretbox.open_(conn_row.get("credentials") or "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def connect(business_id: int, provider: str, creds: dict) -> dict:
    """
    Подключить сервис: проверяем ключи ЖИВЫМ запросом и только потом сохраняем.

    Это принципиально: «подключено» в кабинете должно означать, что данные
    действительно идут. Не прошла проверка — не сохраняем и говорим почему.
    """
    m = REGISTRY.get(provider)
    if not m:
        raise ConnectorError("Неизвестный источник.")

    # Если поле-секрет прислали пустым, а подключение уже есть — оставляем старый
    # ключ: в интерфейсе он показан звёздочками, и заставлять вводить заново глупо.
    existing = database.get_connection(business_id, provider, with_secrets=True)
    if existing:
        old = _creds(existing)
        for f in m.FIELDS:
            if not (creds.get(f["key"]) or "").strip() and old.get(f["key"]):
                creds[f["key"]] = old[f["key"]]

    meta = m.check(creds, (existing or {}).get("meta") or {}) or {}
    blob = secretbox.seal(json.dumps(creds, ensure_ascii=False))
    database.save_connection(business_id, provider, blob, meta)
    return database.get_connection(business_id, provider)


def sync(business_id: int, provider: str) -> dict:
    """
    Забрать новые данные одного сервиса.

    Ошибку НЕ бросаем наружу: записываем в подключение (владелец видит её в
    карточке) и возвращаем результат. Одна упавшая интеграция не должна ломать
    ни страницу, ни фоновую синхронизацию остальных.
    """
    m = REGISTRY.get(provider)
    row = database.get_connection(business_id, provider, with_secrets=True)
    if not m or not row:
        return {"ok": False, "added": 0, "error": "Источник не подключён."}
    try:
        added, cursor = m.sync(business_id, _creds(row), row.get("meta") or {},
                               row.get("cursor"))
    except ConnectorError as e:
        database.mark_connection_synced(business_id, provider, error=str(e))
        return {"ok": False, "added": 0, "error": str(e)}
    except Exception as e:                       # неожиданное — не показываем внутренности
        log.exception("Синхронизация %s упала (biz %s)", provider, business_id)
        database.mark_connection_synced(
            business_id, provider,
            error="Внутренняя ошибка синхронизации. Мы записали её и разберёмся.")
        return {"ok": False, "added": 0, "error": "Внутренняя ошибка синхронизации."}

    database.mark_connection_synced(business_id, provider, added=added, cursor=cursor)
    if added:
        # Новые заказы и деньги должны сразу отражаться на Директоре и брифинге.
        try:
            signals.react(business_id, "finance")
            signals.react(business_id, "order")
        except Exception:
            log.exception("Реактивный слой после синхронизации %s", provider)
        database.log_event(business_id, "integration",
                           f"{m.NAME}: загружено {added}",
                           "Новые данные уже учтены в аналитике и советах",
                           once_key=f"sync:{provider}")
    return {"ok": True, "added": added, "error": None}


def sync_all(business_id: int) -> dict:
    """Синхронизировать все подключения бизнеса. Возвращает сводку по каждому."""
    out = {}
    for provider in database.connected_providers(business_id):
        if provider in REGISTRY:
            out[provider] = sync(business_id, provider)
    return out


def status(business_id: int) -> dict:
    """Что подключено, когда синхронизировалось, что сломалось — для кабинета."""
    rows = {c["provider"]: c for c in database.list_connections(business_id)}
    items = []
    for m in MODULES:
        c = rows.get(m.ID)
        items.append({
            "id": m.ID, "name": m.NAME, "group": m.GROUP, "gives": m.GIVES,
            "howto": m.HOWTO, "fields": m.FIELDS,
            "connected": bool(c),
            "status": (c or {}).get("status"),
            "last_sync_at": (c or {}).get("last_sync_at"),
            "last_error": (c or {}).get("last_error"),
            "items_total": (c or {}).get("items_total") or 0,
            "meta": (c or {}).get("meta") or {},
        })
    return {"items": items}
