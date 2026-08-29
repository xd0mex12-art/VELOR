# -*- coding: utf-8 -*-
"""
ЛИД — коммерческая возможность. Не человек и не заявка, а то, что между ними.

Зачем отдельная сущность
────────────────────────
До этого «лид» в VELOR существовал как строчка «Интерес: пионы» в заметках
клиента. У такой записи нет статуса, нет истории, нет причины проигрыша, и её
нельзя посчитать: «сколько у нас лидов» приходилось выводить из заметок,
переписки и заявок — и три способа давали три разных числа. Теперь способ
один: строка в таблице leads. Всё остальное — производные от неё.

Клиент и лид — разные вещи
──────────────────────────
Клиент — человек. Лид — его намерение купить. У одного человека может быть
несколько лидов: майский букет на день рождения и августовский на свадьбу —
это две возможности, а не два клиента и не одна затянувшаяся переписка.
Поэтому лид ссылается на клиента, а не заменяет его.

Когда лид заводится
───────────────────
Не на каждое сообщение. «Здравствуйте» и «спасибо» — не возможность, а
вежливость. Признак коммерческого намерения ищется правилами: они дешевле
модели, объяснимы словом, которое сработало, и не зависят от того, подключён
ли ИИ. Правило осторожное: лучше не завести лид, чем завести пять ложных —
ненайденную возможность видно в переписке, а выдуманную приходится вычищать
руками, и владелец перестаёт верить воронке целиком.

Один разговор — один лид
────────────────────────
«Сколько стоит?» → «а доставка есть?» → «а можно завтра?» — это один
коммерческий разговор. Пока возможность открыта, новые сообщения продолжают
её, а не плодят новые. Новый лид появляется, когда предыдущий закрыт (купил
или не сложилось) — или когда прошлый разговор давно затих.

Слово человека сильнее догадки
──────────────────────────────
Та же граница, что и в памяти бизнеса: в колонки попадает только сказанное
вслух — клиентом или владельцем. Предположение модели («похоже, бюджет тысяч
пятнадцать») колонкой не становится никогда: оно живёт в meta.guess и видно
как догадка. А поле, которое правил владелец, автоматика не трогает вообще.
"""
from __future__ import annotations

import logging
import re

import database

log = logging.getLogger("velor.leads")


# ── жизненный цикл ─────────────────────────────────────────────────────────
# Одно понятие, а не два. «Статус» и «этап» разделяют там, где у сделки долгий
# путь с несколькими решениями внутри; у малого бизнеса путь короткий, и вторая
# шкала означала бы вторую правду о том же самом. Понадобится этап — он
# добавится колонкой, не ломая ни одного из этих состояний.

NEW = "new"
QUALIFIED = "qualified"
IN_PROGRESS = "in_progress"
WON = "won"
LOST = "lost"

STATUSES = database.LEAD_STATUSES
OPEN = database.LEAD_OPEN

STATUS_RU = {
    NEW:         "Новый",
    QUALIFIED:   "Интерес подтверждён",
    IN_PROGRESS: "В работе",
    WON:         "Купил",
    LOST:        "Не сложилось",
}

STATUS_HINT = {
    NEW:         "человек проявил интерес, с ним ещё не работали",
    QUALIFIED:   "понятно, чего он хочет, и это нам подходит",
    IN_PROGRESS: "идёт разговор: обсуждаем, считаем, договариваемся",
    WON:         "стал заказом",
    LOST:        "не купил — причина записана",
}

# Причины проигрыша. Короткий закрытый список: длинный никто не заполняет
# честно, а без причины проигрыш не отвечает на единственный вопрос, ради
# которого его записывают, — «что мы теряем чаще всего».
LOST_REASONS = {
    "price":       "Дорого",
    "competitor":  "Ушёл к другим",
    "no_response": "Перестал отвечать",
    "timing":      "Не сейчас",
    "unavailable": "У нас этого не было",
    "other":       "Другое",
}
DEFAULT_LOST_REASON = "other"

# Откуда пришла возможность. Берём словарь источников, который в системе уже
# есть (entities.SOURCE_RU), — заводить второй значило бы получить «telegram» и
# «Telegram» как два разных источника в одной и той же воронке.
SOURCES = ("telegram", "instagram", "website", "inbox", "manual", "other")

# Сколько дней открытый разговор считается тем же разговором. Через месяц
# молчания «а можно ещё раз?» — это уже новая возможность, а не продолжение
# старой: между ними был другой повод, другой сезон и, возможно, другая цена.
WINDOW_DAYS = 30

# Поля, которые может занять автоматика, и которые владелец может у неё
# отобрать, поправив руками.
SOFT_FIELDS = ("title", "interest", "value", "currency")


class LeadError(Exception):
    """Действие над лидом невозможно — с объяснением для человека."""


# ── коммерческое намерение ─────────────────────────────────────────────────
# Правилами, а не моделью. Причина та же, по которой прайс разбирается
# правилами: здесь важнее объяснимость, чем догадливость. Сработавшее слово
# можно показать владельцу, а «модель так решила» — нельзя.

# Сильные признаки: человек говорит о покупке прямо.
_STRONG = [
    r"скольк\w*\s+(?:это\s+)?(?:будет\s+)?(?:стоит|стоят|стоить|обойд\w+|выйдет)",
    r"\bпо\s?ч[её]м\b",
    r"хочу\s+(?:заказ\w*|купить|записаться|взять|приобрести|оформить)",
    r"хотел\w*\s+бы\s+(?:заказ\w*|купить|записаться|оформить)",
    r"можно\s+(?:ли\s+)?(?:у\s+вас\s+)?(?:заказ\w*|купить|записаться|забронировать|оформить)",
    r"запишите\s+меня",
    r"записаться\s+(?:на|к)\b",
    r"есть\s+(?:ли\s+)?в\s+наличии",
    r"\bв\s+наличии\b",
    r"(?:оформ\w+|сделать|разместить)\s+заказ",
    r"\bзаказать\s+[\wа-яё]{3,}",
    r"забронир\w+",
    # «нужен» — это «нуж» + «ен», а не «нужн» + окончание: без первой
    # ветки самая частая фраза заявки не опознавалась вовсе.
    r"нуж(?:ен|н\w*)\s+[\wа-яё]{3,}",
    r"как(?:ая|ие|ов[аы])?\s+(?:у\s+вас\s+)?(?:цена|цены|стоимость)",
]

# Слабые: разговор рядом с деньгами, но ещё не о покупке. Одного мало.
_WEAK = [
    r"\bцен[аыу]\b", r"\bпрайс\w*\b", r"\bстоимост\w*\b",
    r"\bскидк\w*", r"\bдоставк\w*", r"\bскольк\w*\b", r"\bсвободн\w*\b",
]

# Отказ. «Спасибо, ничего не нужно» содержит «нужно» и без этой проверки
# заводило бы возможность ровно там, где человек от неё отказался.
# Граница слова перед «не» обязательна: без неё «Мне нужен букет» читается
# как «…не нужен», и живая заявка молча отбрасывается как отказ.
_NO = re.compile(r"\bне\s+нуж(?:ен|н\w*)|ничего\s+не\s+(?:нуж\w*|над\w*)|"
                 r"\bуже\s+(?:не\s+нуж\w*|заказал\w*|купил\w*)|отмен\w+\s+заказ",
                 re.I)

_STRONG_RE = [re.compile(p, re.I) for p in _STRONG]
_WEAK_RE = [re.compile(p, re.I) for p in _WEAK]

MIN_SCORE = 2


def intent(text: str):
    """
    Есть ли в сообщении коммерческое намерение. Возвращает (да/нет, чем доказали).

    Доказательство — это сработавшие слова, а не число. Владелец должен иметь
    возможность посмотреть на лид и сказать «понятно, почему он тут».
    """
    s = (text or "").strip()
    if len(s) < 3:
        return False, []
    if _NO.search(s):
        return False, ["человек сказал, что не нужно"]
    why, score = [], 0
    for rx in _STRONG_RE:
        m = rx.search(s)
        if m:
            score += 2
            why.append(m.group(0).strip().lower())
            break                      # одного прямого признака достаточно
    for rx in _WEAK_RE:
        m = rx.search(s)
        if m:
            score += 1
            why.append(m.group(0).strip().lower())
    return score >= MIN_SCORE, why[:4]


def _short(s, n=90):
    s = re.sub(r"\s+", " ", str(s or "").strip())
    return s if len(s) <= n else s[:n - 1] + "…"


def title_of(text, interest=None):
    """Заголовок возможности — словами того, кто её создал, а не пересказом."""
    return _short(interest or text or "Обращение", 90) or "Обращение"


def stated_value(text):
    """
    Сумма, которую человек НАЗВАЛ САМ. Возвращает (сумма, валюта) или (None, None).

    Это не оценка сделки и не догадка о бюджете: берётся только то, что стоит
    в тексте с признаком денег («15 000 ₽», «тысяч пять»). Голое число суммой
    не считается — «100 цветов» это количество.
    """
    try:
        import understanding
        money = understanding.find_money(text or "")
    except Exception:            # разбор сумм не обязан ронять приём лида
        return None, None
    if not money:
        return None, None
    top = max(money, key=lambda m: m["amount"])
    return int(top["amount"]), top.get("currency") or "RUB"


# ── чтение ────────────────────────────────────────────────────────────────

def open_for(business_id, client_id):
    """
    Живая возможность этого человека — если разговор ещё не остыл.

    Открытый лид месячной давности не продолжают: у людей за месяц меняется
    повод. Мы его не закрываем сами (это решение владельца), но новое обращение
    заводим отдельной строкой, иначе август припишется маю.
    """
    lead = database.open_lead_of_client(business_id, client_id)
    if not lead:
        return None
    when = lead.get("last_activity_at") or lead.get("created_at")
    if _days_since(when) > WINDOW_DAYS:
        return None
    return lead


def _days_since(when):
    import datetime
    s = str(when or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
        return (datetime.datetime.utcnow() - dt).days
    return 0                      # дату не разобрали — считаем разговор свежим


def get(business_id, lead_id):
    return database.get_lead(int(lead_id), business_id)


def overview(business_id):
    """Воронка числами — из таблицы лидов и ниоткуда больше."""
    return database.leads_overview(business_id)


# ── запись ────────────────────────────────────────────────────────────────

def _link(business_id, lead_id, event, *, source_kind="manual", actor="business",
          actor_id=None, note=None, changes=None):
    """След в общей истории записей. Лид попадает туда же, где живут остальные."""
    try:
        database.add_memory_link(business_id, "lead", lead_id, event=event,
                                 source_kind=source_kind, actor=actor,
                                 actor_id=actor_id, note=note,
                                 changes=changes or {})
    except Exception:
        log.exception("История лида не записалась (biz %s, lead %s)", business_id, lead_id)


def create(business_id, *, title=None, interest=None, client_id=None,
           source="manual", channel=None, value=None, currency=None,
           status=NEW, why=None, message_id=None, actor="business", actor_id=None,
           owner_fields=(), meta=None, note=None):
    """
    Завести возможность. Единственная дверь: и разговор, и рука владельца, и
    разбор материала приходят сюда.
    """
    title = _short(title or interest or "Обращение", 200)
    if not title:
        raise LeadError("Нужно короткое описание возможности.")
    if status not in STATUSES:
        raise LeadError("Неизвестный статус возможности.")
    info = dict(meta or {})
    if why:
        info["why"] = list(why)[:6]
    lead_id = database.add_lead(
        business_id, title, client_id=client_id, interest=interest,
        status=status, source=source or "other", channel=channel,
        value=value, currency=currency, meta=info,
        owner_fields=owner_fields,
        first_message_id=message_id, last_message_id=message_id)
    _link(business_id, lead_id, "created",
          source_kind=(source or "manual"), actor=actor, actor_id=actor_id,
          note=note or ("Возможность из «%s»" % (source or "—")))
    database.log_event(business_id, "client", "Новая возможность",
                       _short(title, 180), once_key="lead:%s" % lead_id)
    # Слышим возможность сразу же: и слова клиента, и описание, которое владелец
    # написал руками, — это одинаково его собственные слова, а не догадка.
    _observe(business_id, lead_id, " ".join(x for x in (title, interest) if x),
             message_id=message_id,
             source="owner" if (source or "") == "manual" else "client")
    _react(business_id)
    return lead_id


def _observe(business_id, lead_id, text, *, message_id=None, source="client"):
    """Пересчитать оценку. Оценка не обязана существовать, чтобы лид жил."""
    try:
        import qualify
        return qualify.observe(business_id, lead_id, text,
                               message_id=message_id, source=source)
    except Exception:
        log.exception("Оценка возможности не пересчиталась (biz %s)", business_id)
        return None


def _heard(business_id, client_id, *, message_id=None, text=""):
    """Клиент ответил — сказать об этом тому, кто планировал ему написать."""
    try:
        import followup
        followup.on_client_message(business_id, client_id,
                                   message_id=message_id, text=text)
    except Exception:
        log.exception("Запланированные касания не пересмотрены (biz %s)", business_id)


def _react(business_id):
    """Воронка изменилась — пусть об этом узнают остальные модули."""
    try:
        import signals
        signals.react(business_id, "client")
    except Exception:
        pass


def touch(business_id, lead_id, *, message_id=None):
    """Отметить, что по возможности была активность. Ничего не переписывает."""
    fields = {"last_activity_at": database.now()}
    if message_id:
        fields["last_message_id"] = int(message_id)
    database.update_lead(lead_id, business_id, **fields)


def may_create(business_id, channel):
    """
    Разрешено ли VELOR заводить возможности в этом канале.

    Права берём те же, что у продавца (ai_policy), а не заводим вторые: в
    настройках канала уже написано «заводить лида — можно/нельзя», и уровень
    «только отвечает» обещает владельцу, что ничего не создаётся. Обойти это
    записью в свою же воронку значило бы соврать в собственных настройках.

    Не прочиталось — считаем, что нельзя: несостоявшаяся запись видна в
    переписке, а самовольная — нет.
    """
    try:
        import sales
        pol = sales.policy(business_id, channel or "telegram")
        return sales.CREATE_LEAD in set(pol.get("allowed") or ())
    except Exception:
        log.exception("Права канала не прочитались (biz %s)", business_id)
        return False


def from_message(business_id, client, text, *, source, channel=None,
                 message_id=None, interest=None):
    """
    Сообщение клиента → возможность (или ничего).

    Сообщение при этом НИКУДА не переносится: оно остаётся в переписке, как и
    было. Лид только собирает над ней коммерческий смысл.

    Возвращает id лида или None. Ничего не бросает: разговор с клиентом не
    должен прерываться из-за воронки.
    """
    try:
        client_id = (client or {}).get("id")
        if not business_id or not client_id:
            return None

        # Человек заговорил сам — значит, всё, что мы собирались написать ему
        # по старому плану, больше не нужно. Это правило стоит ВЫШЕ проверки
        # прав: заводить возможность канал может быть не вправе, а отменить
        # лишнее сообщение можно всегда и в любом случае.
        _heard(business_id, client_id, message_id=message_id, text=text)

        if not may_create(business_id, channel or source):
            return None

        alive = open_for(business_id, client_id)
        found, why = intent(text)

        if alive:
            # Тот же разговор: третье сообщение подряд — не третий лид. Но
            # третье сообщение — это новое наблюдение: «сколько стоит» → «а
            # доставка?» → «нужно завтра» и есть растущее намерение.
            touch(business_id, alive["id"], message_id=message_id)
            if found:
                _enrich(business_id, alive, text, interest=interest)
            _observe(business_id, alive["id"], text, message_id=message_id)
            return alive["id"]

        if not found:
            return None

        # Копию переписки внутрь лида не кладём: она уже есть в messages, а
        # first_message_id указывает, с какой реплики всё началось. Две копии
        # одного разговора однажды разойдутся, и станет непонятно, какая правда.
        amount, currency = stated_value(text)
        return create(
            business_id,
            title=title_of(text, interest),
            interest=_short(interest or text, 500),
            client_id=client_id, source=source, channel=channel or source,
            value=amount, currency=currency, why=why, message_id=message_id,
            actor="velor", actor_id=None, note="Клиент написал сам",
            meta={"value_src": "client"} if amount else None)
    except Exception:
        log.exception("Не удалось завести возможность (biz %s)", business_id)
        return None


def _enrich(business_id, lead, text, interest=None):
    """
    Дополнить открытую возможность новым сообщением того же разговора.

    Правило одно: занимаем только пустое и только тем, что человек сказал сам.
    Поле, которое правил владелец, не трогаем никогда — ради этого owner_fields
    и существует.
    """
    owner = set(lead.get("owner_fields") or [])
    fields = {}
    if interest and "interest" not in owner and not (lead.get("interest") or "").strip():
        fields["interest"] = _short(interest, 500)
    if lead.get("value") in (None, 0) and "value" not in owner:
        amount, currency = stated_value(text)
        if amount:
            fields["value"] = amount
            fields["currency"] = currency
            fields["meta"] = dict(lead.get("meta") or {}, value_src="client")
    if fields:
        database.update_lead(lead["id"], business_id, **fields)
        _link(business_id, lead["id"], "edited", source_kind="ai", actor="velor",
              changes=fields, note="Клиент назвал это сам")


def apply_ai(business_id, lead_id, data, stated=()):
    """
    Что понял ИИ — в возможность. Возвращает, что записано и что отклонено.

    Две границы, обе жёсткие:

    1. ПОЛЕ ВЛАДЕЛЬЦА НЕПРИКОСНОВЕННО. Исправил сумму на 20 000 — модель не
       вернёт 15 000 ни при какой уверенности.
    2. ДОГАДКА НЕ СТАНОВИТСЯ ФАКТОМ. В колонку попадает только то, что человек
       сказал вслух (`stated`). Остальное ложится в meta.guess: видно, что
       VELOR так думает, но воронка на этом не считается.
    """
    lead = database.get_lead(int(lead_id), business_id)
    if not lead:
        raise LeadError("Возможность не найдена.")
    owner = set(lead.get("owner_fields") or [])
    stated = set(stated or ())
    fields, guessed, blocked = {}, {}, []

    for name in SOFT_FIELDS:
        if name not in (data or {}):
            continue
        val = data.get(name)
        if val in (None, "", []):
            continue
        if name in owner:
            blocked.append(name)
            continue
        if name in stated:
            fields[name] = _short(val, 500) if isinstance(val, str) else val
        else:
            guessed[name] = val

    if guessed:
        meta = dict(lead.get("meta") or {})
        guesses = dict(meta.get("guess") or {})
        guesses.update(guessed)
        meta["guess"] = guesses
        fields["meta"] = meta

    if fields:
        database.update_lead(lead_id, business_id, **fields)
        _link(business_id, lead_id, "edited", source_kind="ai", actor="velor",
              changes={k: v for k, v in fields.items() if k != "meta"},
              note="Разобрано из разговора")
    return {"written": sorted(k for k in fields if k != "meta"),
            "guessed": sorted(guessed), "blocked": sorted(blocked)}


def owner_update(business_id, lead_id, fields, *, actor="business", actor_id=None):
    """
    Правка владельца. Всё, чего он коснулся, автоматика больше не меняет.

    Это и есть «human-confirmed data wins» — тот же принцип, по которому
    подтверждённый факт памяти нельзя переписать разбором.
    """
    lead = database.get_lead(int(lead_id), business_id)
    if not lead:
        raise LeadError("Возможность не найдена.")

    clean = {}
    for name in ("title", "interest", "client_id", "source", "channel"):
        if name in fields and fields[name] not in (None, ""):
            clean[name] = fields[name]
    # Оценку тоже можно поправить рукой. Сказал владелец «этот лид горячий» —
    # значит горячий: он знает про клиента то, чего нет ни в одном сообщении.
    # Дальше правила эту оценку не пересчитывают (см. owner_fields).
    import qualify
    drop = set()
    for name, allowed in (("intent", qualify.INTENT_ORDER),
                          ("fit", qualify.FIT_ORDER),
                          ("priority", qualify.PRIORITY_ORDER)):
        if name not in fields:
            continue
        if fields[name]:
            if fields[name] not in allowed:
                raise LeadError("Неизвестное значение поля «%s»." % name)
            clean[name] = fields[name]
        elif name in set(lead.get("owner_fields") or []):
            # Пустое значение от человека здесь означает «считай сам»: владелец
            # возвращает поле правилам. Без этого решение, принятое однажды,
            # осталось бы навсегда — а обстоятельства меняются.
            clean[name] = None
            drop.add(name)
    if "value" in fields:
        raw = fields["value"]
        # Пустое значение от человека означает «сумму не знаем», а не «ноль».
        clean["value"] = None if raw in (None, "", "0", 0) else raw
        clean["currency"] = fields.get("currency") or lead.get("currency") or "RUB"
        if clean["value"] is None:
            clean["currency"] = None
    if "status" in fields and fields["status"]:
        return set_status(business_id, lead_id, fields["status"],
                          reason=fields.get("lost_reason"),
                          order_id=fields.get("order_id"),
                          actor=actor, actor_id=actor_id, extra=clean)
    if not clean:
        return lead

    clean["owner_fields"] = sorted(
        (set(lead.get("owner_fields") or []) | set(clean)) - drop)
    clean["last_activity_at"] = database.now()
    database.update_lead(lead_id, business_id, **clean)
    _link(business_id, lead_id, "edited", source_kind="manual", actor=actor,
          actor_id=actor_id, note="Поправил владелец",
          changes={k: v for k, v in clean.items()
                   if k not in ("owner_fields", "last_activity_at")})
    _react(business_id)
    return database.get_lead(int(lead_id), business_id)


def set_status(business_id, lead_id, status, *, reason=None, order_id=None,
               actor="business", actor_id=None, extra=None, source_kind="manual"):
    """
    Сменить состояние возможности.

    WON и LOST — не просто другое слово в колонке: у первого записывается дата
    конверсии и связь с заявкой, у второго — дата и причина. Без них закрытый
    лид не отвечает ни на один вопрос, ради которого его закрывали.
    """
    lead = database.get_lead(int(lead_id), business_id)
    if not lead:
        raise LeadError("Возможность не найдена.")
    if status not in STATUSES:
        raise LeadError("Неизвестное состояние возможности.")

    fields = dict(extra or {})
    fields["status"] = status
    fields["last_activity_at"] = database.now()

    if status == WON:
        fields["converted_at"] = database.now()
        fields["lost_at"] = None
        fields["lost_reason"] = None
        if order_id:
            fields["order_id"] = int(order_id)
    elif status == LOST:
        fields["lost_at"] = database.now()
        fields["converted_at"] = None
        # Причину не выдумываем: неизвестная причина — это «другое», а не
        # правдоподобная версия, которую потом посчитают статистикой.
        fields["lost_reason"] = reason if reason in LOST_REASONS else DEFAULT_LOST_REASON
    else:
        fields["converted_at"] = None
        fields["lost_at"] = None
        fields["lost_reason"] = None

    # Состояние, выставленное человеком, автоматика не пересматривает.
    if actor != "velor":
        fields["owner_fields"] = sorted(set(lead.get("owner_fields") or [])
                                        | {"status"} | set(extra or {}))

    database.update_lead(lead_id, business_id, **fields)
    _link(business_id, lead_id, "edited", source_kind=source_kind, actor=actor,
          actor_id=actor_id, changes={"status": status},
          note="Возможность: " + STATUS_RU.get(status, status))
    if status in (WON, LOST):
        database.log_event(
            business_id, "client",
            "Возможность закрыта: " + STATUS_RU.get(status, status),
            _short(lead.get("title"), 180), once_key="lead-close:%s" % lead_id)
        # Возможность закрыта — писать по ней больше нечего. Отменяем сразу, а
        # не при следующем обходе: очередь, обещающая сообщение купившему
        # клиенту, врёт владельцу ровно до того момента, как оно уйдёт.
        try:
            import followup
            followup.stop_all(business_id, lead_id, reason=status)
        except Exception:
            log.exception("Касания не отменены при закрытии (biz %s)", business_id)
    _react(business_id)
    return database.get_lead(int(lead_id), business_id)


def mark_won(business_id, lead_id, *, order_id=None, actor="business", actor_id=None):
    return set_status(business_id, lead_id, WON, order_id=order_id,
                      actor=actor, actor_id=actor_id)


def mark_lost(business_id, lead_id, *, reason=None, actor="business", actor_id=None):
    return set_status(business_id, lead_id, LOST, reason=reason,
                      actor=actor, actor_id=actor_id)


def on_order(business_id, client_id, order_id, *, amount=None, channel=None):
    """
    Появилась заявка — значит, возможность стала сделкой.

    Новой системы заказов не заводим: заявка остаётся там же, где была, а лид
    только ссылается на неё. Сумму берём из заявки — это факт, а не оценка.

    Возвращает id закрытого лида или None.
    """
    try:
        lead = open_for(business_id, client_id)
        if not lead:
            return None
        extra = {}
        if amount and not lead.get("value") and "value" not in set(lead.get("owner_fields") or []):
            extra["value"] = amount
            extra["currency"] = "RUB"
            extra["meta"] = dict(lead.get("meta") or {}, value_src="order")
        set_status(business_id, lead["id"], WON, order_id=order_id,
                   actor="velor", source_kind=channel or "auto", extra=extra)
        # Если этому человеку недавно уходило касание — цепочка «касание →
        # ответ → заявка» сохраняется. Не «касание принесло деньги»: сохраняем
        # порядок событий, а выводы будут, когда таких цепочек станет много.
        try:
            import followup
            followup.on_order(business_id, client_id, order_id)
        except Exception:
            log.exception("Заявка не связалась с касанием (biz %s)", business_id)
        return lead["id"]
    except Exception:
        log.exception("Не удалось связать заявку с возможностью (biz %s)", business_id)
        return None


def public(lead, gap=None):
    """
    Лид наружу: то же самое, но словами, которые можно показать человеку.

    Вместе с оценкой: насколько человек хочет купить, наше ли это, сколько
    денег и что делать сейчас. Часть оценки зависит от времени и потому
    считается здесь, а не берётся из колонки: «бизнес молчит третий час» через
    сутки так и осталось бы третьим часом.

    gap — уже посчитанный разрыв «клиент написал / бизнес ответил». В списке он
    приходит одним запросом на всю страницу; поодиночке это было бы полсотни
    запросов ради двух дат.
    """
    if not lead:
        return None
    d = dict(lead)
    d["status_ru"] = STATUS_RU.get(d.get("status"), d.get("status") or "")
    d["lost_reason_ru"] = LOST_REASONS.get(d.get("lost_reason") or "") or ""
    try:
        import entities
        d["source_ru"] = entities.SOURCE_RU.get((d.get("source") or "").lower(), "")
    except Exception:
        d["source_ru"] = ""
    d["open"] = d.get("status") in OPEN
    d["owner_fields"] = list(d.get("owner_fields") or [])
    guess = (d.get("meta") or {}).get("guess") or {}
    # Догадки отдаём отдельно и подписанными: интерфейс не должен показывать их
    # так же, как названные суммы.
    d["guess"] = guess
    d["why"] = list((d.get("meta") or {}).get("why") or [])
    try:
        import qualify
        d["q"] = qualify.view(lead, gap=gap)
    except Exception:
        log.exception("Оценка возможности не собралась (lead %s)", lead.get("id"))
        d["q"] = {}
    return d
