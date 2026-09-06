# -*- coding: utf-8 -*-
"""
Instagram как канал VELOR: настоящий Instagram API с Instagram Login.

Что здесь есть и почему именно так
──────────────────────────────────
Meta даёт две сборки Instagram Platform. Мы берём «Instagram API with Instagram
Login»: владелец входит своим аккаунтом Instagram, страница в Facebook не нужна,
все запросы идут на graph.instagram.com. Вторая сборка (через Facebook Login)
требует связки «аккаунт ↔ страница Facebook» и даёт лишнее — теги товаров и
поиск по хэштегам, которых нам не нужно.

Мы просим ровно два права: instagram_business_basic (узнать, чей это аккаунт) и
instagram_business_manage_messages (читать директ и отвечать в него). Права на
комментарии и публикацию не запрашиваем — VELOR ими не пользуется, а лишнее
разрешение это лишний риск для владельца.

Чего Instagram НЕ умеет — и мы это не подделываем
─────────────────────────────────────────────────
1. Переписку начинает только клиент. Написать первым нельзя ничем и никак.
2. Ответить можно в течение 24 часов с последнего сообщения клиента. Человек
   (не автоответчик) может ответить до 7 суток, пометив сообщение тегом
   HUMAN_AGENT; ставить этот тег на ответ модели прямо запрещено правилами Meta.
3. История: по каждой переписке отдаются только 20 последних сообщений. Более
   старые Meta не возвращает — они существуют только там, где мы их сами
   сохранили с момента подключения.
4. Ни почты, ни телефона клиента API не отдаёт. Профиль — это имя, @-логин,
   аватар и признаки подписки. Телефон появится, только если клиент напишет его
   сам, и придёт он обычным путём — через разбор сообщения.
5. Групповых переписок нет.
6. Переписки в папке «Запросы», которые молчат 30 дней, из API пропадают.
7. Handover Protocol (передача треда между приложениями через Meta) в сборке с
   Instagram Login не документирован, поэтому мы его не трогаем. Передача
   разговора человеку сделана честно и внутри VELOR: владелец берёт переписку
   на себя, модель замолкает. Плюс мы слышим эхо — если владелец ответил из
   самого приложения Instagram, VELOR это видит и молчит сам.

Токен доступа
─────────────
Короткий (1 час) меняем на длинный (60 суток) сразу при подключении и обновляем
заранее. Хранится он зашифрованным в таблице подключений — так же, как ключи
всех остальных сервисов, и наружу не выходит никогда.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time

import requests

import database
from connectors.base import AuthError, ConnectorError, stamp, to_stamp

log = logging.getLogger("velor.instagram")

PROVIDER = "instagram"

# ── адреса Meta ────────────────────────────────────────────────────────────
API_VERSION = "v23.0"
GRAPH = "https://graph.instagram.com"
AUTH_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"

# Права, которые просим у владельца. Ровно те, которыми пользуемся.
SCOPES = ["instagram_business_basic", "instagram_business_manage_messages"]

PERMISSIONS_RU = ["узнать, чей это аккаунт", "читать директ",
                  "отвечать в директ от имени бизнеса"]

# Подписки на события. messages обязателен — без него директ не придёт.
# message_echoes нужен, чтобы услышать ответ владельца из самого приложения.
WEBHOOK_FIELDS = ["messages", "message_echoes", "messaging_seen"]

# Ограничения Meta, зашитые в правила ответа.
REPLY_WINDOW_HOURS = 24        # столько есть у автоответа
HUMAN_WINDOW_DAYS = 7          # столько есть у человека с тегом HUMAN_AGENT
HISTORY_LIMIT = 20             # больше по одной переписке Meta не отдаёт

TIMEOUT = 20
TRIES = 3

# Коды ошибок Meta, означающие «доступа больше нет». Их лечит переподключение,
# а не повтор запроса, поэтому они отделены от временных сбоев.
AUTH_CODES = {102, 190, 200, 10, 2500}
AUTH_SUBCODES = {458, 459, 460, 463, 464, 467, 492}
# Отдельно: попытка ответить позже разрешённого окна.
OUT_OF_WINDOW = {2534022, 2018278, 10900}


# ── настройка приложения VELOR в Meta ──────────────────────────────────────

def app_id() -> str:
    return (os.getenv("INSTAGRAM_APP_ID") or "").strip()


def app_secret() -> str:
    return (os.getenv("INSTAGRAM_APP_SECRET") or "").strip()


def configured() -> bool:
    """
    Готово ли приложение Meta.

    Это настройка не бизнеса, а самого VELOR: одно приложение Meta на всех, и
    ключи к нему живут в переменных окружения сервера. Пока их нет, кнопка
    «Подключить» обязана честно не работать — фальшивый экран входа хуже, чем
    прямая надпись «ещё не настроено».
    """
    return bool(app_id() and app_secret())


def public_base() -> str:
    return (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")


def redirect_uri() -> str:
    """
    Адрес возврата после входа. Должен совпадать с тем, что вписан в Meta.

    Meta сверяет его посимвольно, поэтому даём возможность задать его явно:
    домен кабинета и домен, на который смотрит Meta, не всегда один и тот же.
    """
    explicit = (os.getenv("INSTAGRAM_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    base = public_base()
    return base + "/api/instagram/callback" if base else ""


def verify_token() -> str:
    """
    Слово-пароль для проверки вебхука. Его владелец вписывает в кабинет Meta.

    Задаётся переменной окружения. Если её нет — выводим из ключа подписи
    сервера, чтобы значение было стабильным между перезапусками и его можно
    было показать в интерфейсе.
    """
    explicit = (os.getenv("INSTAGRAM_VERIFY_TOKEN") or "").strip()
    if explicit:
        return explicit
    import auth
    return hashlib.sha256(b"velor-instagram-verify|" + auth._secret()).hexdigest()[:32]


def setup_state() -> dict:
    """Чего не хватает, чтобы подключение вообще стало возможным."""
    missing = []
    if not app_id():
        missing.append("INSTAGRAM_APP_ID")
    if not app_secret():
        missing.append("INSTAGRAM_APP_SECRET")
    if not redirect_uri():
        missing.append("PUBLIC_URL (или INSTAGRAM_REDIRECT_URI)")
    base = public_base()
    return {"ready": not missing, "missing": missing,
            "redirect_uri": redirect_uri(),
            "webhook_url": (base + "/api/instagram/webhook") if base else "",
            # Эти два Meta спрашивает обязательным полем, и оба должны отвечать
            # ДО подачи заявки на проверку. Показываем их рядом с остальными,
            # чтобы не искать по документации, что ещё вписать.
            "deauthorize_url": (base + "/api/instagram/deauthorize") if base else "",
            "deletion_url": (base + "/api/instagram/data-deletion") if base else "",
            "verify_token": verify_token() if configured() else "",
            "scopes": list(SCOPES)}


# ── HTTP до Meta ───────────────────────────────────────────────────────────

def _meta_error(payload: dict, status: int) -> ConnectorError:
    """Превратить ответ Meta в понятную владельцу ошибку нужного вида."""
    err = (payload or {}).get("error") or {}
    code = err.get("code")
    sub = err.get("error_subcode")
    msg = (err.get("error_user_msg") or err.get("message") or "").strip()
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    try:
        sub = int(sub)
    except (TypeError, ValueError):
        sub = None

    if sub in OUT_OF_WINDOW:
        return ConnectorError(
            "Instagram не принял сообщение: прошло больше времени, чем он разрешает "
            "на ответ. Клиент должен написать снова.")
    if status in (401, 403) or code in AUTH_CODES or sub in AUTH_SUBCODES:
        return AuthError(
            "Instagram больше не принимает доступ: срок истёк или его отозвали. "
            "Подключите аккаунт заново." + (f" Ответ Meta: {msg}" if msg else ""))
    if code in (4, 17, 32, 613) or status == 429:
        return ConnectorError("Instagram просит подождать — слишком много запросов. "
                              "Повторим позже.")
    return ConnectorError("Instagram отклонил запрос." + (f" {msg}" if msg else ""))


def _call(method: str, url: str, *, params=None, data=None, tries=TRIES) -> dict:
    """
    Запрос к Meta с одинаковой обработкой ошибок.

    Отдельно от общего connectors.base.request, потому что Meta почти никогда
    не отвечает кодом 401: просроченный токен приходит как 400 с кодом 190
    внутри тела. Без разбора тела «переподключите аккаунт» выглядело бы как
    «сервис сломался», и владелец ждал бы у моря погоды.
    """
    last = None
    for attempt in range(max(1, tries)):
        try:
            r = requests.request(method, url, params=params, data=data, timeout=TIMEOUT)
        except requests.RequestException:
            last = ConnectorError("Не удалось связаться с Instagram. Проверьте связь.")
        else:
            try:
                payload = r.json() if r.text else {}
            except ValueError:
                payload = {}
            if r.status_code < 400:
                return payload if isinstance(payload, dict) else {"data": payload}
            err = _meta_error(payload, r.status_code)
            if isinstance(err, AuthError) or r.status_code < 500 and r.status_code != 429:
                raise err
            last = err
        if attempt + 1 < tries:
            time.sleep(1.5 * (2 ** attempt))
    raise last or ConnectorError("Instagram не ответил.")


def _graph(path: str) -> str:
    return f"{GRAPH}/{API_VERSION}/{path.lstrip('/')}"


# ── вход владельца (OAuth) ─────────────────────────────────────────────────

MAC_LEN = 16       # длина подписи в байтах — она же граница при разборе


def _sign_state(business_id: int, issued: int) -> str:
    import auth
    raw = f"{int(business_id)}.{int(issued)}".encode()
    mac = hmac.new(auth._secret(), b"velor-ig-state|" + raw, hashlib.sha256).digest()[:MAC_LEN]
    return base64.urlsafe_b64encode(raw + mac).decode().rstrip("=")


STATE_TTL = 900        # 15 минут: столько живёт начатый вход


def read_state(state: str) -> int | None:
    """
    Достать бизнес из подписанного state.

    state — единственное, что связывает возврат от Meta с конкретной компанией:
    браузер приходит на наш адрес без токена кабинета. Поэтому он подписан
    ключом сервера и живёт четверть часа: чужую компанию так не подключить и
    старую ссылку повторно не разыграть.
    """
    import auth
    if not state:
        return None
    try:
        pad = "=" * (-len(state) % 4)
        blob = base64.urlsafe_b64decode(state + pad)
        # Границу берём по длине подписи, а не по разделителю: подпись — это
        # случайные байты, и точка внутри неё разрезала бы state не в том месте.
        # Один раз в шестнадцать входов такой state отклонялся бы без причины.
        raw, mac = blob[:-MAC_LEN], blob[-MAC_LEN:]
        good = hmac.new(auth._secret(), b"velor-ig-state|" + raw, hashlib.sha256).digest()[:MAC_LEN]
        if not hmac.compare_digest(mac, good):
            return None
        bid_s, issued_s = raw.decode().split(".")
        if time.time() - int(issued_s) > STATE_TTL:
            return None
        return int(bid_s)
    except Exception:
        return None


def authorize_url(business_id: int) -> str:
    """Ссылка на настоящий экран входа Instagram. Своего экрана входа у нас нет."""
    st = setup_state()
    if not st["ready"]:
        raise ConnectorError(
            "Приложение Meta для VELOR ещё не настроено: не хватает "
            + ", ".join(st["missing"]) + ". Подключение Instagram невозможно, "
            "пока это не задано на сервере.")
    from urllib.parse import urlencode
    return AUTH_URL + "?" + urlencode({
        "client_id": app_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": _sign_state(business_id, int(time.time())),
    })


def exchange_code(code: str) -> dict:
    """
    Код возврата → короткий токен → длинный токен (60 суток).

    Меняем сразу: короткий живёт час, и подключение, сделанное вечером,
    перестало бы работать к ночи.
    """
    short = _call("POST", TOKEN_URL, data={
        "client_id": app_id(),
        "client_secret": app_secret(),
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
        "code": (code or "").strip(),
    }, tries=1)
    token = short.get("access_token")
    if not token:
        raise ConnectorError("Instagram не выдал доступ. Попробуйте войти заново.")
    long_ = _call("GET", _graph("access_token"), params={
        "grant_type": "ig_exchange_token",
        "client_secret": app_secret(),
        "access_token": token,
    }, tries=1)
    return {
        "access_token": long_.get("access_token") or token,
        "expires_in": int(long_.get("expires_in") or 3600),
        "user_id": str(short.get("user_id") or "") or None,
    }


def refresh_token(token: str) -> dict:
    """Продлить длинный токен ещё на 60 суток. Меньше суток от роду — Meta откажет."""
    d = _call("GET", _graph("refresh_access_token"), params={
        "grant_type": "ig_refresh_token", "access_token": token})
    return {"access_token": d.get("access_token") or token,
            "expires_in": int(d.get("expires_in") or 0)}


def me(token: str) -> dict:
    """Чей это аккаунт: id, @-логин, тип профиля."""
    return _call("GET", _graph("me"), params={
        "fields": "user_id,username,name,account_type,profile_picture_url",
        "access_token": token})


def subscribe(token: str) -> dict:
    """
    Подписать приложение на события аккаунта.

    Без подписки директ до нас не доедет — Meta просто не станет звонить.
    Если какое-то поле подписки для аккаунта недоступно, отступаем к
    обязательному messages и записываем, на что подписались на самом деле:
    делать вид, что слышим эхо, когда мы его не слышим, нельзя.
    """
    last = ConnectorError("Instagram не принял подписку на события.")
    for fields in (WEBHOOK_FIELDS, ["messages"]):
        try:
            _call("POST", _graph("me/subscribed_apps"),
                  params={"subscribed_fields": ",".join(fields), "access_token": token},
                  tries=1)
            return {"ok": True, "fields": list(fields)}
        except AuthError:
            raise
        except ConnectorError as e:
            last = e
    raise last


def unsubscribe(token: str) -> None:
    """Отписаться при отключении: чужие события нам больше не нужны."""
    _call("DELETE", _graph("me/subscribed_apps"), params={"access_token": token}, tries=1)


# ── работа с перепиской ────────────────────────────────────────────────────

def user_profile(token: str, igsid: str) -> dict:
    """
    Профиль написавшего. Ни почты, ни телефона здесь нет и быть не может.

    Это единственный способ узнать, как зовут клиента: в самом вебхуке только
    его числовой id.
    """
    return _call("GET", _graph(str(igsid)), params={
        "fields": "name,username,profile_pic,is_verified_user,"
                  "is_user_follow_business,is_business_follow_user",
        "access_token": token})


def send_text(token: str, igsid: str, text: str, human: bool = False) -> dict:
    """
    Отправить сообщение клиенту.

    human=True ставит тег HUMAN_AGENT — он расширяет окно ответа с 24 часов до
    7 суток. Ставить его разрешено ТОЛЬКО на сообщение, написанное человеком:
    для автоответа это нарушение правил Meta, поэтому решение принимает не этот
    модуль, а тот, кто знает, кто нажал «Отправить».
    """
    body = {
        "recipient": json.dumps({"id": str(igsid)}),
        "message": json.dumps({"text": (text or "")[:990]}, ensure_ascii=False),
        "access_token": token,
    }
    if human:
        body["messaging_type"] = "MESSAGE_TAG"
        body["tag"] = "HUMAN_AGENT"
    return _call("POST", _graph("me/messages"), data=body, tries=1)


def conversations(token: str, limit: int = 20) -> list[dict]:
    """
    Список переписок аккаунта.

    Нужен один раз при подключении: без него кабинет был бы пуст до первого
    нового сообщения, хотя переписки уже идут.
    """
    d = _call("GET", _graph("me/conversations"),
              params={"platform": "instagram", "fields": "id,updated_time",
                      "limit": max(1, min(int(limit or 20), 50)), "access_token": token})
    return [x for x in (d.get("data") or []) if isinstance(x, dict)]


def conversation_messages(token: str, conversation_id: str) -> list[dict]:
    """
    Сообщения одной переписки — не больше 20 последних.

    Ограничение не наше: Meta отвечает ошибкой «сообщение удалено» на всё, что
    старше двадцатого. Поэтому глубже мы не заглядываем и не обещаем этого.
    """
    d = _call("GET", _graph(str(conversation_id)), params={
        "fields": f"messages.limit({HISTORY_LIMIT}){{id,created_time,from,to,message}}",
        "access_token": token})
    msgs = ((d.get("messages") or {}).get("data")) or []
    return [m for m in msgs if isinstance(m, dict)]


# ── вебхук ─────────────────────────────────────────────────────────────────

def verify_signature(raw_body: bytes, header: str) -> bool:
    """
    Подпись Meta под телом запроса: X-Hub-Signature-256: sha256=…

    Проверять обязательно. Адрес вебхука публичный, и без подписи кто угодно
    мог бы прислать «сообщение от клиента» и заставить VELOR ответить чужому
    человеку от имени бизнеса.
    """
    secret = app_secret()
    if not secret or not header:
        return False
    sent = header.strip()
    if sent.startswith("sha256="):
        sent = sent[7:]
    good = hmac.new(secret.encode(), raw_body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, good)


# ── обратные звонки Meta: отключение и удаление данных ─────────────────────
#
# Meta требует у приложения с Instagram Login два адреса, и оба обязаны
# работать до подачи заявки на проверку. Но дело не в требовании. Без первого
# из них владелец убирает VELOR из своего инстаграма — а кабинет продолжает
# писать «Подключено» над мёртвым токеном. Это молчаливый сбой: самый дорогой
# вид, потому что о нём узнают последними и по чужой жалобе.
#
# Подпись здесь устроена иначе, чем у вебхука: не заголовок над телом запроса,
# а поле формы вида «подпись.тело», и подписана СЫРАЯ строка тела до разбора.
# Разобрать, а потом подписать разобранное — обычный способ проглядеть подделку.

def _b64url(part: str) -> bytes:
    """base64url от Meta приходит без хвостовых «=» — дополняем сами."""
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def read_signed_request(raw: str) -> dict | None:
    """
    Разобрать signed_request от Meta. Подпись не сошлась — None и никаких действий.

    На эти адреса может постучаться кто угодно: они публичные по определению.
    А по такому звонку мы отключаем бизнесу канал продаж, поэтому «похоже на
    правду» здесь недостаточно — только совпавшая подпись нашим же секретом.
    """
    secret = app_secret()
    if not secret or not raw or "." not in str(raw):
        return None
    sig_part, body_part = str(raw).split(".", 1)
    try:
        sent = _b64url(sig_part)
        payload = json.loads(_b64url(body_part).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    # Алгоритм Meta присылает в теле. Принимаем только тот, который проверяем
    # сами: чужое имя алгоритма означает чужой формат подписи.
    if str(payload.get("algorithm") or "").upper().replace("-", "") != "HMACSHA256":
        return None
    good = hmac.new(secret.encode(), body_part.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sent, good):
        return None
    return payload


def deletion_code(ig_user_id: str) -> str:
    """
    Код подтверждения для запроса на удаление.

    Выводим из id аккаунта нашим же секретом, а не берём случайный: тогда его
    не надо нигде хранить, повторный запрос того же человека даёт тот же код,
    а сам id в адресную строку не попадает.
    """
    return hmac.new((app_secret() or "velor").encode(),
                    ("velor-ig-delete|" + str(ig_user_id or "")).encode(),
                    hashlib.sha256).hexdigest()[:16]


def forget(business_id: int, why: str) -> bool:
    """
    Отозвать доступ к Instagram: токена больше нет, канал отключён.

    Переписки при этом остаются. Они рассказывают о покупателях бизнеса и
    принадлежат бизнесу — это его записи, а не полученные от Meta данные о
    том, кто вошёл. Стереть их по звонку извне значило бы отдать чужой
    компании её собственную историю продаж на удаление.
    """
    token = token_of(business_id)
    if token:
        try:
            unsubscribe(token)
        except Exception:
            # Токен уже мёртв — обычное дело, ради этого звонок и пришёл.
            log.info("Instagram: отписка не удалась, бизнес %s", business_id)
    database.delete_connection(business_id, PROVIDER)
    database.log_event(business_id, "integration", "Instagram отключён", why,
                       level="important")
    return True


def forget_by_ig_id(ig_user_id: str, why: str) -> int | None:
    """Найти бизнес по id аккаунта Instagram и отключить канал."""
    biz = database.find_business_by_ig_id(ig_user_id)
    if not biz:
        # Не нашли — говорим вслух. Meta прислала звонок про аккаунт, которого
        # у нас нет: либо канал отключили раньше, либо звонок не наш. Оба
        # случая надо видеть, а не проглатывать.
        log.warning("Instagram: звонок Meta про незнакомый аккаунт %s", ig_user_id)
        return None
    forget(int(biz["id"]), why)
    return int(biz["id"])


ATTACH_RU = {
    "image": "фото", "video": "видео", "audio": "голосовое сообщение",
    "file": "файл", "share": "публикацию", "story_mention": "упоминание в истории",
    "ig_reel": "reels", "template": "карточку", "fallback": "вложение",
}


def _attachment_note(msg: dict) -> str:
    """Человеческая пометка о вложении: что именно прислали."""
    kinds = []
    for a in (msg.get("attachments") or {}).get("data", []) if isinstance(
            msg.get("attachments"), dict) else (msg.get("attachments") or []):
        if isinstance(a, dict):
            kinds.append(ATTACH_RU.get(a.get("type"), "вложение"))
    if not kinds:
        return ""
    uniq = list(dict.fromkeys(kinds))
    return "[клиент прислал " + ", ".join(uniq) + "]"


def token_of(business_id: int) -> str | None:
    """Расшифрованный токен подключения. Наружу это значение не отдаётся никогда."""
    import secretbox
    row = database.get_connection(business_id, PROVIDER, with_secrets=True)
    if not row:
        return None
    try:
        creds = json.loads(secretbox.open_(row.get("credentials") or "") or "{}")
    except ValueError:
        return None
    return (creds.get("access_token") or "").strip() or None


def save_token(business_id: int, token: str, expires_in: int, meta: dict,
               permissions=None, config=None) -> None:
    """Положить токен зашифрованным — тем же способом, что и ключи всех сервисов."""
    import secretbox
    expires_at = stamp(_from_now(expires_in))
    blob = secretbox.seal(json.dumps(
        {"access_token": token, "expires_at": expires_at}, ensure_ascii=False))
    meta = dict(meta or {})
    meta["token_expires_at"] = expires_at
    database.save_connection(business_id, PROVIDER, blob, meta,
                             permissions=permissions or PERMISSIONS_RU,
                             config=config or {})


def _from_now(seconds):
    import datetime
    return datetime.datetime.utcnow() + datetime.timedelta(seconds=int(seconds or 0))


def token_expires_at(business_id: int) -> str | None:
    row = database.get_connection(business_id, PROVIDER)
    return ((row or {}).get("meta") or {}).get("token_expires_at")


REFRESH_BEFORE_DAYS = 10       # обновляем заранее, а не в последний час


def ensure_fresh_token(business_id: int) -> str | None:
    """
    Продлить токен, если до конца осталось меньше десяти суток.

    Токен живёт 60 суток и продлевается только пока он ещё жив: пропустив срок,
    вернуть его нельзя — только просить владельца войти заново. Поэтому запас
    берём с большим полем.
    """
    token = token_of(business_id)
    if not token:
        return None
    expires = token_expires_at(business_id)
    if expires:
        import datetime
        try:
            left = (datetime.datetime.strptime(expires, "%Y-%m-%d %H:%M:%S")
                    - datetime.datetime.utcnow()).days
        except ValueError:
            left = 99
        if left > REFRESH_BEFORE_DAYS:
            return token
    try:
        fresh = refresh_token(token)
    except AuthError as e:
        database.mark_connection_synced(business_id, PROVIDER, error=str(e), needs_auth=True)
        return None
    except ConnectorError:
        return token             # временный сбой: старый токен ещё действует
    row = database.get_connection(business_id, PROVIDER) or {}
    save_token(business_id, fresh["access_token"], fresh.get("expires_in") or 0,
               row.get("meta") or {}, row.get("permissions"), row.get("config"))
    return fresh["access_token"]


# ── приём сообщения ────────────────────────────────────────────────────────

def handle_webhook(payload: dict) -> dict:
    """
    Разобрать звонок Meta. Возвращает сводку — что приняли, что пропустили.

    Ошибка по одной переписке не должна ронять весь пакет: Meta шлёт события
    пачками, и уронив пакет мы потеряли бы и остальные сообщения.
    """
    handled = skipped = 0
    for entry in (payload.get("entry") or []):
        if not isinstance(entry, dict):
            continue
        ig_id = str(entry.get("id") or "")
        biz = database.find_business_by_ig_id(ig_id) if ig_id else None
        if not biz:
            skipped += 1                 # аккаунт не наш — молча мимо
            continue
        for item in (entry.get("messaging") or []):
            if not isinstance(item, dict):
                continue
            try:
                if _handle_one(biz["id"], ig_id, item):
                    handled += 1
                else:
                    skipped += 1
            except Exception:
                log.exception("Instagram: сообщение не обработано (biz %s)", biz["id"])
                skipped += 1
    return {"handled": handled, "skipped": skipped}


def _handle_one(bid: int, ig_id: str, item: dict) -> bool:
    import trial

    msg = item.get("message") or {}
    if not isinstance(msg, dict):
        return False
    if msg.get("is_deleted"):
        return False
    if not msg and not item.get("reaction"):
        return False                     # прочтения и реакции нам сказать нечего

    mid = str(msg.get("mid") or "")
    sender = str((item.get("sender") or {}).get("id") or "")
    recipient = str((item.get("recipient") or {}).get("id") or "")
    if not mid or not sender:
        return False

    # Эхо: сообщение ушло ОТ аккаунта бизнеса. Либо его отправили мы (тогда мы
    # уже записали этот mid при отправке и сюда не дойдём), либо владелец
    # ответил руками из приложения Instagram. Второе — это и есть живой человек,
    # взявший разговор на себя, и модели тут больше делать нечего.
    is_echo = bool(msg.get("is_echo")) or sender == ig_id
    if database.ig_seen_mid(bid, mid):
        return False                     # Meta повторяет доставку — не дублируем

    when = _ts(item.get("timestamp"))
    text = (msg.get("text") or "").strip()
    note = _attachment_note(msg)

    if is_echo:
        who = str(recipient)
        thread = database.ig_thread(bid, who)
        if not thread:
            return False                 # эхо в переписку, которой мы не знаем
        database.save_message(bid, thread["client_id"], "assistant",
                              text or note or "[ответ из Instagram]", channel=PROVIDER)
        database.ig_thread_mark(bid, who, last_out_at=when)
        if not database.ig_thread_paused(bid, who):
            database.ig_thread_pause(bid, who, True, by="human_in_app")
            database.log_event(
                bid, "reply", "Instagram: отвечает человек",
                "Владелец ответил из приложения Instagram — VELOR замолчал в этой "
                "переписке. Вернуть его можно в разделе «Директ Instagram».",
                once_key=f"ig-handoff:{who}")
        return True

    # ── обычное входящее ───────────────────────────────────────────────────
    business = database.get_business(bid) or {}
    token = token_of(bid)
    known = database.ig_thread(bid, sender) or {}
    profile = {}
    # Профиль спрашиваем один раз — при первом сообщении. Instagram считает
    # запросы (две штуки в секунду на аккаунт), и дёргать его на каждую реплику
    # активной переписки значит тратить лимит на то, что мы уже знаем.
    if token and not known.get("username"):
        try:
            profile = user_profile(token, sender) or {}
        except ConnectorError:
            profile = {}                 # имя не узнали — врать не станем

    username = (profile.get("username") or known.get("username") or "").strip()
    name = ((profile.get("name") or known.get("name") or "").strip()
            or (("@" + username) if username else ""))
    client_id, created = database.upsert_external_client(
        bid, sender, PROVIDER, name=name or None,
        notes="Пришёл из директа Instagram")
    database.ig_thread_upsert(bid, sender, client_id, username=username or None,
                              name=name or None,
                              avatar=(profile.get("profile_pic") or None),
                              last_in_at=when)

    body = text or note or "[сообщение без текста]"
    mid = database.save_message(bid, client_id, "user", body, channel=PROVIDER)

    if created:
        database.log_event(bid, "client", "Клиент из Instagram",
                           name or ("@" + username if username else "новый собеседник"))

    # Триал закончился — принимаем и молчим, ровно как в Telegram.
    if trial.access(business)["read_only"]:
        return True

    # Возможность замечаем ЗДЕСЬ, а не там, где отвечает продавец. Разница
    # видна в худший момент: переписку ведёт человек или кончился лимит тарифа —
    # продавец молчит, а клиент всё равно спросил цену. Именно тогда владельцу
    # и нужна воронка.
    if text:
        try:
            import leads
            leads.from_message(bid, {"id": client_id}, text,
                               source=PROVIDER, channel=PROVIDER, message_id=mid)
        except Exception:
            log.exception("Возможность из директа не завелась (biz %s)", bid)

    if database.ig_thread_paused(bid, sender):
        database.log_event(bid, "reply", "Instagram: новое сообщение",
                           body[:200], once_key=f"ig-manual:{sender}")
        return True
    if not text:
        # Картинку в директе VELOR не видит: этот канал отдаёт только ссылку,
        # которая живёт считанные минуты. Молча выдумывать ответ на фото хуже,
        # чем позвать владельца.
        database.log_event(bid, "reply", "Instagram: вложение без текста",
                           note or "Клиент прислал вложение — ответьте сами.",
                           once_key=f"ig-attach:{sender}")
        return True

    import sales
    client = database.get_client(client_id, bid) or {"id": client_id, "name": name}
    try:
        decision = sales.answer(bid, client, text, channel=PROVIDER)
    except Exception:
        # Откатываться на прежнее ядро здесь НЕЛЬЗЯ: оно отвечает без проверки
        # на выдумку, и сбой в защите обернулся бы выдуманной ценой. Молчим и
        # зовём человека — это худший ответ клиенту и единственно честный.
        log.exception("AI-продавец не отработал (biz %s)", bid)
        decision = {"reply": sales.HOLD_REPLY, "handoff": True, "draft": None,
                    "reason": "Внутренняя ошибка ответа — отвечает человек."}

    if decision.get("handoff"):
        _handoff(bid, sender, decision, text)

    reply = decision.get("reply")
    if not reply or not token:
        return True
    try:
        res = send_text(token, sender, reply)
    except AuthError as e:
        database.mark_connection_synced(bid, PROVIDER, error=str(e), needs_auth=True)
        return True
    except ConnectorError as e:
        database.mark_connection_synced(bid, PROVIDER, error=str(e))
        return True
    database.save_message(bid, client_id, "assistant", reply, channel=PROVIDER)
    database.ig_thread_mark(bid, sender, last_out_at=stamp())
    out_mid = str((res or {}).get("message_id") or "")
    if out_mid:
        # Своё же сообщение вернётся эхом. Запоминаем его id, чтобы не принять
        # собственный ответ за вмешательство человека и не замолчать зря.
        database.ig_seen_mid(bid, out_mid)
    return True


def _handoff(bid, igsid, decision, question):
    """
    Передать разговор человеку и объяснить, почему.

    Владельцу показываем и причину, и черновик, который VELOR хотел отправить.
    Причина отвечает на вопрос «что случилось», черновик — на куда более
    полезный «чего не хватает в памяти бизнеса»: чаще всего там ровно та цена
    или то условие, которые никто не внёс.
    """
    reason = (decision.get("reason") or "VELOR не уверен в ответе.")[:400]
    draft = (decision.get("draft") or "")[:2000] or None
    database.ig_thread_pause(bid, igsid, True, by="ai_unsure")
    database.ig_thread_reason(bid, igsid, why=reason, draft=draft)
    database.log_event(
        bid, "reply", "Instagram: нужен человек", reason,
        level="important", once_key=f"ig-unsure:{igsid}")


def _ts(value) -> str:
    """Время события Meta (миллисекунды) в наш формат. Нет — берём текущее."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return stamp()
    import datetime
    return stamp(datetime.datetime.utcfromtimestamp(ms / 1000.0))


# ── ответ человека из кабинета ─────────────────────────────────────────────

def window_state(business_id: int, igsid: str) -> dict:
    """
    Сколько осталось времени на ответ.

    Это не наше правило, а правило Instagram, и владелец должен видеть его до
    того, как напишет ответ: автоответ — 24 часа, человек — 7 суток.
    """
    import datetime
    thread = database.ig_thread(business_id, igsid) or {}
    last_in = to_stamp(thread.get("last_in_at"))
    if not last_in:
        return {"can_reply": False, "hours_left": 0, "human_hours_left": 0,
                "why": "Клиент ещё не писал — Instagram запрещает писать первым."}
    started = datetime.datetime.strptime(last_in, "%Y-%m-%d %H:%M:%S")
    passed = (datetime.datetime.utcnow() - started).total_seconds() / 3600.0
    auto_left = REPLY_WINDOW_HOURS - passed
    human_left = HUMAN_WINDOW_DAYS * 24 - passed
    return {
        "can_reply": human_left > 0,
        "needs_human_tag": auto_left <= 0 < human_left,
        "hours_left": round(max(0.0, auto_left), 1),
        "human_hours_left": round(max(0.0, human_left), 1),
        "why": ("" if human_left > 0 else
                "Прошло больше семи суток с последнего сообщения клиента — "
                "Instagram больше не пропустит ответ. Написать первым нельзя."),
    }


def reply_as_human(business_id: int, igsid: str, text: str) -> dict:
    """
    Ответ владельца из кабинета.

    Тег HUMAN_AGENT ставим только здесь и только когда 24 часа уже прошли: это
    единственный случай, когда его разрешает Meta, — сообщение пишет человек.
    """
    text = (text or "").strip()
    if not text:
        raise ConnectorError("Пустое сообщение отправить нельзя.")
    thread = database.ig_thread(business_id, igsid)
    if not thread:
        raise ConnectorError("Такой переписки нет.")
    win = window_state(business_id, igsid)
    if not win["can_reply"]:
        raise ConnectorError(win["why"])
    token = ensure_fresh_token(business_id)
    if not token:
        raise AuthError("Instagram отключён или доступ истёк — подключите аккаунт заново.")
    res = send_text(token, igsid, text, human=bool(win.get("needs_human_tag")))
    database.save_message(business_id, thread["client_id"], "assistant", text,
                          channel=PROVIDER)
    database.ig_thread_mark(business_id, igsid, last_out_at=stamp())
    mid = str((res or {}).get("message_id") or "")
    if mid:
        database.ig_seen_mid(business_id, mid)
    return {"ok": True, "tagged_human": bool(win.get("needs_human_tag"))}


# ── первая загрузка переписок ──────────────────────────────────────────────

def pull_recent(business_id: int) -> dict:
    """
    Забрать то, что уже есть в директе, — не глубже двадцати сообщений на тред.

    Нужно, чтобы после подключения кабинет не выглядел пустым, пока никто не
    написал. Глубже не идём: Meta отвечает на такие запросы ошибкой, а не
    данными, и обещать «всю историю» было бы обманом.
    """
    token = ensure_fresh_token(business_id)
    if not token:
        return {"ok": False, "added": 0, "error": "Instagram не подключён."}
    row = database.get_connection(business_id, PROVIDER) or {}
    ig_id = str((row.get("meta") or {}).get("ig_id") or "")
    added = 0
    try:
        convs = conversations(token)
    except AuthError as e:
        database.mark_connection_synced(business_id, PROVIDER, error=str(e), needs_auth=True)
        return {"ok": False, "added": 0, "error": str(e)}
    except ConnectorError as e:
        database.mark_connection_synced(business_id, PROVIDER, error=str(e))
        return {"ok": False, "added": 0, "error": str(e)}

    for i, conv in enumerate(convs):
        if i:
            # Instagram считает обращения к переписке: две штуки в секунду на
            # аккаунт. Без паузы первая же выгрузка активного директа упирается
            # в отказ, и половина переписок не доезжает.
            time.sleep(0.55)
        try:
            msgs = conversation_messages(token, conv.get("id"))
        except ConnectorError:
            continue
        for m in sorted(msgs, key=lambda x: str(x.get("created_time") or "")):
            frm = m.get("from") or {}
            other = str(frm.get("id") or "")
            mine = other == ig_id
            counterpart = other
            if mine:
                to = ((m.get("to") or {}).get("data") or [])
                counterpart = str((to[0] or {}).get("id") or "") if to else ""
            if not counterpart:
                continue
            mid = str(m.get("id") or "")
            if not mid or database.ig_seen_mid(business_id, mid):
                continue
            name = (frm.get("username") or "").strip()
            client_id, _ = database.upsert_external_client(
                business_id, counterpart, PROVIDER,
                name=("@" + name) if name and not mine else None)
            when = to_stamp(m.get("created_time")) or stamp()
            database.ig_thread_upsert(business_id, counterpart, client_id,
                                      username=(name if not mine else None),
                                      **({"last_out_at": when} if mine else {"last_in_at": when}))
            body = (m.get("message") or "").strip()
            if body:
                database.save_message(business_id, client_id,
                                      "assistant" if mine else "user", body,
                                      channel=PROVIDER, created_at=when)
                added += 1
    database.mark_connection_synced(business_id, PROVIDER, added=added)
    return {"ok": True, "added": added, "error": None,
            "limit_note": f"Instagram отдаёт не больше {HISTORY_LIMIT} последних "
                          "сообщений в переписке — более старые он не возвращает."}
