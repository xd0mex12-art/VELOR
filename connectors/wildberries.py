# -*- coding: utf-8 -*-
"""
Wildberries — продажи и возвраты.

Владелец берёт токен в личном кабинете: Настройки → Доступ к API → создать
токен с категорией «Статистика». Регистрировать приложение не нужно.

Что даёт VELOR: продажи с суммой к перечислению, возвраты (они уменьшают
выручку) и статистика по дням. Возврат приходит отдельной строкой расхода —
поэтому прибыль в VELOR совпадает с реальностью, а не только с продажами.
"""
import database
from connectors.base import (MAX_ITEMS, ConnectorError, day_of, field, need, now,
                             request, rub, since, to_stamp)

ID = "wildberries"
NAME = "Wildberries"
GROUP = "Магазины и маркетплейсы"
GIVES = ["продажи и суммы", "возвраты", "динамику по дням"]
HOWTO = ("Личный кабинет WB → Настройки → Доступ к API → создайте токен "
         "с доступом к «Статистике» и вставьте его сюда.")
FIELDS = [
    field("token", "Токен API", "Категория доступа — «Статистика»", secret=True),
]

API = "https://statistics-api.wildberries.ru"


def _headers(creds):
    return {"Authorization": need(creds, "token")}


def _sales(creds, date_from, flag=0):
    """
    Продажи. flag=0 — всё, что менялось с указанного момента (может быть
    очень много строк), flag=1 — только за одну указанную дату.

    Ответ WB бывает объёмным, поэтому на выгрузку даём больше времени, чем
    на обычный запрос: у активного продавца первая порция за два месяца
    собирается дольше стандартных 25 секунд.
    """
    data = request("GET", f"{API}/api/v1/supplier/sales", headers=_headers(creds),
                   params={"dateFrom": date_from, "flag": flag},
                   timeout=120 if flag == 0 else None)
    if not isinstance(data, list):
        raise ConnectorError("Wildberries вернул неожиданный ответ. Проверьте права токена "
                             "— нужна категория «Статистика».")
    return data


def check(creds, meta):
    """
    Проверка токена одним дешёвым запросом.

    Берём flag=1 за сегодня: тот же эндпоинт и то же право доступа, но ответ
    заведомо маленький. Раньше здесь стоял flag=0 от 2024-01-01 — то есть на
    проверку ключа заказывалась вся история продаж магазина, и у активного
    продавца подключение отваливалось по таймауту ещё до сохранения.
    Пустой список — это тоже успех: значит, доступ есть, продаж сегодня нет.
    """
    _sales(creds, now().strftime("%Y-%m-%dT00:00:00"), flag=1)
    return dict(meta or {}, verified=True)


def sync(bid, creds, meta, cursor):
    date_from = since(cursor).strftime("%Y-%m-%dT%H:%M:%S")
    rows = _sales(creds, date_from)

    added, newest = 0, None
    for s in rows[:MAX_ITEMS]:
        sale_id = s.get("saleID") or s.get("srid")
        if not sale_id:
            continue
        created = to_stamp(s.get("date"))
        name = (s.get("subject") or s.get("brand") or "товар")
        article = s.get("supplierArticle") or ""
        text = f"{name} {article}".strip()[:200] or f"Продажа WB {sale_id}"

        # forPay — сколько реально придёт на счёт; это и есть выручка бизнеса.
        # rub() даёт величину, знак сам по себе ничего не решает: направление
        # определяем ниже и записываем как доход или расход.
        payout = rub(s.get("forPay") or s.get("finishedPrice"))
        # У возврата saleID начинается с 'R', а сумма приходит отрицательной.
        try:
            raw = float(s.get("forPay") or 0)
        except (TypeError, ValueError):
            raw = 0
        is_return = str(sale_id).upper().startswith("R") or raw < 0

        if is_return:
            database.upsert_external_finance(
                bid, f"return-{sale_id}", ID, "expense", payout,
                category="Возвраты Wildberries", note=text,
                counterparty="Wildberries", op_date=day_of(created))
        else:
            _, is_new = database.upsert_external_order(
                bid, sale_id, ID, text, amount=payout, status="выполнен",
                created_at=created)
            if is_new:
                added += 1
            database.upsert_external_finance(
                bid, f"sale-{sale_id}", ID, "income", payout,
                category="Продажи Wildberries", note=text,
                counterparty="Wildberries", op_date=day_of(created))
        if created and (newest is None or created > newest):
            newest = created
    return added, newest
