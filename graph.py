# -*- coding: utf-8 -*-
"""
Связи бизнеса: кто с кем и через что связан.

Зачем. Отдельные записи — это ещё не понимание. «Клиент Смирнов», «заявка на
букет», «доход 7500» и «услуга Букет на заказ» по отдельности не отвечают ни
на один живой вопрос. Отвечает связь: этот клиент заказывал вот это, на вот
такую сумму, вот тогда. Именно её ждёт клиент, когда пишет «хочу повторить
прошлый заказ», и владелец, когда спрашивает «сколько мы платим Петровой».

Своей графовой базы здесь нет и не нужно. Почти все связи уже выражены
колонками: заявка знает клиента, операция знает сотрудника, поставщика,
клиента и заявку, сообщение знает клиента, знание знает материал, из которого
оно выросло. Заводить рядом второе хранилище тех же фактов значило бы завести
вторую правду, которая рано или поздно разойдётся с первой.

Не хватало ровно одного — связи «многие ко многим»: заявка состоит из
нескольких услуг, услуга участвует во многих заявках. Для неё одна таблица
рёбер (entity_links), и этого достаточно.

Этот модуль — не хранилище, а чтение: он собирает соседей записи из колонок,
из рёбер и из журнала происхождения и отдаёт их одним понятным списком.
"""
import re

import database
import entities

# Связь всегда читается с двух сторон, и слова с этих сторон разные: заявка
# ВКЛЮЧАЕТ услугу, услуга УЧАСТВУЕТ В заявке. Хранится ребро один раз.
KIND_RU = {
    "includes": ("включает", "участвует в"),
    "about":    ("относится к", "упомянут в"),
    "related":  ("связано с", "связано с"),
}

# Служебные слова, по которым нельзя опознавать услугу: они есть в половине
# заявок и связали бы всё со всем.
STOP = {"заказ", "заказа", "заказы", "услуга", "услуги", "товар", "товары",
        "работа", "работы", "цена", "цены", "руб", "рублей", "штук", "клиент"}


def _norm(s):
    return "".join(c.lower() if c.isalnum() else " " for c in str(s or "")).split()


def _stem(w):
    """Основа слова без окончания: «доставкой» и «доставка» — одно и то же."""
    return w[:max(4, len(w) - 3)]


def _significant(title):
    return [w for w in _norm(title) if len(w) >= 4 and w not in STOP]


def mention_score(title, text):
    """
    Насколько название услуги звучит в тексте заявки. 0 — не звучит, 1 — все
    значимые слова на месте. Половина слов — уже достаточно: «Букет на заказ»
    в заявке «Букет 25 роз» узнаётся по слову «букет», а «заказ» служебное.
    """
    words = _significant(title)
    if not words:
        return 0.0
    hay = " ".join(_norm(text))
    hit = sum(1 for w in words if re.search(r"\b" + re.escape(_stem(w)), hay))
    return hit / len(words)


def link_order_items(business_id, order_id, text, min_score=0.5):
    """
    Из чего состоит заявка. Сопоставляем текст заявки с услугами и товарами,
    которые бизнес уже описал в памяти, и ставим рёбра.

    Только по своим же данным: ничего не придумываем, не заводим новых услуг.
    Не узнали — связи просто нет, и это честнее выдуманной.
    """
    if not text or not order_id:
        return []
    linked = []
    try:
        # Пересобираем только автоматические рёбра: то, что владелец связал
        # руками, машина затирать не должна.
        database.drop_entity_links(business_id, "order", order_id,
                                   kind="includes", source="auto")
        for kind in ("service", "product"):
            for row in database.list_facts(business_id, kind):
                score = mention_score(row.get("title"), text)
                if score < min_score:
                    continue
                database.add_entity_link(
                    business_id, "order", order_id, kind, row["id"],
                    kind="includes", confidence=round(score, 2), source="auto",
                    note="узнано по тексту заявки")
                linked.append({"type": kind, "id": row["id"],
                               "title": row.get("title"), "score": round(score, 2)})
    except Exception:
        return linked
    return linked


# ── соседи записи ──────────────────────────────────────────────────────────
# Каждый вид знает, куда смотреть. Описываем это данными, а не десятью ветками
# в одном обработчике: добавится вид — добавится строка.

def _money(n):
    return "{:,}".format(int(n or 0)).replace(",", " ") + " ₽"


def _item(entity_type, raw, via="", link_id=None, kind=None):
    """Сосед в едином виде: заголовок и подпись берём у реестра, а не выдумываем."""
    if not raw:
        return None
    lb = entities.label(entity_type, raw)
    return {"type": entity_type, "id": raw.get("id"), "title": lb.get("title") or "",
            "sub": lb.get("sub") or "", "via": via, "link_id": link_id,
            "kind": kind, "date": raw.get("op_date") or raw.get("created_at")}


def _group(key, title, items, hint="", total=None, money=None):
    items = [i for i in items if i]
    if not items and not total:
        return None
    return {"key": key, "title": title, "hint": hint, "items": items,
            "total": total if total is not None else len(items), "money": money}


def _finance_group(business_id, field, entity_id, title, hint=""):
    rows = database.finance_by_ref(business_id, field, entity_id, limit=20)
    if not rows:
        return None
    totals = database.finance_totals_by_ref(business_id, field, entity_id)
    items = [_item("expense" if r["kind"] == "expense" else "income", r,
                   via="привязана в операции") for r in rows]
    return _group("money", title, items, hint=hint, total=totals["count"],
                  money=totals)


def _links_group(business_id, entity_type, entity_id, skip_types=()):
    """Соседи из таблицы рёбер — то, что колонками не выражается."""
    items = []
    for edge in database.entity_links_of(business_id, entity_type, entity_id):
        if edge["type"] in skip_types:
            continue
        raw = entities.row(business_id, edge["type"], edge["id"]) if \
            edge["type"] in entities.ENTITIES else None
        if not raw:
            continue                     # запись удалили — ребро молчит, а не врёт
        word = KIND_RU.get(edge["kind"], KIND_RU["related"])[0 if edge["dir"] == "out" else 1]
        it = _item(edge["type"], raw, via=word, link_id=edge["link_id"], kind=edge["kind"])
        if it:
            it["auto"] = edge["source"] == "auto"
            it["confidence"] = edge["confidence"]
            items.append(it)
    return items


def _source_group(business_id, entity_type, entity_id):
    """
    Откуда запись взялась и что родилось вместе с ней.

    Материал → разбор → запись уже хранит журнал памяти. Здесь мы читаем его
    как связь: у одного чека и расход, и поставщик — это соседи по источнику.
    """
    links = database.memory_links(business_id, entity_type, entity_id)
    created = next((l for l in links if l.get("event") == "created"), None)
    if not created or not created.get("item_id"):
        return None, []
    item = {"type": "material", "id": created["item_id"],
            "title": created.get("item_title") or created.get("item_filename") or "материал",
            "sub": "исходный материал", "via": "из него взято", "open": False}
    siblings = []
    for l in database.memory_from_item(business_id, created["item_id"]):
        if l["entity_type"] == entity_type and int(l["entity_id"]) == int(entity_id):
            continue
        if l["entity_type"] not in entities.ENTITIES:
            continue
        raw = entities.row(business_id, l["entity_type"], l["entity_id"])
        sib = _item(l["entity_type"], raw, via="из того же материала")
        if sib:
            siblings.append(sib)
    return item, siblings


def _client_groups(business_id, cid):
    groups = []
    orders = database.get_client_orders(cid, business_id, limit=20)
    if orders:
        spent = sum(int(o.get("amount") or 0) for o in orders)
        new = sum(1 for o in orders if (o.get("status") or "новый") == "новый")
        groups.append(_group(
            "orders", "Заявки", [_item("order", o, via="заказывал") for o in orders],
            hint=(f"на {_money(spent)}" + (f", из них новых: {new}" if new else "")),
            total=len(orders)))
        # Услуги, которые клиент уже брал, — через его же заявки. Это и есть
        # ответ на «хочу повторить прошлый заказ».
        seen, items = set(), []
        for o in orders:
            for edge in database.entity_links_of(business_id, "order", o["id"]):
                key = (edge["type"], edge["id"])
                if edge["type"] not in ("service", "product") or key in seen:
                    continue
                seen.add(key)
                raw = entities.row(business_id, edge["type"], edge["id"])
                it = _item(edge["type"], raw, via="брал в заявках")
                if it:
                    items.append(it)
        if items:
            groups.append(_group("services", "Что брал", items,
                                 hint="через свои заявки"))
    msgs = database.count_client_messages(business_id, cid)
    if msgs:
        groups.append(_group("talk", "Обращения", [{
            "type": "message", "id": cid, "title": f"{msgs} обращений",
            "sub": "переписка в Telegram", "via": "писал сам", "open": False}],
            total=msgs))
    groups.append(_finance_group(business_id, "client_id", cid, "Деньги",
                                 "операции, привязанные к клиенту"))
    return groups


def _order_groups(business_id, oid):
    groups = []
    order = database.get_order(oid, business_id) or {}
    if order.get("client_id"):
        client = database.get_client(order["client_id"], business_id)
        groups.append(_group("client", "Клиент",
                             [_item("client", client, via="заказчик")]))
    items = _links_group(business_id, "order", oid)
    if items:
        groups.append(_group("services", "Из чего состоит", items,
                             hint="узнано по тексту заявки или связано вручную"))
    groups.append(_finance_group(business_id, "order_id", oid, "Деньги по заявке",
                                 "оплаты и траты, привязанные к ней"))
    return groups


def _money_groups(business_id, kind, eid):
    row = database.finance_row(eid, business_id) or {}
    groups, near = [], []
    for field, etype, word in (("employee_id", "employee", "выплата человеку"),
                               ("supplier_id", "supplier", "оплата поставщику"),
                               ("client_id", "client", "деньги от клиента"),
                               ("order_id", "order", "по заявке")):
        if not row.get(field):
            continue
        raw = entities.row(business_id, etype, row[field])
        it = _item(etype, raw, via=word)
        if it:
            near.append(it)
    if near:
        groups.append(_group("with", "С кем и по чему", near,
                             hint="эти связи и объясняют, к чему относится операция"))
    # Через заявку операция дотягивается до услуг — так видно, на чём заработали.
    if row.get("order_id"):
        via_order = [i for i in _links_group(business_id, "order", row["order_id"])
                     if i["type"] in ("service", "product")]
        for i in via_order:
            i["via"] = "через заявку"
        if via_order:
            groups.append(_group("services", "Услуги в заявке", via_order))
    return groups


def _fact_groups(business_id, entity_type, eid):
    """Сотрудник, поставщик, услуга, товар, правило, компания."""
    groups = []
    if entity_type == "employee":
        groups.append(_finance_group(business_id, "employee_id", eid, "Выплаты",
                                     "зарплата и прочие выплаты этому человеку"))
    if entity_type == "supplier":
        rows = database.finance_by_ref(business_id, "supplier_id", eid, limit=20)
        invoices = sum(1 for r in rows if (r.get("doc_type") or "") == "invoice")
        g = _finance_group(business_id, "supplier_id", eid, "Оплаты",
                           "операции с этим поставщиком"
                           + (f", счетов: {invoices}" if invoices else ""))
        groups.append(g)
    if entity_type in ("service", "product"):
        edges = [e for e in database.entity_links_of(business_id, entity_type, eid)
                 if e["type"] == "order"]
        orders = database.orders_by_ids(business_id, [e["id"] for e in edges])
        by_id = {o["id"]: o for o in orders}
        items = []
        for e in edges:
            o = by_id.get(e["id"])
            it = _item("order", o, via="участвует в", link_id=e["link_id"], kind=e["kind"])
            if it:
                it["auto"] = e["source"] == "auto"
                items.append(it)
        if items:
            revenue = sum(int(by_id[e["id"]].get("amount") or 0)
                          for e in edges if e["id"] in by_id)
            groups.append(_group("orders", "Заявки", items,
                                 hint=f"выручка по ним: {_money(revenue)}",
                                 money={"income": revenue, "expense": 0,
                                        "count": len(items)}))
        # Клиенты, которые это брали, — вторая ступень того же пути.
        clients, seen = [], set()
        for o in orders:
            if not o.get("client_id") or o["client_id"] in seen:
                continue
            seen.add(o["client_id"])
            it = _item("client", database.get_client(o["client_id"], business_id),
                       via="брал эту позицию")
            if it:
                clients.append(it)
        if clients:
            groups.append(_group("clients", "Кто брал", clients))
    return groups


def neighbors(business_id, entity_type, entity_id):
    """
    Всё, что связано с записью, — одним ответом.

    Порядок групп не случайный: сначала то, ради чего в карточку и заходят
    (кто, что и на сколько), потом источник и прочие рёбра.
    """
    if entity_type not in entities.ENTITIES:
        raise ValueError("Неизвестный вид записи.")
    raw = entities.row(business_id, entity_type, entity_id)
    if raw is None:
        return None
    groups = []
    if entity_type == "client":
        groups += _client_groups(business_id, entity_id)
    elif entity_type == "order":
        groups += _order_groups(business_id, entity_id)
    elif entity_type in ("expense", "income"):
        groups += _money_groups(business_id, entity_type, entity_id)
    else:
        groups += _fact_groups(business_id, entity_type, entity_id)

    skip = {"service", "product"} if entity_type == "order" else set()
    if entity_type in ("service", "product"):
        skip = {"order"}
    other = _links_group(business_id, entity_type, entity_id, skip_types=skip)
    if other:
        groups.append(_group("links", "Связано вручную"
                             if all(not i.get("auto") for i in other) else "Ещё связи", other))

    item, siblings = _source_group(business_id, entity_type, entity_id)
    if item:
        groups.append(_group("source", "Источник", [item],
                             hint="оригинал материала остаётся доступен"))
    if siblings:
        groups.append(_group("siblings", "Из того же материала", siblings))

    groups = [g for g in groups if g]
    lb = entities.label(entity_type, raw)
    return {"entity": entity_type, "id": int(entity_id),
            "title": lb.get("title") or "", "sub": lb.get("sub") or "",
            "entity_title": entities.ENTITIES[entity_type]["title"],
            "groups": groups,
            "total": sum(len(g["items"]) for g in groups)}


# ── досье клиента: ответ на «хочу повторить прошлый заказ» ──────────────────

def client_dossier(business_id, client_id, limit=6):
    """
    Клиент со всей его историей: что брал, на сколько, когда и что любит.

    Это тот самый контекст, без которого ответ на «повторить прошлый заказ»
    невозможен: заказы отдельно от услуг и сумм ничего не значат.
    """
    c = database.get_client(client_id, business_id)
    if not c:
        return None
    orders = database.get_client_orders(client_id, business_id, limit=limit)
    out_orders = []
    for o in orders:
        services = []
        for edge in database.entity_links_of(business_id, "order", o["id"]):
            if edge["type"] not in ("service", "product"):
                continue
            raw = entities.row(business_id, edge["type"], edge["id"])
            if raw:
                services.append(raw.get("title") or "")
        out_orders.append({
            "id": o["id"], "text": (o.get("text") or "").strip(),
            "amount": int(o.get("amount") or 0),
            "date": (o.get("created_at") or "")[:10],
            "wanted": o.get("date_wanted") or "",
            "status": o.get("status") or "новый",
            "services": [s for s in services if s]})
    money = database.finance_totals_by_ref(business_id, "client_id", client_id)
    spent = sum(x["amount"] for x in out_orders) or money["income"]
    return {
        "id": c["id"], "name": c.get("name") or "без имени",
        "phone": c.get("phone") or "", "preferences": c.get("favorite") or "",
        "notes": c.get("notes") or "",
        "orders": out_orders, "orders_count": len(out_orders),
        "last_order": out_orders[0] if out_orders else None,
        "spent": spent, "money": money,
        "messages": database.count_client_messages(business_id, client_id),
        # Лид — тот, кто уже обратился, но ещё ничего не купил. Отдельной
        # таблицы для этого не нужно: разница между лидом и клиентом — это
        # наличие заявки, а не отдельная сущность.
        "is_lead": not out_orders,
    }


def dossier_text(d):
    """Досье строками для промпта. Пусто — значит нечего сказать, и мы молчим."""
    if not d:
        return ""
    lines = []
    if d["last_order"]:
        lo = d["last_order"]
        bits = [lo["text"][:80] or "без описания"]
        if lo["amount"]:
            bits.append(_money(lo["amount"]))
        if lo["date"]:
            bits.append(lo["date"])
        if lo["services"]:
            bits.append("услуги: " + ", ".join(lo["services"][:4]))
        lines.append("Прошлый заказ: " + " · ".join(bits))
    if d["orders_count"] > 1:
        lines.append(f"Всего заказов: {d['orders_count']} на {_money(d['spent'])}")
        for o in d["orders"][1:4]:
            tail = f" ({_money(o['amount'])})" if o["amount"] else ""
            lines.append("  — " + (o["text"][:60] or "без описания") + tail
                         + (", " + o["date"] if o["date"] else ""))
    if d["preferences"]:
        lines.append("Предпочтения: " + d["preferences"])
    if d["is_lead"] and d["messages"]:
        lines.append("Заказов ещё не было — обращался, но не покупал.")
    if not lines:
        return ""
    return ("\n\nСВЯЗИ КЛИЕНТА (факты из базы — на «повторить прошлый заказ» "
            "отвечай по ним, ничего не додумывая):\n" + "\n".join(lines))


# ── к чему относится операция ──────────────────────────────────────────────

_ORDER_NO = re.compile(r"(?:заявк\w*|заказ\w*)\D{0,4}(\d{1,6})", re.I)


def relate_money(business_id, text, extracted):
    """
    К чему относится появившийся расход или доход.

    Людей (сотрудник, поставщик, клиент) узнаёт слой понимания. Здесь —
    заявка: по прямому номеру в тексте либо по открытой заявке того клиента,
    о котором идёт речь. Догадки помечаем уверенностью, а не выдаём за факт.
    """
    found = {}
    why = []
    text = text or ""
    m = _ORDER_NO.search(text)
    if m:
        order = database.get_order(int(m.group(1)), business_id)
        if order:
            found["order_id"] = order["id"]
            why.append({"field": "order_id", "confidence": 0.9,
                        "reason": "в тексте назван номер заявки"})
    cid = (extracted or {}).get("client_id")
    if not found.get("order_id") and cid:
        open_orders = [o for o in database.get_client_orders(cid, business_id, limit=5)
                       if (o.get("status") or "новый") in ("новый", "принят")]
        if len(open_orders) == 1:
            found["order_id"] = open_orders[0]["id"]
            why.append({"field": "order_id", "confidence": 0.6,
                        "reason": "у клиента ровно одна открытая заявка"})
    return found, why
