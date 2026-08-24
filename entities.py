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
import math

import database


class EntityError(Exception):
    """Создать или изменить сущность нельзя — с объяснением для человека."""


# ── типы полей ─────────────────────────────────────────────────────────────
# Тип нужен и для приведения значения, и для того, чтобы интерфейс знал, какое
# поле рисовать. Держим их в одном месте, чтобы фронт не угадывал по имени.
def _as_int(v, label):
    try:
        s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
        x = float(s)
        # Округляем «половину» вверх, как считают люди: встроенный round()
        # округляет 1850.5 до 1850 (к чётному), и в чеке это выглядит как
        # потерянный рубль, который никто не может объяснить.
        n = int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))
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


def _as_ref(v, label):
    """Ссылка на другую запись: сотрудника, клиента, заявку."""
    if v in (None, "", "0", 0):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise EntityError(f"«{label}»: выберите из списка.")
    return n if n > 0 else None


CAST = {"int": _as_int, "text": _as_text, "date": _as_date, "ref": _as_ref}


def f(name, label, kind="text", required=False, hint="", options=None,
      options_of=None, main=True):
    """
    Описание одного поля формы.

    options_of — список вариантов, который зависит от бизнеса (свои
    сотрудники, свои заявки). Считается в момент показа формы: список
    сотрудников меняется чаще, чем код.

    main=False — поле второго ряда. Форма подтверждения во входящих должна
    оставаться короткой: там человек проверяет сумму, а не заполняет карточку.
    """
    return {"name": name, "label": label, "type": kind,
            "required": required, "hint": hint, "options": options,
            "options_of": options_of, "main": main}


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
    def lister(bid, limit, offset, archived=False):
        return database.list_facts(bid, kind, archived=archived)[offset:offset + limit]
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


# ── как запись убирается из работы ─────────────────────────────────────────
# Архив и удаление — разные вещи. Архив: «было правдой, перестало действовать»
# (уволился, сняли с продажи, правило отменили) — запись уходит из работы и из
# знаний ядра, но остаётся вместе с историей. Удаление: «этого не было».
#
# Удалить запись, на которую ссылаются деньги или заказы, нельзя: в выплате
# осталась бы ссылка на несуществующего человека. Такие записи архивируются —
# старые операции при этом продолжают показывать имя.

def _archiver(table):
    def archive(bid, eid, on=True):
        return database.set_archived(table, int(eid), bid, on)
    return archive


def _fact_blockers(kind):
    """Что мешает удалить сотрудника или поставщика — по-человечески."""
    field = {"employee": "employee_id", "supplier": "supplier_id"}.get(kind)

    def blockers(bid, eid):
        if not field:
            return None
        n = database.finance_ref_count(bid, field, eid)
        if n:
            return ("К этой записи привязаны денежные операции (%d). "
                    "Удаление оборвало бы им ссылку — отправьте запись в архив: "
                    "в старых операциях имя останется." % n)
        return None
    return blockers


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
        "count": lambda bid, archived=False, _k=kind: database.count_facts(
            bid, _k, archived=archived),
        "label": _fact_labeler(extra),
        "archive": _archiver("memory_facts"),
        "delete": lambda bid, eid: database.delete_fact(int(eid), bid),
        "blockers": _fact_blockers(kind),
    }


# ── как создаётся каждый вид ───────────────────────────────────────────────

def _opt(value, label):
    return {"value": value, "label": label}


def _employee_options(bid):
    return [_opt(r["id"], r["title"]) for r in database.list_facts(bid, "employee")]


def _supplier_options(bid):
    return [_opt(r["id"], r["title"]) for r in database.list_facts(bid, "supplier")]


def _client_options(bid):
    return [_opt(r["id"], r.get("name") or "без имени")
            for r in database.list_clients(bid, limit=200)]


def _order_options(bid):
    return [_opt(r["id"], _short(r.get("text"), 60) or ("заявка №%s" % r["id"]))
            for r in database.get_orders(bid, limit=100)]


# Откуда пришла операция и каким документом подтверждена. Списки закрытые:
# свободный текст здесь превратился бы в «банк», «Банк», «из банка».
FINANCE_SOURCES = [_opt("manual", "Внесено вручную"), _opt("inbox", "Из входящих"),
                   _opt("import", "Из выписки"), _opt("telegram", "Из Telegram")]
FINANCE_DOCS = [_opt("manual", "Без документа"), _opt("receipt", "Чек"),
                _opt("bank", "Банковская операция"), _opt("invoice", "Счёт"),
                _opt("waybill", "Накладная"), _opt("salary", "Зарплата"),
                _opt("refund", "Возврат"), _opt("income", "Документ о доходе"),
                _opt("import", "Выписка")]


def _money_fields(word_hint):
    """
    Поля денежной операции. Один набор на доход и расход: разница только в
    направлении, и заводить два разных списка полей значило бы завести две
    формы, которые начнут расходиться.
    """
    return [
        f("amount", "Сумма", "int", True),
        f("category", "Категория", "text", False, word_hint),
        f("note", "Описание", "text", False, "что это было"),
        f("op_date", "Дата операции", "date", False, "ГГГГ-ММ-ДД, пусто — сегодня"),
        f("counterparty", "Контрагент", "text", False, "кому или от кого"),
        f("employee_id", "Сотрудник", "ref", False, "если это выплата человеку",
          options_of=_employee_options, main=False),
        f("order_id", "Заявка", "ref", False, "если деньги по конкретному заказу",
          options_of=_order_options, main=False),
        f("supplier_id", "Поставщик", "ref", False, options_of=_supplier_options, main=False),
        f("client_id", "Клиент", "ref", False, options_of=_client_options, main=False),
        f("doc_type", "Документ", "text", False, "чем подтверждено",
          options=FINANCE_DOCS, main=False),
        f("source", "Источник", "text", False, options=FINANCE_SOURCES, main=False),
    ]


def _money_note(data, fallback=""):
    """
    Описание операции. Пусто — значит пусто: подставлять «Из входящих» в
    запись, внесённую руками, — врать о её происхождении. Откуда она пришла,
    и так записано в поле source.
    """
    return _as_text(data.get("note") or fallback, "Описание", 300) or None


MONEY_EXTRA = ("op_date", "counterparty", "source", "doc_type",
               "employee_id", "supplier_id", "client_id", "order_id")


def _money_maker(kind, word):
    def make(bid, data):
        amount = data["amount"]
        eid = database.add_finance_entry(
            bid, kind, data.get("category") or "без категории", amount,
            _money_note(data),
            **{k: data.get(k) for k in MONEY_EXTRA})
        return eid, f"{word} {_money(amount)}" + \
            (f" · {data['category']}" if data.get("category") else "")
    return make


_make_expense = _money_maker("expense", "Расход")
_make_income = _money_maker("income", "Доход")


def _money_reader(kind):
    def read(bid, eid):
        row = database.get_finance_entry(eid, bid)
        if not row or row.get("kind") != kind:
            return None
        out = {"amount": int(row.get("amount") or 0),
               "category": row.get("category") or "",
               "note": row.get("note") or ""}
        for key in MONEY_EXTRA:
            out[key] = row.get(key) or ""
        return out
    return read


def _money_updater(kind, word):
    def update(bid, eid, data):
        row = database.get_finance_entry(eid, bid)
        if not row or row.get("kind") != kind:
            raise EntityError("Операция не найдена.")
        # Передаём ВСЕ поля, а не только заполненные: иначе очистить контрагента
        # или отвязать сотрудника было бы нечем — пустое значение просто не
        # доехало бы до базы, и человек решил бы, что правка не сохраняется.
        fields = {"amount": data["amount"],
                  "category": data.get("category") or "без категории",
                  "note": data.get("note") or None}
        for key in MONEY_EXTRA:
            fields[key] = data.get(key) or None
        database.update_finance_entry(eid, bid, **fields)
        return f"{word} {_money(data['amount'])}" + \
            (f" · {data['category']}" if data.get("category") else "")
    return update


def _money_labeler(row):
    bits = [row.get("category") or "без категории"]
    who = (row.get("employee_name") or row.get("supplier_name")
           or row.get("client_name") or row.get("counterparty"))
    if who:
        bits.append(str(who))
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
    # Текст заявки поправили — состав пересобираем. Иначе связь осталась бы от
    # прошлой редакции и рассказывала бы про услугу, которой в заявке уже нет.
    try:
        import graph
        graph.link_order_items(bid, eid, data["text"])
    except Exception:
        pass
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


def _client_blockers(bid, eid):
    orders = database.orders_of_client_count(bid, eid)
    money = database.finance_ref_count(bid, "client_id", eid)
    if orders or money:
        bits = []
        if orders:
            bits.append("заказов: %d" % orders)
        if money:
            bits.append("операций: %d" % money)
        return ("У клиента есть история (%s). Удаление стёрло бы связь с ней — "
                "отправьте клиента в архив." % ", ".join(bits))
    return None


def _order_blockers(bid, eid):
    money = database.finance_ref_count(bid, "order_id", eid)
    if money:
        return ("К заявке привязаны денежные операции (%d). Сначала отвяжите "
                "их в Финансах." % money)
    return None


def _archive_goal(bid, eid, on=True):
    if not database.get_goal(int(eid), bid):
        return False
    database.update_goal(int(eid), bid, status="archived" if on else "active")
    return True


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
        "list": lambda bid, limit, offset, archived=False: database.list_clients(
            bid, limit=limit, offset=offset, archived=archived),
        "count": lambda bid, archived=False: database.count_clients(bid, archived=archived),
        "label": _label_client,
        "archive": _archiver("clients"),
        "delete": lambda bid, eid: database.delete_client(int(eid), bid),
        "blockers": _client_blockers,
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
        "list": lambda bid, limit, offset, archived=False: (
            [] if archived else database.get_orders(bid, limit=limit, offset=offset)),
        "count": lambda bid, archived=False: (0 if archived else database.count_orders(bid)),
        "label": _label_order,
        # Заявка — событие: оно либо было, либо нет. Отменённую заявку
        # показывает статус, архивировать её незачем.
        "delete": lambda bid, eid: database.delete_order(int(eid), bid),
        "blockers": _order_blockers,
    },
    "expense": {
        "title": "Расход", "plural": "Расходы", "group": "money",
        "where": "finance.html",
        "fields": _money_fields("аренда, реклама, закупка…"),
        "make": _make_expense, "read": _money_reader("expense"),
        "update": _money_updater("expense", "Расход"),
        "row": lambda bid, eid: database.finance_row(eid, bid),
        "list": lambda bid, limit, offset, archived=False: (
            [] if archived else database.finance_rows(
                bid, limit=limit, kind="expense", offset=offset)),
        "count": lambda bid, archived=False: (
            0 if archived else database.count_finance_entries(bid, "expense")),
        "label": _money_labeler,
        # Операция тоже событие: архивировать деньги нельзя — спрятанный, но
        # посчитанный расход врал бы в прибыли. Ошибочную запись удаляют, и
        # след об удалении остаётся в истории.
        "delete": lambda bid, eid: database.delete_finance_entry(int(eid), bid),
    },
    "income": {
        "title": "Доход", "plural": "Доходы", "group": "money",
        "where": "finance.html",
        "fields": _money_fields("продажи, услуги…"),
        "make": _make_income, "read": _money_reader("income"),
        "update": _money_updater("income", "Доход"),
        "row": lambda bid, eid: database.finance_row(eid, bid),
        "list": lambda bid, limit, offset, archived=False: (
            [] if archived else database.finance_rows(
                bid, limit=limit, kind="income", offset=offset)),
        "count": lambda bid, archived=False: (
            0 if archived else database.count_finance_entries(bid, "income")),
        "label": _money_labeler,
        # Операция тоже событие: архивировать деньги нельзя — спрятанный, но
        # посчитанный расход врал бы в прибыли. Ошибочную запись удаляют, и
        # след об удалении остаётся в истории.
        "delete": lambda bid, eid: database.delete_finance_entry(int(eid), bid),
    },
    "goal": {
        "title": "Цель", "plural": "Цели", "group": "goals",
        "where": "goals.html",
        "fields": [f("title", "Цель", "text", True),
                   f("target", "Сколько достичь", "int", True),
                   f("metric", "Показатель", "text", False, "что именно считаем",
                     [_opt(m, database.GOAL_METRICS.get(m, {}).get("name", m))
                      for m in GOAL_METRICS]),
                   f("deadline", "Срок", "date")],
        "make": _make_goal, "read": _read_goal, "update": _update_goal,
        "row": lambda bid, eid: database.get_goal(eid, bid),
        "list": lambda bid, limit, offset, archived=False: database.list_goals(
            bid, archived=archived)[offset:offset + limit],
        "count": lambda bid, archived=False: len(database.list_goals(bid, archived=archived)),
        "label": _label_goal,
        # У цели уже есть жизненный цикл, поэтому архив — это её статус.
        "archive": _archive_goal,
        "delete": lambda bid, eid: database.delete_goal(int(eid), bid),
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
        "list": lambda bid, limit, offset, archived=False: database.list_documents(
            bid, archived=archived)[offset:offset + limit],
        "count": lambda bid, archived=False: database.count_documents(bid, archived=archived),
        "label": _label_document,
        # Архивный документ остаётся файлом, но перестаёт отвечать на вопросы:
        # старый прайс не должен цитироваться как действующий.
        "archive": _archiver("documents"),
        "delete": lambda bid, eid: database.delete_document(int(eid), bid),
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


def _field_public(fl, business_id):
    """
    Поле наружу. Варианты приводим к одному виду {value, label}: интерфейс не
    должен угадывать, список это строк или ссылок.
    """
    out = {k: fl[k] for k in ("name", "label", "type", "required", "hint", "main")}
    opts = fl.get("options")
    if fl.get("options_of") and business_id:
        try:
            opts = fl["options_of"](business_id)
        except Exception:
            opts = []
    if opts:
        out["options"] = [o if isinstance(o, dict) else {"value": o, "label": o}
                          for o in opts]
    else:
        out["options"] = []
    return out


def schema(entity_type, business_id=None):
    """Описание полей вида — интерфейс собирает из него форму правки."""
    e = _entity(entity_type)
    return {"entity": entity_type, "title": e["title"], "plural": e["plural"],
            "group": e["group"], "where": e["where"],
            "can_create": bool(e.get("make")), "can_edit": bool(e.get("update")),
            "can_archive": bool(e.get("archive")), "can_delete": bool(e.get("delete")),
            "fields": [_field_public(x, business_id) for x in e["fields"]]}


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


def _check_refs(business_id, entity_type, clean):
    """
    Ссылка должна вести на СВОЮ запись.

    Без этой проверки в операцию можно было бы вписать id чужого сотрудника:
    в списке он не отобразился бы (join ограничен бизнесом), но в базе бы
    лежал — а тихая ссылка на чужие данные хуже явной ошибки.
    """
    for fl in _entity(entity_type)["fields"]:
        if fl["type"] != "ref" or not clean.get(fl["name"]):
            continue
        getter = fl.get("options_of")
        if not getter:
            continue
        allowed = {str(o["value"]) for o in getter(business_id)}
        if str(clean[fl["name"]]) not in allowed:
            raise EntityError(f"«{fl['label']}»: такой записи у вас нет.")


def create(business_id, entity_type, data):
    """
    Создать сущность. Возвращает (id, человеческое описание).
    Единственная дверь: и автоматика, и подтверждение человеком идут сюда.
    """
    e = _entity(entity_type)
    if not e.get("make"):
        raise EntityError(f"«{e['title']}» так не создаётся.")
    clean = prepare(entity_type, data)
    _check_refs(business_id, entity_type, clean)
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
    # Явная очистка: пустое значение, присланное СПЕЦИАЛЬНО, означает «убрать»,
    # а не «оставить как было». Без этого различия отвязать сотрудника от
    # выплаты или стереть неверного контрагента было бы невозможно.
    for fl in e["fields"]:
        name = fl["name"]
        if fl["required"] or name not in (data or {}):
            continue
        if str(data.get(name) if data.get(name) is not None else "").strip() == "":
            clean.pop(name, None)
    _check_refs(business_id, entity_type, clean)
    text = e["update"](business_id, int(entity_id), clean)
    return text, clean, before


def listing(business_id, entity_type, limit=50, offset=0, archived=False):
    """Записи вида одним списком: сырые строки плюс подпись для интерфейса."""
    e = _entity(entity_type)
    rows = e["list"](business_id, int(limit), int(offset), archived) or []
    out = []
    for r in rows:
        row = dict(r)
        label = e["label"](row)
        out.append({"id": row.get("id"), "entity": entity_type,
                    "title": label["title"], "sub": label.get("sub") or "",
                    "created_at": row.get("created_at"),
                    "archived": is_archived(entity_type, row),
                    "origin": origin_of(entity_type, row)})
    return out


def is_archived(entity_type, raw):
    """Убрана ли запись из работы. У цели это статус, у остальных — отметка."""
    if not raw:
        return False
    if entity_type == "goal":
        return (raw.get("status") or "") == "archived"
    return bool(raw.get("archived_at"))


def archive(business_id, entity_type, entity_id, on=True):
    """Убрать запись из работы или вернуть обратно."""
    e = _entity(entity_type)
    if not e.get("archive"):
        raise EntityError("«%s» в архив не убирается." % e["title"])
    if read(business_id, entity_type, entity_id) is None:
        raise EntityError("Запись не найдена.")
    e["archive"](business_id, int(entity_id), bool(on))
    return ("В архиве: " if on else "Вернули в работу: ") + \
        (label(entity_type, row(business_id, entity_type, entity_id)) or {}).get("title", "")


def blockers(business_id, entity_type, entity_id):
    """Почему удалить нельзя — текстом для человека. Можно — None."""
    e = _entity(entity_type)
    check = e.get("blockers")
    return check(business_id, int(entity_id)) if check else None


def delete(business_id, entity_type, entity_id):
    """
    Удалить запись насовсем. Возвращает подпись удалённого — она нужна, чтобы
    в истории осталось, ЧТО именно убрали, а не только «запись №17».
    """
    e = _entity(entity_type)
    if not e.get("delete"):
        raise EntityError("«%s» удалить нельзя." % e["title"])
    raw = row(business_id, entity_type, entity_id)
    if raw is None:
        raise EntityError("Запись не найдена.")
    stop = blockers(business_id, entity_type, entity_id)
    if stop:
        raise EntityError(stop)
    lb = label(entity_type, raw)
    e["delete"](business_id, int(entity_id))
    # Записи нет — значит, нет и её связей. Ребро в пустоту хуже отсутствия
    # ребра: оно выглядит как факт, но ни на что не указывает.
    try:
        database.drop_entity_links(business_id, entity_type, entity_id)
    except Exception:
        pass
    return " · ".join(x for x in (lb.get("title"), lb.get("sub")) if x)


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


def count(business_id, entity_type, archived=False):
    return int(_entity(entity_type)["count"](business_id, archived) or 0)


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
