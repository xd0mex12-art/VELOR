# -*- coding: utf-8 -*-
"""
ВКонтакте как канал VELOR: сообщения сообщества через Callback API.

Чем этот канал отличается от остальных
──────────────────────────────────────
Instagram у нас устроен так: приложение Meta одно на всех, и верифицирует его
VELOR. Отсюда все его беды — пока приложение не прошло проверку, подключиться
не может никто.

ВКонтакте устроен наоборот, и это главное его достоинство. Своё сообщество
заводит сам владелец бизнеса, ключ доступа выдаёт себе сам и приносит его нам.
Никакой проверки VELOR не проходит, никакого приложения не заводит, и ничьё
юридическое лицо для этого не нужно. Ровно та же схема, что уже работает в
Telegram: клиент приносит токен своего бота.

Как устроено подключение
────────────────────────
Владелец делает у себя в сообществе три вещи:
  1) включает сообщения сообщества;
  2) выдаёт ключ доступа с правом «Сообщения сообщества»;
  3) заводит Callback API: вписывает наш адрес, наш секретный ключ и версию.

Порядок здесь важен и неочевиден. ВК проверяет адрес СРАЗУ, как только владелец
нажмёт «Подтвердить», — присылает на него запрос и ждёт в ответ строку
подтверждения. Значит к этому моменту мы уже должны знать, чьё это сообщество и
какую строку ему возвращать. Поэтому сначала ключ и строка вписываются в VELOR,
и только потом нажимается «Подтвердить» в ВК. В интерфейсе шаги идут именно в
таком порядке, и менять его нельзя.

Чего ВКонтакте НЕ умеет — и мы это не подделываем
─────────────────────────────────────────────────
1. Написать человеку первым нельзя, если он сам не писал сообществу или не
   разрешил сообщения. ВК отвечает на такую попытку ошибкой 901. Ограничение
   мягче, чем у Instagram (там ещё и сутки на ответ), но оно есть.
2. Историю переписки за прошлое ВК отдаёт только по тем разговорам, которые уже
   заведены в сообществе. Того, что было до подключения в личных сообщениях
   владельца, здесь нет и не будет.
3. Ни почты, ни телефона API не отдаёт. Телефон появится, только если человек
   напишет его сам, и придёт обычным путём — разбором сообщения.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import time

import requests

import database
from connectors.base import AuthError, ConnectorError, stamp

log = logging.getLogger("velor.vk")

PROVIDER = "vk"

# ── адреса и версия ────────────────────────────────────────────────────────
API = "https://api.vk.com/method/"
# Версию фиксируем. Плавающая версия означает, что ответ может измениться под
# нами в любой день, а узнаем мы об этом по сломанному каналу у клиента.
API_VERSION = "5.199"

PERMISSIONS_RU = ["читать сообщения сообщества", "отвечать в них"]

TIMEOUT = 20
TRIES = 3

# Коды ВК, означающие «ключ больше не годится». Их отличие от прочих в том, что
# ждать бесполезно: нужно, чтобы владелец выдал ключ заново.
AUTH_CODES = {5, 15, 27, 28}
# «Нельзя написать первым» — это не поломка ключа, а правило площадки.
NO_PERMISSION = {901, 902}
# Временное: слишком часто, флуд-контроль, внутренняя ошибка.
RETRY_CODES = {1, 6, 9, 10}


# ── секретный ключ Callback API ────────────────────────────────────────────

def _when(value) -> str:
    """
    Время сообщения ВК — это unix-секунды, а не строка с датой.

    Отдельная функция, потому что общий to_stamp разбирает чужие ISO-даты и на
    числе молча вернул бы None: сообщение получило бы время загрузки вместо
    своего, и порядок разговора в кабинете разошёлся бы с настоящим.
    """
    import datetime
    try:
        n = int(value)
    except (TypeError, ValueError):
        return stamp()
    if n <= 0:
        return stamp()
    return datetime.datetime.utcfromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")


def callback_secret(business_id: int) -> str:
    """
    Секретное слово, которое владелец вписывает в настройки Callback API.

    Выводим его из ключа подписи сервера и номера бизнеса, а не храним: тогда
    у каждого сообщества оно своё, между перезапусками не меняется и его негде
    потерять. Общее слово на всех было бы хуже — зная его от своего сообщества,
    владелец мог бы подделать событие чужого.
    """
    import auth
    return hmac.new(auth._secret(), ("velor-vk-callback|%s" % business_id).encode(),
                    hashlib.sha256).hexdigest()[:32]


def callback_url(base: str) -> str:
    return (base or "").rstrip("/") + "/api/vk/callback"


# ── разговор с ВК ──────────────────────────────────────────────────────────

def _vk_error(payload: dict) -> ConnectorError:
    """Превратить ответ ВК в понятную владельцу ошибку."""
    err = (payload or {}).get("error") or {}
    code = int(err.get("error_code") or 0)
    msg = str(err.get("error_msg") or "ВКонтакте не принял запрос.")
    if code in AUTH_CODES:
        return AuthError(
            "ВКонтакте не принял ключ доступа сообщества (%s). Выдайте ключ "
            "заново в настройках сообщества и вставьте его в VELOR." % code)
    if code in NO_PERMISSION:
        return ConnectorError(
            "ВКонтакте не разрешает написать этому человеку первым: он должен "
            "написать сообществу сам или разрешить сообщения.")
    return ConnectorError("ВКонтакте: " + msg[:200])


def _call(method: str, token: str, **params) -> dict:
    """
    Один вызов API. Возвращает содержимое response, а не весь конверт.

    ВК отвечает кодом 200 даже на ошибку — она лежит внутри тела. Проверять
    только HTTP-код здесь значит принимать «ключ не подошёл» за успех.
    """
    body = {k: v for k, v in params.items() if v is not None}
    body["access_token"] = token
    body["v"] = API_VERSION
    last = None
    for attempt in range(TRIES):
        try:
            r = requests.post(API + method, data=body, timeout=TIMEOUT)
        except requests.RequestException as e:
            last = ConnectorError("ВКонтакте не отвечает: " + str(e)[:120])
            time.sleep(0.6 * (attempt + 1))
            continue
        try:
            payload = r.json()
        except ValueError:
            last = ConnectorError("ВКонтакте ответил не по-человечески.")
            time.sleep(0.6 * (attempt + 1))
            continue
        if "error" in payload:
            err = _vk_error(payload)
            code = int((payload.get("error") or {}).get("error_code") or 0)
            # Ключ не подошёл — повторять бессмысленно, ответ не изменится.
            if isinstance(err, AuthError) or code not in RETRY_CODES:
                raise err
            last = err
            time.sleep(0.8 * (attempt + 1))
            continue
        return payload.get("response")
    raise last or ConnectorError("ВКонтакте не ответил.")


def group_info(token: str) -> dict:
    """
    Чьё это сообщество. Заодно — живая проверка ключа.

    Вызываем при подключении: принять ключ, не спросив у ВК, чей он, значит
    записать «подключено» и узнать правду в момент первого клиента.
    """
    res = _call("groups.getById", token)
    # ВК менял форму ответа: раньше список, теперь объект с полем groups.
    # Принимаем оба вида — падать из-за косметики чужого API мы не обязаны.
    if isinstance(res, dict):
        items = res.get("groups") or []
    else:
        items = res or []
    if not items:
        raise AuthError("Ключ не привязан ни к одному сообществу.")
    g = items[0] or {}
    return {"group_id": str(g.get("id") or ""),
            "name": (g.get("name") or "").strip(),
            "screen_name": (g.get("screen_name") or "").strip()}


def user_profile(token: str, user_id) -> dict:
    """Имя и аватар собеседника. Не вышло — работаем без имени, но не врём."""
    try:
        res = _call("users.get", token, user_ids=str(user_id), fields="photo_100,domain")
    except ConnectorError:
        return {}
    if not res:
        return {}
    u = res[0] or {}
    name = " ".join(x for x in [(u.get("first_name") or "").strip(),
                                (u.get("last_name") or "").strip()] if x)
    return {"name": name, "screen_name": (u.get("domain") or "").strip(),
            "avatar": u.get("photo_100") or ""}


def send_text(token: str, peer_id, text: str) -> dict:
    """
    Отправить сообщение. random_id обязателен — им ВК отсекает дубли.

    Берём случайное число, а не счётчик: счётчик пришлось бы где-то хранить и
    держать в согласии между несколькими процессами, а цена рассинхрона —
    отправленное дважды сообщение.
    """
    return {"message_id": _call("messages.send", token,
                                peer_id=str(peer_id), message=text,
                                random_id=random.getrandbits(31))}


def conversations(token: str, limit: int = 20) -> list[dict]:
    """Последние разговоры сообщества — для первой загрузки после подключения."""
    res = _call("messages.getConversations", token, count=int(limit)) or {}
    return res.get("items") or []


def conversation_messages(token: str, peer_id, limit: int = 20) -> list[dict]:
    """Последние сообщения одного разговора, от старых к новым."""
    res = _call("messages.getHistory", token, peer_id=str(peer_id),
                count=int(limit), rev=0) or {}
    return list(reversed(res.get("items") or []))


# ── ключ доступа ───────────────────────────────────────────────────────────

def token_of(business_id: int) -> str | None:
    """Расшифрованный ключ сообщества. Наружу это значение не отдаётся никогда."""
    import secretbox
    row = database.get_connection(business_id, PROVIDER, with_secrets=True)
    if not row:
        return None
    try:
        creds = json.loads(secretbox.open_(row.get("credentials") or "") or "{}")
    except ValueError:
        return None
    return (creds.get("token") or "").strip() or None


def confirmation_of(business_id: int) -> str:
    """Строка, которую ждёт ВК при проверке адреса. Секретом не является."""
    row = database.get_connection(business_id, PROVIDER) or {}
    return str((row.get("meta") or {}).get("confirmation") or "")


def save_access(business_id: int, token: str, confirmation: str, info: dict) -> None:
    """Положить ключ зашифрованным — тем же способом, что и ключи всех сервисов."""
    import secretbox
    blob = secretbox.seal(json.dumps({"token": token}, ensure_ascii=False))
    meta = {"group_id": info.get("group_id"), "name": info.get("name"),
            "screen_name": info.get("screen_name"),
            # Строка подтверждения лежит открыто намеренно: её показывает сам
            # ВК любому администратору сообщества, и прятать её незачем — а
            # читать нам её нужно в момент звонка, до всякой расшифровки.
            "confirmation": confirmation}
    config = {"Сообщество": info.get("name") or "",
              "Адрес": ("vk.com/" + info["screen_name"]) if info.get("screen_name") else ""}
    database.save_connection(business_id, PROVIDER, blob, meta,
                             permissions=PERMISSIONS_RU, config=config)


# ── приём события ──────────────────────────────────────────────────────────

def handle_event(payload: dict) -> dict:
    """
    Разобрать звонок Callback API.

    Возвращает, что именно надо ответить ВК: строку подтверждения или «ok».
    Отвечать надо всегда и быстро — не дождавшись, ВК повторит доставку, а
    после нескольких неудач вовсе отключит адрес у сообщества.
    """
    payload = payload if isinstance(payload, dict) else {}
    group_id = str(payload.get("group_id") or "")
    biz = database.find_business_by_vk_group(group_id)
    if not biz:
        # Сообщество нам незнакомо. Говорим об этом вслух: либо канал отключили
        # раньше, либо звонок не наш — оба случая надо видеть, а не проглатывать.
        log.warning("ВК: звонок от незнакомого сообщества %s", group_id)
        return {"reply": "ok"}
    bid = int(biz["id"])

    # Секретное слово проверяем ДО всего остального. Адрес публичный, и без
    # проверки кто угодно мог бы прислать «сообщение от клиента» и заставить
    # VELOR ответить постороннему человеку от имени бизнеса.
    # Сравниваем БАЙТЫ, а не строки: compare_digest на строках отказывается
    # работать с чем угодно, кроме ASCII, и бросает TypeError. А это слово
    # присылает нам посторонний, и в нём может быть любой символ — проверка
    # секрета обязана отвечать «не то слово», а не падать.
    want = callback_secret(bid).encode()
    got = str(payload.get("secret") or "").encode()
    if not hmac.compare_digest(want, got):
        log.warning("ВК: событие сообщества %s с чужим секретным словом", group_id)
        return {"reply": "ok"}

    kind = str(payload.get("type") or "")
    if kind == "confirmation":
        return {"reply": confirmation_of(bid) or "ok"}

    # Повторную доставку отсекаем по event_id — его ВК присылает у каждого
    # события и держит одинаковым между повторами.
    ev = str(payload.get("event_id") or "")
    if ev and database.vk_seen_event(bid, ev):
        return {"reply": "ok"}

    try:
        if kind == "message_new":
            _incoming(bid, payload.get("object") or {})
        elif kind == "message_reply":
            _echo(bid, payload.get("object") or {})
    except Exception:
        # Наружу всё равно «ok»: повторный звонок наших бед не исправит, а
        # молчание заставит ВК отключить адрес у сообщества.
        log.exception("ВК: разбор события не удался (бизнес %s)", bid)
    return {"reply": "ok"}


def _message_of(obj: dict) -> dict:
    """У message_new сообщение лежит внутри, у message_reply — само и есть."""
    inner = obj.get("message")
    return inner if isinstance(inner, dict) else obj


def _attachment_note(msg: dict) -> str:
    """Человеческая пометка о вложении: что именно прислали."""
    RU = {"photo": "фото", "video": "видео", "audio_message": "голосовое сообщение",
          "doc": "файл", "sticker": "стикер", "wall": "запись", "link": "ссылку",
          "audio": "аудио", "market": "товар", "graffiti": "граффити"}
    kinds = [RU.get((a or {}).get("type"), "вложение")
             for a in (msg.get("attachments") or []) if isinstance(a, dict)]
    if not kinds:
        return ""
    return "[клиент прислал " + ", ".join(dict.fromkeys(kinds)) + "]"


def _echo(bid: int, obj: dict) -> None:
    """
    Сообщество ответило само — из ВК, руками владельца.

    Либо это отправили мы (тогда id уже помечен при отправке и сюда не дойдём),
    либо владелец написал из самого ВК. Второе — живой человек, взявший
    разговор на себя, и модели тут больше делать нечего.
    """
    msg = _message_of(obj)
    peer = str(msg.get("peer_id") or "")
    thread = database.vk_thread(bid, peer)
    if not thread:
        return                       # эхо в разговор, которого мы не знаем
    text = (msg.get("text") or "").strip() or _attachment_note(msg) or "[ответ из ВК]"
    database.save_message(bid, thread["client_id"], "assistant", text, channel=PROVIDER)
    database.vk_thread_mark(bid, peer, last_out_at=stamp())
    if not database.vk_thread_paused(bid, peer):
        database.vk_thread_pause(bid, peer, True, by="human_in_app")
        database.log_event(
            bid, "reply", "ВКонтакте: отвечает человек",
            "Владелец ответил из самого ВК — VELOR замолчал в этом разговоре. "
            "Вернуть его можно в разделе «ВКонтакте».",
            once_key="vk-handoff:%s" % peer)


def _incoming(bid: int, obj: dict) -> None:
    """Обычное входящее: весь путь от сообщения до заявки."""
    import trial

    msg = _message_of(obj)
    peer = str(msg.get("peer_id") or msg.get("from_id") or "")
    if not peer:
        return
    # Беседы (peer_id от 2 000 000 000) пропускаем: там пишут друг другу
    # несколько человек, и отвечать в такую от имени бизнеса — влезать в чужой
    # разговор. Личные обращения — то, ради чего канал и нужен.
    if peer.lstrip("-").isdigit() and int(peer) >= 2000000000:
        return

    business = database.get_business(bid) or {}
    token = token_of(bid)
    known = database.vk_thread(bid, peer) or {}
    profile = {}
    # Профиль спрашиваем один раз — при первом сообщении. Дёргать ВК на каждую
    # реплику активного разговора значит тратить лимит на то, что мы уже знаем.
    if token and not known.get("name"):
        profile = user_profile(token, peer) or {}

    name = (profile.get("name") or known.get("name") or "").strip()
    client_id, created = database.upsert_external_client(
        bid, peer, PROVIDER, name=name or None,
        notes="Пришёл из сообщения сообщества ВКонтакте")
    when = _when(msg.get("date"))
    database.vk_thread_upsert(bid, peer, client_id,
                              screen_name=(profile.get("screen_name") or None),
                              name=name or None,
                              avatar=(profile.get("avatar") or None),
                              last_in_at=when)

    text = (msg.get("text") or "").strip()
    body = text or _attachment_note(msg) or "[сообщение без текста]"
    mid = database.save_message(bid, client_id, "user", body, channel=PROVIDER)

    if created:
        database.log_event(bid, "client", "Клиент из ВКонтакте",
                           name or "новый собеседник")

    # Триал закончился — принимаем и молчим, ровно как в Telegram и Instagram.
    if trial.access(business)["read_only"]:
        return

    # Возможность замечаем ЗДЕСЬ, а не там, где отвечает продавец. Разница видна
    # в худший момент: разговор ведёт человек или кончился лимит тарифа —
    # продавец молчит, а клиент всё равно спросил цену. Тогда воронка и нужна.
    if text:
        try:
            import leads
            leads.from_message(bid, {"id": client_id}, text,
                               source=PROVIDER, channel=PROVIDER, message_id=mid)
        except Exception:
            log.exception("Возможность из ВК не завелась (бизнес %s)", bid)

    if database.vk_thread_paused(bid, peer):
        database.log_event(bid, "reply", "ВКонтакте: новое сообщение", body[:200],
                           once_key="vk-manual:%s" % peer)
        return
    if not text:
        # Картинку VELOR не видит. Молча выдумывать ответ на фото хуже, чем
        # позвать владельца.
        database.log_event(bid, "reply", "ВКонтакте: вложение без текста",
                           _attachment_note(msg) or "Клиент прислал вложение — "
                           "ответьте сами.", once_key="vk-attach:%s" % peer)
        return

    import sales
    client = database.get_client(client_id, bid) or {"id": client_id, "name": name}
    try:
        decision = sales.answer(bid, client, text, channel=PROVIDER)
    except Exception:
        # Откатываться на прежнее ядро здесь нельзя: оно отвечает без проверки
        # на выдумку, и сбой в защите обернулся бы выдуманной ценой. Молчим и
        # зовём человека — это худший ответ клиенту и единственно честный.
        log.exception("AI-продавец не отработал (бизнес %s)", bid)
        decision = {"reply": sales.HOLD_REPLY, "handoff": True, "draft": None,
                    "reason": "Внутренняя ошибка ответа — отвечает человек."}

    if decision.get("handoff"):
        _handoff(bid, peer, decision)

    reply = decision.get("reply")
    if not reply or not token:
        return
    try:
        send_text(token, peer, reply)
    except AuthError as e:
        database.mark_connection_synced(bid, PROVIDER, error=str(e), needs_auth=True)
        return
    except ConnectorError as e:
        database.mark_connection_synced(bid, PROVIDER, error=str(e))
        return
    database.save_message(bid, client_id, "assistant", reply, channel=PROVIDER)
    database.vk_thread_mark(bid, peer, last_out_at=stamp())


def _handoff(bid: int, peer: str, decision: dict) -> None:
    """
    Передать разговор человеку и объяснить, почему.

    Владельцу показываем и причину, и черновик, который VELOR хотел отправить.
    Причина отвечает на «что случилось», черновик — на куда более полезное
    «чего не хватает в памяти бизнеса»: чаще всего там ровно та цена или то
    условие, которые никто не внёс.
    """
    reason = (decision.get("reason") or "VELOR не уверен в ответе.")[:400]
    draft = (decision.get("draft") or "")[:2000] or None
    database.vk_thread_pause(bid, peer, True, by="ai_unsure")
    database.vk_thread_reason(bid, peer, why=reason, draft=draft)
    database.log_event(bid, "reply", "ВКонтакте: нужен человек", reason,
                       level="important", once_key="vk-unsure:%s" % peer)


# ── ответ человека из кабинета ─────────────────────────────────────────────

def reply_as_human(business_id: int, peer_id: str, text: str) -> dict:
    """Ответ владельца из кабинета — обычным сообщением сообщества."""
    text = (text or "").strip()
    if not text:
        raise ConnectorError("Пустое сообщение отправить нельзя.")
    thread = database.vk_thread(business_id, peer_id)
    if not thread:
        raise ConnectorError("Такого разговора нет.")
    token = token_of(business_id)
    if not token:
        raise AuthError("ВКонтакте не подключён — вставьте ключ сообщества заново.")
    send_text(token, peer_id, text)
    database.save_message(business_id, thread["client_id"], "assistant", text,
                          channel=PROVIDER)
    database.vk_thread_mark(business_id, peer_id, last_out_at=stamp())
    return {"ok": True}


# ── первая загрузка ────────────────────────────────────────────────────────

def pull_recent(business_id: int) -> dict:
    """
    Забрать то, что уже есть в сообщениях сообщества.

    Нужно, чтобы после подключения кабинет не выглядел пустым, пока никто не
    написал. Глубже двадцати сообщений на разговор не идём: обещать «всю
    историю» мы не можем, а показать половину и назвать её всей — можем зря.
    """
    token = token_of(business_id)
    if not token:
        return {"ok": False, "added": 0, "error": "ВКонтакте не подключён."}
    try:
        convs = conversations(token)
    except AuthError as e:
        database.mark_connection_synced(business_id, PROVIDER, error=str(e), needs_auth=True)
        return {"ok": False, "added": 0, "error": str(e)}
    except ConnectorError as e:
        database.mark_connection_synced(business_id, PROVIDER, error=str(e))
        return {"ok": False, "added": 0, "error": str(e)}

    added = 0
    for item in convs:
        conv = (item or {}).get("conversation") or {}
        peer = str(((conv.get("peer") or {}).get("id")) or "")
        if not peer or (peer.lstrip("-").isdigit() and int(peer) >= 2000000000):
            continue
        try:
            msgs = conversation_messages(token, peer)
        except ConnectorError:
            continue
        profile = user_profile(token, peer) or {}
        name = (profile.get("name") or "").strip()
        client_id, _ = database.upsert_external_client(
            business_id, peer, PROVIDER, name=name or None,
            notes="Пришёл из сообщения сообщества ВКонтакте")
        for m in msgs:
            ev = "history:%s" % (m or {}).get("id")
            if database.vk_seen_event(business_id, ev):
                continue
            out = bool(m.get("out")) or str(m.get("from_id") or "").startswith("-")
            body = (m.get("text") or "").strip() or _attachment_note(m)
            if not body:
                continue
            when = _when(m.get("date"))
            database.save_message(business_id, client_id,
                                  "assistant" if out else "user", body,
                                  channel=PROVIDER, created_at=when)
            database.vk_thread_upsert(
                business_id, peer, client_id,
                screen_name=(profile.get("screen_name") or None),
                name=name or None, avatar=(profile.get("avatar") or None),
                **({"last_out_at": when} if out else {"last_in_at": when}))
            added += 1
    database.mark_connection_synced(business_id, PROVIDER, added=added)
    return {"ok": True, "added": added}
