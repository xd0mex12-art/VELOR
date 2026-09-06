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
import instagram as instagram_api
import vk as vk_api
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
MIND = "mind"

CATEGORIES = {
    # «Разум» идёт первым намеренно. Без модели VELOR продолжает считать числа
    # и вести записи, но перестаёт делать выводы — а выводы и есть продукт.
    # Пока этой категории не было, владелец не имел ни одного места в кабинете,
    # где видно, отвечает модель или нет.
    MIND:          {"title": "Разум",
                    "note": "Модель, которая думает за VELOR. Без неё числа "
                            "считаются, а выводы не собираются."},
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
        self._implemented = implemented

    @property
    def implemented(self) -> bool:
        """
        Готова ли интеграция настолько, чтобы ей можно было пользоваться.

        У большинства это отметка, поставленная в реестре раз и навсегда. Но
        бывает иначе: канал написан целиком, а войти в него нельзя, пока не
        сделана настройка на стороне площадки. Тогда честный ответ — «ещё нет»,
        и он обязан меняться сам, без правки кода.
        """
        return self._implemented

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

    def needs_login(self) -> bool:
        """
        Подключение идёт через экран самого сервиса, а не через нашу форму.

        Разница принципиальная для интерфейса: у ключей есть поля, которые
        владелец копирует, а здесь копировать нечего — его нужно увести на
        настоящую страницу входа. Своего экрана входа мы не рисуем никогда:
        пароль от Instagram вводится только в Instagram.
        """
        return False

    def login_url(self, business_id: int) -> str:
        raise NotAvailable(f"«{self.name}» так не подключается.")

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


class OAuthAdapter(Adapter):
    """
    Сервис, куда владелец входит своей учётной записью, а не ключом.

    Отличается от ApiAdapter одним, но важным: секрет мы не спрашиваем и не
    видим. Владелец уходит на страницу сервиса, вводит пароль там, а нам
    возвращается доступ, выданный лично ему и ограниченный правами, которые он
    подтвердил. Отозвать его он тоже может у себя, не спрашивая нас.
    """
    kind = "oauth"

    def needs_login(self) -> bool:
        return True


class InstagramAdapter(OAuthAdapter):
    """
    Директ Instagram как канал VELOR.

    Состояние собирается из фактов: есть ли выданный доступ, не отозвали ли его,
    не истёк ли срок, не упала ли последняя выгрузка. Отдельно смотрим на срок
    жизни доступа: Instagram выдаёт его на 60 суток, и «Подключено» с мёртвым
    доступом было бы худшим видом вранья — оно выглядит рабочим.
    """

    @property
    def implemented(self) -> bool:
        # Канал написан целиком, но приложение Meta пока не создано, а без него
        # войти нельзя никому. Показывать карточку с кнопкой в таком состоянии
        # значит обещать то, чего сегодня нет, — поэтому до появления ключей
        # Meta канал стоит в «готовится» рядом с остальными неготовыми.
        # Ключи появятся — карточка оживёт сама, без правки кода.
        return self.can_connect()

    def can_connect(self) -> bool:
        # Приложение Meta — настройка сервера VELOR, а не бизнеса. Пока её нет,
        # кнопка обязана не работать: экран входа, который ничем не кончится,
        # хуже честной надписи.
        return instagram_api.configured() and bool(instagram_api.redirect_uri())

    def can_sync(self) -> bool:
        return True

    def login_url(self, business_id):
        return instagram_api.authorize_url(business_id)

    def live(self, business_id):
        row = database.get_connection(business_id, self.id)
        if not row:
            return {"status": DISCONNECTED, "connected_at": None, "last_sync": None,
                    "error": None, "configuration": {}, "items_total": 0,
                    "hint": _ig_note()}
        meta = row.get("meta") or {}
        expires = meta.get("token_expires_at")
        if (row.get("status") or "") == "requires_auth" or _expired(expires):
            status = REQUIRES_AUTH
        elif row.get("last_error"):
            status = ERROR
        else:
            status = CONNECTED
        config = dict(row.get("config") or {})
        if expires:
            config["Доступ действует до"] = expires[:10]
        fields = meta.get("webhook_fields") or []
        if fields:
            # Пишем, на что подписались НА САМОМ ДЕЛЕ. Если Meta не дала эхо,
            # владелец должен знать, что ответы из приложения Instagram VELOR
            # не услышит, — а не узнавать это по последствиям.
            config["События"] = ", ".join(fields)
        return {"status": status,
                "connected_at": row.get("connected_at"),
                "last_sync": row.get("last_sync_at"),
                "error": row.get("last_error"),
                "configuration": config,
                "permissions": row.get("permissions") or [],
                "items_total": row.get("items_total") or 0,
                # Пояснение считаем сейчас, а не при запуске: настройки сервера
                # могут появиться позже, и надпись «канал выключен» обязана
                # исчезнуть в тот же миг, а не до перезапуска.
                "hint": _ig_note(),
                "meta": meta}

    def connect(self, business_id, config):
        # Сюда приходят те, кто прислал форму с полями. Полей у входа нет, и
        # причина отказа у двух случаев разная: либо канал вообще выключен на
        # сервере, либо подключаться нужно другим путём. Одинаковый текст на оба
        # случая отправил бы владельца чинить не то.
        if not self.can_connect():
            raise NotAvailable(_ig_note())
        raise NotAvailable(
            "Instagram подключается входом в сам Instagram: нажмите «Подключить» "
            "и подтвердите доступ на странице Meta. Пароль от аккаунта VELOR "
            "не спрашивает и не хранит.")

    def disconnect(self, business_id):
        # Сначала отписываемся от событий, потом забываем доступ. В обратном
        # порядке Meta продолжала бы звонить в наш вебхук про аккаунт, к
        # которому у нас уже нет ни доступа, ни права.
        token = instagram_api.token_of(business_id)
        if token:
            try:
                instagram_api.unsubscribe(token)
            except ConnectorError:
                pass          # доступ мог быть отозван раньше — это не мешает отключить
        database.delete_connection(business_id, self.id)

    def sync(self, business_id):
        return instagram_api.pull_recent(business_id)


def _expired(stamp_str) -> bool:
    """Истёк ли срок доступа. Нет отметки — считаем живым, гадать не станем."""
    if not stamp_str:
        return False
    import datetime
    try:
        return datetime.datetime.strptime(str(stamp_str)[:19], "%Y-%m-%d %H:%M:%S") \
            < datetime.datetime.utcnow()
    except ValueError:
        return False


class VkAdapter(Adapter):
    """
    Сообщения сообщества ВКонтакте.

    Ключ приносит владелец — тот самый, что он выдал себе в своём сообществе.
    Никакого приложения VELOR в ВК не заводит и никакой проверки не проходит:
    это та же схема, что в Telegram, и именно поэтому канал доступен всем, а
    не только тем, за кого мы поручились.

    Ключ проверяется живым вызовом ДО сохранения. Принять ключ, не спросив у ВК,
    чей он, значит записать «подключено» и узнать правду в момент, когда придёт
    первый клиент, — то есть в худший из возможных.
    """
    kind = "api"

    def can_sync(self) -> bool:
        return True

    def live(self, business_id):
        row = database.get_connection(business_id, self.id)
        if not row:
            return {"status": DISCONNECTED, "connected_at": None, "last_sync": None,
                    "error": None, "configuration": {}, "items_total": 0,
                    "hint": self.note}
        if (row.get("status") or "") == "requires_auth":
            status = REQUIRES_AUTH
        elif row.get("last_error"):
            status = ERROR
        else:
            status = CONNECTED
        config = dict(row.get("config") or {})
        # Пока ВК ни разу не позвонил, «подключено» означает только «ключ
        # принят». Разница важная: ключ может быть верным, а Callback API —
        # не настроенным, и тогда сообщения не придут никогда.
        if not row.get("last_sync_at"):
            config["Callback API"] = "ещё ни одного события"
        return {"status": status,
                "connected_at": row.get("connected_at"),
                "last_sync": row.get("last_sync_at"),
                "error": row.get("last_error"),
                "configuration": config,
                "permissions": row.get("permissions") or [],
                "items_total": row.get("items_total") or 0,
                "hint": self.note,
                "meta": row.get("meta") or {}}

    def connect(self, business_id, config):
        config = config or {}
        # Ключ копируют мышкой из чужого кабинета, и вместе с ним приезжают
        # пробелы и перевод строки. Глазами это не видно, а ВК отвечает
        # «неверный ключ», и владелец ищет ошибку не там.
        token = str(config.get("token") or "").replace(chr(160), " ").strip()
        confirmation = str(config.get("confirmation") or "").replace(chr(160), " ").strip()
        if not token:
            raise NotAvailable("Вставьте ключ доступа сообщества.")
        if not confirmation:
            raise NotAvailable(
                "Вставьте строку подтверждения — ВК показывает её в настройках "
                "Callback API вашего сообщества.")
        try:
            info = vk_api.group_info(token)
        except ConnectorError as e:
            raise NotAvailable(str(e))
        # Одно сообщество — один бизнес. Иначе события чужого сообщества
        # попадали бы в чужую переписку, а это утечка, а не неудобство.
        other = database.find_business_by_vk_group(info.get("group_id"))
        if other and int(other["id"]) != int(business_id):
            raise NotAvailable("Это сообщество уже подключено к другой компании.")
        vk_api.save_access(business_id, token, confirmation, info)

    def disconnect(self, business_id):
        database.delete_connection(business_id, self.id)

    def sync(self, business_id):
        return vk_api.pull_recent(business_id)


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

def _ig_note() -> str:
    """
    Что владелец должен знать до подключения — включая то, чего канал не может.

    Ограничения Instagram не наши, но и прятать их нельзя: человек, который
    ждёт, что VELOR напишет клиенту первым или подтянет переписку за год,
    столкнётся с этим в худший момент — когда уже рассчитывал на канал.
    """
    st = instagram_api.setup_state()
    if not st["ready"]:
        # Раньше здесь перечислялись недостающие переменные окружения. Владельцу
        # цветочного магазина это не говорит ничего, кроме «что-то сломано»:
        # починить он не может, а тревогу получает. Чего именно не хватает,
        # написано в журнале сервера — тому, кто действительно это чинит.
        return ("Директ Instagram готовится. Сейчас клиенты пишут в Telegram — "
                "как только канал откроется, он появится здесь.")
    return ("Instagram разрешает отвечать в течение суток после сообщения клиента "
            "(человеку — до семи), писать первым не даёт никому и отдаёт не больше "
            "20 последних сообщений переписки. Почты и телефона в его API нет.")


def _api(module_id, category, group, permissions, note=""):
    module = connectors.REGISTRY.get(module_id)
    return ApiAdapter(module, category, group, permissions, note) if module else None


class ModelAdapter(Adapter):
    """
    Модель, которая думает за VELOR.

    Состояние проверяется живым запросом, а не наличием ключа: «ключ в файле
    лежит» и «модель отвечает» — разные вещи, и разница как раз та, из-за
    которой продукт молча переставал думать. Ответ сервиса переводится на
    человеческий: 401 — это не «ошибка сети», а «ключ больше не действует».

    Ключ через кабинет не вводится и правильно: он общий для всей установки,
    живёт в .env рядом с сервером и в браузер попадать не должен. Карточка
    говорит, что и куда положить, — вводит человек сам.
    """
    kind = "model"

    def can_connect(self):
        return True          # свой ключ — по желанию, поверх базового

    def can_disconnect(self):
        return True          # отключить свой ключ = вернуться на базовый

    def can_sync(self):
        return True          # «Проверить» — это и есть синхронизация состояния

    def _probe(self, business_id=None):
        """Спросить модель коротко и вернуть (статус, объяснение)."""
        import ai
        if not ai.ai_available(business_id):
            return DISCONNECTED, None
        # Проваливаются все — значит, назвать надо всех. Пока сообщение
        # показывало последнего опрошенного, владелец шёл чинить не тот ключ.
        bad = []
        for name, fn in ai._all_providers(business_id):
            try:
                fn("Отвечай одним словом.", [{"role": "user", "content": "привет"}],
                   max_tokens=8)
                return CONNECTED, None
            except Exception as e:
                bad.append((name, str(e)))
        name = ", ".join(n for n, _ in bad)
        text = " ".join(t for _, t in bad)
        if "401" in text or "Unauthorized" in text or "invalid" in text.lower() \
                or "credentials" in text.lower():
            # Куда идти чинить — зависит от того, ЧЕЙ ключ сломан. Свой ключ
            # бизнеса лежит в базе и меняется здесь же; базовый ключ VELOR
            # живёт в .env на сервере, и владелец компании до него не дотянется.
            if ai.own_key(business_id):
                return REQUIRES_AUTH, (
                    f"Ключ не принимают: {name}. Замените свой ключ здесь же — "
                    "или отключите его, и VELOR вернётся на базовый.")
            return REQUIRES_AUTH, (
                f"Базовый ключ не принимают: {name}. Он отозван, истёк или выдан "
                "другому аккаунту — нужен новый в .env на сервере. Свой ключ можно "
                "подключить здесь, тогда ИИ заработает сразу.")
        if "429" in text or "quota" in text.lower() or "limit" in text.lower():
            return ERROR, f"Отказ по лимиту ({name}): запросы кончились или превышена квота."
        return ERROR, f"Не отвечают: {name}. {text[:140]}"

    def live(self, business_id: int) -> dict:
        import ai, secretbox
        own = ai.own_key(business_id)
        try:
            status, err = self._probe(business_id)
        except Exception as e:
            status, err = ERROR, str(e)[:160]

        # Чей ключ отвечает — половина смысла этой карточки. «Работает» и
        # «работает на вашем ключе» это разные новости, и вторую владелец
        # должен видеть без догадок.
        conf = {"ключ": ("свой (" + own[0] + ", " + secretbox.mask(own[1]) + ")")
                        if own else "базовый ключ VELOR"}
        base = ", ".join(n for n, _ in ai._platform_providers()) or "не настроен"
        conf["базовый"] = base

        if own and status == CONNECTED:
            # Свой ключ мог упасть, а ответить базовый — тогда это не «всё
            # хорошо». Спрашиваем свой отдельно и говорим правду.
            try:
                prov, key = own
                ai._MODEL_FNS[prov]("Отвечай одним словом.",
                                    [{"role": "user", "content": "привет"}],
                                    max_tokens=8, key=key)
            except Exception as e:
                err = ("Ваш ключ не отвечает (" + str(e)[:90] + ") — "
                       "работаем на базовом ключе VELOR.")
                status = REQUIRES_AUTH
        row = database.get_connection(business_id, self.id) or {}
        return {"status": status, "connected_at": row.get("connected_at"),
                "last_sync": None, "error": err, "configuration": conf,
                "items_total": 0}

    def connect(self, business_id: int, config: dict) -> None:
        import ai, secretbox, json as _json
        prov = (config.get("provider") or "").strip().lower()
        key = (config.get("key") or "").strip()
        if prov not in ai._MODEL_FNS:
            raise NotAvailable("Выберите модель: " + ", ".join(sorted(ai._MODEL_FNS)) + ".")
        if not key:
            raise NotAvailable("Вставьте ключ.")
        # Проверяем ДО сохранения: принять ключ, который не работает, значит
        # выключить бизнесу ИИ и не сказать об этом.
        try:
            ai._MODEL_FNS[prov]("Отвечай одним словом.",
                                [{"role": "user", "content": "привет"}],
                                max_tokens=8, key=key)
        except Exception as e:
            raise NotAvailable("Модель не приняла этот ключ: " + str(e)[:160])
        database.save_connection(
            business_id, self.id,
            credentials_blob=secretbox.seal(_json.dumps({"provider": prov, "key": key})),
            status="connected", permissions=self.permissions,
            config={"провайдер": prov})

    def sync(self, business_id: int) -> dict:
        status, err = self._probe(business_id)
        if status != CONNECTED:
            raise NotAvailable(err or "Модель не отвечает.")
        return {"ok": True}


def _build():
    items = [
        ModelAdapter(
            "model", "Модель", MIND, "Разум",
            "Читает ваши цифры и превращает их в выводы: брифинг, риски, "
            "возможности, разбор входящих. Числа и записи считаются без неё, "
            "выводы — нет.",
            ["отправлять текст ваших данных в модель"],
            fields=[
                {"key": "provider", "label": "Модель", "required": True,
                 "placeholder": "gigachat / claude / gemini",
                 "hint": "Чей ключ вы вставляете."},
                {"key": "key", "label": "Ключ", "required": True, "secret": True,
                 "placeholder": "вставьте ключ",
                 "hint": "Хранится зашифрованным и обратно в браузер не отдаётся."},
            ],
            howto="ИИ уже работает на базовом ключе VELOR — подключать ничего не "
                  "нужно. Свой ключ имеет смысл, если хотите свой лимит, свой "
                  "счёт или другую модель. Ключ проверяется живым запросом перед "
                  "сохранением и хранится зашифрованным.",
            note="Свой ключ отключить можно в любой момент — вернётесь на базовый."),
        TelegramAdapter(
            "telegram", "Telegram", COMMUNICATION, "Мессенджеры",
            "Клиенты пишут боту — VELOR отвечает, заводит заявки и помнит историю.",
            ["читать сообщения бота", "отвечать от имени бизнеса"],
            howto="Токен бота вводится в разделе «Бот в Telegram».",
            manage_href="guide.html"),
        VkAdapter(
            "vk", "ВКонтакте", COMMUNICATION, "Мессенджеры",
            "Сообщения сообщества попадают в те же обращения: VELOR отвечает, "
            "заводит клиента и заявку, а вы в любой момент берёте разговор на "
            "себя.",
            vk_api.PERMISSIONS_RU,
            fields=[
                {"key": "token", "label": "Ключ доступа сообщества",
                 "required": True, "secret": True, "placeholder": "vk1.a....",
                 "hint": "Управление → Работа с API → Ключи доступа. Нужно право "
                         "«Сообщения сообщества». Хранится зашифрованным."},
                {"key": "confirmation", "label": "Строка подтверждения",
                 "required": True, "placeholder": "например, a1b2c3d4",
                 "hint": "Управление → Работа с API → Callback API. ВК показывает "
                         "её сам — просто перенесите сюда."},
            ],
            howto="Нужно сообщество ВКонтакте с включёнными сообщениями. Сначала "
                  "вставьте сюда ключ и строку подтверждения, и только потом "
                  "нажимайте «Подтвердить» в самом ВК: он проверяет адрес сразу, "
                  "и к этому моменту мы уже должны знать, чьё это сообщество.",
            note="Написать человеку первым ВКонтакте не даёт, пока он сам не "
                 "написал сообществу или не разрешил сообщения. Истории за время "
                 "до подключения там тоже нет.",
            manage_href="vk.html"),
        InstagramAdapter(
            "instagram", "Instagram", COMMUNICATION, "Мессенджеры",
            "Директ попадает в те же обращения: VELOR отвечает, заводит клиента "
            "и заявку, а вы в любой момент берёте разговор на себя.",
            instagram_api.PERMISSIONS_RU,
            howto="Нужен профессиональный аккаунт Instagram (бизнес или автор). "
                  "Вход происходит на странице Instagram — пароль остаётся там.",
            note=_ig_note(),
            manage_href="instagram.html"),
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
        # Вход на стороне сервиса: интерфейс не должен рисовать форму для полей,
        # которых нет, — он должен увести человека на настоящую страницу входа.
        "needs_login": a.needs_login(),
        "howto": a.howto,
        "note": live.get("hint") or a.note,
        # Дверь в раздел показываем только у готовой интеграции: ссылка на
        # экран того, чего ещё нет, — обещание, данное вёрсткой.
        "manage_href": a.manage_href if a.implemented else "",
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
