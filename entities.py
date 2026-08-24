"""
Универсальная фабрика бизнес-сущностей — она же реестр памяти бизнеса.

Зачем. Подтверждение разбора должно уметь создавать не только расход. Сегодня
это расход и запись в память, завтра — клиент, заказ, услуга, цель. Если
каждый случай писать отдельной веткой в обработчике, через месяц там будет
восемь почти одинаковых веток, и любое новое поле придётся чинить в восьми
местах. Поэтому создание описано ДАННЫМИ: у каждого вида есть список полей и
одна функция, которая его создаёт.

Слой понимания сюда только приносит значения. Проверка обязательных полей,
приведение типов и человеческая формулировка результата — здесь. Так и
автоматическое применение, и подтверждение человеком идут одной дорогой, и
запись в аудит получается одинаковой независимо от того, кто нажал.

Здесь же живёт вторая половина работы — ЧТЕНИЕ. Память бизнеса показывает
одним экраном всё, что VELOR знает: компанию, услуги, товары, сотрудников,
клиентов, правила, цели, документы, поставщиков и деньги. Чтобы этот экран не
превратился в десять разных экранов, каждый вид умеет не только «создать», но
и «перечислить», «прочитать», «изменить» и «подписать одной строкой».

Своих таблиц реестр НЕ заводит. Услуги и правила лежат в memory_facts,
клиенты в clients, деньги в finance_entries, цели в goals, документы в
documents — там, где были. Реестр только знает, где что лежит, и говорит с
ними одним языком.

Добавить новый вид — значит дописать одну запись в ENTITIES. Ни сервер, ни
интерфейс, ни аудит трогать не нужно: форму редактирования интерфейс собирает
из описания полей, которое приезжает вместе с разбором.
"""
import datetime

import database


class EntityError(Exception):
    """Создать или изменить сущность нельзя — с объяснением для человека."""


# ── типы полей ─────────────────────────────────────────────────────────────
# Тип нужен и для приведения значения, и для того, чтобы интерфейс знал, какое
# поле рисовать. Держим их в одном месте, чтобы фронт не угадывал по имени.
def _as_int(v, label):
    try:
        s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
        n = int(round(float(s)))
    except (TypeError, ValueError):
        raise EntityError(f"«{label}»: нужно число.")
    if n < 0:
        raise EntityError(f"«{label}»: число не может быть отрицательным.")
    return n


def _as_text(v, label, limit=2000):
    s = str(v if v is not None else "").strip()
    if len(s) > limit:
        s = s[:limit]
    return s


def _as_date(v, label):
    s = str(v or "").strip()
    if not s:
        return None
    try:
        datetime.date.fromisoformat(s[:10])
    except ValueError:
        raise EntityError(f"«{label}»: дата в виде ГГГГ-ММ-ДД.")
    return s[:10]


CAST = {"int": _as_int, "text": _as_text, "date": _as_date}


def f(name, label, kind="text", required=False, hint="", options=None):
    """Описание одного поля формы подтверждения."""
    return {"name": name, "label": label, "type": kind,
            "required": required, "hint": hint, "options": options}


def _money(n):
    return "{:,}".format(int(n or 0)).replace(",", " ") + " ₽"


def _short(s, n=90):
    s = str(s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


# ── факты памяти: услуги, товары, правила, сотрудники, поставщики, компания ──
# Все они лежат в одной таблице memory_facts и отличаются полем kind. Общий
# набор действий поэтому пишем один раз, а не шесть.

def _fact_data(data, extra):
    return {k: data[k] for k in extra if str(data.get(k) or "").strip()}


def _fact_maker(kind, word, extra=()):
    def make(bid, data):
        fid = database.add_fact(bid, kind, data["title"], data.get("body") or "",
                                _fact_data(data, extra))
        return fid, f"{word}: {data['title']}"
    return make


def _fact_reader(kind, extra=()):
    def read(bid, eid):
        row = database.get_fact(eid, bid)
        if not row or row.get("kind") != kind:
            return None
        out = {"title": row.get("title") or "", "body": row.get("body") or ""}
        for k in extra:
            v = (row.get("data") or {}).get(k)
            if v:
                out[k] = v
        return out
    return read


def _fact_updater(kind, word, extra=()):
    def update(bid, eid, data):
        row = database.get_fact(eid, bid)
        if not row or row.get("kind") != kind:
            raise EntityError("Запись не найдена.")
        database.update_fact(eid, bid, data["title"], data.get("body") or "",
                             _fact_data(data, extra))
        return f"{word}: {data['title']}"
    return update


def _fact_lister(kind):
    def lister(bid, limit, offset):
        return database.list_facts(bid, kind)[offset:offset + limit]
    return lister


def _fact_labeler(extra=()):
    def label(row):
        bits = [str((row.get("data") or {}).get(k)) for k in extra
                if (row.get("data") or {}).get(k)]
        body = (row.get("body") or "").strip()
        if body:
            bits.append(_short(body))
        return {"title": row.get("title") or "без названия", "sub": " · ".join(bits)}
    return label


def _fact_entity(kind, title, plural, word, group, fields, extra=(), where="memory.html"):
    """Собрать описание вида, который хранится в memory_facts."""
    return {
        "title": title, "plural": plural, "group": group, "where": where,
        "fields": fields,
        "make": _fact_maker(kind, word, extra),
        "read": _fact_reader(kind, extra),
        "row": lambda bid, eid: database.get_fact(eid, bid),
        "update": _fact_updater(kind, word, extra),
        "list": _fact_lister(kind),
        "count": lambda bid, _k=kind: database.count_facts(bid, _k),
        "label": _fact_labeler(extra),
    }


# ── как создаётся каждый вид ───────────────────────────────────────────────

def _money_note(data, fallback):
    return _as_text(data.get("note") or fallback, "Заметка", 300)


def _make_expense(bid, data):
    amount = data["amount"]
    eid = database.add_finance_entry(bid, "expense", data.get("category") or "без категории",
                                     amount, _money_note(data, "Из входящих"))
    return eid, f"Расход {amount:,} ₽".replace(",", " ") + \
        (f" · {data['category']}" if data.get("category") else "")


def _make_income(bid, data):
    amount = data["amount"]
    eid = database.add_finance_entry(bid, "income", data.get("category") or "без категории",
                                     amount, _money_note(data, "Из входящих"))
    return eid, f"Доход {amount:,} ₽".replace(",", " ") + \
        (f" · {data['category']}" if data.get("category") else "")


def _money_reader(kind):
    def read(bid, eid):
        row = database.get_finance_entry(eid, bid)
        if not row or row.get("kind") != kind:
            return None
        return {"amount": int(row.get("amount") or 0),
                "category": row.get("category") or "",
                "note": row.get("note") or ""}
    return read


def _money_updater(kind, word):
    def update(bid, eid, data):
        row = database.get_finance_entry(eid, bid)
        if not row or row.get("kind") != kind:
            raise EntityError("Операция не найдена.")
        database.update_finance_entry(eid, bid, amount=data["amount"],
                                      category=data.get("category") or "без категории",
                                      note=data.get("note") or None)
        return f"{word} {_money(data['amount'])}" + \
            (f" · {data['category']}" if data.get("category") else "")
    return update


def _money_labeler(row):
    bits = [row.get("category") or "без категории"]
    if (row.get("note") or "").strip():
        bits.append(_short(row["note"], 60))
    return {"title": _money(row.get("amount")), "sub": " · ".join(bits)}


def _make_client(bid, data):
    # Клиент, заведённый руками, — такой же внешний, как из чужой CRM: у него
    # нет telegram-id, поэтому опознаём его по собственному ключу. Повторное
    # подтверждение того же материала не создаст двойника.
    key = "inbox:" + (data.get("phone") or data["name"]).strip().lower()
    cid, created = database.upsert_external_client(
        bid, key, "inbox", name=data["name"],
        phone=data.get("phone") or None, notes=data.get("notes") or None)
    return cid, ("Клиент: " + data["name"]) if created else ("Клиент уже был: " + data["name"])


def _read_client(bid, eid):
    row = database.get_client(eid, bid)
    if not row:
        return None
    return {"name": row.get("name") or "", "phone": row.get("phone") or "",
            "notes": row.get("notes") or ""}


def _update_client(bid, eid, data):
    if not database.get_client(eid, bid):
        raise EntityError("Клиент не найден.")
    database.update_client(eid, bid, name=data["name"], phone=data.get("phone") or None,
                           notes=data.get("notes") or None)
    return "Клиент: " + data["name"]


def _label_client(row):
    bits = [b for b in (row.get("phone"), row.get("source")) if b]
    return {"title": row.get("name") or "без имени", "sub": " · ".join(bits)}


def _make_order(bid, data):
    oid = database.add_order(bid, data["text"], phone=data.get("phone") or None,
                             date_wanted=_as_date(data.get("date_wanted"), "Дата") or None,
                             amount=data.get("amount") or 0)
    tail = f" на {int(data['amount']):,} ₽".replace(",", " ") if data.get("amount") else ""
    return oid, "Заявка: " + data["text"][:60] + tail


def _read_order(bid, eid):
    row = database.get_order(eid, bid)
    if not row:
        return None
    return {"text": row.get("text") or "", "amount": int(row.get("amount") or 0),
            "phone": row.get("phone") or "", "date_wanted": row.get("date_wanted") or ""}


def _update_order(bid, eid, data):
    if not database.get_order(eid, bid):
        raise EntityError("Заявка не найдена.")
    database.update_order(eid, bid, text=data["text"], amount=data.get("amount") or 0,
                          phone=data.get("phone") or None,
                          date_wanted=data.get("date_wanted") or None)
    return "Заявка: " + data["text"][:60]


def _label_order(row):
    bits = [row.get("status") or "новый"]
    if row.get("amount"):
        bits.append(_money(row["amount"]))
    return {"title": _short(row.get("text"), 70) or "без описания", "sub": " · ".join(bits)}


GOAL_METRICS = ("income", "profit", "clients", "orders", "subscribers")


def _make_goal(bid, data):
    metric = data.get("metric") if data.get("metric") in GOAL_METRICS else "income"
    gid = database.add_goal(bid, metric, data["title"], data["target"],
                            deadline=_as_date(data.get("deadline"), "Срок"))
    return gid, "Цель: " + data["title"]


def _read_goal(bid, eid):
    row = database.get_goal(eid, bid)
    if not row:
        return None
    return {"title": row.get("title") or "", "target": int(row.get("target") or 0),
            "metric": row.get("metric") or "income", "deadline": row.get("deadline") or ""}


def _update_goal(bid, eid, data):
    if not database.get_goal(eid, bid):
        raise EntityError("Цель не найдена.")
    metric = data.get("metric") if data.get("metric") in GOAL_METRICS else "income"
    database.update_goal(eid, bid, title=data["title"], target=data["target"],
                         metric=metric, deadline=data.get("deadline") or None)
    return "Цель: " + data["title"]


def _label_goal(row):
    meta = database.GOAL_METRICS.get(row.get("metric") or "", {})
    bits = ["{:,}".format(int(row.get("target") or 0)).replace(",", " ")
            + " " + (meta.get("unit") or "")]
    if meta.get("name"):
        bits.append(meta["name"])
    if row.get("deadline"):
        bits.append("до " + row["deadline"])
    return {"title": row.get("title") or "цель", "sub": " · ".join(b.strip() for b in bits)}


def _read_document(bid, eid):
    row = database.get_document(eid, bid)
    if not row:
        return None
    return {"filename": row.get("filename") or ""}


def _update_document(bid, eid, data):
    if not database.get_document(eid, bid):
        raise EntityError("Документ не найден.")
    database.rename_document(eid, bid, data["filename"])
    return "Документ: " + data["filename"]


def _plural(n, one, few, many):
    a, b = abs(n) % 100, abs(n) % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    return one if b == 1 else many


def _label_document(row):
    n = int(row.get("chunks") or 0)
    tail = f"{n} " + _plural(n, "фрагмент", "фрагмента", "фрагментов") if n else ""
    return {"title": row.get("filename") or "без имени", "sub": tail}


# Реестр. Всё, что знает система о создании и чтении знаний, лежит здесь.
# group — как это показывать в памяти бизнеса; порядок ключей = порядок разделов.
ENTITIES = {
    "company": _fact_entity(
        "company", "Сведения о компании", "Компания", "О компании", "company",
        [f("title", "Что за сведения", "text", True, "реквизиты, адрес, режим работы"),
         f("body", "Значение", "text")]),
    "service": _fact_entity(
        "service", "Услуга", "Услуги", "Услуга", "offer",
        [f("title", "Название", "text", True),
         f("body", "Цена, срок, условия", "text")]),
    "product": _fact_entity(
        "product", "Товар", "Товары", "Товар", "offer",
        [f("title", "Название", "text", True),
         f("body", "Цена, наличие, описание", "text")]),
    "employee": _fact_entity(
        "employee", "Сотрудник", "Сотрудники", "Сотрудник", "people",
        [f("title", "Имя", "text", True),
         f("role", "Должность", "text", False, "мастер, менеджер, курьер"),
         f("contact", "Контакт", "text", False, "телефон или @ник"),
         f("body", "Заметка", "text", False, "график, зона ответственности")],
        extra=("role", "contact")),
    "supplier": _fact_entity(
        "supplier", "Поставщик", "Поставщики", "Поставщик", "people",
        [f("title", "Название", "text", True),
         f("contact", "Контакт", "text"),
         f("terms", "Условия", "text", False, "сроки, минимальный заказ, отсрочка"),
         f("body", "Заметка", "text")],
        extra=("contact", "terms")),
    "rule": _fact_entity(
        "rule", "Правило", "Правила работы", "Правило", "rules",
        [f("title", "Правило", "text", True),
         f("body", "Подробности", "text")]),
    "client": {
        "title": "Клиент", "plural": "Клиенты", "group": "people",
        "where": "clients.html",
        "fields": [f("name", "Имя", "text", True),
                   f("phone", "Телефон", "text"),
                   f("notes", "Заметка", "text")],
        "make": _make_client, "read": _read_client, "update": _update_client,
        "row": lambda bid, eid: database.get_client(eid, bid),
        "list": lambda bid, limit, offset: database.list_clients(bid, limit=limit, offset=offset),
        "count": lambda bid: database.count_clients(bid),
        "label": _label_client,
    },
    "order": {
        "title": "Заявка", "plural": "Заявки", "group": "money",
        "where": "orders.html",
        "fields": [f("text", "Что заказали", "text", True),
                   f("amount", "Сумма", "int"),
                   f("phone", "Телефон", "text"),
                   f("date_wanted", "На какую дату", "date")],
        "make": _make_order, "read": _read_order, "update": _update_order,
        "row": lambda bid, eid: database.get_order(eid, bid),
        "list": lambda bid, limit, offset: database.get_orders(bid, limit=limit, offset=offset),
        "count": lambda bid: database.count_orders(bid),
        "label": _label_order,
    },
    "expense": {
        "title": "Расход", "plural": "Расходы", "group": "money",
        "where": "finance.html",
        "fields": [f("amount", "Сумма", "int", True),
                   f("category", "Категория", "text", False, "аренда, реклама, закупка…"),
                   f("note", "Заметка", "text")],
        "make": _make_expense, "read": _money_reader("expense"),
        "update": _money_updater("expense", "Расход"),
        "row": lambda bid, eid: database.get_finance_entry(eid, bid),
        "list": lambda bid, limit, offset: database.list_finance_entries(
            bid, limit=limit, kind="expense", offset=offset),
        "count": lambda bid: database.count_finance_entries(bid, "expense"),
        "label": _money_labeler,
    },
    "income": {
        "title": "Доход", "plural": "Доходы", "group": "money",
        "where": "finance.html",
        "fields": [f("amount", "Сумма", "int", True),
                   f("category", "Категория", "text", False, "продажи, услуги…"),
                   f("note", "Заметка", "text")],
        "make": _make_income, "read": _money_reader("income"),
        "update": _money_updater("income", "Доход"),
        "row": lambda bid, eid: database.get_finance_entry(eid, bid),
        "list": lambda bid, limit, offset: database.list_finance_entries(
            bid, limit=limit, kind="income", offset=offset),
        "count": lambda bid: database.count_finance_entries(bid, "income"),
        "label": _money_labeler,
    },
    "goal": {
        "title": "Цель", "plural": "Цели", "group": "goals",
        "where": "goals.html",
        "fields": [f("title", "Цель", "text", True),
                   f("target", "Сколько достичь", "int", True),
                   f("metric", "Показатель", "text", False, "income, profit, clients, orders",
                     list(GOAL_METRICS)),
                   f("deadline", "Срок", "date")],
        "make": _make_goal, "read": _read_goal, "update": _update_goal,
        "row": lambda bid, eid: database.get_goal(eid, bid),
        "list": lambda bid, limit, offset: database.list_goals(bid)[offset:offset + limit],
        "count": lambda bid: len(database.list_goals(bid)),
        "label": _label_goal,
    },
    "document": {
        "title": "Документ", "plural": "Документы", "group": "docs",
        "where": "memory.html",
        # Документ не создаётся формой: он появляется загрузкой файла, который
        # тут же разбирается на фрагменты. Придумать документ «из полей»
        # значило бы завести пустую карточку без содержимого.
        "fields": [f("filename", "Название", "text", True)],
        "make": None, "read": _read_document, "update": _update_document,
        "row": lambda bid, eid: database.get_document(eid, bid),
        "list": lambda bid, limit, offset: database.list_documents(bid)[offset:offset + limit],
        "count": lambda bid: database.count_documents(bid),
        "label": _label_document,
    },
}

# Разделы памяти бизнеса. Человеку важно не «двенадцать таблиц», а четыре
# вопроса: что мы предлагаем, кто вокруг нас, по каким правилам работаем и как
# идут деньги.
GROUPS = {
    "company": "Компания",
    "offer":   "Что продаём",
    "people":  "Люди",
    "rules":   "Правила",
    "goals":   "Цели",
    "money":   "Деньги",
    "docs":    "Документы",
}


def types_of_group(group):
    return [t for t, e in ENTITIES.items() if e["group"] == group]


def _entity(entity_type):
    e = ENTITIES.get(entity_type)
    if not e:
        raise EntityError("Неизвестный вид записи.")
    return e


def schema(entity_type):
    """Описание полей вида — интерфейс собирает из него форму правки."""
    e = _entity(entity_type)
    return {"entity": entity_type, "title": e["title"], "plural": e["plural"],
            "group": e["group"], "where": e["where"],
            "can_create": bool(e.get("make")), "can_edit": bool(e.get("update")),
            "fields": [dict(x) for x in e["fields"]]}


def prepare(entity_type, data):
    """
    Привести присланные значения к нужным типам и проверить обязательные.

    Возвращает чистый словарь ТОЛЬКО из описанных полей: всё лишнее
    отбрасывается. Так через форму подтверждения нельзя дописать в запись
    поле, которого в описании нет.
    """
    e = _entity(entity_type)
    clean = {}
    for fl in e["fields"]:
        raw = (data or {}).get(fl["name"])
        if raw in (None, ""):
            if fl["required"]:
                raise EntityError(f"Не хватает поля «{fl['label']}».")
            continue
        clean[fl["name"]] = CAST[fl["type"]](raw, fl["label"])
    for fl in e["fields"]:
        if fl["required"] and clean.get(fl["name"]) in (None, "", 0) and fl["type"] == "int":
            raise EntityError(f"«{fl['label']}»: нужно значение больше нуля.")
    return clean


def create(business_id, entity_type, data):
    """
    Создать сущность. Возвращает (id, человеческое описание).
    Единственная дверь: и автоматика, и подтверждение человеком идут сюда.
    """
    e = _entity(entity_type)
    if not e.get("make"):
        raise EntityError(f"«{e['title']}» так не создаётся.")
    clean = prepare(entity_type, data)
    entity_id, text = e["make"](business_id, clean)
    return entity_id, text, clean


def read(business_id, entity_type, entity_id):
    """Текущие значения записи по полям её вида. Нет записи — None."""
    e = _entity(entity_type)
    if not e.get("read"):
        return None
    return e["read"](business_id, int(entity_id))


def update(business_id, entity_type, entity_id, data):
    """
    Изменить запись. Возвращает (описание, что стало, что было).

    Правка частичная: незаполненные поля берутся из текущего значения, а не
    затираются пустотой. Иначе форма, показавшая три поля из пяти, стирала бы
    два оставшихся.
    """
    e = _entity(entity_type)
    if not e.get("update"):
        raise EntityError(f"«{e['title']}» здесь изменить нельзя.")
    before = read(business_id, entity_type, entity_id)
    if before is None:
        raise EntityError("Запись не найдена.")
    merged = dict(before)
    for k, v in (data or {}).items():
        merged[k] = v
    clean = prepare(entity_type, merged)
    text = e["update"](business_id, int(entity_id), clean)
    return text, clean, before


def listing(business_id, entity_type, limit=50, offset=0):
    """Записи вида одним списком: сырые строки плюс подпись для интерфейса."""
    e = _entity(entity_type)
    rows = e["list"](business_id, int(limit), int(offset)) or []
    out = []
    for r in rows:
        row = dict(r)
        label = e["label"](row)
        out.append({"id": row.get("id"), "entity": entity_type,
                    "title": label["title"], "sub": label.get("sub") or "",
                    "created_at": row.get("created_at"),
                    "origin": origin_of(entity_type, row)})
    return out


def row(business_id, entity_type, entity_id):
    """Сырая строка записи — нужна, чтобы подписать её так же, как в списке."""
    e = _entity(entity_type)
    raw = e["row"](business_id, int(entity_id))
    return dict(raw) if raw else None


def label(entity_type, raw):
    """Одна строка про запись: название и подпись под ним."""
    if not raw:
        return {"title": "", "sub": ""}
    return _entity(entity_type)["label"](dict(raw))


# Факты памяти и виды записей называются одинаково — кроме целей. «Цель» в
# ENTITIES это измеримая цель из goals, а факт kind='goal' — старая текстовая
# заметка со страницы базы знаний. Смешать их значило бы показать в памяти
# запись, которой по этому id не существует.
FACT_ENTITY_BY_KIND = {
    "company": "company", "service": "service", "product": "product",
    "rule": "rule", "employee": "employee", "supplier": "supplier",
}


def count(business_id, entity_type):
    return int(_entity(entity_type)["count"](business_id) or 0)


# Откуда запись взялась, если связь с материалом не записана. Ответ есть у
# самих таблиц: у клиента и заявки есть source, у операции — source и импорт.
# Это честнее, чем писать «неизвестно» там, где система знает ответ.
SOURCE_RU = {
    "telegram": "из Telegram", "inbox": "из входящих", "ozon": "из Ozon",
    "amocrm": "из amoCRM", "csv": "из выписки", "xlsx": "из выписки",
    "pdf": "из выписки", "manual": "внесено вручную", "web": "внесено вручную",
}


def origin_of(entity_type, row):
    src = (row.get("source") or "").strip().lower() if isinstance(row, dict) else ""
    if not src:
        return None
    return SOURCE_RU.get(src, "из «%s»" % src)
