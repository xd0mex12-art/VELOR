"""
Универсальная фабрика бизнес-сущностей.

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

Добавить новый вид — значит дописать одну запись в ENTITIES. Ни сервер, ни
интерфейс, ни аудит трогать не нужно: форму редактирования интерфейс собирает
из описания полей, которое приезжает вместе с разбором.
"""
import datetime

import database


class EntityError(Exception):
    """Создать сущность нельзя — с объяснением для человека."""


# ── типы полей ─────────────────────────────────────────────────────────────
# Тип нужен и для приведения значения, и для того, чтобы интерфейс знал, какое
# поле рисовать. Держим их в одном месте, чтобы фронт не угадывал по имени.
def _as_int(v, label):
    try:
        s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
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


def _fact_maker(kind, word):
    def make(bid, data):
        fid = database.add_fact(bid, kind, data["title"], data.get("body") or "")
        return fid, f"{word}: {data['title']}"
    return make


def _make_client(bid, data):
    # Клиент, заведённый руками, — такой же внешний, как из чужой CRM: у него
    # нет telegram-id, поэтому опознаём его по собственному ключу. Повторное
    # подтверждение того же материала не создаст двойника.
    key = "inbox:" + (data.get("phone") or data["name"]).strip().lower()
    cid, created = database.upsert_external_client(
        bid, key, "inbox", name=data["name"],
        phone=data.get("phone") or None, notes=data.get("notes") or None)
    return cid, ("Клиент: " + data["name"]) if created else ("Клиент уже был: " + data["name"])


def _make_order(bid, data):
    oid = database.add_order(bid, data["text"], phone=data.get("phone") or None,
                             date_wanted=_as_date(data.get("date_wanted"), "Дата") or None,
                             amount=data.get("amount") or 0)
    tail = f" на {int(data['amount']):,} ₽".replace(",", " ") if data.get("amount") else ""
    return oid, "Заявка: " + data["text"][:60] + tail


GOAL_METRICS = ("income", "profit", "clients", "orders", "subscribers")


def _make_goal(bid, data):
    metric = data.get("metric") if data.get("metric") in GOAL_METRICS else "income"
    gid = database.add_goal(bid, metric, data["title"], data["target"],
                            deadline=_as_date(data.get("deadline"), "Срок"))
    return gid, "Цель: " + data["title"]


# Реестр. Всё, что знает система о создании сущностей, лежит здесь.
ENTITIES = {
    "expense": {
        "title": "Расход",
        "where": "finance.html",
        "fields": [f("amount", "Сумма", "int", True),
                   f("category", "Категория", "text", False, "аренда, реклама, закупка…"),
                   f("note", "Заметка", "text")],
        "make": _make_expense,
    },
    "income": {
        "title": "Доход",
        "where": "finance.html",
        "fields": [f("amount", "Сумма", "int", True),
                   f("category", "Категория", "text", False, "продажи, услуги…"),
                   f("note", "Заметка", "text")],
        "make": _make_income,
    },
    "client": {
        "title": "Клиент",
        "where": "clients.html",
        "fields": [f("name", "Имя", "text", True),
                   f("phone", "Телефон", "text"),
                   f("notes", "Заметка", "text")],
        "make": _make_client,
    },
    "order": {
        "title": "Заявка",
        "where": "orders.html",
        "fields": [f("text", "Что заказали", "text", True),
                   f("amount", "Сумма", "int"),
                   f("phone", "Телефон", "text"),
                   f("date_wanted", "На какую дату", "date")],
        "make": _make_order,
    },
    "service": {
        "title": "Услуга",
        "where": "memory.html",
        "fields": [f("title", "Название", "text", True),
                   f("body", "Цена, срок, условия", "text")],
        "make": _fact_maker("service", "Услуга"),
    },
    "product": {
        "title": "Товар",
        "where": "memory.html",
        "fields": [f("title", "Название", "text", True),
                   f("body", "Цена, наличие, описание", "text")],
        "make": _fact_maker("product", "Товар"),
    },
    "rule": {
        "title": "Правило",
        "where": "memory.html",
        "fields": [f("title", "Правило", "text", True),
                   f("body", "Подробности", "text")],
        "make": _fact_maker("rule", "Правило"),
    },
    "goal": {
        "title": "Цель",
        "where": "goals.html",
        "fields": [f("title", "Цель", "text", True),
                   f("target", "Сколько достичь", "int", True),
                   f("metric", "Показатель", "text", False, "income, profit, clients, orders",
                     list(GOAL_METRICS)),
                   f("deadline", "Срок", "date")],
        "make": _make_goal,
    },
}


def schema(entity_type):
    """Описание полей вида — интерфейс собирает из него форму правки."""
    e = ENTITIES.get(entity_type)
    if not e:
        raise EntityError("Неизвестный вид записи.")
    return {"entity": entity_type, "title": e["title"], "where": e["where"],
            "fields": [dict(x) for x in e["fields"]]}


def prepare(entity_type, data):
    """
    Привести присланные значения к нужным типам и проверить обязательные.

    Возвращает чистый словарь ТОЛЬКО из описанных полей: всё лишнее
    отбрасывается. Так через форму подтверждения нельзя дописать в запись
    поле, которого в описании нет.
    """
    e = ENTITIES.get(entity_type)
    if not e:
        raise EntityError("Неизвестный вид записи.")
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
    clean = prepare(entity_type, data)
    e = ENTITIES[entity_type]
    entity_id, text = e["make"](business_id, clean)
    return entity_id, text, clean
