# -*- coding: utf-8 -*-
"""
Подключения — одна дверь во внешний мир.

Зачем нужен ещё один слой. Внизу уже есть коннекторы (папка connectors): у
каждого свои ключи, свой формат, своя манера ломаться. Но владельцу всё равно,
чем Ozon отличается от amoCRM: он спрашивает одно и то же — подключено ли,
когда обновлялось, что оно видит и почему не работает. Этот модуль отвечает на
эти вопросы одинаково для всего, что вообще может быть подключено: для живого
API, для Telegram-бота, для банковской выписки файлом и для того, чего мы ещё
не написали.

Главное правило — не врать про состояние. «Подключено» здесь не флажок,
который кто-то поставил, а вывод из фактов: ключ проверен живым запросом,
последняя синхронизация не упала, прямо сейчас не идёт выгрузка. Поэтому
статус НЕ хранится отдельной колонкой «как есть», а собирается каждый раз
заново. Интеграция, которой нет, показывается отключённой и прямо говорит, что
её ещё не написали, — «скоро будет» в зелёной рамке хуже честного «нет».

Добавить новый сервис — значит добавить адаптер и строку в реестр. Ни сервер,
ни интерфейс от этого не меняются: они разговаривают только с этим слоем.
"""
from __future__ import annotations

import logging

import connectors
import database
from connectors.base import ConnectorError

log = logging.getLogger("velor.connections")


class NotAvailable(ConnectorError):
    """Действие для этого подключения не поддерживается — с объяснением."""


# ── статусы ────────────────────────────────────────────────────────────────
# Пять состояний, и каждое означает разное действие владельца. Смешивать их
# нельзя: «сервис лежит» и «ключ отозвали» лечатся по-разному.
CONNECTED = "CONNECTED"
DISCONNECTED = "DISCONNECTED"
ERROR = "ERROR"
REQUIRES_AUTH = "REQUIRES_AUTH"
SYNCING = "SYNCING"

STATUSES = {
    CONNECTED:     {"label": "Подключено", "tone": "good",
                    "means": "Доступ проверен, данные приходят."},
    DISCONNECTED:  {"label": "Не подключено", "tone": "calm",
                    "means": "VELOR отсюда ничего не берёт."},
    ERROR:         {"label": "Ошибка", "tone": "bad",
                    "means": "Последняя попытка не удалась — причина в карточке."},
    REQUIRES_AUTH: {"label": "Нужен доступ", "tone": "warn",
                    "means": "Ключ отозван или истёк — подключите заново."},
    SYNCING:       {"label": "Синхронизация", "tone": "busy",
                    "means": "Прямо сейчас забираем данные."},
}

COMMUNICATION = "communication"
DATA = "data"

CATEGORIES = {
    COMMUNICATION: {"title": "Общение",
                    "note": "Каналы, по которым с вами говорят клиенты."},
    DATA:          {"title": "Данные",
                    "note": "Источники, из которых VELOR берёт цифры и записи."},
}


# ── единый интерфейс ───────────────────────────────────────────────────────

class Adapter:
    """
    Один способ подключиться. Всё, что умеет подключение, описано здесь.

    Наследники отвечают ровно за четыре вещи: рассказать о себе, сказать своё
    настоящее состояние, подключиться и отключиться. Всё остальное — общий код.
    """
    kind = "api"

    def __init__(self, id, name, category, group, gives, permissions,
                 fields=(), howto="", note="", manage_href="", implemented=True):
        self.id = id
        self.name = name
        self.category = category
        self.group = group
        self.gives = gives                # что даёт бизнесу, человеческим языком
        self.permissions = list(permissions)   # какие права запрашиваем
        self.fields = list(fields)        # чем настраивается (описание формы)
        self.howto = howto
        self.note = note
        self.manage_href = manage_href
        self.implemented = implemented

    # — что можно делать —
    def can_connect(self) -> bool:
        return self.implemented

    def can_sync(self) -> bool:
        return False

    def can_disconnect(self) -> bool:
        return self.implemented

    # — состояние —
    def live(self, business_id: int) -> dict:
        """Настоящее состояние подключения. По умолчанию — не подключено."""
        return {"status": DISCONNECTED, "connected_at": None, "last_sync": None,
                "error": None, "configuration": {}, "items_total": 0}

    # — действия —
    def connect(self, business_id: int, config: dict) -> None:
        raise NotAvailable(
            f"«{self.name}» подключается не здесь"
            + (f": {self.howto}" if self.howto else "."))

    def disconnect(self, business_id: int) -> None:
        database.delete_connection(business_id, self.id)

    def sync(self, business_id: int) -> dict:
        raise NotAvailable(f"«{self.name}» нечего синхронизировать.")


class ApiAdapter(Adapter):
    """
    Сервис с ключом доступа: подключение проверяется живым запросом.

    Вся работа с ключами, шифрованием и выгрузкой уже написана в connectors —
    здесь только приведение к общему виду.
    """
    kind = "api"

    def __init__(self, module, category, group, permissions, note=""):
        # GIVES у коннекторов бывает списком — приводим к одной строке здесь,
        # чтобы интерфейс не разбирался, чем ему прислали пользу сервиса.
        gives = module.GIVES
        if isinstance(gives, (list, tuple)):
            gives = ", ".join(str(g) for g in gives)
        super().__init__(module.ID, module.NAME, category, group, gives,
                         permissions, fields=module.FIELDS, howto=module.HOWTO,
                         note=note)
        self.module = module

    def can_sync(self) -> bool:
        return True

    def live(self, business_id):
        row = database.get_connection(business_id, self.id)
        if not row:
            # Подключения нет — и никаких «почти подключено». Ошибку прошлой
            # попытки мы не храним: она относилась к тому, чего уже нет.
            return {"status": DISCONNECTED, "connected_at": None, "last_sync": None,
                    "error": None, "configuration": {}, "items_total": 0}
        if connectors.is_syncing(business_id, self.id):
            status = SYNCING
        elif (row.get("status") or "") == "requires_auth":
            status = REQUIRES_AUTH
        elif row.get("last_error"):
            status = ERROR
        else:
            status = CONNECTED
        return {"status": status,
                "connected_at": row.get("connected_at"),
                "last_sync": row.get("last_sync_at"),
                "error": row.get("last_error"),
                "configuration": row.get("config") or {},
                "permissions": row.get("permissions") or [],
                "items_total": row.get("items_total") or 0,
                "meta": row.get("meta") or {}}

    def connect(self, business_id, config):
        connectors.connect(business_id, self.id, config or {},
                           permissions=self.permissions)

    def sync(self, business_id):
        return connectors.sync(business_id, self.id)


class TelegramAdapter(Adapter):
    """
    Telegram — единственный канал, который у VELOR действительно работает.

    Токен бота живёт в самом бизнесе, а не в таблице подключений: он появился
    раньше этого слоя и на нём держится вся переписка с клиентами. Дублировать
    его сюда значило бы завести вторую правду о том же токене.
    """
    kind = "native"

    def can_connect(self) -> bool:
        return False          # токен вводится в настройках бота, там же инструкция

    def can_disconnect(self) -> bool:
        return False

    def live(self, business_id):
        b = database.get_business(business_id) or {}
        token = (b.get("tg_bot_token") or "").strip()
        if not token:
            return {"status": DISCONNECTED, "connected_at": None, "last_sync": None,
                    "error": None, "configuration": {}, "items_total": 0}
        # Показываем хвост токена: узнать своего бота можно, украсть — нет.
        # Даты подключения нет и придумывать её нельзя: когда именно вписали
        # токен, мы не записывали, а дата создания бизнеса — это другое.
        return {"status": CONNECTED,
                "connected_at": None,
                "last_sync": database.last_message_at(business_id),
                "error": None,
                "configuration": {"Бот": "…" + token[-6:]},
                "items_total": database.count_client_messages_all(business_id)}


class ImportAdapter(Adapter):
    """
    Данные приходят файлом, а не по ключу: банковская выписка, таблица Excel.

    Прямого подключения к банку у нас нет, и делать вид, что есть, нельзя.
    Зато есть честный путь: владелец выгружает выписку и загружает её к нам —
    это работает сегодня. Поэтому статус всегда «не подключено», но рядом
    написано, где лежит рабочая дорога и когда ею пользовались в последний раз.
    """
    kind = "file"

    def can_connect(self) -> bool:
        return False

    def can_disconnect(self) -> bool:
        return False

    def live(self, business_id):
        last = database.last_import(business_id)
        return {"status": DISCONNECTED,
                "connected_at": None,
                "last_sync": (last or {}).get("created_at"),
                "error": None,
                "configuration": {},
                "items_total": (last or {}).get("added") or 0,
                "hint": ("Последняя загрузка: " + (last or {}).get("filename", "")
                         if last else "")}


class PlannedAdapter(Adapter):
    """
    Интеграции ещё нет.

    Такой адаптер существует ровно для того, чтобы про неё не соврали: он
    честно говорит «не подключено», отказывается подключаться и объясняет, что
    именно не написано. Карточка «скоро» с зелёной галочкой создаёт у владельца
    ложное чувство, что данные идут, — а они не идут.
    """
    kind = "planned"

    def __init__(self, *a, **kw):
        kw["implemented"] = False
        super().__init__(*a, **kw)

    def connect(self, business_id, config):
        raise NotAvailable(
            f"Интеграция с «{self.name}» ещё не написана. "
            "Когда появится — она будет здесь, и мы не станем показывать её "
            "подключённой раньше времени.")


# ── реестр ─────────────────────────────────────────────────────────────────
# Порядок = порядок в кабинете. Сначала то, чем пользуются каждый день.

def _api(module_id, category, group, permissions, note=""):
    module = connectors.REGISTRY.get(module_id)
    return ApiAdapter(module, category, group, permissions, note) if module else None


def _build():
    items = [
        TelegramAdapter(
            "telegram", "Telegram", COMMUNICATION, "Мессенджеры",
            "Клиенты пишут боту — VELOR отвечает, заводит заявки и помнит историю.",
            ["читать сообщения бота", "отвечать от имени бизнеса"],
            howto="Токен бота вводится в разделе «Бот в Telegram».",
            manage_href="guide.html"),
        PlannedAdapter(
            "instagram", "Instagram", COMMUNICATION, "Мессенджеры",
            "Директ и комментарии в одном месте с остальными обращениями.",
            ["читать директ", "отвечать в директ"],
            note="Нужен бизнес-аккаунт и доступ Meta — интеграции пока нет."),
        PlannedAdapter(
            "whatsapp", "WhatsApp", COMMUNICATION, "Мессенджеры",
            "Переписка с клиентами там, где им привычнее.",
            ["читать сообщения", "отвечать от имени бизнеса"],
            note="Нужен WhatsApp Business API — интеграции пока нет."),
        PlannedAdapter(
            "website", "Сайт", COMMUNICATION, "Сайт",
            "Виджет на сайте: посетитель пишет — попадает в те же заявки.",
            ["принимать сообщения с сайта"],
            note="Виджет ещё не написан."),
    ]
    slack = _api("slack", COMMUNICATION, "Мессенджеры",
                 ["читать каналы", "писать в канал"])
    if slack:
        items.append(slack)

    items += [
        PlannedAdapter(
            "bank", "Банк", DATA, "Деньги",
            "Операции по счёту подтягивались бы сами, без выгрузок.",
            ["читать выписку по счёту"],
            note="Прямого подключения к банку нет. Сегодня работает загрузка "
                 "выписки файлом.",
            manage_href="import.html"),
        ImportAdapter(
            "statement", "Выписка файлом", DATA, "Деньги",
            "CSV, XLSX или PDF из банк-клиента: VELOR разложит операции по категориям.",
            ["читать загруженный файл"],
            howto="Выгрузите выписку в банке и загрузите её в разделе «Импорт».",
            manage_href="import.html"),
        ImportAdapter(
            "excel", "Excel", DATA, "Таблицы",
            "Таблица с операциями или заявками — загрузкой файла.",
            ["читать загруженный файл"],
            howto="Раздел «Импорт» принимает XLSX и CSV.",
            manage_href="import.html"),
        PlannedAdapter(
            "google_sheets", "Google Sheets", DATA, "Таблицы",
            "Таблица обновляется у вас — цифры обновляются у VELOR.",
            ["читать выбранную таблицу"],
            note="Нужен доступ Google OAuth — интеграции пока нет."),
        PlannedAdapter(
            "google_drive", "Google Drive", DATA, "Документы",
            "Договоры и прайсы из папки попадали бы в память бизнеса сами.",
            ["читать выбранную папку"],
            note="Нужен доступ Google OAuth — интеграции пока нет."),
    ]
    for pid, group, perms in (
            ("bitrix24", "CRM", ["читать сделки", "читать контакты"]),
            ("amocrm",   "CRM", ["читать сделки", "читать контакты"]),
            ("hubspot",  "CRM", ["читать сделки", "читать контакты"]),
            ("ozon",         "Маркетплейсы", ["читать заказы", "читать товары"]),
            ("wildberries",  "Маркетплейсы", ["читать заказы", "читать продажи"]),
            ("shopify",      "Магазины",     ["читать заказы", "читать покупателей"]),
            ("woocommerce",  "Магазины",     ["читать заказы", "читать покупателей"]),
            ("yookassa",     "Платежи",      ["читать платежи"]),
            ("cloudpayments", "Платежи",     ["читать платежи"]),
            ("stripe",       "Платежи",      ["читать платежи"])):
        a = _api(pid, DATA, group, perms)
        if a:
            items.append(a)
    return items


ADAPTERS = {a.id: a for a in _build()}
ORDER = [a.id for a in _build()]


def adapter(provider: str) -> Adapter:
    a = ADAPTERS.get(provider)
    if not a:
        raise NotAvailable("Неизвестное подключение.")
    return a


# ── чтение ─────────────────────────────────────────────────────────────────

def state(business_id: int, provider: str) -> dict:
    """
    Паспорт одного подключения: всё, что о нём известно, одним словарём.

    Права и настройка показываются даже у неподключённого — владелец должен
    видеть, о чём его попросят, ДО того, как согласится.
    """
    a = adapter(provider)
    live = a.live(business_id)
    status = live.get("status") or DISCONNECTED
    return {
        "provider": a.id,
        "name": a.name,
        "category": a.category,
        "category_title": CATEGORIES[a.category]["title"],
        "group": a.group,
        "kind": a.kind,
        "gives": a.gives,
        "status": status,
        "status_label": STATUSES[status]["label"],
        "status_tone": STATUSES[status]["tone"],
        "status_means": STATUSES[status]["means"],
        "connected_at": live.get("connected_at"),
        # Права показываем объявленные: у подключения они те, что записаны при
        # подключении, у остальных — те, что мы запросим.
        "permissions": live.get("permissions") or a.permissions,
        "configuration": live.get("configuration") or {},
        "fields": a.fields,
        "last_sync": live.get("last_sync"),
        "error": live.get("error"),
        "items_total": live.get("items_total") or 0,
        "implemented": a.implemented,
        "can_connect": a.can_connect(),
        "can_sync": a.can_sync() and status in (CONNECTED, ERROR, REQUIRES_AUTH),
        "can_disconnect": a.can_disconnect() and status != DISCONNECTED,
        "howto": a.howto,
        "note": live.get("hint") or a.note,
        "manage_href": a.manage_href,
    }


def catalog(business_id: int) -> dict:
    """Весь раздел «Подключения» одним ответом: категории, карточки, итоги."""
    items = [state(business_id, pid) for pid in ORDER]
    cats = []
    for key, meta in CATEGORIES.items():
        mine = [i for i in items if i["category"] == key]
        cats.append({"key": key, "title": meta["title"], "note": meta["note"],
                     "connected": sum(1 for i in mine if i["status"] == CONNECTED),
                     "total": len(mine), "items": mine})
    live = [i for i in items if i["status"] == CONNECTED]
    trouble = [i for i in items if i["status"] in (ERROR, REQUIRES_AUTH)]
    return {
        "categories": cats,
        "statuses": [{"key": k, **v} for k, v in STATUSES.items()],
        "summary": {
            "connected": len(live),
            "trouble": len(trouble),
            "available": sum(1 for i in items if i["implemented"]),
            "planned": sum(1 for i in items if not i["implemented"]),
            "total": len(items),
        },
    }


# ── действия ───────────────────────────────────────────────────────────────

def connect(business_id: int, provider: str, config: dict) -> dict:
    """Подключить. Ключи проверяются живым запросом — иначе не сохраняем."""
    a = adapter(provider)
    if not a.can_connect():
        # Отказ должен объяснять причину, а она у всех разная: интеграции ещё
        # нет, канал настраивается в другом разделе, данные приходят файлом.
        # Поэтому спрашиваем сам адаптер, а не отвечаем за него.
        a.connect(business_id, config or {})
        raise NotAvailable(a.note or f"«{a.name}» подключается в другом месте.")
    a.connect(business_id, config or {})
    return state(business_id, provider)


def disconnect(business_id: int, provider: str) -> dict:
    """Отключить. Загруженные данные остаются: они принадлежат бизнесу."""
    a = adapter(provider)
    if not a.can_disconnect():
        raise NotAvailable(f"«{a.name}» здесь не отключается.")
    a.disconnect(business_id)
    return state(business_id, provider)


def sync(business_id: int, provider: str) -> dict:
    """Забрать данные сейчас."""
    a = adapter(provider)
    if not a.can_sync():
        raise NotAvailable(f"«{a.name}» нечего синхронизировать.")
    result = a.sync(business_id)
    return {"result": result, "state": state(business_id, provider)}
