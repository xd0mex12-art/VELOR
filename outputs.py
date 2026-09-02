# -*- coding: utf-8 -*-
"""
РЕЗУЛЬТАТЫ — то, что владелец уносит из VELOR наружу.

До этого слоя продукт умел всё, кроме последнего шага. Он принимал материал,
понимал его, замечал проблему, готовил действие — а на вопрос «сделай мне
отчёт по продажам за август» в кабинете стояла страница «Инструменты» с пятью
карточками «Скоро». Возможность была объявлена, но её не существовало.

Здесь она появляется. Правила, из которых всё остальное следует:

1. ЦИФРЫ БЕРУТСЯ ИЗ БАЗЫ, А НЕ ИЗ МОДЕЛИ. Каждое число в отчёте посчитано
   тем же запросом, что показывает кабинет. ИИ не участвует в счёте вовсе:
   его роль — комментарий, и он подписан комментарием.

2. ДОГАДКА НАЗВАНА ДОГАДКОЙ. Блок типа `inference` рисуется отдельно и с
   подписью. Смешать вывод модели с посчитанным значило бы обесценить оба —
   ровно то же правило, по которому живут находки (initiatives.py).

3. РЕЗУЛЬТАТ РАБОТАЕТ БЕЗ ИИ. Отчёт без ИИ теряет комментарий, но остаётся
   отчётом. Письмо и КП собираются по шаблону из реальных данных. Иначе
   «результат» был бы доступен только когда чужой сервис в настроении.

4. ДОКУМЕНТ ХРАНИТСЯ ЦЕЛИКОМ. Числа в отчёте — снимок на момент сборки.
   Пересчитать их при открытии заново значило бы показать под тем же именем
   другой документ.

5. НАРУЖУ НИЧЕГО НЕ УХОДИТ САМО. Письмо здесь только создаётся. Отправка —
   внешнее действие, и она обязана идти через actions.py; почтового канала у
   VELOR сегодня нет, поэтому и отправки нет — а не «как будто есть».

Чего здесь нет
──────────────
Своего генератора PDF, своего хранилища и своей выборки данных. PDF рисует
reportlab тем же шрифтом, что экспорт таблиц (exporters.pdf_font), файлы
кладёт storage.py, цифры считает database.py.
"""
from __future__ import annotations

import datetime
import io
import logging

import database
import exporters
import storage

log = logging.getLogger("velor.outputs")


class OutputError(Exception):
    """Результат собрать нельзя, и человеку сказано почему."""


# ── что VELOR умеет сделать ────────────────────────────────────────────────
#
# Реестр, а не список карточек в вёрстке. Правило то же, что в actions.py: в
# нём нет ни одной строки «на будущее» — если ниже нет сборщика, наверху нет
# и карточки. Список возможностей, обещающий несуществующее, хуже отсутствия
# списка: по нему принимают решения.

PDF = "pdf"
TEXT = "text"

KINDS = {
    "sales_report": {
        "title": "Отчёт по продажам",
        "about": "Заявки, выручка, воронка и клиенты за период — с сравнением "
                 "с предыдущим таким же периодом.",
        "group": "Отчёты",
        "file": PDF,
        "params": [
            {"name": "days", "title": "Период", "type": "choice", "default": 30,
             "choices": [{"value": 7, "title": "7 дней"},
                         {"value": 30, "title": "30 дней"},
                         {"value": 90, "title": "90 дней"}]},
        ],
    },
    "finance_report": {
        "title": "Финансовый отчёт",
        "about": "Доходы, расходы, прибыль и маржа за период, разбивка по "
                 "категориям и выплаты людям.",
        "group": "Отчёты",
        "file": PDF,
        "params": [
            {"name": "days", "title": "Период", "type": "choice", "default": 30,
             "choices": [{"value": 7, "title": "7 дней"},
                         {"value": 30, "title": "30 дней"},
                         {"value": 90, "title": "90 дней"}]},
        ],
    },
    "offer": {
        "title": "Коммерческое предложение",
        "about": "КП с вашими услугами и ценами из памяти бизнеса. Можно "
                 "адресовать конкретному клиенту.",
        "group": "Документы",
        "file": PDF,
        "params": [
            {"name": "client_id", "title": "Кому", "type": "client", "default": None,
             "optional": True},
            {"name": "note", "title": "Что учесть", "type": "text", "default": "",
             "optional": True,
             "placeholder": "Напр.: нужен монтаж под ключ, срок — две недели"},
        ],
    },
    "client_letter": {
        "title": "Письмо клиенту",
        "about": "Черновик письма по конкретной заявке или клиенту: "
                 "напоминание, статус заказа, благодарность, возвращение.",
        "group": "Письма",
        "file": TEXT,
        "params": [
            {"name": "client_id", "title": "Клиент", "type": "client", "default": None,
             "optional": True},
            {"name": "order_id", "title": "Заявка №", "type": "order", "default": None,
             "optional": True},
            {"name": "purpose", "title": "О чём письмо", "type": "choice",
             "default": "status",
             "choices": [{"value": "status", "title": "Статус заказа"},
                         {"value": "reminder", "title": "Напоминание"},
                         {"value": "thanks", "title": "Благодарность"},
                         {"value": "return", "title": "Позвать вернуться"}]},
            {"name": "note", "title": "Что учесть", "type": "text", "default": "",
             "optional": True, "placeholder": "Напр.: перенесли доставку на пятницу"},
        ],
    },
}

PURPOSE_RU = {
    "status": "рассказать, на каком этапе заказ",
    "reminder": "напомнить о себе и подтолкнуть к решению",
    "thanks": "поблагодарить за покупку",
    "return": "позвать вернуться после долгой паузы",
}

# Отправка письма — внешнее действие, и она обязана идти через actions.py.
# Почтового канала у VELOR сегодня нет, исполнителя нет, значит нет и права:
# заводить в реестре действие без исполнителя запрещено там же (actions.py).
# Поэтому здесь честная константа, а не кнопка, которая ничего не делает.
CAN_SEND_EMAIL = False


def catalog(business_id: int) -> dict:
    """Что можно собрать. Форма для страницы результатов."""
    import ai
    return {
        "kinds": [{"id": k, **{f: v for f, v in meta.items()}} for k, meta in KINDS.items()],
        "ai": ai.ai_available(),
        "can_send_email": CAN_SEND_EMAIL,
    }


# ── документ ───────────────────────────────────────────────────────────────
#
# Один формат для экрана и для PDF. Раньше «сделать PDF» означало бы отдельную
# вёрстку под каждый вид, и содержимое файла со временем разошлось бы с тем,
# что человек видел на экране перед скачиванием.

def _doc(title, subtitle, meta, blocks, sources):
    return {"title": title, "subtitle": subtitle, "meta": meta,
            "blocks": [b for b in blocks if b], "sources": sources}


def _kv(title, rows, note=None):
    rows = [[k, v] for k, v in rows if v is not None]
    return {"type": "kv", "title": title, "rows": rows, "note": note} if rows else None


def _table(title, columns, rows, note=None):
    return {"type": "table", "title": title, "columns": columns,
            "rows": rows, "note": note} if rows else None


def _text(title, body):
    body = (body or "").strip()
    return {"type": "text", "title": title, "body": body} if body else None


def _inference(body):
    """Догадка модели. Отдельный тип, чтобы её нельзя было случайно
    отрисовать наравне с посчитанным."""
    body = (body or "").strip()
    return {"type": "inference", "title": "Комментарий VELOR",
            "body": body,
            "note": "Это разбор ИИ по приведённым выше числам, а не расчёт. "
                    "Проверяйте выводы."} if body else None


# ── деньги и числа по-русски ───────────────────────────────────────────────

def _money(n):
    return f"{int(n or 0):,}".replace(",", " ") + " ₽"


def _num(n):
    return f"{int(n or 0):,}".replace(",", " ")


def _delta(cur, prev, money=True):
    """«Было → стало» одной строкой. Проценты не считаем от нуля: рост с нуля
    не «на 100%», а «с нуля», и подставлять сюда число значило бы соврать."""
    cur, prev = int(cur or 0), int(prev or 0)
    fmt = _money if money else _num
    if not prev:
        return f"{fmt(cur)} (в прошлом периоде — {fmt(prev)})"
    pct = round((cur - prev) * 100 / prev)
    sign = "+" if pct > 0 else ""
    return f"{fmt(cur)} ({sign}{pct}% к прошлому периоду, было {fmt(prev)})"


def _period_ru(days):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(days))
    return f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"


def _biz(bid):
    return database.get_business(bid) or {"name": "Бизнес"}


# ── сборщики ───────────────────────────────────────────────────────────────

def _commentary(business, title, facts_text):
    """Комментарий ИИ к посчитанному. Отсутствие комментария — не ошибка:
    отчёт без него остаётся отчётом, а вот отчёт, который не собрался из-за
    недоступного чужого сервиса, бесполезен."""
    import ai
    if not ai.ai_available():
        return None, "rules"
    try:
        return (ai.report_commentary(business, title, facts_text) or None), "llm"
    except Exception:
        log.exception("Комментарий к результату не собрался")
        return None, "rules"


def _sales_report(bid, params):
    days = int(params.get("days") or 30)
    if days not in (7, 30, 90):
        days = 30
    business = _biz(bid)

    cur = database.orders_period(bid, days=days)
    prev = database.orders_period(bid, days=days, offset=days)
    money = database.money_period(bid, days=days)
    money_prev = database.money_period(bid, days=days, offset=days)
    leads = database.leads_period(bid, days=days)
    leads_prev = database.leads_period(bid, days=days, offset=days)
    new_clients = database.counts_period(bid, "clients", days=days)
    new_clients_prev = database.counts_period(bid, "clients", days=days, offset=days)
    by_service = database.service_revenue(bid, days=days)
    funnel = database.leads_overview(bid)

    period = _period_ru(days)
    summary = _kv("Итоги периода", [
        ("Заявок", _delta(cur["count"], prev["count"], money=False)),
        ("Сумма заявок", _delta(cur["amount"], prev["amount"])),
        ("Средний чек", _money(cur["avg"]) if cur["avg"] else "нет заявок с суммой"),
        ("Выручка по деньгам", _delta(money["income"], money_prev["income"])),
        ("Новых клиентов", _delta(new_clients, new_clients_prev, money=False)),
    ], note=(f"Сумма проставлена у {cur['with_amount']} заявок из {cur['count']} — "
             f"средний чек считается только по ним."
             if cur["count"] and cur["with_amount"] != cur["count"] else None))

    funnel_block = _kv("Возможности за период", [
        ("Появилось", _delta(leads.get("created", 0), leads_prev.get("created", 0), money=False)),
        ("Выиграно", _delta(leads.get("won", 0), leads_prev.get("won", 0), money=False)),
        ("Потеряно", _delta(leads.get("lost", 0), leads_prev.get("lost", 0), money=False)),
        ("Открыто сейчас", _num(funnel["open"])),
        ("Конверсия по закрытым", f"{funnel['conversion']}% "
                                  f"(из {_num(funnel['closed'])} закрытых)"
                                  if funnel["closed"] else "закрытых возможностей ещё не было"),
    ])

    services = _table("Что покупали", ["Позиция", "Заявок", "Сумма заявок"],
                      [[s["title"], _num(s["orders"]), _money(s["amount"])]
                       for s in by_service[:15]],
                      note="Заявка с двумя позициями даёт обеим свою полную сумму — "
                           "доли по позициям не складываются в 100%.")

    facts = (f"Отчёт по продажам за {days} дн. ({period}). "
             f"Заявок {cur['count']} против {prev['count']} в прошлом периоде. "
             f"Сумма заявок {cur['amount']} ₽ против {prev['amount']} ₽. "
             f"Выручка по деньгам {money['income']} ₽ против {money_prev['income']} ₽. "
             f"Новых клиентов {new_clients} против {new_clients_prev}. "
             f"Возможностей появилось {leads.get('created', 0)}, "
             f"выиграно {leads.get('won', 0)}, потеряно {leads.get('lost', 0)}.")
    comment, engine = _commentary(business, "продажи", facts)

    doc = _doc(
        "Отчёт по продажам",
        business.get("name") or "Бизнес",
        [("Период", period), ("Глубина", f"{days} дн."),
         ("Собран", datetime.date.today().strftime("%d.%m.%Y"))],
        [summary, funnel_block, services, _inference(comment)],
        ["Заявки", "Финансовые операции", "Возможности", "Клиенты"],
    )
    return doc, None, engine


def _finance_report(bid, params):
    days = int(params.get("days") or 30)
    if days not in (7, 30, 90):
        days = 30
    business = _biz(bid)

    cur = database.money_period(bid, days=days)
    prev = database.money_period(bid, days=days, offset=days)
    expense_cats = database.category_period(bid, kind="expense", days=days)
    income_cats = database.category_period(bid, kind="income", days=days)
    payroll = database.payroll_period(bid, days=days)
    total = database.finance_summary(bid)

    margin = (round(cur["profit"] * 100 / cur["income"], 1) if cur["income"] else None)
    period = _period_ru(days)

    summary = _kv("Деньги за период", [
        ("Доход", _delta(cur["income"], prev["income"])),
        ("Расход", _delta(cur["expense"], prev["expense"])),
        ("Прибыль", _delta(cur["profit"], prev["profit"])),
        # Маржа при нулевой выручке не «0%» — её просто нет. То же правило,
        # что в database.finance_summary; второго ответа быть не должно.
        ("Маржа", f"{margin}%" if margin is not None else "нет выручки — делить не на что"),
        ("Операций в периоде", _num(cur["entries"])),
    ], note=(None if cur["entries"] else
             "За период не записано ни одной операции — числа выше это и означают."))

    exp = sorted(expense_cats.items(), key=lambda kv: -kv[1]["total"])
    inc = sorted(income_cats.items(), key=lambda kv: -kv[1]["total"])
    exp_block = _table("Расходы по категориям", ["Категория", "Сумма", "Операций"],
                       [[k, _money(v["total"]), _num(v["n"])] for k, v in exp[:20]])
    inc_block = _table("Доходы по категориям", ["Категория", "Сумма", "Операций"],
                       [[k, _money(v["total"]), _num(v["n"])] for k, v in inc[:20]])

    people = _kv("Выплаты людям", [
        ("Всего за период", _money(payroll.get("total"))),
        ("Выплат", _num(payroll.get("n"))),
        ("Человек", _num(payroll.get("people"))),
    ]) if payroll.get("total") else None

    all_time = _kv("За всё время", [
        ("Доход", _money(total["income"])),
        ("Расход", _money(total["expense"])),
        ("Прибыль", _money(total["profit"])),
        ("Маржа", f"{total['margin']}%" if total["margin"] is not None else "нет выручки"),
        ("Записей", _num(total["entries"])),
    ])

    facts = (f"Финансы за {days} дн. ({period}). Доход {cur['income']} ₽ "
             f"против {prev['income']} ₽ в прошлом периоде. Расход {cur['expense']} ₽ "
             f"против {prev['expense']} ₽. Прибыль {cur['profit']} ₽. "
             f"Маржа {margin if margin is not None else 'нет выручки'}. "
             "Крупнейшие расходы: "
             + ("; ".join(f"{k} {v['total']} ₽" for k, v in exp[:5]) or "нет"))
    comment, engine = _commentary(business, "финансы", facts)

    doc = _doc(
        "Финансовый отчёт",
        business.get("name") or "Бизнес",
        [("Период", period), ("Глубина", f"{days} дн."),
         ("Собран", datetime.date.today().strftime("%d.%m.%Y"))],
        [summary, exp_block, inc_block, people, all_time, _inference(comment)],
        ["Финансовые операции", "Выплаты сотрудникам"],
    )
    return doc, None, engine


def _catalog_lines(bid):
    """Услуги и товары с ценами — из памяти бизнеса, а не из головы."""
    out = []
    for kind in ("service", "product"):
        for f in database.list_facts(bid, kind=kind):
            data = f.get("data") or {}
            price = data.get("price") if isinstance(data, dict) else None
            out.append({
                "title": f.get("title") or "без названия",
                "body": (f.get("body") or "").strip(),
                "price": price,
                "verified": bool(f.get("verified", 1)),
            })
    return out


def _offer(bid, params):
    business = _biz(bid)
    client = None
    if params.get("client_id"):
        client = database.get_client(int(params["client_id"]), bid)
        if not client:
            raise OutputError("Такого клиента нет в вашей базе.")
    items = _catalog_lines(bid)
    if not items:
        raise OutputError(
            "В памяти бизнеса пока нет ни услуг, ни товаров — предложение будет "
            "пустым. Добавьте их во «Входящих» или в базе знаний.")

    note = (params.get("note") or "").strip()[:400]
    rows = [[i["title"],
             (i["body"][:160] if i["body"] else "—"),
             (_money(i["price"]) if i["price"] else "по запросу")] for i in items[:40]]
    unverified = [i["title"] for i in items[:40] if not i["verified"]]

    facts = ("Компания: " + (business.get("name") or "—")
             + ". Чем занимается: " + ((business.get("about") or "—")[:300])
             + ". Кому: " + (client.get("name") if client else "без адресата")
             + ". Позиции: " + "; ".join(
                 f"{i['title']}"
                 + (f" — {i['price']} ₽" if i["price"] else "")
                 for i in items[:20])
             + (". Учесть: " + note if note else ""))

    body, engine = _write("offer", business, facts, _offer_fallback(business))

    doc = _doc(
        "Коммерческое предложение",
        (f"Для: {client['name']}" if client and client.get("name")
         else (business.get("name") or "Бизнес")),
        [("От кого", business.get("name") or "—"),
         ("Кому", (client.get("name") if client else "—")),
         ("Дата", datetime.date.today().strftime("%d.%m.%Y"))],
        [_text(None, body),
         _table("Услуги и цены", ["Позиция", "Что входит", "Цена"], rows,
                note=("Позиции без подтверждения владельцем: "
                      + ", ".join(unverified) if unverified else None)),
         _text("О компании", (business.get("about") or "").strip()[:600])],
        ["Память бизнеса (услуги и товары)"] + (["Карточка клиента"] if client else []),
    )
    return doc, body, engine


def _letter(bid, params):
    business = _biz(bid)
    client = order = None
    if params.get("order_id"):
        order = database.get_order(int(params["order_id"]), bid)
        if not order:
            raise OutputError("Такой заявки нет в вашей базе.")
        if order.get("client_id"):
            client = database.get_client(int(order["client_id"]), bid)
    if client is None and params.get("client_id"):
        client = database.get_client(int(params["client_id"]), bid)
        if not client:
            raise OutputError("Такого клиента нет в вашей базе.")
    if client is None and order is None:
        raise OutputError("Выберите клиента или заявку — письмо без адресата "
                          "пришлось бы придумывать.")

    purpose = params.get("purpose") or "status"
    if purpose not in PURPOSE_RU:
        purpose = "status"
    note = (params.get("note") or "").strip()[:400]

    facts_parts = ["Компания: " + (business.get("name") or "—")]
    if client:
        facts_parts.append(
            f"Клиент: {client.get('name') or 'без имени'}, "
            f"заказов {client.get('orders_count') or 0}, "
            f"на сумму {int(client.get('total_spent') or 0)} ₽"
            + (f", последний заказ {str(client.get('last_order_at'))[:10]}"
               if client.get("last_order_at") else ""))
    if order:
        facts_parts.append(
            f"Заявка №{order['id']}: {(order.get('text') or '').strip()[:300]}"
            f", статус «{order.get('status') or '—'}»"
            + (f", сумма {int(order['amount'])} ₽" if order.get("amount") else "")
            + (f", нужно к {order['date_wanted']}" if order.get("date_wanted") else ""))
    facts_parts.append("Задача письма: " + PURPOSE_RU[purpose])
    if note:
        facts_parts.append("Учесть: " + note)
    facts = ". ".join(facts_parts)

    body, engine = _write("letter", business, facts,
                          _letter_fallback(business, purpose, client, order, note))

    who = (client.get("name") if client else None) or (f"по заявке №{order['id']}" if order else "клиенту")
    doc = _doc(
        "Письмо клиенту",
        f"Кому: {who}",
        [("Клиент", (client.get("name") if client else "—")),
         ("Заявка", (f"№{order['id']}" if order else "—")),
         ("О чём", PURPOSE_RU[purpose].capitalize()),
         ("Дата", datetime.date.today().strftime("%d.%m.%Y"))],
        [_text(None, body),
         {"type": "note",
          "body": "Черновик готов. Отправка письма — внешнее действие: у VELOR "
                  "сегодня нет почтового канала, поэтому он никому его не "
                  "отправил и не отправит сам. Скопируйте текст и отправьте "
                  "привычным способом."}],
        (["Карточка клиента"] if client else []) + (["Заявка"] if order else []),
    )
    return doc, body, engine


def _write(kind, business, facts_text, fallback):
    """
    Текст документа. Есть ИИ — пишет он, по реальным данным; нет ИИ — берётся
    заранее собранный шаблон.

    Запасной путь не «вариант на крайний случай»: без него возможность
    существовала бы через раз — ровно тогда, когда чужой сервис в настроении.
    И собирает его вызывающий, а не эта функция: facts_text написан для
    модели, и подставить его человеку в письмо значило бы отправить клиенту
    служебную строку вида «Задача письма: напомнить о себе».
    """
    import ai
    if ai.ai_available():
        try:
            text = (ai.write_document(business, kind, facts_text) or "").strip()
            if text:
                return text, "llm"
        except Exception:
            log.exception("Документ %s не собрался моделью", kind)
    return fallback, "rules"


def _offer_fallback(business):
    name = business.get("name") or "наша компания"
    return (f"Здравствуйте!\n\n"
            f"{name} подготовила для вас предложение. Ниже — перечень услуг и "
            f"цен, действующих на {datetime.date.today().strftime('%d.%m.%Y')}.\n\n"
            f"Готовы обсудить объём и сроки — напишите нам, и мы посчитаем "
            f"под вашу задачу.")


def _letter_fallback(business, purpose, client, order, note):
    """Письмо без ИИ. Ни одного числа и ни одного обещания сверх того, что
    уже лежит в базе: письмо уходит клиенту, и выдуманный срок в нём стоит
    ровно столько же, сколько выдуманная цена."""
    name = business.get("name") or "наша компания"
    who = (client or {}).get("name")
    hello = f"Здравствуйте, {who}!" if who else "Здравствуйте!"
    num = f"№{order['id']}" if order else ""
    what = (order.get("text") or "").strip() if order else ""

    if purpose == "status" and order:
        body = f"Пишем по вашей заявке {num}"
        if what:
            body += f" — {what}"
        body += f". Сейчас она в статусе «{order.get('status') or 'в работе'}»."
        if order.get("date_wanted"):
            body += f" Ориентируемся на {order['date_wanted']}."
    elif purpose == "status":
        body = "Пишем, чтобы рассказать, как идут дела по вашему заказу."
    elif purpose == "reminder":
        body = ("Напоминаем о себе: если вопрос ещё актуален, мы готовы "
                "вернуться к нему в любой момент."
                + (f" Речь о заявке {num}." if num else ""))
    elif purpose == "thanks":
        body = ("Спасибо, что выбрали нас."
                + (f" Заявка {num} закрыта." if num else "")
                + " Нам было приятно работать с вами.")
    else:                                     # «позвать вернуться»
        body = ("Давно вас не было — будем рады снова видеть вас у нас. "
                "Если понадобится помощь или совет, просто напишите.")

    tail = f"\n\n{note}" if note else ""
    return f"{hello}\n\n{body}{tail}\n\nС уважением,\n{name}"


BUILDERS = {
    "sales_report": _sales_report,
    "finance_report": _finance_report,
    "offer": _offer,
    "client_letter": _letter,
}


# ── PDF ────────────────────────────────────────────────────────────────────

def to_pdf(doc: dict) -> bytes:
    """Документ в PDF. Шрифт — общий с экспортом таблиц, иначе кириллица в
    одном файле выходила бы буквами, а в другом квадратами."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer)

    base = exporters.pdf_font()
    bold = "ArialB" if base == "Arial" else base

    st_title = ParagraphStyle("t", fontName=bold, fontSize=18, leading=22)
    st_sub = ParagraphStyle("s", fontName=base, fontSize=11, leading=14,
                            textColor=colors.HexColor("#555555"))
    st_meta = ParagraphStyle("m", fontName=base, fontSize=9, leading=12,
                             textColor=colors.HexColor("#777777"))
    st_h = ParagraphStyle("h", fontName=bold, fontSize=12, leading=15,
                          spaceBefore=6, spaceAfter=2)
    st_body = ParagraphStyle("b", fontName=base, fontSize=10, leading=14)
    st_note = ParagraphStyle("n", fontName=base, fontSize=8.5, leading=11,
                             textColor=colors.HexColor("#777777"))
    st_cell = ParagraphStyle("c", fontName=base, fontSize=9, leading=12)
    st_head = ParagraphStyle("th", fontName=bold, fontSize=9, leading=12,
                             textColor=colors.white)

    def esc(v):
        return (str("" if v is None else v).replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>"))

    out = io.BytesIO()
    pdf = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=doc.get("title") or "VELOR")
    el = [Paragraph(esc(doc.get("title")), st_title)]
    if doc.get("subtitle"):
        el.append(Paragraph(esc(doc["subtitle"]), st_sub))
    meta = " · ".join(f"{k}: {v}" for k, v in (doc.get("meta") or []) if v)
    if meta:
        el.append(Spacer(1, 4))
        el.append(Paragraph(esc(meta), st_meta))
    el.append(Spacer(1, 12))

    width = pdf.width

    for b in doc.get("blocks") or []:
        kind = b.get("type")
        if b.get("title"):
            el.append(Paragraph(esc(b["title"]), st_h))
        if kind == "kv":
            rows = [[Paragraph(esc(k), st_cell), Paragraph(esc(v), st_cell)]
                    for k, v in b.get("rows") or []]
            if rows:
                t = Table(rows, colWidths=[width * 0.38, width * 0.62])
                t.setStyle(TableStyle([
                    ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#E4E0F2")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ]))
                el.append(t)
        elif kind == "table":
            cols = b.get("columns") or []
            data = [[Paragraph(esc(c), st_head) for c in cols]]
            for r in b.get("rows") or []:
                data.append([Paragraph(esc(v), st_cell) for v in r])
            t = Table(data, repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#8052FF")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#F6F3FF")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            el.append(t)
        elif kind == "inference":
            el.append(Paragraph(esc(b.get("body")), st_body))
            el.append(Spacer(1, 3))
            el.append(Paragraph(esc(b.get("note")), st_note))
        else:
            el.append(Paragraph(esc(b.get("body")), st_body))
        if b.get("note") and kind != "inference":
            el.append(Spacer(1, 3))
            el.append(Paragraph(esc(b["note"]), st_note))
        el.append(Spacer(1, 12))

    src = doc.get("sources") or []
    el.append(Spacer(1, 6))
    el.append(Paragraph(
        "Источник данных: " + (", ".join(src) if src else "данные бизнеса в VELOR")
        + ". Собрано VELOR AI "
        + datetime.datetime.now().strftime("%d.%m.%Y в %H:%M") + ".", st_note))
    pdf.build(el)
    return out.getvalue()


# ── сборка и хранение ──────────────────────────────────────────────────────

def _filename(kind, doc):
    stamp = datetime.date.today().isoformat()
    return f"velor-{kind}-{stamp}.pdf"


def build(business_id: int, kind: str, params: dict | None = None) -> dict:
    """
    Собрать результат и положить его в историю.

    Файл кладём в то же хранилище, что и оригиналы входящих: второе файловое
    хранилище означало бы два разных ответа на вопрос «а это переживёт
    передеплой».
    """
    meta = KINDS.get(kind)
    if not meta:
        raise OutputError("Такого результата VELOR не собирает.")
    params = dict(params or {})

    doc, body, engine = BUILDERS[kind](business_id, params)

    file_key = file_name = None
    size = 0
    if meta["file"] == PDF:
        try:
            blob = to_pdf(doc)
        except Exception as e:
            log.exception("PDF не собрался (biz %s, %s)", business_id, kind)
            raise OutputError("Не удалось собрать PDF — попробуйте ещё раз.") from e
        file_name = _filename(kind, doc)
        file_key = storage.new_key(file_name)
        try:
            storage.put(business_id, file_key, blob)
        except storage.StorageError as e:
            raise OutputError("Файл собрался, но не сохранился в хранилище.") from e
        size = len(blob)

    out_id = database.add_output(
        business_id, kind, title=doc.get("title"), subtitle=doc.get("subtitle"),
        request=params, doc=doc, sources=doc.get("sources"), body=body,
        file_key=file_key, file_name=file_name, file_size=size, engine=engine)
    return database.get_output(out_id, business_id)


def public(row: dict) -> dict:
    """Что уходит на экран. Ключа файла в хранилище среди этого нет: право на
    файл определяет запись, а не знание имени, и публиковать имя незачем."""
    if not row:
        return {}
    meta = KINDS.get(row.get("kind")) or {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "kind_title": meta.get("title") or row["kind"],
        "group": meta.get("group") or "",
        "title": row.get("title"),
        "subtitle": row.get("subtitle"),
        "request": row.get("request") or {},
        "sources": row.get("sources") or [],
        "body": row.get("body"),
        "doc": row.get("doc") or {},
        "has_file": bool(row.get("file_key")),
        "file_name": row.get("file_name"),
        "file_size": int(row.get("file_size") or 0),
        # По чему собрано: посчитанные цифры или текст модели. Владелец имеет
        # право знать это, не открывая документ.
        "engine": row.get("engine") or "rules",
        "engine_ru": "с комментарием ИИ" if row.get("engine") == "llm"
                     else "только по вашим данным",
        "status": row.get("status") or "READY",
        "error": row.get("error"),
        "created_at": row.get("created_at"),
    }


def brief(row: dict) -> dict:
    """Строка истории: без самого документа — список не должен тащить каждый
    отчёт целиком."""
    d = public(row)
    d.pop("doc", None)
    d.pop("body", None)
    return d


def remove(business_id: int, output_id: int) -> bool:
    """Удалить результат вместе с файлом."""
    key = database.delete_output(output_id, business_id)
    if key is None:
        return False
    try:
        storage.delete(business_id, key)
    except Exception:
        log.exception("Файл результата не удалился (biz %s, key %s)", business_id, key)
    return True


def file_of(business_id: int, output_id: int):
    """Файл результата. Право проверяется по записи в базе, а не по ключу."""
    row = database.get_output(output_id, business_id)
    if not row or not row.get("file_key"):
        raise OutputError("У этого результата нет файла.")
    try:
        data = storage.get(business_id, row["file_key"])
    except storage.StorageError as e:
        raise OutputError("Файл не найден в хранилище.") from e
    return data, "application/pdf", row.get("file_name") or "velor.pdf"
