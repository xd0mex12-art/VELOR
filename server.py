"""
Веб-сервер: отдаёт сайт (папка web/) и предоставляет API для заказов.
Это и есть будущая "API-розетка" — через неё приложение/панель общается с базой.

Запуск:
    python server.py
Потом открой в браузере:  http://127.0.0.1:8000
Панель заказов:           http://127.0.0.1:8000/dashboard.html
"""
import datetime
import hashlib
import secrets
import json
import logging
import re
# Модульный импорт вместо трёх локальных: потоки нужны и на уровне модуля
# (замок на дописывание брифинга), а не только внутри функций.
import threading

import requests

from fastapi import (FastAPI, Header, HTTPException, UploadFile, File, Form,
                     Request)
from fastapi.responses import FileResponse, Response, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

import database
import signals
import finance_import
import exporters
import auth
import ratelimit
import errorlog
import safeurl
import config
import botcore
import connectors
import trial
import identity
import storage
import understanding
import intake
import entities
import graph
import director
import connections
import instagram
import sales
import leads
import actions
import initiatives
import followup
import qualify
import outputs
from urllib.parse import quote as _urlquote
from config import (OWNER_LOGIN, OWNER_PASSWORD,
                    ACCESS_TTL_MIN, REFRESH_TTL_DAYS)

app = FastAPI(title="VELOR AI API")

# Гарантируем, что база и таблицы существуют.
database.init_db()


# ---------- ЛОГИРОВАНИЕ И ЕДИНАЯ ОБРАБОТКА ОШИБОК ----------
# Все необработанные ошибки пишем в errors.log с трассировкой, а пользователю
# отдаём короткое понятное сообщение по-русски — без стектрейса и деталей.
# Путь лога берём из config.LOG_DIR (в Docker это том /logs), плюс дублируем в
# stdout — чтобы `docker logs` тоже показывал ошибки.
import os as _os
_os.makedirs(config.LOG_DIR, exist_ok=True)
_log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_file_h = logging.FileHandler(_os.path.join(config.LOG_DIR, "errors.log"), encoding="utf-8")
_file_h.setFormatter(_log_fmt)
_stream_h = logging.StreamHandler()
_stream_h.setFormatter(_log_fmt)
logging.basicConfig(level=logging.WARNING, handlers=[_file_h, _stream_h])
log = logging.getLogger("velor")


# Проверяем учётные данные владельца при старте: в production со стандартными
# значениями (admin/admin) config.check_owner_credentials() бросит RuntimeError
# и сервер не запустится; в dev — просто предупреждаем в логе и в консоли.
_cred_warning = config.check_owner_credentials()
if _cred_warning:
    log.warning("%s", _cred_warning)
    print("\n[ВНИМАНИЕ] " + _cred_warning + "\n")


# ---------- ЗАЩИТНЫЕ HTTP-ЗАГОЛОВКИ ----------
# Раньше они жили только в конфиге nginx, а nginx поднимается лишь в
# docker-compose. Боевой деплой (Render) — это голый uvicorn, то есть в проде не
# было НИ ОДНОГО защитного заголовка. Ставим их в самом приложении: тогда они
# есть везде, где бы приложение ни запускалось.
#
# Про CSP. Кабинет написан инлайновыми <script> и обработчиками onclick, поэтому
# полностью запретить инлайн нельзя — это переписывание всех 36 страниц. Но самое
# ценное CSP даёт и так: 'self' на connect/img/script закрывает УТЕЧКУ. Даже если
# в имя клиента из Telegram подсунут скрипт, он не сможет отправить токен из
# localStorage на чужой сервер — ни fetch'ем, ни картинкой, ни формой.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",       # инлайновые скрипты страниц
    "style-src 'self' 'unsafe-inline'",        # инлайновые стили страниц
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self'",                      # запросы только к своему API
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",                  # защита от кликджекинга
])


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=(), payment=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    # HSTS — только когда соединение действительно защищено, иначе локальный
    # http-запуск на 127.0.0.1 браузер запомнит как «только https» и сломает разработку.
    if request.url.scheme == "https" or \
            request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https":
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    log.exception("Ошибка при %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Что-то пошло не так на нашей стороне. Мы уже записали ошибку — "
                           "попробуйте повторить чуть позже."},
    )


# ============================================================
#  АУТЕНТИФИКАЦИЯ (JWT: access + refresh)
# ============================================================
# access-токен (короткий, подписанный) приходит в заголовке X-Auth — как и
# раньше, поэтому бизнес-логика эндпоинтов не меняется. refresh-токен (длинный,
# хранится в базе хешем) обменивается на новый access через /api/refresh.

def _issue_tokens(subject: str, business_id: int = None) -> dict:
    """Выпустить пару токенов и сохранить refresh в базе (для отзыва)."""
    claims = {"role": subject}
    if business_id is not None:
        claims["bid"] = business_id
    access = auth.make_access_token(claims)
    refresh = auth.new_refresh_token()
    expires = (datetime.datetime.utcnow()
               + datetime.timedelta(days=REFRESH_TTL_DAYS)).isoformat()
    database.save_refresh_token(refresh, subject, business_id, expires)
    database.purge_expired_refresh()
    return {"token": access, "refresh_token": refresh,
            "expires_in": ACCESS_TTL_MIN * 60}


def _auth_payload(x_auth: str):
    """Разобрать access-токен из заголовка X-Auth (или None)."""
    return auth.decode_access_token(x_auth)


def _client_ip(request: Request) -> str:
    """IP клиента для защиты от перебора (учитываем прокси через X-Forwarded-For)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Понятные тексты на нарушение уникальности (её гарантирует база, а не проверка
# «сначала посмотрели, потом записали» — та не выдерживает одновременных запросов).
_DUPLICATE_TEXT = {
    "login": "Такой логин уже занят — придумайте другой.",
    "tg_bot_token": "Этот бот уже подключён к другой компании. "
                    "Создайте отдельного бота в @BotFather для этой компании.",
}


class LoginIn(BaseModel):
    login: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


@app.post("/api/login")
def api_login(body: LoginIn, request: Request):
    """Вход владельца VELOR AI."""
    key = "owner:" + _client_ip(request)
    wait = ratelimit.login_retry_after(key)
    if wait:
        raise HTTPException(status_code=429, detail=(
            "Слишком много попыток входа. Подождите " + ratelimit.human_wait(wait)
            + " и попробуйте снова."))
    if body.login == OWNER_LOGIN and body.password == OWNER_PASSWORD:
        ratelimit.note_login_success(key)
        return {"ok": True, **_issue_tokens("owner")}
    ratelimit.note_login_fail(key)
    raise HTTPException(status_code=401, detail="Неверный логин или пароль")


def require_owner(x_auth: str = Header(default="")):
    """Защита админ-эндпоинтов: пускаем только владельца по валидному access-токену."""
    payload = _auth_payload(x_auth)
    if not payload or payload.get("role") != "owner":
        raise HTTPException(status_code=401, detail="Нужен вход")
    return True


class RegisterIn(BaseModel):
    name: str
    login: str
    password: str
    about: str | None = None
    consent: bool = False   # согласие с условиями и обработкой ПД (152-ФЗ)
    fingerprint: str | None = None   # отпечаток устройства (защита от повторного триала)


@app.post("/api/register")
def api_register(body: RegisterIn, request: Request):
    """Саморегистрация бизнеса: создаёт аккаунт и сразу пускает в панель."""
    wait = ratelimit.register_retry_after("reg:" + _client_ip(request))
    if wait:
        raise HTTPException(status_code=429, detail=(
            "Слишком много регистраций с этого устройства. Попробуйте снова через "
            + ratelimit.human_wait(wait) + "."))
    if not body.consent:
        raise HTTPException(status_code=400,
                            detail="Нужно принять условия и согласие на обработку персональных данных")
    name = (body.name or "").strip()
    login = (body.login or "").strip()
    password = body.password or ""
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Впишите название бизнеса")
    if len(login) < 3:
        raise HTTPException(status_code=400, detail="Логин — минимум 3 символа")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Пароль — минимум 4 символа")
    if database.login_taken(login):
        raise HTTPException(status_code=409, detail="Такой логин уже занят — придумайте другой")

    # Аккаунт создаётся ОДНОЙ вставкой вместе с логином и паролем. Проверка выше —
    # только ради понятного текста ошибки; настоящую гарантию даёт уникальный
    # индекс в базе, поэтому одновременную регистрацию с тем же логином ловим здесь.
    try:
        bid = database.create_business(
            name=name,
            about=(body.about or "").strip() or "малый бизнес: приём заказов и заявок",
            greeting="Здравствуйте! Напишите, что вам нужно — я приму заявку и всё оформлю.",
            login=login,
            password=password,
        )
    except database.DuplicateError:
        raise HTTPException(status_code=409, detail="Такой логин уже занят — придумайте другой")

    # ── Регистрация НЕ запускает триал: аккаунт в онбординге, отсчёт 14 дней
    #    стартует по кнопке «Запустить VELOR» (/api/trial/start). Fingerprint —
    #    только МЯГКИЙ сигнал: считаем risk_score, при высоком сообщаем владельцу,
    #    но НЕ блокируем (жёсткий блок — по Telegram при запуске). ──
    fp = (body.fingerprint or "").strip() or None
    trial.register_state(bid)
    # Заводим личность владельца (Owner Identity) — к ней будет привязан триал.
    # Fingerprint здесь — вспомогательный признак (для risk_score и слабой связки
    # email+fingerprint), НЕ причина отказа.
    try:
        identity.ensure(bid, method="email", fingerprint=fp)
    except Exception:
        pass
    # risk_score — сигнал абьюза (НЕ блокировка). Берём максимум из оценки по
    # личности владельца (owner_identity) и legacy-оценки (trial_registry).
    risk_o, reasons_o = identity.assess_risk(bid, fingerprint=fp)
    risk_l, reasons_l = trial.assess_risk(fingerprint=fp)
    risk = max(risk_o, risk_l)
    reasons = reasons_o or reasons_l
    database.update_business(bid, risk_score=risk)
    try:
        identity.ensure(bid, risk_score=risk)
    except Exception:
        pass
    trial.record_usage(bid, fingerprint=fp, ip=_client_ip(request))
    if risk >= 40:
        try:
            database.log_event(bid, "security", "Подозрительная регистрация",
                               "Возможен повторный триал: " + "; ".join(reasons)
                               + f". risk_score {risk}.",
                               level="important", once_key="risk")
        except Exception:
            pass

    # Фиксируем факт согласия (152-ФЗ): что принято и когда — для доказуемости.
    try:
        database.log_event(bid, "consent", "Принято согласие на обработку данных",
                           "Пользовательское соглашение + Согласие на обработку ПД (ред. 2026-07-29), "
                           "IP " + _client_ip(request), level="info", once_key="consent")
    except Exception:
        pass
    return {"ok": True, "business_id": bid, "name": name, "onboarding": True,
            **_issue_tokens("business", bid)}


# ---------- TRIAL / ПОДПИСКА ----------

def _ai_locked(bid) -> bool:
    """ИИ на паузе (триал завершён)?

    Нужен там, где модель дёргается ЛЕНИВО при открытии страницы — брифинг,
    обзор недели, дневник, резюме клиента, совет директоров. Такие страницы
    нельзя закрывать ошибкой 402 (данные должны оставаться видны), но и
    тратить на них общий ключ ИИ после окончания триала нельзя: показываем
    то, что уже сохранено, и молчим.
    """
    try:
        return bool(trial.access(database.get_business(bid))["read_only"])
    except Exception:
        return False


def require_active(bid):
    """Гейт активных операций (ИИ, бот, создание). read-only → 402 с понятным текстом."""
    st = trial.access(database.get_business(bid))
    if st["read_only"]:
        raise HTTPException(status_code=402,
                            detail="Пробный период завершён. Оформите подписку, чтобы продолжить работу.")
    return st


@app.get("/api/trial")
def api_trial(business_id: int = 0, x_auth: str = Header(default="")):
    """Состояние триала/подписки для фронта: баннер-отсчёт и экран окончания."""
    bid = _resolve_bid(x_auth, business_id)
    st = trial.access(database.get_business(bid))
    st["stats"] = trial.stats(bid)
    return st


def _owner_verify_code(bid):
    """Выдать/переиспользовать одноразовый код привязки личного Telegram владельца."""
    b = database.get_business(bid) or {}
    code = (b.get("tg_verify_code") or "").strip()
    if not code:
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        database.update_business(bid, tg_verify_code=code)
    return code


@app.post("/api/trial/start")
def api_trial_start(x_auth: str = Header(default="")):
    """Кнопка «Запустить VELOR»: с этого момента идёт отсчёт 14 дней.

    Триал привязан к ЛИЧНОСТИ ВЛАДЕЛЬЦА, а не к боту. Поэтому запуск требует
    подтверждения владельца: он отправляет одноразовый код своему боту, мы
    фиксируем его личный Telegram id. Создание нового бота НЕ даёт новый триал —
    личность та же. Архитектура готова заменить/дополнить это телефоном (identity).
    """
    bid = _resolve_bid(x_auth, 0)
    b = database.get_business(bid) or {}
    st = trial.access(b)
    if st["phase"] in ("trial", "subscribed", "legacy"):
        return {"ok": True, **st}   # уже запущен — идемпотентно

    token = (b.get("tg_bot_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400,
                            detail="Подключите Telegram-бота, чтобы запустить VELOR.")

    # Шаг 1. Личность владельца ещё не подтверждена → просим отправить код боту.
    if not trial.owner_verified(bid):
        code = _owner_verify_code(bid)
        bot_username = None
        me = _tg_api(token, "getMe")
        if me and me.get("ok") and me.get("result"):
            bot_username = me["result"].get("username")
        return {
            "ok": False,
            "needs_verify": True,
            "code": code,
            "bot": bot_username,
            "detail": "Подтвердите, что вы владелец: откройте своего бота"
                      + (f" @{bot_username}" if bot_username else "")
                      + f" и отправьте ему код {code}.",
        }

    # Шаг 2. Владелец подтверждён — проверяем, не брал ли он триал ранее.
    used, reason = trial.owner_used(bid)
    if used:
        raise HTTPException(status_code=409,
                            detail="Пробный период для данного владельца бизнеса уже был использован.")

    # Legacy-признак (id бота) — пишем для совместимости, но защита уже на личности.
    tg_bot_id = None
    me = _tg_api(token, "getMe")
    if me and me.get("ok") and me.get("result"):
        tg_bot_id = "tg:" + str(me["result"].get("id"))
    trial.launch(bid, telegram_id=tg_bot_id)
    return {"ok": True, **trial.access(database.get_business(bid))}


class TrialAdminIn(BaseModel):
    days: int | None = None
    date: str | None = None       # 'YYYY-MM-DD [HH:MM:SS]'
    plan: str | None = None       # starter | business | pro
    months: int | None = None


@app.post("/api/admin/businesses/{bid}/trial-extend")
def api_admin_trial_extend(bid: int, body: TrialAdminIn, x_auth: str = Header(default="")):
    """Владелец VELOR: продлить триал на N дней."""
    require_owner(x_auth)
    trial.extend_trial(bid, days=body.days or 7)
    return {"ok": True, **trial.access(database.get_business(bid))}


@app.post("/api/admin/businesses/{bid}/trial-end")
def api_admin_trial_end(bid: int, body: TrialAdminIn, x_auth: str = Header(default="")):
    """Владелец VELOR: задать точную дату окончания триала."""
    require_owner(x_auth)
    if body.date:
        trial.set_trial_end(bid, body.date)
    return {"ok": True, **trial.access(database.get_business(bid))}


@app.post("/api/admin/businesses/{bid}/trial-disable")
def api_admin_trial_disable(bid: int, x_auth: str = Header(default="")):
    """Владелец VELOR: завершить триал сейчас (перевести в режим только чтение)."""
    require_owner(x_auth)
    trial.disable(bid)
    return {"ok": True}


@app.post("/api/admin/businesses/{bid}/subscription")
def api_admin_subscription(bid: int, body: TrialAdminIn, x_auth: str = Header(default="")):
    """Владелец VELOR: активировать платную подписку или сменить тариф после оплаты.
    Смена тарифа — тот же вызов с другим plan. Архитектурный хук для будущей платёжки."""
    require_owner(x_auth)
    trial.activate_subscription(bid, plan=(body.plan or "business"), months=(body.months or 1))
    return {"ok": True, **trial.access(database.get_business(bid))}


@app.post("/api/admin/businesses/{bid}/subscription-extend")
def api_admin_subscription_extend(bid: int, body: TrialAdminIn, x_auth: str = Header(default="")):
    """Владелец VELOR: продлить действующую подписку (аддитивно). После повторной
    оплаты будущая платёжка вызовет этот же путь."""
    require_owner(x_auth)
    trial.extend_subscription(bid, months=(body.months or 1))
    return {"ok": True, **trial.access(database.get_business(bid))}


@app.get("/api/admin/trial-overview")
def api_admin_trial_overview(x_auth: str = Header(default="")):
    """Владелец VELOR: сводка по триалам/подпискам + воронка конверсии."""
    require_owner(x_auth)
    rows = database.list_businesses_with_stats()
    counts = {"total": len(rows), "onboarding": 0, "trial": 0,
              "subscribed": 0, "locked": 0, "suspicious": 0}
    items = []
    for b in rows:
        st = trial.access(b)
        ph = st["phase"]
        key = "subscribed" if ph in ("subscribed", "legacy") else ph
        counts[key] = counts.get(key, 0) + 1
        risky = (b.get("risk_score") or 0) >= 40
        if risky:
            counts["suspicious"] += 1
        items.append({"id": b["id"], "name": b.get("name"), "phase": ph,
                      "days_left": st["days_left"], "plan": st["plan"],
                      "trial_end": st["trial_end"], "risk_score": b.get("risk_score") or 0,
                      "suspicious": risky})
    return {"counts": counts, "funnel": database.trial_funnel(),
            "plans": trial.PLANS, "businesses": items}


@app.post("/api/business-login")
def api_business_login(body: LoginIn, request: Request):
    """Вход бизнеса в свою панель."""
    key = "biz:" + _client_ip(request)
    wait = ratelimit.login_retry_after(key)
    if wait:
        raise HTTPException(status_code=429, detail=(
            "Слишком много попыток входа. Подождите " + ratelimit.human_wait(wait)
            + " и попробуйте снова."))
    biz = database.find_business_by_login(body.login, body.password)
    if not biz:
        ratelimit.note_login_fail(key)
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    ratelimit.note_login_success(key)
    return {"ok": True, "business_id": biz["id"], "name": biz["name"],
            **_issue_tokens("business", biz["id"])}


@app.post("/api/refresh")
def api_refresh(body: RefreshIn):
    """Обменять действующий refresh-токен на новый access-токен."""
    row = database.get_valid_refresh(body.refresh_token)
    if not row:
        raise HTTPException(status_code=401, detail="Сессия истекла — войдите заново")
    claims = {"role": row["subject"]}
    if row["business_id"] is not None:
        claims["bid"] = row["business_id"]
    return {"ok": True, "token": auth.make_access_token(claims),
            "expires_in": ACCESS_TTL_MIN * 60}


@app.post("/api/logout")
def api_logout(body: RefreshIn):
    """Выход: отзываем refresh-токен, после чего его нельзя обменять на access."""
    database.revoke_refresh_token(body.refresh_token)
    return {"ok": True}


def require_business(x_auth: str = Header(default="")) -> int:
    """
    Возвращает business_id, к которому привязан токен.
    Владелец (VELOR AI) может смотреть любой бизнес — тогда business_id берётся
    из строки запроса. Бизнес — только свой.
    """
    payload = _auth_payload(x_auth)
    if payload:
        if payload.get("role") == "business":
            bid = payload.get("bid")
            if isinstance(bid, int) and bid > 0:
                return bid
            # токен бизнеса без корректного bid — не угадываем, а отклоняем
            raise HTTPException(status_code=401, detail="Сессия недействительна — войдите заново")
        if payload.get("role") == "owner":
            return -1  # владелец: business_id задаётся параметром (см. эндпоинты)
    raise HTTPException(status_code=401, detail="Нужен вход")


# ============================================================
#  АДМИНКА: все бизнесы, деньги, чаты, полная настройка
# ============================================================

@app.get("/api/admin/overview")
def api_admin_overview(x_auth: str = Header(default="")):
    require_owner(x_auth)
    businesses = database.list_businesses_with_stats()
    return {
        "businesses": businesses,
        "totals": {
            "count": len(businesses),
            "income": sum(b.get("fee") or 0 for b in businesses),        # твой доход (сумма абонплат)
            "turnover": sum(b.get("turnover") or 0 for b in businesses), # общий оборот всех бизнесов
            "orders": sum(b.get("orders_count") or 0 for b in businesses),
        },
    }


class BusinessNew(BaseModel):
    name: str
    about: str | None = None
    greeting: str | None = None
    plan: str | None = None
    fee: int | None = None
    login: str | None = None
    password: str | None = None


@app.post("/api/admin/businesses")
def api_admin_create(body: BusinessNew, x_auth: str = Header(default="")):
    require_owner(x_auth)
    try:
        bid = database.create_business(name=body.name, about=body.about,
                                       greeting=body.greeting,
                                       login=body.login, password=body.password)
    except database.DuplicateError:
        raise HTTPException(status_code=409, detail="Такой логин уже занят — придумайте другой")
    extra = {k: v for k, v in {"plan": body.plan, "fee": body.fee}.items() if v is not None}
    if extra:
        database.update_business(bid, **extra)
    return {"ok": True, "id": bid}


class BusinessEdit(BaseModel):
    name: str | None = None
    about: str | None = None
    greeting: str | None = None
    plan: str | None = None
    fee: int | None = None
    tg_bot_token: str | None = None
    login: str | None = None
    password: str | None = None


@app.post("/api/admin/businesses/{bid}")
def api_admin_edit(bid: int, body: BusinessEdit, x_auth: str = Header(default="")):
    require_owner(x_auth)
    try:
        database.update_business(bid, **body.model_dump(exclude_none=True))
    except database.DuplicateError as e:
        raise HTTPException(status_code=409, detail=_DUPLICATE_TEXT[e.field])
    return {"ok": True}


@app.delete("/api/admin/businesses/{bid}")
def api_admin_delete(bid: int, x_auth: str = Header(default="")):
    require_owner(x_auth)
    database.delete_business(bid)
    return {"ok": True}


@app.get("/api/admin/businesses/{bid}/chats")
def api_admin_chats(bid: int, x_auth: str = Header(default="")):
    require_owner(x_auth)
    return database.get_chats(bid)


@app.get("/api/admin/businesses/{bid}/chats/{client_id}")
def api_admin_chat(bid: int, client_id: int, x_auth: str = Header(default="")):
    require_owner(x_auth)
    return database.get_chat(bid, client_id)


# ---------- ДИАГНОСТИКА ИИ (только владелец) ----------

@app.get("/api/admin/ai-status")
def api_admin_ai_status(x_auth: str = Header(default="")):
    """Какой AI-провайдер сейчас основной и кто реально отвечает. Делает крошечный
    живой вызов (ping). Ключи НЕ раскрываются — только имена и модель/URL."""
    require_owner(x_auth)
    import ai
    configured = [name for name, _ in ai._all_providers()]   # порядок приоритета
    active = None
    try:
        active = ai.ping()                                   # первый ответивший провайдер
    except Exception:
        logging.exception("ai-status: ping не удался")
    return {
        "configured": configured,          # напр. ["gemini","gigachat"]
        "primary": configured[0] if configured else None,
        "active": active,                  # кто реально ответил на пробный запрос
        "gemini_enabled": bool(ai.GEMINI_API_KEY),
        "gemini_model": config.GEMINI_MODEL if ai.GEMINI_API_KEY else None,
        "gemini_base_url": config.GEMINI_BASE_URL if ai.GEMINI_API_KEY else None,
    }


# ---------- ЖУРНАЛ ОШИБОК (только владелец) ----------

@app.get("/api/admin/errors")
def api_admin_errors(date: str = "", q: str = "", level: str = "",
                     limit: int = 500, x_auth: str = Header(default="")):
    """Записи из errors.log с фильтром по дате/уровню/поиску. Только владелец."""
    require_owner(x_auth)
    return errorlog.query(date=date, q=q, level=level, limit=limit)


@app.get("/api/admin/errors/download")
def api_admin_errors_download(x_auth: str = Header(default="")):
    """Скачать сам файл errors.log. Только владелец (токен в заголовке X-Auth;
    фронт качает через fetch+blob, чтобы токен не попадал в URL)."""
    require_owner(x_auth)
    import os
    if not os.path.exists(errorlog.LOG_FILE):
        raise HTTPException(status_code=404, detail="Файл журнала пока пуст.")
    return FileResponse(errorlog.LOG_FILE, media_type="text/plain; charset=utf-8",
                        filename="errors.log")


# ---------- Модель входящего заказа (для будущего приёма извне) ----------
class OrderIn(BaseModel):
    text: str
    phone: str | None = None
    address: str | None = None
    date_wanted: str | None = None
    amount: str | int | float | None = None   # сумма заказа — основа всего оборота
    # Кому эта заявка. Без клиента она остаётся сама по себе, и возможность,
    # из которой она выросла, висит открытой навсегда — VELOR продолжает
    # считать, что человек ждёт ответа, хотя он уже купил.
    client_id: int | None = None
    business_id: int = 0


# ---------- ПАНЕЛЬ БИЗНЕСА (защищено — каждый видит только своё) ----------

def _resolve_bid(x_auth: str, requested: int) -> int:
    """
    business_id для панельных запросов.

    Бизнес видит только свой бизнес — параметр запроса игнорируется, берётся
    id из токена (чужую компанию открыть нельзя). Владелец может смотреть любой
    бизнес, но обязан указать какой: если id не передан или такого бизнеса нет —
    возвращаем понятную ошибку и НЕ выполняем запрос (раньше здесь молча
    подставлялся бизнес №1, что могло открыть чужие данные).
    """
    bid = require_business(x_auth)      # 401, если токена нет
    if bid != -1:
        return bid                      # бизнес — только свой, параметр не влияет
    if not requested or requested <= 0:
        raise HTTPException(status_code=400,
                            detail="Не указана компания — добавьте business_id.")
    if not database.get_business(requested):
        raise HTTPException(status_code=404, detail="Компания не найдена.")
    return requested


@app.get("/api/orders")
def api_orders(business_id: int = 0, limit: int = 50, offset: int = 0,
               x_auth: str = Header(default="")):
    """Заказы бизнеса — для его панели.

    Отдаём страницу заказов И настоящие итоги по всей таблице. Раньше страница
    показывала последние 20 записей и других чисел не было вовсе — из-за этого
    и панель, и ИИ считали, что у компании ровно столько заказов, сколько влезло
    в выборку.
    """
    bid = _resolve_bid(x_auth, business_id)
    limit = max(1, min(limit, 200))
    items = database.get_orders(bid, limit=limit, offset=max(0, offset))
    return {"items": items, **database.orders_overview(bid)}


@app.post("/api/orders")
def api_add_order(order: OrderIn, x_auth: str = Header(default="")):
    """Создать заказ. Только авторизованный владелец своей компании (через ту же
    систему X-Auth, что и вся панель). Анонимное создание заявок извне закрыто —
    business_id берётся из токена, а не из тела запроса (защита арендаторов)."""
    bid = _resolve_bid(x_auth, order.business_id)
    biz = database.get_business(bid)
    if not biz:
        raise HTTPException(status_code=400,
                            detail="Не указана или неизвестна компания (business_id).")
    if trial.access(biz)["read_only"]:
        raise HTTPException(status_code=402,
                            detail="Приём новых заявок приостановлен: у компании завершён пробный период.")
    order_id = database.add_order(
        business_id=bid,
        text=order.text,
        phone=order.phone,
        address=order.address,
        date_wanted=order.date_wanted,
        amount=order.amount,
        client_id=order.client_id,
    )
    signals.react(bid, "order")   # заказ влияет на Директора, брифинг, риски
    # Заявка, заведённая рукой владельца, — такая же заявка. Если за ней стоит
    # разговор, возможность закрывается сделкой ровно так же, как если бы
    # заявку оформил сам VELOR.
    won = leads.link_order(bid, order_id, client_id=order.client_id,
                           amount=order.amount, channel="manual")
    return {"ok": True, "order_id": order_id, "lead_id": won}


class AmountIn(BaseModel):
    amount: str | int | float | None = None
    business_id: int = 0


@app.post("/api/orders/{order_id}/amount")
def api_update_amount(order_id: int, body: AmountIn, x_auth: str = Header(default="")):
    """Проставить сумму заказа. Из неё складываются оборот компании, сумма
    покупок клиента и весь денежный анализ — поэтому это отдельное быстрое
    действие прямо в списке заказов, а не поле в глубокой форме."""
    bid = _resolve_bid(x_auth, body.business_id)
    value = database.set_order_amount(order_id, bid, body.amount)
    signals.react(bid, "order")
    return {"ok": True, "order_id": order_id, "amount": value}


# Разрешённые статусы заказа
ALLOWED_STATUSES = {"новый", "принят", "выполнен", "отменён"}


class StatusIn(BaseModel):
    status: str
    business_id: int = 0


@app.post("/api/orders/{order_id}/status")
def api_update_status(order_id: int, body: StatusIn, x_auth: str = Header(default="")):
    """Сменить статус заказа — только в своём бизнесе."""
    if body.status not in ALLOWED_STATUSES:
        return {"ok": False, "error": "unknown status"}
    bid = _resolve_bid(x_auth, body.business_id)
    database.update_order_status(order_id, body.status, bid)
    signals.react(bid, "order")
    return {"ok": True, "order_id": order_id, "status": body.status}


# ---------- КЛИЕНТЫ (CRM) ----------

@app.get("/api/clients")
def api_clients(business_id: int = 0, q: str = "", segment: str = "all",
                limit: int = 50, offset: int = 0, x_auth: str = Header(default="")):
    """
    Список клиентов бизнеса: поиск, срез базы и постраничная загрузка.

    Кроме самих строк отдаём то, что владелец спрашивает у базы клиентов на
    самом деле: сколько живых, сколько уснуло и какой средний чек. Все цифры
    считаются по уже существующим заказам и сообщениям — ничего не выдумываем.
    """
    bid = _resolve_bid(x_auth, business_id)
    query = q.strip() or None
    segment = segment if segment in database.SEGMENTS else "all"
    limit = max(1, min(limit, 200))
    items = database.list_clients(bid, query=query, limit=limit,
                                  offset=max(0, offset), segment=segment)
    stats = database.clients_overview(bid, query=query)
    shown = database.count_segment(bid, query=query, segment=segment)
    orders = database.orders_overview(bid)
    paid = orders["with_amount"]
    return {
        "items": items,
        "total": shown,                 # сколько в текущем срезе — для «показать ещё»
        "segment": segment,
        "all_total": stats["total"],    # вся база, независимо от среза
        "with_phone": stats["with_phone"],
        "orders_total": stats["orders_total"],
        "active": database.active_clients(bid),
        "sleeping": database.count_segment(bid, segment="sleeping"),
        "new30": database.count_segment(bid, segment="new"),
        "avg_check": round(orders["turnover"] / paid) if paid else 0,
        "sleeping_days": database.SLEEPING_DAYS,
    }


@app.get("/api/clients/{client_id}/orders")
def api_client_orders(client_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """История заказов одного клиента."""
    return database.get_client_orders(client_id, _resolve_bid(x_auth, business_id))


@app.get("/api/clients/{client_id}/messages")
def api_client_messages(client_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """История переписки одного клиента."""
    return database.get_client_messages(client_id, _resolve_bid(x_auth, business_id))


def _client_facts_text(client, orders, messages):
    """Свести факты о клиенте в текст для модели (что покупает, на сколько, о чём пишет)."""
    lines = [f"Имя: {client.get('name') or 'без имени'}"]
    if client.get("phone"):
        lines.append(f"Телефон: {client['phone']}")
    if client.get("favorite"):
        lines.append(f"Предпочтения: {client['favorite']}")
    if client.get("notes"):
        lines.append(f"Заметки владельца: {client['notes']}")
    lines.append(f"Заказов: {client.get('orders_count', 0)}, "
                 f"сумма покупок: {int(client.get('total_spent') or 0)} руб.")
    if orders:
        lines.append("Заказы:")
        for o in orders[:12]:
            amt = f" — {int(o['amount'])} руб." if o.get("amount") else ""
            lines.append(f"  · {(o.get('text') or '').strip()[:100]}{amt} ({o.get('status') or ''})")
    if messages:
        lines.append("Последние реплики переписки:")
        for m in messages[-12:]:
            who = "клиент" if m.get("role") == "user" else "сотрудник"
            lines.append(f"  {who}: {(m.get('content') or '').strip()[:150]}")
    return "\n".join(lines)


def _ensure_client_summary(bid, client, orders, messages, force=False):
    """Собрать резюме клиента раз в сутки (или принудительно). Возвращает (summary, advice)."""
    import ai
    today = datetime.date.today().isoformat()
    if not force and client.get("summary_day") == today:
        return client.get("ai_summary") or "", client.get("ai_advice") or ""
    if not ai.ai_available() or _ai_locked(bid):
        return client.get("ai_summary") or "", client.get("ai_advice") or ""
    business = database.get_business(bid) or {}
    facts = _client_facts_text(client, orders, messages)
    # Сбой модели (нет денег на ключе, таймаут, 401) НЕ должен ронять карточку
    # клиента: контакты, заказы и переписка важнее резюме. Отдаём прошлое резюме.
    try:
        res = ai.client_summary(business, facts)
    except Exception:
        logging.exception("Резюме клиента не собралось (biz %s, client %s)", bid, client.get("id"))
        return client.get("ai_summary") or "", client.get("ai_advice") or ""
    summary, advice = res.get("summary", ""), res.get("advice", "")
    database.save_client_summary(client["id"], bid, summary, advice, today)
    return summary, advice


@app.get("/api/clients/{client_id}")
def api_client_card(client_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """Полная карточка клиента: контакты, заказы, переписка, сумма, резюме AI."""
    bid = _resolve_bid(x_auth, business_id)
    client = database.get_client(client_id, bid)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    orders = database.get_client_orders(client_id, bid)
    messages = database.get_client_messages(client_id, bid)
    summary, advice = _ensure_client_summary(bid, client, orders, messages)
    client.pop("password", None)
    # Возможности человека — прямо в его карточке. Один человек, несколько
    # возможностей: майский букет и августовская свадьба стоят здесь рядом и
    # видны как две разные истории, а не как одна запутанная переписка.
    return {"client": client, "orders": orders, "messages": messages,
            "leads": [leads.public(l) for l in database.leads_of_client(bid, client_id)],
            "summary": summary, "advice": advice}


@app.post("/api/clients/{client_id}/summary/refresh")
def api_client_summary_refresh(client_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """Пересобрать резюме клиента принудительно."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    client = database.get_client(client_id, bid)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    orders = database.get_client_orders(client_id, bid)
    messages = database.get_client_messages(client_id, bid)
    summary, advice = _ensure_client_summary(bid, client, orders, messages, force=True)
    return {"summary": summary, "advice": advice}


class ClientPatch(BaseModel):
    name: str | None = None
    phone: str | None = None
    birthday: str | None = None
    notes: str | None = None
    favorite: str | None = None
    business_id: int = 0


@app.post("/api/clients/{client_id}")
def api_update_client(client_id: int, body: ClientPatch, x_auth: str = Header(default="")):
    """Обновить карточку клиента — только в своём бизнесе."""
    bid = _resolve_bid(x_auth, body.business_id)
    fields = body.model_dump(exclude={"business_id"}, exclude_none=True)
    database.update_client(client_id, bid, **fields)
    signals.react(bid, "client")   # клиентская база влияет на рекомендации Директора
    return {"ok": True}


# ---------- ВОЗМОЖНОСТИ (лиды) ----------
#
# Обычный ресурс, а не двадцать эндпоинтов: список, карточка, создание, правка
# и смена состояния. Всё остальное про лид уже умеет реестр записей
# (/api/memory/entity/lead) — второй набор форм заводить незачем.

class LeadIn(BaseModel):
    title: str
    interest: str | None = None
    client_id: int | None = None
    value: int | None = None
    source: str | None = None
    business_id: int = 0


class LeadPatch(BaseModel):
    title: str | None = None
    interest: str | None = None
    client_id: int | None = None
    # Пустая строка означает «сумму не знаем» и отличается от «не присылали
    # поле вовсе»: первое стирает значение, второе оставляет как было.
    value: str | None = None
    source: str | None = None
    # Оценку владелец тоже может поставить рукой: он знает про клиента то,
    # чего нет ни в одном сообщении. Дальше правила её не пересчитывают.
    intent: str | None = None
    fit: str | None = None
    priority: str | None = None
    business_id: int = 0


class LeadStatusIn(BaseModel):
    status: str
    lost_reason: str | None = None
    order_id: int | None = None
    # Сумма сделки. Нужна только для «купил»: заявка, которая заводится вместе
    # с решением, должна нести настоящие деньги, а не оценку возможности.
    amount: str | int | float | None = None
    business_id: int = 0


def _lead_dicts():
    """Словари состояний, причин и уровней — фронт не должен знать их наизусть."""
    return {
        "statuses": [{"key": s, "title": leads.STATUS_RU[s],
                      "hint": leads.STATUS_HINT.get(s, ""),
                      "open": s in leads.OPEN} for s in leads.STATUSES],
        "reasons": [{"key": k, "title": v} for k, v in leads.LOST_REASONS.items()],
        "sources": [{"key": s, "title": entities.SOURCE_RU.get(s, s)}
                    for s in leads.SOURCES],
        "levels": {
            "intent": [{"key": k, "title": qualify.INTENT_RU[k],
                        "hint": qualify.INTENT_HINT.get(k, "")}
                       for k in qualify.INTENT_ORDER],
            "fit": [{"key": k, "title": qualify.FIT_RU[k],
                     "hint": qualify.FIT_HINT.get(k, "")}
                    for k in qualify.FIT_ORDER],
            "priority": [{"key": k, "title": qualify.PRIORITY_RU[k]}
                         for k in qualify.PRIORITY_ORDER],
        },
    }


def _leads_public(bid, rows):
    """
    Возможности наружу вместе с оценкой.

    Разрыв «клиент написал — бизнес молчит» спрашиваем ОДНИМ запросом на всю
    страницу: поодиночке это полсотни походов в базу ради двух дат.
    """
    gaps = database.last_exchanges(bid, [r.get("client_id") for r in rows])
    # Живое касание — тоже одним запросом на страницу и по той же причине.
    fus = database.followups_of_leads(bid, [r.get("id") for r in rows])
    out = []
    for row in rows:
        item = leads.public(row, gap=gaps.get(row.get("client_id")))
        item["f"] = followup.public(fus.get(row.get("id")))
        out.append(item)
    return out


@app.get("/api/leads")
def api_leads(business_id: int = 0, status: str = "", limit: int = 50,
              offset: int = 0, x_auth: str = Header(default="")):
    """
    Воронка бизнеса: сами возможности, оценка каждой и цифры по всем.

    Открытые отдаются в порядке «кому отвечать первым», а не по дате: список,
    отсортированный по времени, отвечает на вопрос «что случилось недавно», а
    владельцу нужен ответ на «где я теряю деньги прямо сейчас».
    """
    bid = _resolve_bid(x_auth, business_id)
    status = status if (status in leads.STATUSES or status == "open") else None
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    if status in (None, "open"):
        # Сортировка по важности возможна только над всем срезом: приоритет
        # считается в момент показа, и в SQL его нет. Берём срез целиком (он у
        # малого бизнеса невелик) и режем на страницы уже здесь.
        rows = database.list_leads(bid, status=status, limit=500, offset=0)
        items = _leads_public(bid, rows)
        items.sort(key=lambda i: qualify.rank(i.get("q") or {}), reverse=True)
        page = items[offset:offset + limit]
    else:
        rows = database.list_leads(bid, status=status, limit=limit, offset=offset)
        page = _leads_public(bid, rows)
        items = None

    stats = leads.overview(bid)
    # Сколько возможностей требуют внимания прямо сейчас. Считается по тем же
    # правилам, что и приоритет в карточке: второго счётчика с другой логикой
    # в системе быть не должно.
    if items is not None:
        open_items = [i for i in items if i.get("open")]
    else:
        open_items = _leads_public(bid, database.list_leads(bid, status="open",
                                                            limit=500))
    stats["attention"] = sum(
        1 for i in open_items
        if (i.get("q") or {}).get("priority") in (qualify.URGENT, qualify.HIGH))
    stats["waiting"] = sum(1 for i in open_items if (i.get("q") or {}).get("unanswered"))
    stats["followup"] = followup.overview(bid)
    stats["actions"] = actions.overview(bid)

    out = {"items": page, "stats": stats,
           "total": database.count_leads(bid, status=status)}
    out.update(_lead_dicts())
    return out


@app.get("/api/leads/{lead_id}")
def api_lead_card(lead_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """
    Карточка возможности: сама она, переписка, из которой выросла, и история.

    Переписка не копия: она берётся из messages по границам разговора. Поэтому
    ответ на вопрос «почему не сложилось» — это те же слова, которые клиент
    писал на самом деле.
    """
    bid = _resolve_bid(x_auth, business_id)
    lead = database.get_lead(lead_id, bid)
    if not lead:
        raise HTTPException(status_code=404, detail="Возможность не найдена")
    out = {"lead": leads.public(lead),
           "messages": database.lead_messages(bid, lead_id),
           "history": database.memory_links(bid, "lead", lead_id),
           "order": database.get_order(lead["order_id"], bid) if lead.get("order_id") else None,
           # Касания — часть жизни возможности, а не отдельная сущность рядом:
           # владелец смотрит на неё в одном месте и решает в одном месте.
           "followups": [followup.public(f) for f in
                         database.list_followups(bid, lead_id=lead_id, limit=20)],
           # Что VELOR делал по этой возможности и что ему не дали сделать.
           # Отдельная страница журнала отвечает на вопрос «что было вообще»;
           # здесь нужен ответ на «что было по этому человеку», и гонять за
           # ним владельца в другой раздел незачем.
           "actions": [actions.public(a) for a in
                       database.list_actions(bid, target_type="lead",
                                             target_id=lead_id, limit=20)]}
    out.update(_lead_dicts())
    return out


@app.post("/api/leads")
def api_lead_add(body: LeadIn, x_auth: str = Header(default="")):
    """
    Завести возможность руками.

    Ради встречи в офлайне не нужно выдумывать сообщение от клиента: владелец
    просто описывает, кого встретил и чего человек хочет.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Нужно описать, чего хочет человек")
    if body.client_id and not database.get_client(body.client_id, bid):
        raise HTTPException(status_code=404, detail="Такого клиента у вас нет")
    actor, actor_id = _actor(x_auth, bid)
    lead_id = leads.create(
        bid, title=title[:200], interest=(body.interest or "").strip()[:2000] or None,
        client_id=body.client_id, value=body.value or None,
        currency="RUB" if body.value else None,
        source=(body.source if body.source in leads.SOURCES else "manual"),
        actor=actor, actor_id=actor_id, note="Заведено вручную",
        owner_fields=[k for k, v in (("title", title), ("interest", body.interest),
                                     ("value", body.value)) if v])
    return {"ok": True, "id": lead_id, "lead": leads.public(database.get_lead(lead_id, bid))}


@app.post("/api/leads/{lead_id}")
def api_lead_update(lead_id: int, body: LeadPatch, x_auth: str = Header(default="")):
    """Правка владельца. Всё, чего он коснулся, автоматика больше не меняет."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    if not database.get_lead(lead_id, bid):
        raise HTTPException(status_code=404, detail="Возможность не найдена")
    fields = body.model_dump(exclude={"business_id"}, exclude_unset=True)
    if fields.get("client_id") and not database.get_client(fields["client_id"], bid):
        raise HTTPException(status_code=404, detail="Такого клиента у вас нет")
    actor, actor_id = _actor(x_auth, bid)
    try:
        lead = leads.owner_update(bid, lead_id, fields, actor=actor, actor_id=actor_id)
    except leads.LeadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "lead": leads.public(lead)}


@app.post("/api/leads/{lead_id}/status")
def api_lead_status(lead_id: int, body: LeadStatusIn, x_auth: str = Header(default="")):
    """
    Отметить, чем кончилось. «Купил» связывается с существующей заявкой, «не
    сложилось» требует причины — иначе проигрыш ничего не объясняет.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    if not database.get_lead(lead_id, bid):
        raise HTTPException(status_code=404, detail="Возможность не найдена")
    if body.status not in leads.STATUSES:
        raise HTTPException(status_code=400, detail="Неизвестное состояние")
    if body.order_id and not database.get_order(body.order_id, bid):
        raise HTTPException(status_code=404, detail="Такой заявки у вас нет")
    actor, actor_id = _actor(x_auth, bid)
    try:
        if body.status == leads.WON:
            # «Купил» означает, что заявка существует. Если её ещё нет, она
            # заводится здесь же — из того, что записано в возможности.
            # Иначе конверсия росла бы, а оборот стоял на месте.
            lead = leads.win(bid, lead_id, order_id=body.order_id,
                             amount=body.amount, actor=actor, actor_id=actor_id)
        else:
            lead = leads.set_status(bid, lead_id, body.status,
                                    reason=body.lost_reason,
                                    order_id=body.order_id, actor=actor,
                                    actor_id=actor_id)
    except leads.LeadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "lead": leads.public(lead),
            "order_id": lead.get("order_id")}


@app.post("/api/leads/{lead_id}/delete")
def api_lead_delete(lead_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """Удалить ошибочную возможность. Клиент и переписка остаются на месте."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    if not database.delete_lead(lead_id, bid):
        raise HTTPException(status_code=404, detail="Возможность не найдена")
    return {"ok": True}


# ---------- КАСАНИЯ (follow-up) ----------
#
# Отдельных страниц у касаний нет и не нужно: касание — это шаг в жизни
# возможности, и решается оно там же, где на возможность смотрят.


class FollowupText(BaseModel):
    text: str
    business_id: int = 0


def _followup_or_404(bid, followup_id):
    row = database.get_followup(followup_id, bid)
    if not row:
        raise HTTPException(status_code=404, detail="Касание не найдено")
    return row


@app.get("/api/followups")
def api_followups(business_id: int = 0, status: str = "", limit: int = 50,
                  x_auth: str = Header(default="")):
    """
    Очередь касаний: что готово, что запланировано, что остановлено и почему.
    """
    bid = _resolve_bid(x_auth, business_id)
    kwargs = {}
    if status == "live":
        kwargs["live"] = True
    elif status in database.FOLLOWUP_STATUSES:
        kwargs["status"] = status
    rows = database.list_followups(bid, limit=max(1, min(limit, 200)), **kwargs)
    items = []
    for row in rows:
        item = followup.public(row)
        lead = database.get_lead(row["lead_id"], bid)
        item["lead_title"] = (lead or {}).get("title") or ""
        item["client_name"] = (lead or {}).get("client_name") or ""
        items.append(item)
    return {"items": items, "stats": followup.overview(bid),
            "reasons": [{"key": k, "title": followup.REASON_RU[k]}
                        for k in followup.REASONS],
            "max_attempts": followup.MAX_ATTEMPTS,
            "hours": followup.work_hours(bid)}


@app.post("/api/leads/{lead_id}/followup")
def api_followup_prepare(lead_id: int, business_id: int = 0,
                         x_auth: str = Header(default="")):
    """
    Подготовить касание прямо сейчас.

    Ничего не отправляет. Если повода нет — так и отвечает: выдумывать причину
    написать человеку только потому, что владелец нажал кнопку, нельзя.
    """
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    lead = database.get_lead(lead_id, bid)
    if not lead:
        raise HTTPException(status_code=404, detail="Возможность не найдена")
    row = followup.plan(bid, lead)
    if not row:
        return {"ok": True, "followup": None,
                "why": "Повода написать сейчас нет — VELOR не придумывает его."}
    return {"ok": True, "followup": followup.public(row)}


@app.post("/api/followups/{followup_id}/edit")
def api_followup_edit(followup_id: int, body: FollowupText,
                      x_auth: str = Header(default="")):
    """Владелец переписал текст своими словами."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    _followup_or_404(bid, followup_id)
    try:
        row = followup.edit(bid, followup_id, body.text)
    except followup.FollowupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "followup": followup.public(row)}


@app.post("/api/followups/{followup_id}/approve")
def api_followup_approve(followup_id: int, business_id: int = 0,
                         x_auth: str = Header(default="")):
    """«Можно писать» — но отправкой это ещё не становится."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    _followup_or_404(bid, followup_id)
    try:
        row = followup.approve(bid, followup_id)
    except followup.FollowupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "followup": followup.public(row)}


@app.post("/api/followups/{followup_id}/send")
def api_followup_send(followup_id: int, business_id: int = 0,
                      x_auth: str = Header(default="")):
    """
    Отправить сейчас — рукой владельца.

    Это единственная отправка, которая не спрашивает разрешения автономии:
    разрешение и есть нажатие. Все остальные проверки — живая ли возможность,
    не ответил ли клиент, пропустит ли канал — остаются на месте.
    """
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    _followup_or_404(bid, followup_id)
    actor, actor_id = _actor(x_auth, bid)
    try:
        row = followup.send(bid, followup_id, auto=False, actor=actor,
                            actor_id=actor_id)
    except followup.FollowupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    out = {"ok": row["status"] == database.FU_SENT,
           "followup": followup.public(row)}
    if not out["ok"]:
        out["why"] = row.get("stop_reason") or row.get("error") or "не отправлено"
    return out


@app.post("/api/followups/{followup_id}/cancel")
def api_followup_cancel(followup_id: int, business_id: int = 0,
                        x_auth: str = Header(default="")):
    """«Не писать». Отмена записывается как решение, а не как отсутствие его."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    _followup_or_404(bid, followup_id)
    try:
        row = followup.cancel(bid, followup_id)
    except followup.FollowupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "followup": followup.public(row)}



# ---------- АВТОНОМНОСТЬ И ЖУРНАЛ ДЕЙСТВИЙ ----------
#
# Три вопроса владельца в одном месте: что VELOR может делать сам, что сейчас
# ждёт моего решения и что он уже сделал. Отдельной системы под это не
# заводится: настройка живёт в существующей `ai_policy`, а очередь решений —
# это те же записи журнала, у которых состояние «ждёт».


class AutonomyIn(BaseModel):
    action: str | None = None
    mode: str
    business_id: int = 0


class ActionText(BaseModel):
    text: str | None = None
    business_id: int = 0


@app.get("/api/autonomy")
def api_autonomy(business_id: int = 0, x_auth: str = Header(default="")):
    """Что VELOR вправе делать сам — и почему у некоторых действий потолок."""
    bid = _resolve_bid(x_auth, business_id)
    return {**actions.settings(bid), "overview": actions.overview(bid)}


@app.post("/api/autonomy")
def api_autonomy_set(body: AutonomyIn, x_auth: str = Header(default="")):
    """
    Изменить полномочия: одно действие или все разом.

    «Разрешить всё» не отменяет ограничений безопасности — оно поднимает каждое
    действие настолько, насколько тому вообще позволено. Запись денег остаётся
    на подтверждении, сколько бы раз кнопку ни нажали.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    who, who_id = _actor(x_auth, bid)
    try:
        if body.action:
            actions.set_mode(bid, body.action, body.mode, actor=who, actor_id=who_id)
        else:
            actions.set_all(bid, body.mode, actor=who, actor_id=who_id)
    except actions.ActionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **actions.settings(bid), "overview": actions.overview(bid)}


@app.get("/api/actions")
def api_actions(business_id: int = 0, kind: str = "all", limit: int = 80,
                x_auth: str = Header(default="")):
    """Журнал: что VELOR сделал, чего ему не дали и что не получилось."""
    bid = _resolve_bid(x_auth, business_id)
    return {"items": actions.feed(bid, kind=kind, limit=max(1, min(limit, 200))),
            "pending": actions.pending(bid),
            "overview": actions.overview(bid),
            "filters": [{"key": k, "title": actions.FILTER_RU[k]}
                        for k in actions.FILTERS]}


@app.get("/api/actions/{action_id}")
def api_action_one(action_id: int, business_id: int = 0,
                   x_auth: str = Header(default="")):
    """Одно действие целиком: на чём основано, что было и что стало."""
    bid = _resolve_bid(x_auth, business_id)
    row = database.get_action(action_id, bid)
    if not row:
        raise HTTPException(status_code=404, detail="Действие не найдено")
    return {"action": actions.public(row)}


@app.post("/api/actions/{action_id}/approve")
def api_action_approve(action_id: int, body: ActionText,
                       x_auth: str = Header(default="")):
    """
    «Разрешить» — это разрешение на ЭТО действие, а не на все такие впредь.

    Постоянное разрешение живёт на странице автономности. Смешивать одно с
    другим нельзя: нажатие под конкретным сообщением не должно однажды
    означать «пиши всем клиентам всегда».
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    who, who_id = _actor(x_auth, bid)
    if not database.get_action(action_id, bid):
        raise HTTPException(status_code=404, detail="Действие не найдено")
    payload = {"message": body.text.strip()} if (body.text or "").strip() else None
    try:
        row = actions.approve(bid, action_id, actor=who, actor_id=who_id,
                              payload=payload)
    except actions.ActionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    said = actions.public(row)
    return {"ok": row["status"] == database.AC_SUCCEEDED, "action": said,
            "why": said.get("error") or said.get("result") or ""}


@app.post("/api/actions/{action_id}/reject")
def api_action_reject(action_id: int, body: ActionText,
                      x_auth: str = Header(default="")):
    """«Не надо». Отказ — тоже решение, и он тоже остаётся в журнале."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    who, who_id = _actor(x_auth, bid)
    if not database.get_action(action_id, bid):
        raise HTTPException(status_code=404, detail="Действие не найдено")
    row = actions.reject(bid, action_id, actor=who, actor_id=who_id)
    return {"ok": True, "action": actions.public(row)}


@app.post("/api/actions/{action_id}/cancel")
def api_action_cancel(action_id: int, body: ActionText,
                      x_auth: str = Header(default="")):
    """Снять предложение, не отказывая: повод исчез сам."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    if not database.get_action(action_id, bid):
        raise HTTPException(status_code=404, detail="Действие не найдено")
    row = actions.cancel(bid, action_id, reason="Снято.")
    return {"ok": True, "action": actions.public(row)}


# ---------- НАХОДКИ VELOR ----------
#
# Отдельной «панели наблюдения» здесь нет намеренно. Находка — не отчёт о
# проделанной работе, а повод что-то сделать; поэтому она живёт там же, где
# владелец решает: на главной, рядом с тем, что уже требует внимания.


class InitiativeIn(BaseModel):
    business_id: int = 0


@app.get("/api/initiatives")
def api_initiatives(business_id: int = 0, all: bool = False, limit: int = 30,
                    x_auth: str = Header(default="")):
    """
    Что VELOR заметил. Открытые — сверху и по цене вопроса, а не по времени.

    Показ отмечается здесь же: находка, которую владелец увидел, перестаёт
    быть новостью. «Увидел» при этом не значит «разобрался» — она остаётся
    живой, пока не исчезнет сам сигнал.
    """
    bid = _resolve_bid(x_auth, business_id)
    items = initiatives.feed(bid, only_open=not all, limit=max(1, min(limit, 100)))
    for row in items:
        if row["fresh"]:
            try:
                initiatives.seen(bid, row["id"])
            except Exception:
                logging.exception("Отметка о показе не сохранилась (biz %s)", bid)
    return {"items": items, "overview": initiatives.overview(bid),
            "types": [{"key": k, "title": m["title"], "group": m["group"]}
                      for k, m in initiatives.TYPES.items()]}


@app.get("/api/initiatives/{initiative_id}")
def api_initiative_one(initiative_id: int, business_id: int = 0,
                       x_auth: str = Header(default="")):
    """Одна находка целиком: на чём основана, что предлагается, чем кончилось."""
    bid = _resolve_bid(x_auth, business_id)
    row = database.get_initiative(initiative_id, bid)
    if not row:
        raise HTTPException(status_code=404, detail="Находка не найдена")
    return {"initiative": initiatives.public(row)}


@app.post("/api/initiatives/scan")
def api_initiatives_scan(body: InitiativeIn, x_auth: str = Header(default="")):
    """Посмотреть прямо сейчас, не дожидаясь обхода."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    got = initiatives.scan(bid)
    return {"ok": True, "scan": got, "items": initiatives.feed(bid, limit=30),
            "overview": initiatives.overview(bid)}


@app.post("/api/initiatives/{initiative_id}/ack")
def api_initiative_ack(initiative_id: int, body: InitiativeIn,
                       x_auth: str = Header(default="")):
    """«Понял». Не «решил»: сигнал остаётся, пока не исчезнет сам."""
    bid = _resolve_bid(x_auth, body.business_id)
    try:
        row = initiatives.acknowledge(bid, initiative_id)
    except initiatives.InitiativeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "initiative": initiatives.public(row)}


@app.post("/api/initiatives/{initiative_id}/dismiss")
def api_initiative_dismiss(initiative_id: int, body: InitiativeIn,
                           x_auth: str = Header(default="")):
    """«Не интересно». Замолкаем — но не навсегда, если картина изменится."""
    bid = _resolve_bid(x_auth, body.business_id)
    try:
        row = initiatives.dismiss(bid, initiative_id)
    except initiatives.InitiativeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "initiative": initiatives.public(row)}


@app.post("/api/initiatives/{initiative_id}/act")
def api_initiative_act(initiative_id: int, body: InitiativeIn,
                       x_auth: str = Header(default="")):
    """
    Сделать то, что находка предлагает.

    Находка не даёт прав. Каждое действие идёт обычным путём — реестр,
    полномочия, исполнитель, журнал, — и то, что предложил его VELOR, ничего
    в этом пути не меняет.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    try:
        got = initiatives.act(bid, initiative_id)
    except initiatives.InitiativeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **got, "actions_overview": actions.overview(bid)}


# ---------- НАСТРОЙКИ БИЗНЕСА ----------

@app.get("/api/business")
def api_get_business(business_id: int = 0, x_auth: str = Header(default="")):
    """Настройки бизнеса — для его страницы «Настройки»."""
    b = database.get_business(_resolve_bid(x_auth, business_id)) or {}
    b.pop("password", None)   # пароль наружу не отдаём
    return b


class BusinessPatch(BaseModel):
    name: str | None = None
    about: str | None = None
    greeting: str | None = None
    tg_bot_token: str | None = None    # свой бот бизнеса (вставляет в настройках)
    knowledge: str | None = None       # база знаний: прайс, услуги, условия
    tone: str | None = None            # стиль общения: professional / friendly / strict
    ai_name: str | None = None         # имя AI-сотрудника (личность)
    ai_avatar: str | None = None       # символ аватара
    ai_traits: str | None = None       # черты характера через запятую
    ai_desc: str | None = None         # описание характера своими словами
    business_id: int = 0


@app.post("/api/business")
def api_update_business(body: BusinessPatch, x_auth: str = Header(default="")):
    """Сохранить настройки бизнеса — VELOR AI сразу подстроится под него."""
    bid = _resolve_bid(x_auth, body.business_id)
    fields = body.model_dump(exclude={"business_id"}, exclude_none=True)
    try:
        database.update_business(bid, **fields)
    except database.DuplicateError as e:
        # Чаще всего это токен бота, уже привязанный к другой компании. Пустить
        # такое нельзя: клиенты чужого бота начали бы попадать в эту панель.
        raise HTTPException(status_code=409, detail=_DUPLICATE_TEXT[e.field])
    # Завершение настройки = запуск полноценного Trial. Если бизнес ещё в онбординге
    # (отсчёт 14 дней не шёл — раньше это давало бессрочный бесплатный доступ), стартуем
    # триал через TrialService. launch идемпотентен и наполняет trial_registry/owner_identity,
    # поэтому совместимость с триалом, реестром и Telegram-верификацией сохранена.
    try:
        if trial.access(database.get_business(bid))["phase"] == "onboarding":
            trial.launch(bid)
    except Exception:
        logging.exception("Не удалось запустить триал по завершении настройки (biz %s)", bid)
    # Сохранили токен бота → сразу подключаем webhook, чтобы клиенты писали в кабинет
    # без отдельного процесса. Best-effort: если не вышло — настройки всё равно сохранены.
    webhook = None
    tok = (fields.get("tg_bot_token") or "").strip()
    if tok and _public_base():
        try:
            webhook = bool((set_webhook_for(tok) or {}).get("ok"))
        except Exception:
            logging.exception("Не удалось поставить webhook при сохранении токена (biz %s)", bid)
    return {"ok": True, "webhook": webhook}


@app.get("/api/series")
def api_series(business_id: int = 0, days: int = 30, x_auth: str = Header(default="")):
    """
    Дневной ряд, средний чек и разделение клиентов на новых и вернувшихся.

    Всё считает база, ни одного обращения к модели: график — это доказательство
    числа, стоящего над ним, а доказательство не может быть предположением.
    Поэтому ряд считается теми же выражениями, что и сводка Директора, и по
    тем же правилам — иначе линия начнёт спорить с цифрой.

    Там, где посчитать честно нельзя, число не выдумывается: вместо него в
    gaps идёт объяснение, чего именно не хватает. Это тот же механизм, которым
    молчит Директор.
    """
    bid = _resolve_bid(x_auth, business_id)
    days = 7 if days <= 7 else (90 if days >= 90 else 30)
    try:
        series = database.daily_series(bid, days)
        now = database.orders_period(bid, days, 0)
        prev = database.orders_period(bid, days, days)
        who_now = database.clients_split(bid, days, 0)
        who_prev = database.clients_split(bid, days, days)
        span = database.data_span(bid)
    except Exception:
        logging.exception("Ряд не собрался (biz %s)", bid)
        raise HTTPException(status_code=502, detail="Не удалось собрать ряд.")

    gaps = []

    # Средний чек. База — только заявки с проставленной суммой и не отменённые:
    # отменённая заявка денег не принесла, а заявка без суммы ничего не говорит
    # о чеке. Сколько заявок осталось за бортом — пишем прямо.
    avg_now, avg_prev = now["avg"], prev["avg"]
    no_amount = now["count"] - now["cancelled"] - now["with_amount"]
    aov = {
        "value": avg_now,
        "was": avg_prev,
        "change": (round((avg_now - avg_prev) * 100 / avg_prev)
                   if avg_now and avg_prev else None),
        "counted": now["with_amount"], "orders": now["count"],
        "cancelled": now["cancelled"], "no_amount": max(0, no_amount),
        "source": (f"{now['with_amount']} из {now['count']} заявок за {days} дн. "
                   "с проставленной суммой"),
    }
    if avg_now is None and now["count"]:
        gaps.append(f"Средний чек не посчитан: у заявок за {days} дн. не проставлена "
                    "сумма. Проставьте её в разделе «Заявки» — и чек появится сам.")
    elif no_amount > 0 and avg_now is not None:
        gaps.append(f"Средний чек посчитан не по всем заявкам: у {no_amount} из "
                    f"{now['count']} сумма не проставлена.")
    if now["cancelled"]:
        gaps.append(f"Отменённые заявки ({now['cancelled']}) в деньгах не учтены: "
                    "отмена не принесла выручки.")

    # Новые и вернувшиеся. Вернувшийся — тот, у кого есть заказ раньше начала
    # периода. Это расчёт по базе, а не мнение модели.
    who = {
        "new": who_now["new"], "returning": who_now["returning"],
        "active": who_now["active"],
        "was_new": who_prev["new"], "was_returning": who_prev["returning"],
        "source": (f"{who_now['active']} клиентов с заявками за {days} дн.; "
                   "вернувшийся — тот, у кого заказ был и раньше"),
    }
    if not who_now["active"] and now["count"]:
        gaps.append("Разделить клиентов на новых и вернувшихся нельзя: заявки за "
                    "период не привязаны к клиентам.")
    if span["days"] < days and who_now["returning"] == 0 and who_now["active"]:
        gaps.append(f"Вернувшихся пока нет и быть не может: данные в системе всего "
                    f"{span['days']} дн. — раньше периода заказов ещё не было.")

    return {"days": days, "series": series, "aov": aov, "clients": who,
            "gaps": gaps, "span": span}


@app.get("/api/director")
def api_director(business_id: int = 0, days: int = 30, x_auth: str = Header(default="")):
    """
    Что происходит с бизнесом: исполнительная сводка и разбор.

    Ни одного обращения к модели: каждая цифра приходит запросом к базе, и у
    каждой указано, из чего она сложилась. Там, где данных мало, Директор
    молчит и говорит об этом прямо — выдуманная аналитика опаснее пустой.
    """
    bid = _resolve_bid(x_auth, business_id)
    days = 7 if days <= 7 else (90 if days >= 90 else 30)
    try:
        return director.briefing(bid, days)
    except Exception:
        logging.exception("Директор не собрался (biz %s)", bid)
        raise HTTPException(status_code=502, detail="Не удалось собрать сводку.")


@app.get("/api/stats")
def api_stats(business_id: int = 0, x_auth: str = Header(default="")):
    """Аналитика пользы VELOR: обработано сообщений, заказов, сэкономлено времени."""
    bid = _resolve_bid(x_auth, business_id)
    s = database.business_stats(bid)
    # оценка сэкономленного времени: ~2 мин на обработанное сообщение клиента
    s["minutes_saved"] = s["messages"] * 2
    return s


# ---------- ГЛАВНАЯ: всё одним запросом ----------

def _slot(text, note="", href="", kind=""):
    """Одна мысль VELOR для главной: что сказать, почему и куда вести."""
    text = (text or "").strip()
    return {"text": text, "note": (note or "").strip(), "href": href, "kind": kind} if text else None


def _velor_says(bid, sig, totals, risks, opps, advice, business):
    """
    Четыре слота блока «Что говорит VELOR»: наблюдение, проблема, рекомендация,
    возможность.

    Ни одного обращения к ИИ: берём то, что уже посчитано (тренды) и уже
    сохранено (риски, совет директора, дневник). Главная обязана открываться
    мгновенно, а мысль на ней — быть той же, что и в разделах, иначе владелец
    видит два разных мнения об одном бизнесе.

    Слоты не повторяются: одна и та же фраза не займёт два места — вместо
    дубля берём следующую по важности.
    """
    cur, ch = sig["current"], sig["change"]
    used = set()

    def take(slot):
        if not slot:
            return None
        key = slot["text"].strip().lower()
        if key in used:
            return None
        used.add(key)
        return slot

    # ── проблема: первым делом то, что уже признано риском ──
    problem = take(_slot(risks[0]["title"], risks[0]["why"], "risks.html", "риск")) if risks else None
    if not problem:
        money = signals.top_insight(bid)          # живое денежное следствие, без ИИ
        problem = take(_slot(money["text"], money["note"], money["href"], "финансы")) if money else None
    if not problem and sig["stale_orders"]:
        problem = take(_slot(
            f"Заявки ждут ответа: {sig['stale_orders']}",
            "Висят дольше трёх дней. Каждый день ожидания — клиент, который уходит к другим.",
            "orders.html", "заявки"))

    # ── наблюдение: факт о бизнесе, а не оценка. Сначала деньги, потом спрос ──
    observation = None
    for slot in (
        _slot(f"Прибыль за 30 дней: {cur['profit']:,} ₽".replace(",", " "),
              f"Доход {cur['income']:,} ₽, расход {cur['expense']:,} ₽.".replace(",", " ")
              + (f" К прошлому месяцу {ch['profit']:+d}%." if ch.get("profit") is not None else
                 " Сравнить пока не с чем — это первый месяц данных."),
              "finance.html", "деньги") if (cur["income"] or cur["expense"]) else None,
        _slot(f"Заявок за 30 дней: {cur['orders']}",
              f"Всего в работе {totals['total']}, из них новых {totals['new']}."
              + (f" К прошлому месяцу {ch['orders']:+d}%." if ch.get("orders") is not None else ""),
              "orders.html", "спрос") if cur["orders"] else None,
        _slot(f"Обращений от клиентов: {cur['messages']}",
              "Столько раз к вам написали за 30 дней — на все ответил VELOR.",
              "clients.html", "спрос") if cur["messages"] else None,
        # Данных за месяц нет вовсе — говорим об этом прямо, а не показываем ноль
        # как достижение. Ноль в красивой рамке выглядит как сломанный экран.
        _slot("Данных за последний месяц пока нет",
              f"В базе {database._plural(totals['total'], 'заявка', 'заявки', 'заявок')}"
              " — движения за 30 дней не было. Как только появится, я начну считать тренды."
              if totals["total"] else
              "Ни заявок, ни обращений, ни денег. Наполните VELOR — и я начну считать за вас.",
              "orders.html" if totals["total"] else "guide.html", "старт"),
    ):
        observation = take(slot)
        if observation:
            break

    # ── рекомендация: что сделать. Совет директора старше дневника ──
    recommendation = None
    board = database.list_board_recs(bid, limit=1)
    if board:
        recommendation = take(_slot(board[0]["problem"], board[0].get("effect") or board[0].get("why") or "",
                                    "board.html", "совет директора"))
    if not recommendation and advice:
        recommendation = take(_slot(advice, "Вывод из вчерашнего дневника.", "journal.html", "дневник"))
    if not recommendation and totals["new"]:
        recommendation = take(_slot(
            f"Разберите {database._plural(totals['new'], 'новую заявку', 'новые заявки', 'новых заявок')}",
            "Пока заявка не в работе, она не приносит денег.", "orders.html", "заявки"))
    if not recommendation and not (business.get("knowledge") or "").strip():
        recommendation = take(_slot(
            "Расскажите VELOR о компании",
            "Пока база знаний пуста, сотрудник отвечает клиентам общими словами.",
            "memory.html", "настройка"))

    opportunity = take(_slot(opps[0]["title"], opps[0]["why"], "opportunities.html",
                             "возможность")) if opps else None

    return {"observation": observation, "problem": problem,
            "recommendation": recommendation, "opportunity": opportunity}


def _safe_director(bid: int):
    """Сводка Директора для главной. Упал — главная всё равно открывается."""
    try:
        return director.briefing(bid)
    except Exception:
        logging.exception("Директор не собрался для главной (biz %s)", bid)
        return None


def _attention(bid, sig, totals, risks, business):
    """
    «Что требует внимания» — список дел, а не наблюдений. Каждая строка ведёт
    туда, где её можно закрыть, и появляется только если действительно есть.
    """
    items = []

    # Действия, которые VELOR подготовил и ждёт разрешения. Стоят первыми не
    # ради важности: пока владелец не ответил, работа остановлена, и это
    # единственная строка списка, где ждут не клиента и не деньги, а его.
    try:
        waiting = actions.overview(bid).get("waiting") or 0
    except Exception:
        waiting = 0
    if waiting:
        items.append({"title": "Требуют вашего решения: " + database._plural(
            waiting, "действие", "действия", "действий"),
            "note": "VELOR подготовил и ждёт — сам он этого не сделает.",
            "href": "autonomy.html", "level": "urgent"})

    # То, что VELOR заметил сам. Стоит сразу за очередью решений и прежде
    # заявок: заявку владелец видит и без подсказки, а «шесть горячих клиентов
    # ждут пятый час» — ровно то, на что у него не хватает глаз.
    try:
        items += initiatives.attention(bid)
    except Exception:
        logging.exception("Находки не попали в список внимания (biz %s)", bid)

    # _plural сам подставляет число, поэтому дописывать его отдельно нельзя.
    if sig["stale_orders"]:
        items.append({"title": database._plural(sig["stale_orders"], "заявка ждёт", "заявки ждут",
                                                "заявок ждут") + " дольше трёх дней",
                      "note": "Клиент считает, что о нём забыли.",
                      "href": "orders.html", "level": "urgent"})

    if totals["new"]:
        items.append({"title": "Ждёт разбора: " + database._plural(
            totals["new"], "новая заявка", "новые заявки", "новых заявок"),
            "note": "Пока заявка не в работе, она не приносит денег.",
            "href": "orders.html", "level": "warn"})

    # Заказы без суммы — прямая дыра в обороте и среднем чеке.
    no_amount = max(0, totals["total"] - totals.get("with_amount", totals["total"]))
    if no_amount:
        items.append({"title": "Без суммы: " + database._plural(
            no_amount, "заявка", "заявки", "заявок"),
            "note": "Пока сумма не проставлена, оборот и средний чек занижены.",
            "href": "orders.html", "level": "info"})

    # Источники, которые перестали отдавать данные.
    try:
        broken = [c for c in database.list_connections(bid) if c.get("status") == "error"]
    except Exception:
        broken = []
    for c in broken[:2]:
        items.append({"title": f"Источник «{c.get('provider')}» не отвечает",
                      "note": (c.get("last_error") or "")[:160],
                      "href": "integrations.html", "level": "warn"})

    if len(risks) > 1:
        items.append({"title": f"Рисков без решения: {len(risks)}",
                      "note": "VELOR отметил их, но вы ещё не разобрали.",
                      "href": "risks.html", "level": "warn"})

    if not (business.get("knowledge") or "").strip():
        items.append({"title": "База знаний пуста",
                      "note": "Сотрудник отвечает клиентам общими словами, а не о вашем деле.",
                      "href": "memory.html", "level": "info"})

    if not business.get("tg_bot_token"):
        items.append({"title": "Telegram-бот не подключён",
                      "note": "Клиенты пока не могут написать вашему сотруднику.",
                      "href": "guide.html", "level": "info"})

    order = {"urgent": 0, "warn": 1, "info": 2}
    items.sort(key=lambda i: order.get(i["level"], 3))
    return items[:5]


@app.get("/api/home")
def api_home(business_id: int = 0, x_auth: str = Header(default="")):
    """
    Сводка для главной страницы. Один запрос вместо шести и ни одного обращения
    к ИИ: берём то, что уже посчитано и сохранено, — главная должна открываться мгновенно.
    """
    bid = _resolve_bid(x_auth, business_id)
    business = database.get_business(bid) or {}
    sig = database.risk_signals(bid)
    # Итоги — из базы (COUNT/SUM по всей таблице), а не из обрезанной выборки.
    totals = database.orders_overview(bid)
    orders = database.get_orders(bid, limit=100)

    risks = [r for r in database.list_risks(bid) if r["status"] == "new"]
    opps = [o for o in database.list_opportunities(bid) if o["status"] == "new"]
    journal = database.list_journal(bid, limit=1)
    advice = journal[0]["advice"] if journal and journal[0]["advice"] else ""

    orders_new = [o for o in orders if o.get("status") == "новый"]

    # «Сегодня важно»: одна главная мысль. Сначала то, что горит.
    money_insight = signals.top_insight(bid)   # живое денежное следствие (без ИИ)
    if risks and risks[0]["level"] == 1:
        focus = {"text": risks[0]["title"], "note": risks[0]["why"], "kind": "риск", "href": "risks.html"}
    elif money_insight:
        focus = money_insight
    elif totals["new"]:
        focus = {"text": f"Разберите {database._plural(totals['new'], 'новую заявку', 'новые заявки', 'новых заявок')}",
                 "note": (orders_new[0].get("text") or "")[:140] if orders_new else "",
                 "kind": "заявки", "href": "orders.html"}
    elif advice:
        focus = {"text": advice, "note": "Совет из вчерашнего дневника", "kind": "совет", "href": "journal.html"}
    elif opps:
        focus = {"text": opps[0]["title"], "note": opps[0]["why"], "kind": "возможность",
                 "href": "opportunities.html"}
    elif not (business.get("knowledge") or "").strip():
        focus = {"text": "Расскажите VELOR о компании", "kind": "настройка",
                 "note": "Пока база знаний пуста, сотрудник отвечает клиентам общими словами.",
                 "href": "memory.html"}
    else:
        focus = {"text": "Спокойный день — всё под контролем", "kind": "порядок",
                 "note": "Новых заявок нет, тревожных сигналов тоже.", "href": ""}

    return {
        "business": {"name": business.get("name"), "ai_name": business.get("ai_name"),
                     "ai_avatar": business.get("ai_avatar")},
        "focus": focus,
        "orders": {"new": totals["new"], "today": totals["today"],
                   "total": totals["total"], "turnover": totals["turnover"],
                   "with_amount": totals["with_amount"],
                   "change": sig["change"]["orders"]},
        "money": {"income": sig["current"]["income"], "profit": sig["current"]["profit"],
                  "income_change": sig["change"]["income"], "profit_change": sig["change"]["profit"]},
        "clients": {"new": sig["current"]["clients"], "change": sig["change"]["clients"],
                    # «Активные» — те, кто писал или заказывал за 30 дней. Общее
                    # число клиентов растёт вечно и владельцу ничего не говорит.
                    "active": database.active_clients(bid), "total": database.count_clients(bid)},
        "velor": _velor_says(bid, sig, totals, risks, opps, advice, business),
        "attention": _attention(bid, sig, totals, risks, business),
        # Продажи одной строкой: что пришло, что готово, что вышло. Считается
        # по тем же таблицам, что и воронка, — второго отчёта о продажах в
        # системе нет.
        "sales": database.sales_today(bid),
        # Только счётчик и самое важное: сами карточки главная просит отдельным
        # запросом — они не должны задерживать цифры наверху.
        "initiatives": initiatives.overview(bid),
        "today": datetime.date.today().isoformat(),
        "forecast": signals.forecast(bid),   # прогноз на конец месяца (обновляется с расходами)
        "advice": advice,
        "advice_day": journal[0]["day"] if journal else None,
        "risks": {"count": len(risks), "top": risks[0] if risks else None},
        "opportunities": {"count": len(opps), "top": opps[0] if opps else None},
        "activity": database.list_events(bid, limit=6),
        "health": database.business_health(bid)["score"],
        # Директор считается здесь же: главная должна отвечать на «что
        # происходит с бизнесом» одним запросом, а не пятью.
        "director": _safe_director(bid),
    }


# ---------- РИСКИ ----------

def _risk_text(business, s):
    """Тренды словами + пометки ОПАСНО там, где цифры говорят сами за себя."""
    c, p, ch = s["current"], s["previous"], s["change"]
    fmt = lambda k, name, unit="": (
        f"{name}: {c[k]}{unit} за 30 дней против {p[k]}{unit} в предыдущие 30" +
        (f" ({ch[k]:+d}%)" if ch[k] is not None else " (не с чем сравнить)"))
    lines = [f"Бизнес: {business.get('name') or 'компания'}"]
    if business.get("about"):
        lines.append(f"Чем занимается: {business['about']}")
    lines += [fmt("income", "Доход", " ₽"), fmt("expense", "Расход", " ₽"),
              fmt("profit", "Прибыль", " ₽"), fmt("clients", "Новых клиентов"),
              fmt("orders", "Заявок"), fmt("messages", "Обращений клиентов")]
    if s["top_source"]:
        lines.append(f"Источников дохода: {s['sources']}; главный — «{s['top_source']['category']}», "
                     f"{s['top_source']['share']}% всего дохода")
    if s["top_client_share"] is not None:
        lines.append(f"На одного клиента приходится {s['top_client_share']}% заказов")
    if s["stale_orders"]:
        lines.append(f"Заявок висит без ответа больше 3 дней: {s['stale_orders']}")

    if ch["expense"] is not None and ch["expense"] >= 20:
        lines.append(f"ОПАСНО — расходы выросли на {ch['expense']}%.")
    if ch["profit"] is not None and ch["profit"] <= -20:
        lines.append(f"ОПАСНО — прибыль упала на {abs(ch['profit'])}%.")
    if c["profit"] < 0:
        lines.append("ОПАСНО — за последние 30 дней бизнес отработал в минус.")
    if ch["clients"] is not None and ch["clients"] <= -30:
        lines.append(f"ОПАСНО — новых клиентов стало меньше на {abs(ch['clients'])}%.")
    if ch["messages"] is not None and ch["messages"] <= -30:
        lines.append(f"ОПАСНО — обращений стало меньше на {abs(ch['messages'])}%: спрос падает.")
    if s["top_source"] and s["top_source"]["share"] >= 70 and s["sources"] > 0:
        lines.append(f"ОПАСНО — {s['top_source']['share']}% дохода держится на одном источнике.")
    if s["top_client_share"] is not None and s["top_client_share"] >= 50:
        lines.append(f"ОПАСНО — половина и больше заказов приходится на одного клиента.")
    return "\n".join(lines)


@app.get("/api/risks")
def api_risks(business_id: int = 0, x_auth: str = Header(default="")):
    """Сохранённые риски + сами тренды, чтобы владелец видел цифры."""
    bid = _resolve_bid(x_auth, business_id)
    return {"items": database.list_risks(bid), "signals": database.risk_signals(bid)}


@app.post("/api/risks/scan")
def api_risks_scan(business_id: int = 0, x_auth: str = Header(default="")):
    """Проверить бизнес на риски."""
    import ai
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    if not ai.ai_available():
        return {"ok": False, "error": "ИИ сейчас недоступен"}
    business = database.get_business(bid) or {"name": "VELOR AI"}
    signals = database.risk_signals(bid)
    try:
        items = ai.find_risks(business, _risk_text(business, signals))
    except Exception:
        return {"ok": False, "error": "Не удалось разобрать ответ ИИ — попробуйте ещё раз"}
    if not items:
        return {"ok": False, "error": "ИИ вернул ответ не по форме — попробуйте ещё раз"}
    database.save_risks(bid, items)
    database.log_event(bid, "risk", f"Найдено рисков: {len(items)}",
                       "; ".join(i["title"] for i in items[:3]), level="important")
    return {"ok": True, "items": database.list_risks(bid)}


class RiskStatusIn(BaseModel):
    status: str
    business_id: int = 0


@app.post("/api/risks/{risk_id}/status")
def api_risk_status(risk_id: int, body: RiskStatusIn, x_auth: str = Header(default="")):
    """Убрать риск из списка или вернуть обратно."""
    bid = _resolve_bid(x_auth, body.business_id)
    if body.status not in ("new", "hidden"):
        raise HTTPException(status_code=400, detail="Неизвестный статус")
    database.set_risk_status(risk_id, bid, body.status)
    return {"ok": True}


# ---------- ВОЗМОЖНОСТИ РОСТА ----------

def _signals_text(business, s):
    """Цифры бизнеса словами — на их основе ИИ ищет возможности."""
    lines = [f"Бизнес: {business.get('name') or 'компания'}"]
    if business.get("about"):
        lines.append(f"Чем занимается: {business['about']}")
    lines += [
        f"Доход всего: {s['income']} ₽",
        f"Расход всего: {s['expense']} ₽",
        f"Прибыль: {s['profit']} ₽" + (f" (маржа {s['margin']}%)" if s["margin"] is not None else ""),
        f"Клиентов в базе: {s['clients']}, из них не писали больше 30 дней: {s['sleeping']}",
        f"Клиентов с повторными заказами: {s['repeat_clients']}",
        f"Заявок всего: {s['orders_total']}, из них не разобрано: {s['orders_open']}",
        f"Последняя заявка: {s['last_order'] or 'заявок ещё не было'}",
        f"Сообщений от клиентов за 30 дней: {s['messages_30d']}",
    ]
    if s["top_expense"]:
        lines.append("Крупнейшие расходы: " +
                     ", ".join(f"{e['category']} — {e['total']} ₽" for e in s["top_expense"]))
    if s["top_income"]:
        lines.append("Что приносит доход: " +
                     ", ".join(f"{e['category']} — {e['total']} ₽" for e in s["top_income"]))
    if s.get("losing"):
        lines.append("ТРЕВОЖНО — на этих направлениях тратим больше, чем зарабатываем: " +
                     ", ".join(f"{l['category']}: доход {l['income']} ₽ против расхода {l['expense']} ₽"
                               for l in s["losing"]))
    if s["margin"] is not None and s["margin"] < 15:
        lines.append(f"ТРЕВОЖНО — маржа всего {s['margin']}%: почти весь доход съедают расходы.")
    if s["sleeping"] and s["clients"] and s["sleeping"] / s["clients"] >= 0.5:
        lines.append("ТРЕВОЖНО — половина базы и больше не возвращается.")
    return "\n".join(lines)


@app.get("/api/opportunities")
def api_opportunities(business_id: int = 0, x_auth: str = Header(default="")):
    """Сохранённые возможности роста."""
    bid = _resolve_bid(x_auth, business_id)
    return {"items": database.list_opportunities(bid), "signals": database.growth_signals(bid)}


@app.post("/api/opportunities/scan")
def api_opportunities_scan(business_id: int = 0, x_auth: str = Header(default="")):
    """Пересмотреть бизнес и найти свежие возможности."""
    import ai
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    if not ai.ai_available():
        return {"ok": False, "error": "ИИ сейчас недоступен"}
    business = database.get_business(bid) or {"name": "VELOR AI"}
    signals = database.growth_signals(bid)
    try:
        items = ai.find_opportunities(business, _signals_text(business, signals))
    except Exception:
        return {"ok": False, "error": "Не удалось разобрать ответ ИИ — попробуйте ещё раз"}
    if not items:
        return {"ok": False, "error": "ИИ вернул ответ не по форме — попробуйте ещё раз"}
    database.save_opportunities(bid, items)
    database.log_event(bid, "opportunity", f"Найдено возможностей: {len(items)}",
                       "; ".join(i["title"] for i in items[:3]), level="important")
    return {"ok": True, "items": database.list_opportunities(bid)}


class OppStatusIn(BaseModel):
    status: str
    business_id: int = 0


@app.post("/api/opportunities/{opp_id}/status")
def api_opportunity_status(opp_id: int, body: OppStatusIn, x_auth: str = Header(default="")):
    """Отметить возможность сделанной или убрать её."""
    bid = _resolve_bid(x_auth, body.business_id)
    if body.status not in ("new", "done", "hidden"):
        raise HTTPException(status_code=400, detail="Неизвестный статус")
    database.set_opportunity_status(opp_id, bid, body.status)
    return {"ok": True}


# ---------- ИДЕИ РАЗВИТИЯ ----------

def _generate_ideas(bid):
    """Придумать новую порцию идей и добавить в копилку. Возвращает, сколько добавлено."""
    import ai
    if not ai.ai_available():
        return 0, "ИИ сейчас недоступен"
    business = database.get_business(bid) or {"name": "VELOR AI"}
    signals = database.growth_signals(bid)
    try:
        items = ai.generate_ideas(business, _signals_text(business, signals),
                                  avoid_titles=database.idea_titles(bid))
    except Exception:
        return 0, "Не удалось разобрать ответ ИИ — попробуйте ещё раз"
    if not items:
        return 0, "ИИ вернул ответ не по форме — попробуйте ещё раз"
    added = database.add_ideas(bid, items)
    if added:
        database.log_event(bid, "idea", f"Новые идеи развития: {added}",
                           "; ".join(i["title"] for i in items[:3]))
    return added, None


@app.get("/api/ideas")
def api_ideas(business_id: int = 0, x_auth: str = Header(default="")):
    """Идеи развития. Если копилка пуста — сразу накидываем первую порцию."""
    bid = _resolve_bid(x_auth, business_id)
    items = database.list_ideas(bid)
    if not items:
        _generate_ideas(bid)
        items = database.list_ideas(bid)
    return {"items": items}


@app.post("/api/ideas/more")
def api_ideas_more(business_id: int = 0, x_auth: str = Header(default="")):
    """Накидать ещё идей и добавить их к уже собранным."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    added, error = _generate_ideas(bid)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "added": added, "items": database.list_ideas(bid)}


@app.post("/api/ideas/{idea_id}/status")
def api_idea_status(idea_id: int, body: OppStatusIn, x_auth: str = Header(default="")):
    """Отметить идею: взял в работу (done) или убрать (hidden)."""
    bid = _resolve_bid(x_auth, body.business_id)
    if body.status not in ("new", "done", "hidden"):
        raise HTTPException(status_code=400, detail="Неизвестный статус")
    database.set_idea_status(idea_id, bid, body.status)
    return {"ok": True}


# ---------- СОВЕТ ДИРЕКТОРОВ ----------

def _board_facts_text(bid):
    """Комплексная сводка по всем источникам — на вход совету директоров.
    Всё числами: финансы, клиенты, контент, документы, история, цели."""
    business = database.get_business(bid) or {}
    fin = database.finance_summary(bid)
    sig = database.growth_signals(bid)
    stats = database.business_stats(bid)
    _ord = database.orders_overview(bid)
    docs = database.list_documents(bid)
    goals = database.list_goals(bid, only_active=True)
    content_30 = database.count_events(bid, ["content", "knowledge"], 30)
    docs_30 = database.count_events(bid, ["document"], 30)
    recent = database.list_notifications(bid, limit=12)

    lines = [f"Бизнес: {business.get('name') or 'компания'}"]
    if business.get("about"):
        lines.append(f"Чем занимается: {business['about']}")
    lines += [
        "— ФИНАНСЫ —",
        f"Доход всего {fin['income']} ₽, расход {fin['expense']} ₽, прибыль {fin['profit']} ₽"
        + (f", маржа {sig['margin']}%" if sig.get("margin") is not None else ""),
    ]
    if sig.get("top_expense"):
        lines.append("Крупнейшие расходы: " +
                     ", ".join(f"{e['category']} {e['total']} ₽" for e in sig["top_expense"]))
    if sig.get("losing"):
        lines.append("ТРЕВОЖНО — тратим больше, чем зарабатываем на: " +
                     ", ".join(f"{l['category']} (доход {l['income']} против расхода {l['expense']})"
                               for l in sig["losing"]))
    lines += [
        "— КЛИЕНТЫ —",
        f"Всего клиентов {sig['clients']}, спят больше 30 дней {sig['sleeping']}, "
        f"с повторными заказами {sig['repeat_clients']}.",
        f"Заказов всего {stats['orders_total']}, выполнено {stats['orders_done']}, "
        f"сообщений обработано {stats['messages']}."
        + (f" Оборот по заказам {_ord['turnover']} ₽, средний чек "
           f"{_ord['turnover'] // _ord['total']} ₽."
           if _ord.get("turnover") and _ord.get("total") else
           " Суммы у заказов не проставлены — оборот и средний чек посчитать нельзя."),
        "— КОНТЕНТ —",
        f"За 30 дней подготовлено материалов и знаний: {content_30}.",
        "— ДОКУМЕНТЫ —",
        f"Документов в базе: {len(docs)}, загружено за 30 дней: {docs_30}."
        + (" Примеры: " + ", ".join(d["filename"] for d in docs[:4]) if docs else ""),
        "— ЦЕЛИ —",
    ]
    if goals:
        for g in goals:
            pace = ("отстаёт" if g.get("pace") == "behind"
                    else "в графике" if g.get("pace") == "ahead" else "")
            lines.append(f"Цель «{g['title']}»: {g['percent']}% {pace}".rstrip())
    else:
        lines.append("Целей пока не поставлено.")
    if recent:
        lines.append("— НЕДАВНИЕ СОБЫТИЯ —")
        for e in recent[:12]:
            lines.append(f"{(e.get('created_at') or '')[:10]} {e.get('title')}"
                         + (f" — {e['detail']}" if e.get("detail") else ""))
    return "\n".join(lines)


def _generate_board(bid, day):
    """Провести заседание: собрать рекомендации, отсеять повторы, запомнить день."""
    import ai
    if not ai.ai_available():
        return 0, "ИИ сейчас недоступен"
    business = database.get_business(bid) or {"name": "VELOR AI"}
    try:
        items = ai.board_recommendations(business, _board_facts_text(bid),
                                         avoid=database.board_decided_titles(bid))
    except Exception:
        return 0, "Не удалось разобрать ответ ИИ — попробуйте ещё раз"
    added = database.add_board_recs(bid, day, items)
    database.mark_board_day(bid, day)
    if added:
        database.log_event(bid, "board", f"Совет директоров: {added} рекомендаций",
                           "; ".join(i["problem"][:60] for i in items[:3]), level="important")
    return added, None


@app.get("/api/board")
def api_board(business_id: int = 0, x_auth: str = Header(default="")):
    """Рекомендации совета директоров. Раз в день собираются автоматически."""
    bid = _resolve_bid(x_auth, business_id)
    day = datetime.date.today().isoformat()
    business = database.get_business(bid) or {}
    # Пересобираем заседание, если данные менялись (реактивно) или его ещё не было сегодня.
    dirty = signals.is_dirty(bid, "board")
    need = dirty or (business.get("board_day") != day and not database.list_board_recs(bid))
    if need and not _ai_locked(bid):
        _, err = _generate_board(bid, day)
        if not err:
            signals.settle(bid, "board")
    return {"day": day, "items": database.list_board_recs(bid),
            "history": database.list_board_history(bid, limit=30)}


@app.post("/api/board/refresh")
def api_board_refresh(business_id: int = 0, x_auth: str = Header(default="")):
    """Пересобрать заседание принудительно (не повторяя уже решённое)."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    day = datetime.date.today().isoformat()
    added, error = _generate_board(bid, day)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "added": added, "items": database.list_board_recs(bid)}


@app.post("/api/board/{rec_id}/status")
def api_board_status(rec_id: int, body: OppStatusIn, x_auth: str = Header(default="")):
    """Решение по рекомендации: accepted (принять), deferred (отложить), ignored (игнор)."""
    bid = _resolve_bid(x_auth, body.business_id)
    if body.status not in ("new", "accepted", "deferred", "ignored"):
        raise HTTPException(status_code=400, detail="Неизвестный статус")
    database.set_board_status(rec_id, bid, body.status)
    return {"ok": True}


# ---------- AI JOURNAL (ежедневный отчёт) ----------

def _facts_text(business, facts):
    """Сухие цифры дня словами — их читает ИИ, когда пишет дневник."""
    lines = [f"Бизнес: {business.get('name') or 'компания'}",
             f"Дата: {facts['day']}",
             f"Новых клиентов: {facts['clients_new']}",
             f"Новых заявок: {facts['orders_new']}",
             f"Сообщений от клиентов: {facts['messages']}",
             f"Загружено документов: {facts['docs_new']}",
             f"Доход за день: {facts['income']} ₽",
             f"Расход за день: {facts['expense']} ₽"]
    if facts["events"]:
        lines.append("События дня:")
        for e in facts["events"][:20]:
            lines.append("- " + e["title"] + (f" ({e['detail']})" if e.get("detail") else ""))
    return "\n".join(lines)


def _write_day(bid, business, day, use_ai=True):
    """Собрать и сохранить запись журнала за один день."""
    facts = database.day_facts(bid, day)
    empty = not any((facts["clients_new"], facts["orders_new"], facts["messages"],
                     facts["docs_new"], facts["income"], facts["expense"], facts["events"]))
    if empty:
        database.save_journal(bid, day, "День без событий: клиенты не писали, движений по деньгам не было.",
                              facts, "")
        return facts
    happened, advice = "", ""
    if use_ai:
        import ai
        if ai.ai_available():
            try:
                happened, advice = ai.journal_entry(business, _facts_text(business, facts))
            except Exception:
                pass
    if not happened:      # ИИ недоступен — журнал всё равно ведётся, по фактам
        parts = []
        if facts["messages"]:    parts.append(f"обращений {facts['messages']}")
        if facts["orders_new"]:  parts.append(f"новых заявок {facts['orders_new']}")
        if facts["clients_new"]: parts.append(f"новых клиентов {facts['clients_new']}")
        if facts["income"]:      parts.append(f"доход {facts['income']} ₽")
        if facts["expense"]:     parts.append(f"расход {facts['expense']} ₽")
        happened = "За день: " + ", ".join(parts) + "." if parts else "Тихий день."
    database.save_journal(bid, day, happened, facts, advice)
    return facts


def _ensure_journal(bid, back=7, budget=3):
    """
    Дописать журнал за пропущенные дни. Сервер работает не круглосуточно,
    поэтому записи создаются при открытии журнала — за каждый день ровно одна.
    budget — сколько дней за раз можно собрать с участием ИИ, чтобы не подвешивать страницу.
    """
    business = database.get_business(bid) or {"name": "VELOR AI"}
    if _ai_locked(bid):
        return                      # триал завершён: показываем уже написанное
    have = database.journal_days(bid)
    today = datetime.date.today().isoformat()
    for i in range(back):
        day = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        if day in have:
            if day != today:
                continue
            # запись за сегодня переписываем, только если за день что-то изменилось,
            # иначе каждое открытие страницы дёргало бы ИИ заново
            old, now = database.get_journal(bid, day), database.day_facts(bid, day)
            if old and all(old[k] == now[k] for k in ("clients_new", "docs_new", "income", "expense")):
                continue
        use_ai = budget > 0
        _write_day(bid, business, day, use_ai)
        if use_ai:
            budget -= 1


@app.get("/api/journal")
def api_journal(business_id: int = 0, x_auth: str = Header(default="")):
    """Лента ежедневных отчётов. Пропущенные дни дописываются при открытии."""
    bid = _resolve_bid(x_auth, business_id)
    try:
        _ensure_journal(bid)
    except Exception:
        pass                       # лента должна открыться даже если ИИ упал
    return {"entries": database.list_journal(bid)}


@app.post("/api/journal/refresh")
def api_journal_refresh(business_id: int = 0, x_auth: str = Header(default="")):
    """Пересобрать отчёт за сегодня — кнопкой или из планировщика задач."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    business = database.get_business(bid) or {"name": "VELOR AI"}
    _write_day(bid, business, datetime.date.today().isoformat())
    return {"ok": True}


# ---------- ИСТОРИЯ БИЗНЕСА (timeline) ----------

class EventIn(BaseModel):
    title: str
    detail: str | None = None
    kind: str = "note"
    business_id: int = 0


@app.get("/api/timeline")
def api_timeline(kind: str = "", limit: int = 200,
                 business_id: int = 0, x_auth: str = Header(default="")):
    """История компании: клиенты, заказы, документы, деньги, тариф, настройки."""
    bid = _resolve_bid(x_auth, business_id)
    return {"events": database.list_events(bid, min(limit, 500), kind or None)}


@app.post("/api/timeline")
def api_timeline_add(body: EventIn, x_auth: str = Header(default="")):
    """Своя запись в историю — например «подняли цены» или «открыли вторую точку»."""
    bid = _resolve_bid(x_auth, body.business_id)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Нужен текст события")
    database.log_event(bid, body.kind or "note", title, (body.detail or "").strip() or None)
    return {"ok": True}


@app.post("/api/timeline/{event_id}/delete")
def api_timeline_delete(event_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    database.delete_event(event_id, _resolve_bid(x_auth, business_id))
    return {"ok": True}


# ---------- ЗДОРОВЬЕ БИЗНЕСА ----------

# ---------- ГЛОБАЛЬНЫЙ ПОИСК ----------

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _detect_period(question):
    """
    Распознать период из вопроса, когда ИИ его не вернул («в июне», «за неделю»,
    «вчера», «сегодня»). Даты считаем сами — модели тут доверять нельзя.
    Возвращает (since, until) в ISO или (None, None).
    """
    q = (question or "").lower()
    today = datetime.date.today()

    if "сегодня" in q:
        return today.isoformat(), today.isoformat()
    if "вчера" in q:
        y = today - datetime.timedelta(days=1)
        return y.isoformat(), y.isoformat()
    if "недел" in q:
        return (today - datetime.timedelta(days=7)).isoformat(), today.isoformat()
    if "месяц" in q:
        return (today - datetime.timedelta(days=30)).isoformat(), today.isoformat()

    for stem, month in _MONTHS.items():
        if stem in q:
            ym = re.search(r"20\d{2}", q)
            year = int(ym.group()) if ym else today.year
            first = datetime.date(year, month, 1)
            nxt = datetime.date(year + (month == 12), (month % 12) + 1, 1)
            last = nxt - datetime.timedelta(days=1)
            return first.isoformat(), last.isoformat()
    return None, None


def _fallback_terms(question):
    """Если ИИ недоступен — ищем по значимым словам вопроса, обрезав окончания."""
    stop = {"покажи", "найди", "какие", "какой", "какая", "сколько", "все", "всех", "мне",
            "было", "были", "был", "что", "кто", "где", "когда", "про", "для", "или",
            "мои", "наши", "есть", "нужно", "хочу"}
    words = re.findall(r"[а-яёa-z0-9]{3,}", (question or "").lower())
    return [w[:-1] if len(w) > 5 else w for w in words if w not in stop][:4]


def _found_text(found):
    """Найденное — короткими строками для модели."""
    lines = []
    for c in found.get("clients", [])[:8]:
        lines.append(f"Клиент: {c.get('name') or 'без имени'}, заказов {c.get('orders_count', 0)}")
    for o in found.get("orders", [])[:8]:
        lines.append(f"Заказ №{o['id']} ({o.get('status')}): {(o.get('text') or '')[:90]}"
                     + (f", клиент {o['client']}" if o.get("client") else ""))
    for d in found.get("documents", [])[:6]:
        lines.append(f"Документ {d.get('filename')}: {(d.get('excerpt') or '')[:120]}")
    t = found.get("finance_totals") or {}
    if t.get("n"):
        lines.append(f"Финансы: найдено {t['n']} операций, доходы {t['income']} ₽, "
                     f"расходы {t['expense']} ₽")
    for f in found.get("finance", [])[:8]:
        kind = "доход" if f["kind"] == "income" else "расход"
        lines.append(f"{f['day']} {kind} {f['amount']} ₽, {f.get('category')}: "
                     f"{(f.get('note') or '')[:70]}")
    for m in found.get("messages", [])[:6]:
        who = "клиент" if m.get("role") == "user" else "сотрудник"
        lines.append(f"Сообщение ({who}): {(m.get('content') or '')[:110]}")
    for f in found.get("memory", [])[:6]:
        lines.append(f"Память ({f.get('kind')}): {f.get('title')} — {(f.get('body') or '')[:80]}")
    for k in found.get("knowledge", [])[:4]:
        lines.append(f"База знаний: {k[:120]}")
    return "\n".join(lines) if lines else "Ничего не найдено."


class SearchIn(BaseModel):
    question: str
    business_id: int = 0


@app.post("/api/search")
def api_search(body: SearchIn, x_auth: str = Header(default="")):
    """
    Поиск по всей базе на обычном языке.
    ИИ разбирает вопрос и пересказывает ответ, но ищет и считает — SQL.
    """
    import ai
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    question = (body.question or "").strip()
    if len(question) < 2:
        raise HTTPException(status_code=400, detail="Напишите, что ищем")

    plan = {}
    if ai.ai_available():
        try:
            plan = ai.parse_search_query(question, datetime.date.today().isoformat())
        except Exception:
            plan = {}

    terms = plan.get("terms") or _fallback_terms(question)
    sources = [s for s in (plan.get("sources") or []) if s in database.SEARCH_SOURCES]
    # период: если ИИ его не вытащил — считаем сами по словам вопроса
    since, until = plan.get("since"), plan.get("until")
    if not since and not until:
        since, until = _detect_period(question)
    found = database.global_search(bid, terms, since, until, sources or None)

    hits = sum(len(v) for k, v in found.items() if isinstance(v, list))
    answer = ""
    if ai.ai_available():
        try:
            answer = ai.search_answer(database.get_business(bid) or {}, question, _found_text(found))
        except Exception:
            answer = ""

    return {"ok": True, "question": question, "answer": answer, "hits": hits,
            "terms": terms, "since": since, "until": until, "found": found}


# ---------- ЦЕНТР УВЕДОМЛЕНИЙ ----------

@app.get("/api/notifications")
def api_notifications(kind: str = "", unread: int = 0, q: str = "",
                      business_id: int = 0, x_auth: str = Header(default="")):
    """
    Уведомления с фильтром, поиском и счётчиками непрочитанного.
    kind — типы через запятую (пусто = все).
    """
    bid = _resolve_bid(x_auth, business_id)
    database.notify_plan_limit(bid)              # проверяем лимит на каждом заходе
    kinds = [k.strip() for k in kind.split(",") if k.strip()] or None
    return {
        "notifications": database.list_notifications(
            bid, kinds=kinds, unread_only=bool(unread), query=q or None),
        "unread": database.unread_count(bid),
        "by_kind": database.unread_by_kind(bid),
        "kinds": database.NOTIFY_KINDS,
    }


@app.get("/api/notifications/count")
def api_notifications_count(business_id: int = 0, x_auth: str = Header(default="")):
    """Только число непрочитанных — для значка в навигации."""
    return {"unread": database.unread_count(_resolve_bid(x_auth, business_id))}


@app.post("/api/notifications/{event_id}/read")
def api_notification_read(event_id: int, business_id: int = 0,
                          x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, business_id)
    return {"ok": True, "marked": database.mark_read(bid, event_id=event_id)}


@app.post("/api/notifications/read-all")
def api_notifications_read_all(kind: str = "", business_id: int = 0,
                               x_auth: str = Header(default="")):
    """Прочитать всё — или всё в выбранных типах, если задан фильтр."""
    bid = _resolve_bid(x_auth, business_id)
    kinds = [k.strip() for k in kind.split(",") if k.strip()] or None
    return {"ok": True, "marked": database.mark_read(bid, kinds=kinds)}


# ---------- ИМПОРТ ФИНАНСОВ ----------
#
# Конвейер: источник → операции → категории → база. Каждый шаг ничего не знает
# о соседях, поэтому новый источник (банк, 1С, CRM) подключается одной функцией
# в finance_import.SOURCES, а вся логика ниже остаётся нетронутой.

def _categorize(bid, operations):
    """
    Разложить расходы по категориям: сначала выученные правила владельца,
    потом обычные правила по ключевым словам, и только остаток — в ИИ.
    """
    import ai
    learned = database.learned_rules(bid)
    unknown = []

    for i, op in enumerate(operations):
        if op["direction"] == "income":
            op["category"], op["confidence"] = "выручка", 1.0
            continue
        category, confidence, _ = finance_import.guess_category(
            op["description"], op["counterparty"], learned)
        if category:
            op["category"], op["confidence"] = category, confidence
        else:
            op["category"], op["confidence"] = "прочее", 0.0
            unknown.append({"i": i, "text": (op["description"] + " " + op["counterparty"]).strip()})

    # ИИ зовём один раз на всю пачку и только за тем, что правила не осилили
    if unknown and ai.ai_available():
        # Модель нумерует ответы по-своему («первый, второй»), поэтому даём ей
        # сплошные номера 1..N и сами возвращаем их к настоящим операциям.
        # Иначе её «1» прилетает в operations[1] и затирает чужую категорию.
        numbered = [{"i": n, "text": u["text"]} for n, u in enumerate(unknown, 1)]
        back = {n: u["i"] for n, u in enumerate(unknown, 1)}
        try:
            guesses = ai.categorize_operations(
                database.get_business(bid) or {}, numbered, finance_import.CATEGORIES)
        except Exception:
            guesses = {}
        for n, category in guesses.items():
            index = back.get(n)
            if index is None:
                continue                      # номер, которого мы не отправляли
            # 0.6 — «ИИ решил»: ниже порога уверенности, попадёт в список на проверку
            operations[index]["category"] = category
            operations[index]["confidence"] = 0.6
    return operations


def _import_totals(operations):
    income = sum(o["amount"] for o in operations if o["direction"] == "income")
    expense = sum(o["amount"] for o in operations if o["direction"] == "expense")
    return {"income": income, "expense": expense, "profit": income - expense}


@app.post("/api/finance/import")
async def api_finance_import(file: UploadFile = File(...),
                             business_id: int = 0,
                             x_auth: str = Header(default="")):
    """Загрузка выписки: разбираем файл, раскладываем по категориям, пишем в базу."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл больше 8 МБ")

    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in finance_import.SOURCES:
        raise HTTPException(status_code=400, detail="Поддерживаются CSV, XLSX и PDF")

    operations = finance_import.parse(file.filename, raw)
    if not operations:
        raise HTTPException(
            status_code=400,
            detail="Не нашёл операций в файле. Нужны колонки с датой и суммой.")

    operations = _categorize(bid, operations)
    import_id = database.start_import(bid, file.filename, ext)
    added = database.add_operations(bid, operations, import_id, ext)
    database.finish_import(import_id, bid, len(operations), added, len(operations) - added)
    signals.react(bid, "finance")   # деньги → прогноз, Директор, брифинг, риски

    return {
        "ok": True,
        "filename": file.filename,
        "total": len(operations),
        "added": added,
        "skipped": len(operations) - added,
        "totals": _import_totals(operations),
        "by_category": database.expenses_by_category(bid),
        "unsure": database.list_operations(bid, limit=100, unsure_only=True),
    }


@app.get("/api/finance/operations")
def api_finance_operations(unsure: int = 0, business_id: int = 0,
                           x_auth: str = Header(default="")):
    """Загруженные операции. unsure=1 — только те, в категории которых не уверены."""
    bid = _resolve_bid(x_auth, business_id)
    summary = database.finance_summary(bid)
    return {
        "operations": database.list_operations(bid, unsure_only=bool(unsure)),
        "by_category": database.expenses_by_category(bid),
        "totals": {"income": summary["income"], "expense": summary["expense"],
                   "profit": summary["profit"]},
        "categories": finance_import.CATEGORIES,
        "rules": database.list_category_rules(bid),
    }


class CategoryIn(BaseModel):
    category: str
    remember: bool = True                  # запомнить выбор для похожих операций
    business_id: int = 0


@app.post("/api/finance/operations/{entry_id}/category")
def api_operation_category(entry_id: int, body: CategoryIn, x_auth: str = Header(default="")):
    """
    Владелец поправил категорию. Запоминаем выбор и сразу применяем его
    к похожим операциям — и к уже загруженным, и ко всем будущим.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    category = (body.category or "").strip().lower()
    if category not in finance_import.CATEGORIES:
        raise HTTPException(status_code=400, detail="Неизвестная категория")

    row = database.set_operation_category(entry_id, bid, category)
    if not row:
        raise HTTPException(status_code=404, detail="Операция не найдена")

    applied, pattern = 0, None
    if body.remember:
        pattern = finance_import.learn_pattern(row.get("note"), row.get("counterparty"))
        if pattern:
            database.learn_category(bid, pattern, category)
            applied = database.apply_rule_to_existing(bid, pattern, category)

    signals.react(bid, "finance")   # пересчёт категорий меняет структуру расходов
    return {"ok": True, "pattern": pattern, "applied": applied}


@app.post("/api/finance/rules/forget")
def api_rule_forget(pattern: str, business_id: int = 0,
                    x_auth: str = Header(default="")):
    database.forget_category(_resolve_bid(x_auth, business_id), pattern)
    return {"ok": True}


# ---------- УТРЕННИЙ БРИФИНГ ----------

def _briefing_numbers(bid):
    """Всё, что можно посчитать без ИИ: вчерашний день и текущее состояние дел."""
    today = datetime.date.today()
    yday = (today - datetime.timedelta(days=1)).isoformat()
    month_start = today.replace(day=1).isoformat()

    y = database.day_facts(bid, yday)
    with database._connect() as conn:
        one = lambda q, *a: conn.execute(q, a).fetchone()[0] or 0
        month_income = one(
            """SELECT SUM(amount) FROM finance_entries WHERE business_id = ?
                 AND kind='income' AND date(created_at) >= date(?)""", bid, month_start)
        month_expense = one(
            """SELECT SUM(amount) FROM finance_entries WHERE business_id = ?
                 AND kind='expense' AND date(created_at) >= date(?)""", bid, month_start)
        orders_open = one(
            "SELECT COUNT(*) FROM orders WHERE business_id = ? AND status = 'новый'", bid)
        orders_stale = one(
            """SELECT COUNT(*) FROM orders WHERE business_id = ? AND status = 'новый'
                 AND date(created_at) <= date('now','-3 day')""", bid)

    # что требует внимания — считаем кодом, не спрашивая модель
    attention = []
    if orders_stale:
        attention.append(database._plural(orders_stale, "заявка висит", "заявки висят", "заявок висят")
                         + " без движения больше трёх дней")
    for g in database.list_goals(bid, only_active=True):
        if g["days_left"] is not None and g["days_left"] < 0:
            attention.append(f"срок цели «{g['title']}» прошёл, набрано {g['percent']}%")
        elif g["pace"] == "behind":
            attention.append(
                f"цель «{g['title']}» отстаёт: {g['percent']}%"
                + (f", нужно по {g['per_day']} {g['unit']} в день" if g.get("per_day") else ""))
    if y["expense"] > y["income"] and y["expense"]:
        attention.append(f"вчера потратили больше, чем заработали: {y['expense']} против {y['income']} ₽")

    opp = next((o for o in database.list_opportunities(bid) if o["status"] == "new"), None)
    risk = next((r for r in database.list_risks(bid) if r["status"] == "new"), None)

    return {
        "date": today.isoformat(),
        "yesterday": yday,
        "income_yday": y["income"], "expense_yday": y["expense"],
        "profit_yday": y["income"] - y["expense"],
        "income_month": month_income, "expense_month": month_expense,
        "profit_month": month_income - month_expense,
        "orders_new": y["orders_new"], "orders_open": orders_open,
        "clients_new": y["clients_new"],
        "attention": attention,
        "opportunity": ({"title": opp["title"], "action": opp["action"]} if opp else None),
        "risk": ({"title": risk["title"], "action": risk["action"]} if risk else None),
    }


def _briefing_text(n):
    """Те же цифры словами — на вход модели."""
    lines = [
        f"Вчера ({n['yesterday']}): доход {n['income_yday']} ₽, расход {n['expense_yday']} ₽, "
        f"прибыль {n['profit_yday']} ₽.",
        f"С начала месяца: доход {n['income_month']} ₽, расход {n['expense_month']} ₽, "
        f"прибыль {n['profit_month']} ₽.",
        f"Вчера новых заявок: {n['orders_new']}. Сейчас необработанных заявок: {n['orders_open']}.",
        f"Вчера новых клиентов: {n['clients_new']}.",
    ]
    if n["attention"]:
        lines.append("Требует внимания: " + "; ".join(n["attention"]) + ".")
    else:
        lines.append("Ничего критичного в делах не висит.")
    if n["opportunity"]:
        lines.append(f"Главная возможность: {n['opportunity']['title']} — {n['opportunity']['action']}")
    if n["risk"]:
        lines.append(f"Главный риск: {n['risk']['title']} — {n['risk']['action']}")
    return "\n".join(lines)


# Кого уже дописываем — чтобы два одновременных запроса не позвали модель дважды.
_BRIEF_WORDS_RUNNING = set()
_BRIEF_WORDS_LOCK = threading.Lock()


def _build_briefing(bid, day, with_words=True):
    """
    Собрать брифинг за день и сохранить.

    Делится на два приёма намеренно. Числа и список «требует внимания»
    считает код — это мгновенно. Приветствие, строку «сегодня» и совет пишет
    модель, и это десятки секунд. Пока они собирались в одном вызове внутри
    запроса, окно брифинга всплывало через минуту после захода — то есть
    тогда, когда владелец уже ушёл со страницы.

    Поэтому with_words=False отдаёт готовое сразу, а слова дописываются
    отдельным заходом и подставляются в уже открытое окно.
    """
    import ai
    numbers = _briefing_numbers(bid)
    words = {}
    if with_words and ai.ai_available():
        try:
            words = ai.morning_briefing(database.get_business(bid) or {}, _briefing_text(numbers))
        except Exception:
            words = {}
    # Цифры и список «требует внимания» посчитаны кодом — модель их не перезаписывает,
    # её формулировка про внимание идёт отдельной строкой под списком.
    payload = dict(numbers)
    for key in ("greeting", "today", "advice"):
        if words.get(key):
            payload[key] = words[key]
    # attention от модели намеренно не берём: список уже точный, а модель добавляет
    # к нему выдуманные числа («третья зависшая заявка») и противоречит сама себе.
    payload.setdefault("greeting", "Доброе утро.")
    # Признак «слова ещё не пришли»: по нему кабинет знает, что стоит один раз
    # переспросить и подставить их в открытое окно.
    payload["words"] = bool(words)
    database.save_briefing(bid, day, json.dumps(payload, ensure_ascii=False))
    return payload


def _briefing_words_later(bid, day):
    """Дописать слова модели к уже отданному брифингу."""
    try:
        _build_briefing(bid, day, with_words=True)
    except Exception:
        pass
    finally:
        with _BRIEF_WORDS_LOCK:
            _BRIEF_WORDS_RUNNING.discard((bid, day))


def _load_briefing(bid, day, force=False):
    # Реактивность: если после сборки менялись данные — пересобираем брифинг,
    # чтобы Директор учёл свежие риски (напр. из документов) и цифры.
    if signals.is_dirty(bid, "briefing") and not _ai_locked(bid):
        force = True
    row = None if force else database.get_briefing(bid, day)
    if row and row.get("payload"):
        try:
            return json.loads(row["payload"]), row.get("shown_on")
        except json.JSONDecodeError:
            pass
    if _ai_locked(bid):
        return None, (row or {}).get("shown_on")   # триал завершён — новый не собираем

    # Отдаём посчитанное немедленно, слова модели дописываем следом. Ждать
    # модель внутри запроса значит показывать пустой экран, пока она думает.
    payload = _build_briefing(bid, day, with_words=False)
    signals.settle(bid, "briefing")

    import ai
    if ai.ai_available():
        key = (bid, day)
        with _BRIEF_WORDS_LOCK:
            start = key not in _BRIEF_WORDS_RUNNING
            if start:
                _BRIEF_WORDS_RUNNING.add(key)
        if start:
            threading.Thread(target=_briefing_words_later, args=(bid, day),
                             name="velor-brief-words", daemon=True).start()
    return payload, (row or {}).get("shown_on")


@app.get("/api/briefing")
def api_briefing(business_id: int = 0, x_auth: str = Header(default="")):
    """Утренний брифинг за сегодня. Готовится один раз в сутки, дальше отдаётся из базы."""
    bid = _resolve_bid(x_auth, business_id)
    day = datetime.date.today().isoformat()
    payload, shown_on = _load_briefing(bid, day)
    return {"day": day, "payload": payload, "first_today": shown_on != day}


@app.post("/api/briefing/refresh")
def api_briefing_refresh(business_id: int = 0, x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    day = datetime.date.today().isoformat()
    return {"day": day, "payload": _build_briefing(bid, day)}


@app.post("/api/briefing/seen")
def api_briefing_seen(business_id: int = 0, x_auth: str = Header(default="")):
    """Владелец прочитал — сегодня больше не всплываем."""
    bid = _resolve_bid(x_auth, business_id)
    database.mark_briefing_shown(bid, datetime.date.today().isoformat())
    return {"ok": True}


@app.get("/api/briefing/list")
def api_briefing_list(business_id: int = 0, x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, business_id)
    out = []
    for r in database.list_briefings(bid):
        try:
            out.append({"day": r["day"], "payload": json.loads(r["payload"] or "{}")})
        except json.JSONDecodeError:
            continue
    return {"briefings": out}


# ---------- ЕЖЕНЕДЕЛЬНЫЙ ОБЗОР ----------

def _week_bounds(day):
    """Понедельник..воскресенье недели, в которую попадает day (date)."""
    monday = day - datetime.timedelta(days=day.weekday())
    return monday, monday + datetime.timedelta(days=6)


def _pct_change(cur, prev):
    """Изменение в процентах относительно прошлой недели (None, если не с чем сравнить)."""
    if not prev:
        return None
    return round((cur - prev) / prev * 100)


def _weekly_text(f, prev, goals):
    """Итоги недели словами — на вход модели."""
    ws, we = f["week_start"], f["week_end"]
    lines = [
        f"Неделя {ws} — {we}.",
        f"Деньги: доход {f['income']} ₽, расход {f['expense']} ₽, прибыль {f['profit']} ₽.",
        f"Прошлая неделя для сравнения: доход {prev['income']} ₽, расход {prev['expense']} ₽, "
        f"прибыль {prev['profit']} ₽.",
        f"Новых клиентов: {f['clients_new']} (прошлая неделя — {prev['clients_new']}).",
        f"Новых заказов: {f['orders_new']}, из них выполнено: {f['orders_done']}. "
        f"Сообщений от клиентов: {f['messages']}.",
        f"Контент-активность за неделю (посты, документы, знания): {f['content']} "
        f"(прошлая неделя — {prev['content']}).",
    ]
    if f["expense_top"]:
        top = ", ".join(f"{c['category']} {int(c['total'])} ₽" for c in f["expense_top"])
        lines.append(f"Больше всего денег ушло на: {top}.")
    if goals:
        lines.append("Цели:")
        for g in goals:
            part = (f"  · «{g['title']}»: {g['percent']}%"
                    + (f", темп: {'отстаёт' if g['pace']=='behind' else 'в графике'}" if g.get("pace") else ""))
            lines.append(part)
    else:
        lines.append("Целей пока не поставлено.")
    return "\n".join(lines)


def _build_weekly(bid, week_start):
    """Собрать обзор недели и сохранить. Модель зовём один раз на неделю."""
    import ai
    ws = datetime.date.fromisoformat(week_start)
    we = ws + datetime.timedelta(days=6)
    prev_ws = ws - datetime.timedelta(days=7)
    prev_we = ws - datetime.timedelta(days=1)

    facts = database.week_facts(bid, ws.isoformat(), we.isoformat())
    prev = database.week_facts(bid, prev_ws.isoformat(), prev_we.isoformat())
    goals = database.list_goals(bid, only_active=True)

    words = {}
    if ai.ai_available():
        try:
            words = ai.weekly_review(database.get_business(bid) or {},
                                     _weekly_text(facts, prev, goals))
        except Exception:
            words = {}

    payload = {
        "week_start": ws.isoformat(), "week_end": we.isoformat(),
        "finance": {
            "income": facts["income"], "expense": facts["expense"], "profit": facts["profit"],
            "income_change": _pct_change(facts["income"], prev["income"]),
            "profit_change": _pct_change(facts["profit"], prev["profit"]),
            "expense_top": facts["expense_top"],
        },
        "clients": {"new": facts["clients_new"], "prev_new": prev["clients_new"],
                    "messages": facts["messages"]},
        "orders": {"new": facts["orders_new"], "done": facts["orders_done"]},
        "content": {"count": facts["content"], "prev_count": prev["content"]},
        "goals": [{"title": g["title"], "percent": g["percent"], "pace": g.get("pace"),
                   "unit": g.get("unit")} for g in goals],
        # формулировки модели
        "achievements": words.get("achievements", ""),
        "mistakes": words.get("mistakes", ""),
        "finance_note": words.get("finance", ""),
        "content_note": words.get("content", ""),
        "clients_note": words.get("clients", ""),
        "goals_note": words.get("goals", ""),
        "next_week": words.get("next_week", ""),
    }
    database.save_weekly_review(bid, ws.isoformat(), json.dumps(payload, ensure_ascii=False))
    return payload


_WEEKLY_NARRATIVE = ("achievements", "mistakes", "finance_note",
                     "content_note", "clients_note", "goals_note", "next_week")


def _load_weekly(bid, week_start, force=False):
    import ai
    locked = _ai_locked(bid)
    row = None if (force and not locked) else database.get_weekly_review(bid, week_start)
    if row and row.get("payload"):
        try:
            payload = json.loads(row["payload"])
            # Если прошлый сбор не получил формулировок из-за сбоя модели, а модель
            # снова доступна — пересобираем, чтобы не залипал пустой обзор.
            empty = not any(payload.get(k) for k in _WEEKLY_NARRATIVE)
            if locked or not (empty and ai.ai_available()):
                return payload
        except json.JSONDecodeError:
            pass
    if locked:
        return None                     # триал завершён — новый обзор не собираем
    return _build_weekly(bid, week_start)


@app.get("/api/weekly")
def api_weekly(business_id: int = 0, week: str = "",
               x_auth: str = Header(default="")):
    """Обзор за неделю (по умолчанию — текущую). Готовится один раз, дальше из базы."""
    bid = _resolve_bid(x_auth, business_id)
    try:
        base = datetime.date.fromisoformat(week) if week else datetime.date.today()
    except ValueError:
        base = datetime.date.today()
    monday, _ = _week_bounds(base)
    return {"week_start": monday.isoformat(), "payload": _load_weekly(bid, monday.isoformat())}


@app.post("/api/weekly/refresh")
def api_weekly_refresh(business_id: int = 0, week: str = "",
                       x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    try:
        base = datetime.date.fromisoformat(week) if week else datetime.date.today()
    except ValueError:
        base = datetime.date.today()
    monday, _ = _week_bounds(base)
    return {"week_start": monday.isoformat(), "payload": _build_weekly(bid, monday.isoformat())}


@app.get("/api/weekly/list")
def api_weekly_list(business_id: int = 0, x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, business_id)
    out = []
    for r in database.list_weekly_reviews(bid):
        try:
            out.append({"week_start": r["week_start"], "payload": json.loads(r["payload"] or "{}")})
        except json.JSONDecodeError:
            continue
    return {"reviews": out}


# ---------- РЕЗУЛЬТАТЫ ----------
#
# Здесь раньше жил список «инструментов»: пять карточек с пометкой «Скоро»,
# ни одного обработчика и endpoint, отвечающий 501. Возможность была
# объявлена в продукте, но её не существовало — а по объявленным
# возможностям владелец принимает решения.
#
# Теперь реестр один и он в outputs.py: если там нет сборщика, здесь нет и
# карточки. Отвечать 501 стало нечему.


class OutputIn(BaseModel):
    kind: str
    params: dict = {}
    business_id: int = 0


@app.get("/api/outputs/kinds")
def api_output_kinds(business_id: int = 0, x_auth: str = Header(default="")):
    """Что VELOR умеет собрать и какие поля для этого нужны."""
    bid = _resolve_bid(x_auth, business_id)
    return outputs.catalog(bid)


@app.get("/api/outputs")
def api_outputs(business_id: int = 0, kind: str = "", limit: int = 30,
                offset: int = 0, x_auth: str = Header(default="")):
    """История результатов: что собрано, когда и по какому запросу."""
    bid = _resolve_bid(x_auth, business_id)
    kind = kind if kind in outputs.KINDS else None
    rows = database.list_outputs(bid, kind=kind, limit=limit, offset=max(0, offset))
    return {"items": [outputs.brief(r) for r in rows],
            "total": database.count_outputs(bid, kind=kind)}


@app.post("/api/outputs")
def api_output_build(body: OutputIn, x_auth: str = Header(default="")):
    """Собрать результат. Цифры считает база, файл кладётся в то же
    хранилище, что и оригиналы входящих."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    try:
        row = outputs.build(bid, body.kind, body.params or {})
    except outputs.OutputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        log.exception("Результат %s не собрался (biz %s)", body.kind, bid)
        raise HTTPException(status_code=500,
                            detail="Не удалось собрать результат — попробуйте ещё раз.")
    return {"ok": True, "item": outputs.public(row)}


@app.get("/api/outputs/{output_id}")
def api_output_one(output_id: int, business_id: int = 0,
                   x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, business_id)
    row = database.get_output(output_id, bid)
    if not row:
        raise HTTPException(status_code=404, detail="Результат не найден.")
    return {"item": outputs.public(row)}


@app.get("/api/outputs/{output_id}/file")
def api_output_file(output_id: int, business_id: int = 0,
                    x_auth: str = Header(default="")):
    """Отдать файл результата. Право на файл определяет запись в базе, а не
    знание имени в хранилище."""
    bid = _resolve_bid(x_auth, business_id)
    try:
        data, mime, name = outputs.file_of(bid, output_id)
    except outputs.OutputError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(content=data, media_type=mime, headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + _urlquote(name),
        "Cache-Control": "no-store",
    })


@app.post("/api/outputs/{output_id}/delete")
def api_output_delete(output_id: int, business_id: int = 0,
                      x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    if not outputs.remove(bid, output_id):
        raise HTTPException(status_code=404, detail="Результат не найден.")
    return {"ok": True}


@app.get("/api/tools")
def api_tools(business_id: int = 0, x_auth: str = Header(default="")):
    """
    Старый адрес страницы «Инструменты». Оставлен, чтобы уже открытая
    вкладка и закладка не упирались в 404, но отвечает он теперь реестром
    результатов — то есть тем, что действительно работает.
    """
    bid = _resolve_bid(x_auth, business_id)
    return {"tools": [{"id": k, "name": m["title"], "about": m["about"],
                       "group": m["group"], "ready": True}
                      for k, m in outputs.KINDS.items()],
            "moved_to": "results.html"}


# ---------- ПОДКЛЮЧЁННЫЕ СЕРВИСЫ (источники знаний) ----------
#
# Правило страницы «Источники»: «Подключено» пишем ТОЛЬКО после успешного живого
# запроса к сервису. Поэтому подключение и проверка — это одно действие, а не два.


class ConnectIn(BaseModel):
    credentials: dict = {}
    business_id: int = 0


class ConnectConfigIn(BaseModel):
    config: dict = {}
    business_id: int = 0


@app.get("/api/connections/catalog")
def api_connections_catalog(business_id: int = 0, x_auth: str = Header(default="")):
    """
    Весь раздел «Подключения»: категории, карточки, статусы, права.

    Состояние собирается из фактов при каждом запросе: есть ли ключ, не упала
    ли последняя синхронизация, идёт ли выгрузка прямо сейчас. Хранимого
    флажка «подключено» не существует — его невозможно забыть погасить.
    """
    bid = _resolve_bid(x_auth, business_id)
    return connections.catalog(bid)


@app.get("/api/connections/state/{provider}")
def api_connection_state(provider: str, business_id: int = 0,
                         x_auth: str = Header(default="")):
    """Паспорт одного подключения."""
    bid = _resolve_bid(x_auth, business_id)
    try:
        return connections.state(bid, provider)
    except connections.NotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/connections/{provider}/connect")
def api_connection_connect(provider: str, body: ConnectConfigIn,
                           x_auth: str = Header(default="")):
    """
    Подключить через единый интерфейс.

    Ключи проверяются живым запросом к сервису — «подключено» появляется
    только после того, как данные действительно пришли. Интеграция, которой
    ещё нет, отвечает отказом с объяснением, а не молчаливой галочкой.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    config = {k: (str(v) if v is not None else "") for k, v in (body.config or {}).items()}
    try:
        st = connections.connect(bid, provider, config)
    except connections.NotAvailable as e:
        raise HTTPException(status_code=422, detail=str(e))
    except connectors.ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Первую порцию данных забираем в фоне: ключ уже проверен, держать
    # человека перед крутящейся кнопкой незачем.
    if st.get("can_sync"):
        if _os.getenv("DISABLE_SYNC_WORKER"):
            connectors.sync(bid, provider)
            st = connections.state(bid, provider)
        else:
            import threading
            threading.Thread(target=connectors.sync, args=(bid, provider),
                             name=f"velor-first-sync-{provider}", daemon=True).start()
    return {"ok": True, "state": st}


@app.post("/api/connections/{provider}/disconnect")
def api_connection_disconnect(provider: str, business_id: int = 0,
                              x_auth: str = Header(default="")):
    """Отключить. Загруженные данные остаются — они принадлежат бизнесу."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    try:
        return {"ok": True, "state": connections.disconnect(bid, provider)}
    except connections.NotAvailable as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/connections")
def api_connections(business_id: int = 0, x_auth: str = Header(default="")):
    """Каталог источников + состояние подключений этого бизнеса.

    Секреты наружу не отдаются никогда — только статус, время синхронизации,
    сколько записей загружено и текст последней ошибки.
    """
    bid = _resolve_bid(x_auth, business_id)
    return connectors.status(bid)


@app.post("/api/connections/{provider}")
def api_connection_save(provider: str, body: ConnectIn,
                        x_auth: str = Header(default="")):
    """Подключить сервис: проверяем ключи живым запросом и сохраняем зашифрованно."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    if provider not in connectors.REGISTRY:
        raise HTTPException(status_code=404, detail="Неизвестный источник.")
    creds = {k: (str(v) if v is not None else "") for k, v in (body.credentials or {}).items()}
    try:
        connectors.connect(bid, provider, creds)
    except connectors.ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Первую порцию данных забираем в фоне, а не прямо здесь.
    #
    # Раньше выгрузка шла внутри этого запроса, и у живого магазина с историей
    # заказов человек сидел перед крутящейся кнопкой все 25 секунд, а у
    # Wildberries вдобавок ловил 429: проверка ключа и выгрузка били в один
    # эндпоинт подряд, а он пускает примерно раз в минуту.
    #
    # Теперь ключ проверен (иначе мы бы сюда не дошли), подключение сохранено,
    # и кабинет отвечает мгновенно. Данные догоняются следом, страница их
    # дожидается сама и показывает результат.
    if _os.getenv("DISABLE_SYNC_WORKER"):
        return {"ok": True, "connection": database.get_connection(bid, provider),
                "synced": connectors.sync(bid, provider)}

    import threading
    threading.Thread(target=connectors.sync, args=(bid, provider),
                     name=f"velor-first-sync-{provider}", daemon=True).start()
    return {"ok": True, "connection": database.get_connection(bid, provider),
            "synced": {"ok": True, "added": 0, "pending": True, "error": None}}


@app.post("/api/connections/{provider}/sync")
def api_connection_sync(provider: str, business_id: int = 0,
                        x_auth: str = Header(default="")):
    """Забрать новое прямо сейчас (кнопка «Обновить» в карточке источника)."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    if provider not in connectors.REGISTRY:
        raise HTTPException(status_code=404, detail="Неизвестный источник.")
    return connectors.sync(bid, provider)


@app.post("/api/connections/sync-all")
def api_connections_sync_all(business_id: int = 0, x_auth: str = Header(default="")):
    """Обновить все подключённые источники разом."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    return {"ok": True, "results": connectors.sync_all(bid)}


@app.post("/api/connections/{provider}/delete")
def api_connection_delete(provider: str, business_id: int = 0,
                          x_auth: str = Header(default="")):
    """Отключить сервис. Загруженные данные остаются — они принадлежат бизнесу."""
    bid = _resolve_bid(x_auth, business_id)
    database.delete_connection(bid, provider)
    return {"ok": True}


# ---------- ФОНОВАЯ СИНХРОНИЗАЦИЯ ИСТОЧНИКОВ ----------
#
# Подключённый сервис должен приносить данные сам, а не только по кнопке.
# Отдельный воркер поднимать не будем (на бесплатном хостинге его негде держать
# — по той же причине бот живёт вебхуком): достаточно фонового потока внутри
# веб-процесса. Он спит, просыпается раз в SYNC_EVERY_MIN и обходит бизнесы,
# у которых есть подключения.
#
# Осознанные ограничения:
#   • аккаунты в read-only (триал кончился) пропускаем — не тратим чужие лимиты;
#   • ошибка одного источника не мешает остальным (см. connectors.sync);
#   • при нескольких воркерах uvicorn каждый будет синхронизировать своё, но
#     upsert по external_id идемпотентен — дублей не возникнет.

SYNC_EVERY_MIN = int(_os.getenv("SYNC_EVERY_MIN", "30"))
SYNC_STALE_MIN = int(_os.getenv("SYNC_STALE_MIN", "25"))   # что считаем «пора обновить»


def _needs_sync(conn_row) -> bool:
    last = conn_row.get("last_sync_at")
    if not last:
        return True
    try:
        dt = datetime.datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    age = (datetime.datetime.utcnow() - dt).total_seconds() / 60
    return age >= SYNC_STALE_MIN


def _sync_round():
    """Один обход всех бизнесов с подключениями."""
    for biz in database.list_businesses_with_stats():
        bid = biz["id"]
        try:
            if trial.access(database.get_business(bid))["read_only"]:
                continue
            for conn_row in database.list_connections(bid):
                if conn_row.get("status") == "paused" or not _needs_sync(conn_row):
                    continue
                connectors.sync(bid, conn_row["provider"])
        except Exception:
            logging.exception("Фоновая синхронизация: бизнес %s", bid)


def _followup_round():
    """
    Один обход касаний по всем бизнесам.

    Отдельного планировщика для этого не заводим: он уже есть — вот этот. Своя
    очередь, свой воркер и свой ритм означали бы второй способ узнать, что
    пора действовать, и первый же рассинхрон между ними стоил бы отправленного
    дважды сообщения.
    """
    for biz in database.list_businesses_with_stats():
        bid = biz["id"]
        try:
            followup.run(bid)
        except Exception:
            logging.exception("Обход касаний: бизнес %s", bid)
        try:
            # Предложение, которое ждало решения неделю, выполнять поздно:
            # обстоятельства, из-за которых оно появилось, давно другие.
            actions.settle(bid)
        except Exception:
            logging.exception("Просроченные предложения: бизнес %s", bid)
        try:
            # Тот же обход смотрит и на состояние бизнеса. Второй планировщик
            # означал бы второй ответ на вопрос «что сейчас происходит», и
            # первое же расхождение владелец увидел бы как две разные правды.
            initiatives.scan(bid)
            initiatives.settle(bid)
        except Exception:
            logging.exception("Обход находок: бизнес %s", bid)
        try:
            # Сверка звеньев цепочки. Ничего не чинит — только называет
            # противоречия в журнал ошибок: молча исправлять состояние,
            # которого мы не понимаем, значит спрятать ошибку, а не устранить.
            leads.reconcile(bid)
        except Exception:
            logging.exception("Сверка состояний: бизнес %s", bid)


def _sync_worker():
    import time as _time
    # Небольшая задержка на старте: пусть сервер сначала поднимется и ответит
    # на первые запросы, а тяжёлые сетевые вызовы пойдут следом.
    _time.sleep(60)
    while True:
        try:
            _sync_round()
        except Exception:
            logging.exception("Фоновая синхронизация: обход не удался")
        try:
            _followup_round()
        except Exception:
            logging.exception("Обход касаний не удался")
        _time.sleep(max(5, SYNC_EVERY_MIN) * 60)


@app.on_event("startup")
def _start_sync_worker():
    if _os.getenv("DISABLE_SYNC_WORKER"):
        return                      # выключатель для тестов и локальной отладки
    import threading
    threading.Thread(target=_sync_worker, name="velor-sync", daemon=True).start()
    log.info("Фоновая синхронизация источников: раз в %s мин", SYNC_EVERY_MIN)


# ---------- ЭКСПОРТ ДАННЫХ ----------

@app.get("/api/export")
def api_export_list(business_id: int = 0, x_auth: str = Header(default="")):
    """Что можно выгрузить и в каких форматах — для страницы экспорта."""
    _resolve_bid(x_auth, business_id)
    return {
        "datasets": [{"key": k, "title": exporters.DATASET_TITLES[k]}
                     for k in exporters.DATASET_KEYS],
        "formats": exporters.FORMATS,
    }


@app.get("/api/export/{key}.{fmt}")
def api_export(key: str, fmt: str, business_id: int = 0,
               x_auth: str = Header(default="")):
    """Выгрузить набор данных в выбранном формате: CSV, Excel или PDF."""
    bid = _resolve_bid(x_auth, business_id)
    if key not in exporters.DATASET_KEYS or fmt not in exporters.FORMATS:
        raise HTTPException(status_code=404, detail="Неизвестный набор данных или формат")
    try:
        blob, mime, filename = exporters.export(bid, key, fmt)
    except Exception:
        raise HTTPException(status_code=500, detail="Не удалось собрать файл — попробуйте ещё раз")
    return Response(content=blob, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------- ЦЕЛИ БИЗНЕСА ----------

class GoalIn(BaseModel):
    metric: str
    title: str
    target: int
    deadline: str | None = None
    business_id: int = 0


class GoalPatch(BaseModel):
    title: str | None = None
    target: int | None = None
    deadline: str | None = None
    manual_value: int | None = None
    status: str | None = None
    business_id: int = 0


def _goals_text(goals):
    """Цели с прогрессом словами — то, что читает ИИ перед советом."""
    lines = []
    for g in goals:
        line = (f"id {g['id']}. {g['title']} — {g['metric_name']}: "
                f"набрано {g['current']} из {g['target']} {g['unit']} ({g['percent']}%)")
        if g["days_left"] is not None:
            line += (f", до срока {g['days_left']} дн." if g["days_left"] >= 0
                     else f", срок прошёл {abs(g['days_left'])} дн. назад")
        if g.get("per_day"):
            line += f", нужно по {g['per_day']} {g['unit']} в день"
        if g["pace"] == "behind":
            line += " — ОТСТАЁТ ОТ ПЛАНА"
        elif g["pace"] == "ahead":
            line += " — идёт с опережением"
        lines.append(line)
    return "\n".join(lines)


def _ensure_goal_advice(bid, goals):
    """Совет по каждой цели — один раз в день, чтобы не дёргать ИИ на каждый заход."""
    import ai
    today = datetime.date.today().isoformat()
    stale = [g for g in goals if g["status"] == "active" and g.get("advice_day") != today]
    if not stale or not ai.ai_available():
        return goals
    if _ai_locked(bid):
        return goals
    try:
        business = database.get_business(bid) or {}
        advices = ai.goal_actions(business, _goals_text(stale))
    except Exception:
        return goals
    for g in goals:
        if g["id"] in advices:
            g["advice"], g["advice_day"] = advices[g["id"]], today
            database.save_goal_advice(g["id"], bid, advices[g["id"]], today)
    return goals


@app.get("/api/goals")
def api_goals(business_id: int = 0, x_auth: str = Header(default="")):
    """Цели с прогрессом. Советы ИИ обновляются раз в сутки."""
    bid = _resolve_bid(x_auth, business_id)
    goals = database.list_goals(bid)
    return {"goals": _ensure_goal_advice(bid, goals), "metrics": database.GOAL_METRICS}


@app.post("/api/goals")
def api_goal_add(body: GoalIn, x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, body.business_id)
    if body.metric not in database.GOAL_METRICS:
        raise HTTPException(status_code=400, detail="Неизвестный показатель")
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Нужно название цели")
    if body.target <= 0:
        raise HTTPException(status_code=400, detail="Цель должна быть больше нуля")
    gid = database.add_goal(bid, body.metric, title[:160], body.target, body.deadline)
    actor, actor_id = _actor(x_auth, bid)
    database.add_memory_link(bid, "goal", gid, event="created", source_kind="manual",
                             confidence=1.0, actor=actor, actor_id=actor_id,
                             note="Поставлена вручную")
    return {"ok": True, "id": gid}


@app.post("/api/goals/{goal_id}")
def api_goal_update(goal_id: int, body: GoalPatch, x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, body.business_id)
    database.update_goal(goal_id, bid, title=body.title, target=body.target,
                         deadline=body.deadline, manual_value=body.manual_value,
                         status=body.status)
    return {"ok": True}


@app.post("/api/goals/{goal_id}/delete")
def api_goal_delete(goal_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    database.delete_goal(goal_id, _resolve_bid(x_auth, business_id))
    return {"ok": True}


# ---------- ПАМЯТЬ AI: услуги, товары, правила, цели ----------

class FactIn(BaseModel):
    kind: str                              # service | product | rule | goal
    title: str
    body: str | None = None
    business_id: int = 0


@app.get("/api/facts")
def api_facts(business_id: int = 0, x_auth: str = Header(default="")):
    """Всё, что владелец занёс в память списком — по разделам."""
    return {"facts": database.list_facts(_resolve_bid(x_auth, business_id))}


@app.post("/api/facts")
def api_fact_add(body: FactIn, x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, body.business_id)
    title = (body.title or "").strip()
    if body.kind not in database.FACT_KINDS:
        raise HTTPException(status_code=400, detail="Неизвестный раздел памяти")
    if not title:
        raise HTTPException(status_code=400, detail="Нужно название")
    fid = database.add_fact(bid, body.kind, title[:160], (body.body or "").strip()[:2000] or None)
    entity = entities.FACT_ENTITY_BY_KIND.get(body.kind)
    if entity:
        actor, actor_id = _actor(x_auth, bid)
        database.add_memory_link(bid, entity, fid, event="created", source_kind="manual",
                                 confidence=1.0, actor=actor, actor_id=actor_id,
                                 note="Внесено вручную")
    return {"ok": True, "id": fid}


@app.post("/api/facts/{fact_id}")
def api_fact_update(fact_id: int, body: FactIn, x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, body.business_id)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Нужно название")
    database.update_fact(fact_id, bid, title[:160], (body.body or "").strip()[:2000] or None)
    return {"ok": True}


@app.post("/api/facts/{fact_id}/delete")
def api_fact_delete(fact_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    database.delete_fact(fact_id, _resolve_bid(x_auth, business_id))
    return {"ok": True}


@app.get("/api/health")
def api_health(business_id: int = 0, x_auth: str = Header(default="")):
    """Оценка здоровья бизнеса 0–100: активность, клиенты, финансы, профиль, знания, заявки."""
    return database.business_health(_resolve_bid(x_auth, business_id))


# ---------- СВОИ AI-СОТРУДНИКИ ----------

class AgentIn(BaseModel):
    name: str
    persona: str                       # характер и задача сотрудника своими словами
    avatar: str | None = None
    business_id: int = 0


@app.get("/api/agents")
def api_agents(business_id: int = 0, x_auth: str = Header(default="")):
    """Свои AI-сотрудники бизнеса — показываются чипами рядом с готовыми ролями."""
    return {"agents": database.list_agents(_resolve_bid(x_auth, business_id))}


@app.post("/api/agents")
def api_agent_add(body: AgentIn, x_auth: str = Header(default="")):
    bid = _resolve_bid(x_auth, body.business_id)
    name = (body.name or "").strip()
    persona = (body.persona or "").strip()
    if not name or not persona:
        raise HTTPException(status_code=400, detail="Нужны имя и описание характера")
    aid = database.add_agent(bid, name[:60], persona[:1200], (body.avatar or "").strip()[:4] or None)
    return {"ok": True, "id": aid}


@app.post("/api/agents/{agent_id}/delete")
def api_agent_delete(agent_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    database.delete_agent(agent_id, _resolve_bid(x_auth, business_id))
    return {"ok": True}


# ---------- ТАРИФЫ И ЛИМИТЫ ----------

@app.get("/api/plan")
def api_plan(business_id: int = 0, x_auth: str = Header(default="")):
    """Текущий тариф бизнеса, расход сообщений за месяц и остаток. + список всех тарифов."""
    bid = _resolve_bid(x_auth, business_id)
    business = database.get_business(bid) or {"id": bid, "plan": ""}
    return {"status": database.plan_status(business), "plans": database.PLANS}


# ---------- VELOR RESEARCH (анализ конкурентов) ----------

import urllib.request as _urlreq


class _SafeRedirect(_urlreq.HTTPRedirectHandler):
    """Проверять КАЖДЫЙ адрес в цепочке редиректов. Иначе публичный сайт мог бы
    ответить «перейди на http://169.254.169.254» и обойти проверку на входе."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_public_url(url: str) -> str:
    """Разрешить только публичный http(s)-адрес (защита от SSRF).

    Сама проверка живёт в safeurl.py — её используют и коннекторы, где адрес
    тоже приходит от пользователя (вебхук Bitrix24, домен магазина).
    """
    try:
        return safeurl.normalize(url)
    except safeurl.UnsafeUrl as e:
        raise HTTPException(status_code=400, detail=str(e))


def _fetch_url_text(url: str) -> str:
    """Скачать страницу и вытащить видимый текст (без тегов). '' при ошибке."""
    import re as _re
    import urllib.request
    url = _check_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (VELOR Research)"})
    # Редиректы не отключаем (сайты их используют штатно), но и не даём уводить
    # себя внутрь сети: каждый следующий адрес снова проходит проверку.
    opener = urllib.request.build_opener(_SafeRedirect)
    with opener.open(req, timeout=8) as r:
        raw = r.read(600_000).decode("utf-8", errors="ignore")
    raw = _re.sub(r"(?is)<(script|style|head|nav|footer)[^>]*>.*?</\1>", " ", raw)
    text = _re.sub(r"(?s)<[^>]+>", " ", raw)
    text = _re.sub(r"&[a-z]+;", " ", text)
    return _re.sub(r"\s+", " ", text).strip()


class ResearchIn(BaseModel):
    url: str | None = None
    text: str | None = None
    business_id: int = 0


@app.post("/api/research")
def api_research(body: ResearchIn, x_auth: str = Header(default="")):
    """Анализ конкурента: по ссылке (скачаем сами) или по вставленному тексту."""
    import ai
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    material = (body.text or "").strip()
    # Адрес проверяем ПЕРВЫМ делом, до всего остального. Если поставить проверку
    # после «а настроен ли ИИ», то при выключенном ИИ запрос на внутренний адрес
    # тихо получал бы обычный ответ — то есть защиты от SSRF фактически не было бы.
    if not material and body.url:
        _check_public_url(body.url)
    if not ai.ai_available():
        return {"ok": False, "answer": None}
    if not material and body.url:
        try:
            material = _fetch_url_text(body.url)
        except HTTPException:
            raise           # понятная причина отказа (внутренний адрес, битая ссылка)
        except Exception:
            return {"ok": False, "error": "Не удалось открыть ссылку — проверьте адрес или вставьте текст вручную."}
    if not material:
        return {"ok": False, "error": "Дайте ссылку на конкурента или вставьте описание."}
    business = database.get_business(bid) or {"name": "VELOR AI"}
    try:
        return {"ok": True, "answer": ai.competitor_analysis(business, material[:6000])}
    except Exception:
        return {"ok": False, "answer": None}


# ---------- ДОКУМЕНТЫ (RAG: знания из файлов) ----------

def _extract_text(filename: str, data: bytes) -> str:
    """Достать текст из PDF / DOCX / TXT. Возвращает '' если формат не поддержан."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith(".docx"):
        import io
        import docx
        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    if name.endswith(".txt"):
        for enc in ("utf-8", "cp1251"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
    return ""


# ============================================================
#  UNIVERSAL INBOX — одно место для любого материала о бизнесе
# ============================================================
# Смысл: владелец не должен заранее решать, «это финансы или клиенты».
# Он бросает сюда что угодно — заметку, скриншот, счёт, выписку, прайс, —
# а разбор появится отдельным слоем позже. Поэтому здесь НЕТ ни одной
# догадки о содержимом: мы принимаем, сохраняем оригинал, пишем метаданные
# и ставим статус RECEIVED.

# Пределы и список принимаемых типов живут в intake.py: они одинаковы для
# кабинета, бота и всего, что появится дальше. Здесь — только имена, под
# которыми их знает остальной сервер.
INBOX_MAX_BYTES = intake.MAX_BYTES
INBOX_MAX_FILES = intake.MAX_FILES
INBOX_NOTE_MAX = intake.NOTE_MAX
INBOX_TYPES = intake.TYPES

# Показать в браузере можно только то, что браузер не исполнит. SVG сюда не
# входит намеренно: это документ со скриптами, отданный inline — готовый XSS
# на нашем домене. Всё остальное уходит вложением.
INBOX_INLINE = {"image/jpeg", "image/png", "image/gif", "image/webp",
                "image/bmp", "application/pdf"}


# Разбор имени файла — общий с приёмом: одно и то же имя не должно
# превращаться в разное в зависимости от того, кто его читает.
_inbox_ext = intake.ext_of
_inbox_safe_name = intake.safe_name


def _inbox_disposition(kind: str, filename: str) -> str:
    """
    Заголовок Content-Disposition для имени, в котором может быть кириллица.

    В HTTP-заголовок помещается только latin-1, поэтому «счёт.pdf» валил
    выдачу файла с UnicodeEncodeError. По RFC 5987 имя кладут дважды: ASCII-
    запасное для старых клиентов и filename* в UTF-8 для всех остальных.
    """
    name = _inbox_safe_name(filename)
    # Запасное имя — только печатные ASCII без кавычек, точки с запятой и
    # управляющих символов: всё, чем можно было бы разорвать заголовок.
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "", name).strip()
    # У полностью кириллического имени от ASCII остаётся одно расширение
    # («.png»). Такое имя браузер предложит сохранить как файл без названия —
    # даём внятную основу; полное имя всё равно приедет в filename*.
    if not ascii_name or ascii_name.lstrip(".") == _inbox_ext(name):
        ascii_name = "file" + ("." + _inbox_ext(name) if _inbox_ext(name) else "")
    quoted = _urlquote(name, safe="")
    # filename* по RFC 5987: charset'language'значение — язык не указываем,
    # поэтому между апострофами пусто.
    return (kind + '; filename="' + ascii_name + '"'
            + "; filename*=UTF-8''" + quoted)


def _actor(x_auth: str, bid: int):
    """
    Кто выполняет действие. Владелец VELOR может работать в панели компании —
    и в истории должно быть видно, что правил не сам бизнес, а мы.
    """
    payload = _auth_payload(x_auth) or {}
    role = payload.get("role")
    if role == "business":
        return "business", payload.get("bid") or bid
    if role == "owner":
        return "owner", None
    return "business", bid


# Запуск разбора — тоже часть приёма, а не веб-слоя: бот и почта должны
# разбирать материал так же, как кабинет, и падать так же безобидно.
_inbox_understand = intake.understand
_understand_safely = intake._understand_safely


# Названия модулей аналитики по-человечески: владелец не обязан знать слова
# «briefing» и «forecast», но обязан видеть, что именно пересчиталось.
MODULE_RU = {"forecast": "прогноз", "board": "совет директоров",
             "briefing": "утренний брифинг", "risks": "риски",
             "opportunities": "возможности"}


def _inbox_pipeline(item: dict, result: dict | None, history: list) -> list:
    """
    Путь материала внутри VELOR: принято → прочитано → понято → записано →
    повлияло на выводы.

    Ни одной придуманной стадии. Каждый шаг существует только если у него есть
    подтверждение в базе: строка inbox_items, строка inbox_results, решение в
    inbox_decisions, запись сущности. Последний шаг выводится не из фантазии, а
    из signals.DEPENDENCIES — это тот самый список модулей, которые реально
    помечаются на пересборку, когда такая запись появляется.

    Стадия без подтверждения не рисуется вовсе: показать «понял» там, где
    разбора не было, — значит соврать про работу, которой не делали.
    """
    out = []
    kind = "заметка" if item.get("kind") == "text" else (item.get("mime") or "файл")
    out.append({"key": "received", "label": "Принято",
                "at": item.get("created_at"),
                "detail": f"{kind} · источник: {item.get('source') or 'веб'}"})

    if result:
        engine = result.get("engine")
        if engine:
            how = ("разобрал модель " + (result.get("model") or "")) if engine == "llm" \
                  else "разобрано правилами, без модели"
            out.append({"key": "read", "label": "Прочитано",
                        "at": result.get("created_at"), "detail": how.strip()})
        if result.get("type") and result.get("type") != "UNKNOWN":
            conf = result.get("confidence")
            bits = [result.get("type_ru") or ""]
            if result.get("level_ru"):
                bits.append("уверенность " + result["level_ru"])
            if isinstance(conf, (int, float)) and conf:
                bits.append(f"{round(float(conf) * 100)}%")
            out.append({"key": "understood", "label": "Понято",
                        "at": result.get("created_at"),
                        "detail": " · ".join(b for b in bits if b)})

    # Записано — только по настоящим решениям, у которых есть сущность.
    wrote = [h for h in (history or []) if h.get("entity_type")]
    if wrote:
        names = []
        for h in wrote:
            # Название вида записи берём из реестра entities: интерфейс не
            # должен знать внутренние ключи вроде "expense".
            meta = entities.ENTITIES.get(h["entity_type"]) or {}
            names.append(str(meta.get("title") or h["entity_type"]))
        last = wrote[-1]
        out.append({"key": "recorded", "label": "Записано",
                    "at": last.get("created_at"),
                    "detail": ", ".join(dict.fromkeys(names)),
                    "href": "memory.html"})

        # Повлияло — список модулей, которые из-за этой записи считают заново.
        mods = []
        for h in wrote:
            domain = REACT_DOMAIN.get(h["entity_type"])
            for m in signals.DEPENDENCIES.get(domain, ()):
                if m not in mods:
                    mods.append(m)
        if mods:
            out.append({"key": "affected", "label": "Повлияло на выводы",
                        "at": last.get("created_at"),
                        "detail": ", ".join(MODULE_RU.get(m, m) for m in mods),
                        "href": "dashboard.html"})
    return out


def _inbox_result_public(res: dict | None, item: dict | None = None) -> dict | None:
    """
    Разбор наружу: человеческие подписи и готовая форма подтверждения.

    Форму собираем здесь, а не на фронте: описание полей живёт в entities.py,
    и интерфейс не должен знать, из чего состоит расход или клиент. Добавили
    новый вид записи — форма появилась сама.
    """
    if not res:
        return None
    out = {k: res.get(k) for k in
           ("id", "item_id", "type", "confidence", "level", "summary",
            "extracted_data", "suggested_actions", "engine", "model", "error",
            "applied", "relations", "notes", "created_at")}
    out["type_ru"] = understanding.TYPE_RU.get(res.get("type"), "Не разобрал")
    out["level_ru"] = understanding.LEVEL_RU.get(res.get("level"), "низкая")
    out["needs"] = understanding.NEEDS_RU.get(res.get("level"), "нужно уточнение")

    acts = []
    for a in (res.get("suggested_actions") or []):
        a = dict(a)
        entity = understanding.ENTITY_BY_ACTION.get(a.get("action"))
        if entity:
            try:
                # Схему собираем ПОД БИЗНЕС: списки сотрудников, поставщиков и
                # заявок у каждого свои, и подставить чужие было бы утечкой.
                sch = entities.schema(entity, (item or {}).get("business_id"))
            except entities.EntityError:
                sch = None
            if sch:
                values = understanding.prefill(a["action"], res, item or {})
                a["entity"] = entity
                a["entity_title"] = sch["title"]
                a["where"] = sch["where"]
                a["fields"] = [dict(f, value=(values or {}).get(f["name"], ""))
                               for f in sch["fields"]]
        acts.append(a)
    out["suggested_actions"] = acts
    # Типы для случая «VELOR не понял, скажите сами» — тот же закрытый список.
    out["types"] = [{"type": t, "title": understanding.TYPE_RU[t]}
                    for t in understanding.TYPES if t != "UNKNOWN"]
    return out


def _inbox_public(item: dict) -> dict:
    """Что отдаём наружу. storage_key наружу не уходит: это внутреннее имя
    в хранилище, и знать его клиенту незачем — файл отдаётся по id записи."""
    out = {k: item.get(k) for k in
           ("id", "kind", "title", "body", "filename", "mime", "size",
            "source", "status", "error", "archived_at", "created_at", "updated_at")}
    out["has_file"] = bool(item.get("storage_key"))
    out["can_preview"] = bool(item.get("storage_key")) and item.get("mime") in INBOX_INLINE
    return out


@app.get("/api/inbox")
def api_inbox_list(business_id: int = 0, status: str = "", archived: int = 0,
                   limit: int = 50, offset: int = 0, x_auth: str = Header(default="")):
    """Лента входящих + разбивка по статусам для фильтров."""
    bid = _resolve_bid(x_auth, business_id)
    status = status if status in database.INBOX_STATUSES else None
    arch = bool(archived)
    items = database.list_inbox(bid, status=status, archived=arch,
                               limit=limit, offset=offset)
    # Разборы забираем одним запросом на всю страницу: иначе лента из тридцати
    # материалов сделала бы тридцать походов в базу.
    results = database.get_inbox_results(bid, [i["id"] for i in items])
    decisions = database.count_inbox_decisions(bid, [i["id"] for i in items])
    out = []
    for i in items:
        row = _inbox_public(i)
        row["result"] = _inbox_result_public(results.get(i["id"]), i)
        row["decisions"] = decisions.get(i["id"], 0)
        out.append(row)
    return {
        "items": out,
        "total": database.count_inbox(bid, status=status, archived=arch),
        "overview": database.inbox_overview(bid),
        "statuses": list(database.INBOX_STATUSES),
        "max_mb": INBOX_MAX_BYTES // (1024 * 1024),
        "max_files": INBOX_MAX_FILES,
        "accept": sorted(INBOX_TYPES),
    }


class InboxNote(BaseModel):
    text: str = ""
    title: str | None = None
    source: str = "web"
    business_id: int = 0


def _intake_source(raw: str) -> str:
    """Откуда пришло. Незнакомое слово — «web»: врать про источник нельзя."""
    value = (raw or "web").strip().lower()
    return value if value in database.INBOX_SOURCES else "web"


def _item_public(bid: int, item_id: int, duplicate: bool = False,
                 of: int | None = None) -> dict:
    """Принятый материал наружу: сам материал плюс разбор, если он уже готов."""
    raw = database.get_inbox_item(item_id, bid)
    item = _inbox_public(raw)
    item["result"] = _inbox_result_public(database.get_inbox_result(bid, item_id), raw)
    if duplicate:
        item["duplicate"] = True
        item["duplicate_of"] = of
    note = intake.unreadable_note(raw.get("filename") or "")
    if note:
        item["cannot_read"] = note
    return item


@app.post("/api/inbox")
def api_inbox_note(body: InboxNote, x_auth: str = Header(default="")):
    """Текстовая заметка: самый частый способ что-то «скинуть» на ходу."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    actor, actor_id = _actor(x_auth, bid)
    try:
        got = intake.receive_text(bid, body.text or "", title=body.title,
                                  source=_intake_source(body.source),
                                  actor=actor, actor_id=actor_id)
    except intake.IntakeError as e:
        # Слишком длинная заметка — это «не помещается», а не «неверный запрос»:
        # у 413 в интерфейсе своя подсказка, и терять её нельзя.
        code = 413 if "длиннее" in str(e) else 400
        raise HTTPException(status_code=code, detail=str(e))
    return {"ok": True,
            "item": _item_public(bid, got["item_id"], got["duplicate"], got["of"])}


@app.post("/api/inbox/upload")
async def api_inbox_upload(files: list[UploadFile] = File(...),
                           business_id: int = 0, source: str = "web",
                           x_auth: str = Header(default="")):
    """
    Приём файлов. Одним запросом можно прислать несколько — результат по
    каждому отдельный: один битый файл не должен отменять остальные.
    """
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    if not files:
        raise HTTPException(status_code=400, detail="Не выбран ни один файл.")
    if len(files) > INBOX_MAX_FILES:
        raise HTTPException(status_code=413,
                            detail=f"За один раз — не больше {INBOX_MAX_FILES} файлов.")

    actor, actor_id = _actor(x_auth, bid)
    saved, failed = [], []
    for f in files:
        name = _inbox_safe_name(f.filename)
        try:
            data = await f.read()
        except Exception:
            failed.append({"filename": name, "error": "Файл не удалось прочитать"})
            continue
        try:
            got = intake.receive_file(bid, f.filename or name, data,
                                      source=_intake_source(source),
                                      actor=actor, actor_id=actor_id)
        except intake.IntakeError as e:
            failed.append({"filename": name, "error": str(e).rstrip(".")})
            continue
        saved.append(_item_public(bid, got["item_id"], got["duplicate"], got["of"]))

    return {"ok": bool(saved), "saved": saved, "failed": failed}


@app.post("/api/inbox/intake")
async def api_intake(text: str = Form(default=""),
                     files: list[UploadFile] = File(default=None),
                     source: str = Form(default="web"),
                     business_id: int = Form(default=0),
                     x_auth: str = Header(default="")):
    """
    Одна дверь: текст и файлы одной отправкой.

    Так человек и говорит: «вот прайс, цены с сентября» — фраза и файл вместе.
    Раньше это были два запроса, и пояснение отрывалось от того, что оно
    поясняет. Ни одного поля «выберите тип» здесь нет и не будет: разбираться,
    что прислали, — работа VELOR, а не отправителя.

    Отвечаем по каждой части отдельно: принято, уже было, не приняли. Один
    плохой файл в пачке не отменяет остальные.
    """
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    actor, actor_id = _actor(x_auth, bid)

    payload = []
    for f in (files or []):
        try:
            payload.append((f.filename or "файл", await f.read()))
        except Exception:
            payload.append((f.filename or "файл", b""))   # пустое отсеет приём

    try:
        got = intake.receive(bid, text=text, files=payload,
                             source=_intake_source(source),
                             actor=actor, actor_id=actor_id)
    except intake.IntakeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "ok": bool(got["accepted"]),
        "accepted": [_item_public(bid, i) for i in got["accepted"]],
        # Повтор — не ошибка и не успех. Показываем ровно то, что случилось:
        # «это уже есть, вот оно» — и ведём к первому материалу, из которого
        # выводы уже сделаны.
        "duplicates": [dict(_item_public(bid, d["id"], True, d["of"]),
                            what=d["what"]) for d in got["duplicates"]],
        "rejected": got["rejected"],
    }


@app.get("/api/inbox/{item_id}")
def api_inbox_item(item_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """Одна запись целиком — для просмотра."""
    bid = _resolve_bid(x_auth, business_id)
    item = database.get_inbox_item(item_id, bid)
    if not item:
        raise HTTPException(status_code=404, detail="Материал не найден.")
    res = _inbox_result_public(database.get_inbox_result(bid, item_id), item)
    hist = database.list_inbox_decisions(bid, item_id)
    pub = _inbox_public(item)
    return {"item": pub, "result": res, "history": hist,
            "pipeline": _inbox_pipeline(pub, res, hist)}


@app.get("/api/inbox/{item_id}/file")
def api_inbox_file(item_id: int, business_id: int = 0, download: int = 0,
                   x_auth: str = Header(default="")):
    """
    Отдать оригинал. Проверка владельца здесь обязательна и делается по базе,
    а не по ключу: ключ нигде не публикуется, но право на файл определяет
    именно запись, а не знание имени.
    """
    bid = _resolve_bid(x_auth, business_id)
    item = database.get_inbox_item(item_id, bid)
    if not item or not item.get("storage_key"):
        raise HTTPException(status_code=404, detail="У этого материала нет файла.")
    try:
        data = storage.get(bid, item["storage_key"])
    except storage.StorageError:
        # Запись есть, а файла нет — честно говорим, что оригинал потерян,
        # и помечаем материал, чтобы это было видно в ленте.
        database.set_inbox_status(item_id, bid, "FAILED", "Оригинал не найден в хранилище")
        raise HTTPException(status_code=410, detail="Оригинал файла не найден в хранилище.")

    mime = item.get("mime") or "application/octet-stream"
    inline = (not download) and mime in INBOX_INLINE
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": _inbox_disposition(
                "inline" if inline else "attachment", item.get("filename")),
            # Браузер не должен угадывать тип сам: угаданный text/html из
            # пользовательского файла — это выполнение чужой разметки на нашем домене.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # Кэш короткий и только приватный: повторный просмотр не тянет
            # файл заново, но удалённый материал не живёт в браузере полчаса.
            "Cache-Control": "private, max-age=60",
        },
    )


@app.get("/api/inbox/{item_id}/result")
def api_inbox_result(item_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """Что VELOR понял про материал (последний разбор)."""
    bid = _resolve_bid(x_auth, business_id)
    if not database.get_inbox_item(item_id, bid):
        raise HTTPException(status_code=404, detail="Материал не найден.")
    return {"result": _inbox_result_public(database.get_inbox_result(bid, item_id))}


@app.post("/api/inbox/{item_id}/process")
def api_inbox_process(item_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """
    Разобрать заново. Нужен, когда модель была недоступна в момент приёма или
    ответила мимо: прошлый разбор при этом сохраняется, а не затирается.
    """
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    if not database.get_inbox_item(item_id, bid):
        raise HTTPException(status_code=404, detail="Материал не найден.")
    try:
        understanding.process(bid, item_id)
    except Exception:
        logging.exception("Inbox: повторный разбор упал (biz %s, материал %s)", bid, item_id)
        database.set_inbox_status(item_id, bid, "FAILED", "Разбор не удался")
        raise HTTPException(status_code=502, detail="Не удалось разобрать материал.")
    raw = database.get_inbox_item(item_id, bid)
    return {"ok": True,
            "result": _inbox_result_public(database.get_inbox_result(bid, item_id), raw),
            "item": _inbox_public(raw)}


class InboxAction(BaseModel):
    action: str
    data: dict | None = None      # значения из формы; нет — берём предложенные ИИ
    business_id: int = 0


class InboxDismiss(BaseModel):
    reason: str = ""
    business_id: int = 0


class InboxClassify(BaseModel):
    type: str
    business_id: int = 0


@app.post("/api/inbox/{item_id}/action")
def api_inbox_action(item_id: int, body: InboxAction, x_auth: str = Header(default="")):
    """
    Выполнить предложенное действие — по нажатию человека.

    Именно здесь проходит граница безопасности: расход и доход попадают в
    финансы ТОЛЬКО отсюда. Сам VELOR такие действия не выполняет ни при какой
    уверенности (см. understanding.apply_action, auto=True).
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    res = database.get_inbox_result(bid, item_id)
    if not res:
        raise HTTPException(status_code=409, detail="Материал ещё не разобран.")
    already = {a.get("action") for a in (res.get("applied") or [])}
    if body.action in already:
        raise HTTPException(status_code=409, detail="Это действие уже выполнено.")
    offered = {a.get("action") for a in (res.get("suggested_actions") or [])}
    if body.action not in offered:
        # Выполнять можно только предложенное: иначе через этот эндпоинт можно
        # было бы провести любую операцию, сославшись на чужой разбор.
        raise HTTPException(status_code=400, detail="Такое действие для этого материала не предлагалось.")
    item = database.get_inbox_item(item_id, bid)
    # Что предлагал ИИ — фиксируем ДО выполнения: после создания записи узнать
    # исходное предложение будет уже неоткуда.
    proposed = understanding.prefill(body.action, res, item) or {}
    try:
        detail, entity, entity_id, used, made = understanding.apply_action(
            bid, item_id, body.action, res, auto=False, data=body.data)
    except understanding.ActionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logging.exception("Inbox: действие не выполнено (biz %s, материал %s)", bid, item_id)
        raise HTTPException(status_code=502, detail="Не удалось выполнить действие.")

    changes = understanding.diff(proposed, used) if body.data is not None else {}
    actor, actor_id = _actor(x_auth, bid)
    said_type, said_id = understanding.named_result(entity, entity_id, made)
    decision_id = database.add_inbox_decision(
        bid, item_id, "edited" if changes else "confirmed", result_id=res["id"],
        action=body.action, entity_type=said_type, entity_id=said_id,
        original=proposed, corrected=used, changes=changes,
        actor=actor, actor_id=actor_id, note=detail)
    # Знание родилось — записываем, из чего. Без этой строки запись в памяти
    # выглядит как взявшаяся ниоткуда, и проверить её будет нечем. У прайса
    # записей столько же, сколько позиций: происхождение нужно каждой.
    for one in (made or []):
        database.add_memory_link(
            bid, one["entity_type"], one["entity_id"],
            event="edited" if one["what"] == "updated" else "created",
            source_kind="inbox", item_id=item_id, result_id=res["id"],
            decision_id=decision_id, confidence=res.get("confidence"),
            actor=actor, actor_id=actor_id,
            note=("Обновлено из прайса: " if one["what"] == "updated"
                  else "Из прайса: ") + one["title"][:120])
    if entity and entity_id and not made:
        database.add_memory_link(
            bid, entity, entity_id, event="created", source_kind="inbox",
            item_id=item_id, result_id=res["id"], decision_id=decision_id,
            confidence=res.get("confidence"), actor=actor, actor_id=actor_id,
            note=detail)

    applied = list(res.get("applied") or [])
    applied.append({"action": body.action, "auto": False, "detail": detail,
                    "entity_type": said_type, "entity_id": said_id,
                    "items": len(made) or None,
                    "edited": bool(changes)})
    database.mark_result_applied(res["id"], bid, applied)
    # То же предложение видно и в списке решений. Это одна запись, показанная с
    # двух сторон, — подтвердить её дважды нельзя, иначе расход попал бы в
    # финансы двумя строками.
    understanding.close_proposals(bid, item_id, body.action)
    said_action = understanding.POLICY_ACTION.get(body.action)
    if said_action:
        # Сделанное рукой владельца попадает в тот же журнал, что сделанное
        # VELOR. Разделить их на «настоящие действия» и «действия ИИ» значило
        # бы получить две истории одного дела.
        actions.record(bid, said_action, actor=actor, actor_id=actor_id,
                       mode=actions.MANUAL, status=database.AC_SUCCEEDED,
                       target_type=said_type or "inbox",
                       target_id=said_id or item_id,
                       reason="Вы подтвердили: " + (detail or "")[:200],
                       based_on={"item_id": item_id, "inbox_action": body.action},
                       before=proposed if changes else None, after=used,
                       result=(detail or "")[:200])
    if body.action != "ask_user":
        database.set_inbox_status(item_id, bid, "PROCESSED")
    raw = database.get_inbox_item(item_id, bid)
    return {"ok": True, "detail": detail, "entity_type": said_type, "entity_id": said_id,
            "changes": changes,
            "result": _inbox_result_public(database.get_inbox_result(bid, item_id), raw),
            "item": _inbox_public(raw),
            "history": database.list_inbox_decisions(bid, item_id)}


@app.post("/api/inbox/{item_id}/dismiss")
def api_inbox_dismiss(item_id: int, body: InboxDismiss, x_auth: str = Header(default="")):
    """
    «Не учитывать»: VELOR понял неправильно или материал не нужен.

    Ничего не удаляем — ни материал, ни разбор. Отказ это тоже сведение о
    качестве работы ИИ, и он должен остаться в истории.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    item = database.get_inbox_item(item_id, bid)
    if not item:
        raise HTTPException(status_code=404, detail="Материал не найден.")
    res = database.get_inbox_result(bid, item_id)
    actor, actor_id = _actor(x_auth, bid)
    database.add_inbox_decision(
        bid, item_id, "dismissed", result_id=(res or {}).get("id"),
        original=(res or {}).get("extracted_data") or {},
        actor=actor, actor_id=actor_id,
        note=(body.reason or "").strip()[:300] or "Отклонено владельцем")
    understanding.close_proposals(bid, item_id,
                                  reason="Вы отклонили материал во «Входящих».")
    database.set_inbox_status(item_id, bid, "PROCESSED")
    raw = database.get_inbox_item(item_id, bid)
    return {"ok": True,
            "result": _inbox_result_public(database.get_inbox_result(bid, item_id), raw),
            "item": _inbox_public(raw),
            "history": database.list_inbox_decisions(bid, item_id)}


@app.post("/api/inbox/{item_id}/classify")
def api_inbox_classify(item_id: int, body: InboxClassify, x_auth: str = Header(default="")):
    """
    «Уточнить»: при низкой уверенности человек сам говорит, что это.

    Ответ человека — не догадка, поэтому уверенность становится полной, а
    прошлый разбор ИИ сохраняется рядом: по паре «что решил ИИ / что сказал
    человек» и видно, где модель ошибается.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    item = database.get_inbox_item(item_id, bid)
    if not item:
        raise HTTPException(status_code=404, detail="Материал не найден.")
    kind = (body.type or "").strip().upper()
    if kind not in understanding.TYPES or kind == "UNKNOWN":
        raise HTTPException(status_code=400, detail="Неизвестный вид материала.")

    was = database.get_inbox_result(bid, item_id) or {}
    extracted = dict(was.get("extracted_data") or {})
    result = {
        "type": kind, "confidence": 1.0, "level": "HIGH",
        "summary": understanding.TYPE_RU[kind] + " — определил владелец",
        "extracted_data": extracted,
        "suggested_actions": understanding.suggest(kind, extracted),
        "engine": "human", "model": None, "error": None, "applied": [],
    }
    database.save_inbox_result(bid, item_id, result)
    database.set_inbox_status(item_id, bid, "NEEDS_REVIEW")
    actor, actor_id = _actor(x_auth, bid)
    database.add_inbox_decision(
        bid, item_id, "classified", result_id=was.get("id"),
        original={"type": was.get("type")}, corrected={"type": kind},
        changes=understanding.diff({"type": was.get("type")}, {"type": kind}),
        actor=actor, actor_id=actor_id,
        note="Владелец уточнил: " + understanding.TYPE_RU[kind])
    raw = database.get_inbox_item(item_id, bid)
    return {"ok": True,
            "result": _inbox_result_public(database.get_inbox_result(bid, item_id), raw),
            "item": _inbox_public(raw),
            "history": database.list_inbox_decisions(bid, item_id)}


@app.get("/api/inbox/{item_id}/history")
def api_inbox_history(item_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """История решений по материалу: кто, когда, что подтвердил и что исправил."""
    bid = _resolve_bid(x_auth, business_id)
    if not database.get_inbox_item(item_id, bid):
        raise HTTPException(status_code=404, detail="Материал не найден.")
    return {"history": database.list_inbox_decisions(bid, item_id),
            "stats": database.inbox_correction_stats(bid)}


@app.post("/api/inbox/{item_id}/archive")
def api_inbox_archive(item_id: int, business_id: int = 0, undo: int = 0,
                      x_auth: str = Header(default="")):
    """Убрать из ленты, не теряя материал (undo=1 — вернуть обратно)."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    if not database.archive_inbox_item(item_id, bid, archived=not undo):
        raise HTTPException(status_code=404, detail="Материал не найден.")
    return {"ok": True, "item": _inbox_public(database.get_inbox_item(item_id, bid))}


@app.post("/api/inbox/{item_id}/delete")
def api_inbox_delete(item_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """
    Удалить материал вместе с оригиналом. Сначала запись, потом файл: если
    упасть между шагами, лучше остаться с осиротевшим файлом в хранилище,
    чем со ссылкой на файл, которого уже нет.
    """
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    item = database.get_inbox_item(item_id, bid)
    if not item:
        raise HTTPException(status_code=404, detail="Материал не найден.")
    database.delete_inbox_item(item_id, bid)
    if item.get("storage_key"):
        try:
            storage.delete(bid, item["storage_key"])
        except Exception:
            logging.exception("Inbox: запись удалена, оригинал остался (biz %s)", bid)
    return {"ok": True}


# ---------- ПАМЯТЬ БИЗНЕСА ----------
# Одно место, где видно всё, что VELOR знает о бизнесе, и главное — откуда он
# это знает. Ни один вид записи здесь не зашит: разделы, поля и подписи берутся
# из реестра entities.py, а происхождение — из memory_links.

ACTOR_RU = {"business": "владелец", "owner": "поддержка VELOR", "velor": "VELOR"}

# Какое изменение какую аналитику пересобирает. Владелец исправил сумму —
# прибыль, прогноз, Директор и утренний брифинг должны считать заново, а не
# на следующий день: иначе на одном экране одна правда, на другом другая.
REACT_DOMAIN = {"expense": "finance", "income": "finance", "order": "order",
                "client": "client", "document": "document", "goal": "goal",
                "service": "memory", "product": "memory", "rule": "memory",
                "employee": "memory", "supplier": "memory", "company": "memory"}


def _react(bid: int, entity_type: str) -> None:
    domain = REACT_DOMAIN.get(entity_type)
    if domain:
        signals.react(bid, domain)


def _ai_values(history: list) -> dict:
    """
    Что предлагал ИИ, когда запись появилась.

    Берём из решения (original) — это ровно то, что показали человеку, — а
    если решения не было, из самого разбора. Правка владельца лежит отдельно,
    поэтому исходное предложение ИИ не затирается никогда: без него нельзя
    сказать, где он ошибается.
    """
    created = next((h for h in history if h.get("event") == "created"), None)
    if not created:
        return {}
    original = ((created.get("decision") or {}).get("original")
                or (created.get("result") or {}).get("extracted") or {})
    return {k: v for k, v in dict(original).items() if not str(k).endswith("_id")}


def _corrections(fields: list, ai: dict, values: dict) -> list:
    """Где живое значение разошлось с предложением ИИ."""
    out = []
    for fl in fields:
        name = fl["name"]
        if name not in ai:
            continue
        was, now = ai.get(name), values.get(name)
        if str(was if was is not None else "").strip() == str(now if now is not None else "").strip():
            continue
        out.append({"field": name, "label": fl["label"], "ai": was, "now": now})
    return out


# Три разных происхождения, и путать их нельзя. «VELOR записал сам» — это
# автоприменение. «VELOR предложил, вы подтвердили» — разбор, который прошёл
# через человека. «Внесено вручную» — человек с нуля. Доверие к цифре у них
# разное, поэтому и подпись разная.
BY_RU = {"velor": "записал VELOR",
         "confirmed": "VELOR предложил, вы подтвердили",
         "manual": "внесено вручную"}


def _link_by(link) -> str:
    if not link:
        return ""
    if link.get("actor") == "velor":
        return "velor"
    return "confirmed" if link.get("source_kind") == "inbox" else "manual"


def _memory_link_public(link: dict) -> dict:
    """Одно событие памяти наружу: что случилось, откуда и с какой уверенностью."""
    out = {k: link.get(k) for k in
           ("id", "event", "source_kind", "item_id", "result_id", "decision_id",
            "confidence", "changes", "actor", "note", "created_at")}
    out["actor_ru"] = ACTOR_RU.get(link.get("actor"), link.get("actor") or "")
    out["by"] = _link_by(link)
    out["by_ru"] = BY_RU.get(out["by"], "")
    if link.get("item_id"):
        out["item"] = {"id": link["item_id"],
                       "title": link.get("item_title") or link.get("item_filename"),
                       "filename": link.get("item_filename"),
                       "kind": link.get("item_kind"),
                       "archived": bool(link.get("item_archived"))}
    if link.get("result_id"):
        out["result"] = {"id": link["result_id"],
                         "type": link.get("result_type"),
                         "type_ru": understanding.TYPE_RU.get(link.get("result_type"), ""),
                         "confidence": link.get("result_confidence"),
                         "summary": link.get("result_summary"),
                         "extracted": link.get("result_extracted") or {}}
    if link.get("decision_id"):
        out["decision"] = {"decision": link.get("decision"),
                           "action": link.get("decision_action"),
                           "original": link.get("decision_original") or {},
                           "corrected": link.get("decision_corrected") or {},
                           "changes": link.get("decision_changes") or {}}
    return out


def _safe_graph(bid: int, entity_type: str, entity_id: int):
    """Связи для карточки. Упали — карточка всё равно открывается."""
    try:
        return graph.neighbors(bid, entity_type, entity_id)
    except Exception:
        logging.exception("Связи не собрались (biz %s, %s %s)", bid, entity_type, entity_id)
        return None


def _memory_type(entity_type: str):
    try:
        return entities.schema(entity_type)
    except entities.EntityError:
        raise HTTPException(status_code=404, detail="Такого вида записей нет.")


@app.get("/api/memory/map")
def api_memory_map(business_id: int = 0, x_auth: str = Header(default="")):
    """
    Что VELOR знает о бизнесе — по разделам, с долей знаний, у которых записан
    источник. Доля важнее количества: знание без источника нечем проверить.
    """
    bid = _resolve_bid(x_auth, business_id)
    sourced = database.memory_sourced_counts(bid)
    groups, total, linked_all, documented_all = [], 0, 0, 0
    for key, title in entities.GROUPS.items():
        kinds = []
        for t in entities.types_of_group(key):
            sch = entities.schema(t)
            n = entities.count(bid, t)
            got = sourced.get(t) or {}
            linked = min(int(got.get("linked", 0)), n)
            documented = min(int(got.get("documented", 0)), n)
            total += n
            linked_all += linked
            documented_all += documented
            kinds.append({"type": t, "title": sch["title"], "plural": sch["plural"],
                          "where": sch["where"], "can_edit": sch["can_edit"],
                          "can_create": sch["can_create"],
                          "can_archive": sch["can_archive"],
                          "can_delete": sch["can_delete"], "count": n,
                          "archived": entities.count(bid, t, archived=True),
                          "linked": linked, "documented": documented})
        if kinds:
            groups.append({"key": key, "title": title, "types": kinds,
                           "count": sum(k["count"] for k in kinds)})
    return {"groups": groups, "total": total, "linked": linked_all,
            "documented": documented_all,
            "recent": [_memory_link_public(l) for l in database.memory_recent(bid, 8)]}


@app.get("/api/memory/list")
def api_memory_list(type: str = "", business_id: int = 0, limit: int = 50, offset: int = 0,
                    archived: int = 0, x_auth: str = Header(default="")):
    """
    Записи одного вида вместе с их происхождением.

    archived=1 — то, что владелец убрал из работы. Архив должен быть виден:
    невидимый архив ничем не отличается от удаления.
    """
    bid = _resolve_bid(x_auth, business_id)
    sch = _memory_type(type)
    limit = max(1, min(int(limit or 50), 200))
    items = entities.listing(bid, type, limit=limit, offset=max(0, int(offset or 0)),
                             archived=bool(archived))
    ids = [i["id"] for i in items]
    origins = database.memory_origins(bid, type, ids)
    edits = database.memory_edit_counts(bid, type, ids)
    links = database.entity_link_counts(bid, type, ids)
    for it in items:
        link = origins.get(it["id"])
        it["source"] = _memory_link_public(link) if link else None
        it["edits"] = int(edits.get(it["id"], 0))
        it["links"] = int(links.get(it["id"], 0))
    return {"type": type, "title": sch["title"], "plural": sch["plural"],
            "where": sch["where"], "can_edit": sch["can_edit"],
            "can_create": sch["can_create"], "can_archive": sch["can_archive"],
            "can_delete": sch["can_delete"], "archived": bool(archived),
            "fields": sch["fields"] if sch["can_create"] else [],
            "total": entities.count(bid, type, archived=bool(archived)),
            "archived_total": entities.count(bid, type, archived=True),
            "items": items}


@app.get("/api/memory/entity/{entity_type}/{entity_id}")
def api_memory_entity(entity_type: str, entity_id: int, business_id: int = 0,
                      x_auth: str = Header(default="")):
    """
    Одна запись целиком: значения полей, источник, что из него извлекли и вся
    история правок. Оригинал документа остаётся доступен по item_id.
    """
    bid = _resolve_bid(x_auth, business_id)
    sch = _memory_type(entity_type)
    # Схему собираем под бизнес: в форме правки списки сотрудников и заявок
    # должны быть свои.
    sch = entities.schema(entity_type, bid)
    values = entities.read(bid, entity_type, entity_id)
    if values is None:
        raise HTTPException(status_code=404, detail="Запись не найдена.")
    raw = entities.row(bid, entity_type, entity_id) or {}
    history = [_memory_link_public(l)
               for l in database.memory_links(bid, entity_type, entity_id)]
    source = next((h for h in history if h["event"] == "created"), None)
    ai = _ai_values(history)
    return {"entity": entity_type, "id": entity_id,
            "schema": sch, "values": values,
            "label": entities.label(entity_type, raw),
            "created_at": raw.get("created_at"),
            "archived": entities.is_archived(entity_type, raw),
            "blocked": entities.blockers(bid, entity_type, entity_id),
            "origin": entities.origin_of(entity_type, raw),
            "ai": ai, "corrections": _corrections(sch["fields"], ai, values),
            "source": source, "history": history,
            "graph": _safe_graph(bid, entity_type, entity_id)}


class MemoryEdit(BaseModel):
    data: dict
    business_id: int = 0


class ArchiveIn(BaseModel):
    on: bool = True
    business_id: int = 0


@app.post("/api/memory/entity/{entity_type}")
def api_memory_create(entity_type: str, body: MemoryEdit,
                      x_auth: str = Header(default="")):
    """
    Завести запись руками — любую: услугу, сотрудника, правило, цель, клиента,
    заявку, доход, расход. Дорога та же, что у подтверждённого разбора, и
    запись в истории получается такая же — меняется только автор.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    sch = _memory_type(entity_type)
    if not sch["can_create"]:
        raise HTTPException(status_code=400,
                            detail="«%s» так не создаётся." % sch["title"])
    try:
        eid, detail, _clean = entities.create(bid, entity_type, body.data or {})
    except entities.EntityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logging.exception("Память: не удалось создать %s (biz %s)", entity_type, bid)
        raise HTTPException(status_code=502, detail="Не удалось сохранить запись.")
    actor, actor_id = _actor(x_auth, bid)
    database.add_memory_link(bid, entity_type, eid, event="created", source_kind="manual",
                             confidence=1.0, actor=actor, actor_id=actor_id, note=detail)
    _react(bid, entity_type)
    raw = entities.row(bid, entity_type, eid) or {}
    return {"ok": True, "id": eid, "detail": detail,
            "label": entities.label(entity_type, raw)}


@app.post("/api/memory/entity/{entity_type}/{entity_id}/archive")
def api_memory_archive(entity_type: str, entity_id: int, body: ArchiveIn,
                       x_auth: str = Header(default="")):
    """
    Убрать запись из работы или вернуть обратно.

    Архив — не удаление: запись цела, история цела, старые операции
    продолжают показывать имя. Из знаний ядра она при этом уходит.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    _memory_type(entity_type)
    try:
        detail = entities.archive(bid, entity_type, entity_id, body.on)
    except entities.EntityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    actor, actor_id = _actor(x_auth, bid)
    database.add_memory_link(bid, entity_type, entity_id,
                             event="archived" if body.on else "restored",
                             source_kind="manual", actor=actor, actor_id=actor_id,
                             note=detail)
    _react(bid, entity_type)
    history = [_memory_link_public(l)
               for l in database.memory_links(bid, entity_type, entity_id)]
    return {"ok": True, "archived": bool(body.on), "detail": detail, "history": history}


@app.post("/api/memory/entity/{entity_type}/{entity_id}/delete")
def api_memory_delete(entity_type: str, entity_id: int, business_id: int = 0,
                      x_auth: str = Header(default="")):
    """
    Удалить запись насовсем.

    Запись уходит, след — нет: что удалили, кто и когда, остаётся в истории.
    Если на запись ссылаются деньги или заказы, удаление не проходит и
    объясняет почему: оборванная ссылка хуже лишней строки.
    """
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    _memory_type(entity_type)
    try:
        gone = entities.delete(bid, entity_type, entity_id)
    except entities.EntityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    actor, actor_id = _actor(x_auth, bid)
    database.add_memory_link(bid, entity_type, entity_id, event="removed",
                             source_kind="manual", actor=actor, actor_id=actor_id,
                             note="Удалено: " + gone)
    _react(bid, entity_type)
    return {"ok": True, "detail": "Удалено: " + gone}


@app.post("/api/memory/entity/{entity_type}/{entity_id}")
def api_memory_edit(entity_type: str, entity_id: int, body: MemoryEdit,
                    x_auth: str = Header(default="")):
    """
    Изменить знание. Что было и что стало — в истории; исходное предложение ИИ
    остаётся нетронутым, поэтому видно, где он ошибся и как это поправили.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    _memory_type(entity_type)
    try:
        detail, now, before = entities.update(bid, entity_type, entity_id, body.data or {})
    except entities.EntityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logging.exception("Память: правка не удалась (biz %s, %s %s)",
                          bid, entity_type, entity_id)
        raise HTTPException(status_code=502, detail="Не удалось сохранить правку.")

    changes = understanding.diff(before, now)
    if changes:
        actor, actor_id = _actor(x_auth, bid)
        database.add_memory_link(bid, entity_type, entity_id, event="edited",
                                 source_kind="manual", changes=changes,
                                 actor=actor, actor_id=actor_id, note=detail)
    _react(bid, entity_type)
    raw = entities.row(bid, entity_type, entity_id) or {}
    history = [_memory_link_public(l)
               for l in database.memory_links(bid, entity_type, entity_id)]
    ai = _ai_values(history)
    return {"ok": True, "detail": detail, "changes": changes, "values": now,
            "label": entities.label(entity_type, raw), "history": history,
            "ai": ai, "corrections": _corrections(
                entities.schema(entity_type, bid)["fields"], ai, now)}


class LinkIn(BaseModel):
    src_type: str
    src_id: int
    dst_type: str
    dst_id: int
    kind: str = "related"
    business_id: int = 0


class UnlinkIn(BaseModel):
    link_id: int
    business_id: int = 0


@app.get("/api/graph/entity/{entity_type}/{entity_id}")
def api_graph_entity(entity_type: str, entity_id: int, business_id: int = 0,
                     x_auth: str = Header(default="")):
    """
    С чем связана запись: люди, заявки, деньги, услуги, источник.

    Ничего не выдумываем: связи берутся из колонок, рёбер и журнала памяти.
    Пусто — значит, эта запись пока ни с чем не связана, и так и написано.
    """
    bid = _resolve_bid(x_auth, business_id)
    try:
        data = graph.neighbors(bid, entity_type, entity_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Такого вида записей нет.")
    if data is None:
        raise HTTPException(status_code=404, detail="Запись не найдена.")
    return data


@app.get("/api/graph/client/{client_id}")
def api_graph_client(client_id: int, business_id: int = 0,
                     x_auth: str = Header(default="")):
    """
    Досье клиента одним ответом: что брал, на сколько, когда и что любит.

    Тот же контекст, который VELOR подкладывает себе, отвечая клиенту на
    «хочу повторить прошлый заказ» — владелец должен видеть его глазами.
    """
    bid = _resolve_bid(x_auth, business_id)
    d = graph.client_dossier(bid, client_id)
    if not d:
        raise HTTPException(status_code=404, detail="Клиент не найден.")
    return d


@app.post("/api/graph/link")
def api_graph_link(body: LinkIn, x_auth: str = Header(default="")):
    """Связать две записи руками. Обе должны быть свои и существовать."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    for t, i in ((body.src_type, body.src_id), (body.dst_type, body.dst_id)):
        if t not in entities.ENTITIES:
            raise HTTPException(status_code=400, detail="Неизвестный вид записи.")
        if entities.row(bid, t, i) is None:
            raise HTTPException(status_code=404, detail="Запись не найдена.")
    if body.src_type == body.dst_type and body.src_id == body.dst_id:
        raise HTTPException(status_code=400, detail="Запись нельзя связать сама с собой.")
    kind = body.kind if body.kind in graph.KIND_RU else "related"
    actor, actor_id = _actor(x_auth, bid)
    link_id = database.add_entity_link(bid, body.src_type, body.src_id,
                                       body.dst_type, body.dst_id, kind=kind,
                                       source="manual", note="связано вручную")
    # Связь — тоже знание о бизнесе, и её появление должно быть видно в
    # истории обеих записей, а не только в самой связи.
    lb = entities.label(body.dst_type, entities.row(bid, body.dst_type, body.dst_id))
    database.add_memory_link(bid, body.src_type, body.src_id, event="linked",
                             source_kind="manual", actor=actor, actor_id=actor_id,
                             note="Связано: " + (lb.get("title") or ""))
    return {"ok": True, "id": link_id,
            "graph": graph.neighbors(bid, body.src_type, body.src_id)}


@app.post("/api/graph/unlink")
def api_graph_unlink(body: UnlinkIn, x_auth: str = Header(default="")):
    """Убрать связь. Сами записи остаются на месте."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    database.delete_entity_link(body.link_id, bid)
    return {"ok": True}


@app.get("/api/memory/source/{item_id}")
def api_memory_source(item_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """Обратный взгляд: что выросло из одного материала."""
    bid = _resolve_bid(x_auth, business_id)
    item = database.get_inbox_item(item_id, bid)
    if not item:
        raise HTTPException(status_code=404, detail="Материал не найден.")
    grown = []
    for link in database.memory_from_item(bid, item_id):
        t = link["entity_type"]
        if t not in entities.ENTITIES:
            continue
        raw = entities.row(bid, t, link["entity_id"])
        if not raw:
            continue          # запись удалили — цепочка обрывается честно
        lb = entities.label(t, raw)
        grown.append({"entity": t, "id": link["entity_id"],
                      "entity_title": entities.ENTITIES[t]["title"],
                      "title": lb["title"], "sub": lb.get("sub") or "",
                      "confidence": link.get("confidence"),
                      "created_at": link.get("created_at")})
    return {"item": _inbox_public(item),
            "result": _inbox_result_public(database.get_inbox_result(bid, item_id), item),
            "entities": grown}


@app.get("/api/documents")
def api_documents(business_id: int = 0, x_auth: str = Header(default="")):
    """Список загруженных документов бизнеса."""
    bid = _resolve_bid(x_auth, business_id)
    return {"documents": database.list_documents(bid)}


@app.post("/api/documents/upload")
async def api_documents_upload(file: UploadFile = File(...),
                               business_id: int = 0,
                               x_auth: str = Header(default="")):
    """Загрузить PDF/DOCX/TXT: извлекаем текст, режем на чанки, кладём в знания."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        return {"ok": False, "error": "Файл больше 8 МБ"}
    try:
        text = _extract_text(file.filename, data)
    except Exception:
        return {"ok": False, "error": "Не удалось прочитать файл"}
    if not text.strip():
        return {"ok": False, "error": "Пустой файл или неподдерживаемый формат (нужен PDF, DOCX или TXT)"}
    doc_id, n = database.add_document(bid, file.filename, text)
    actor, actor_id = _actor(x_auth, bid)
    database.add_memory_link(bid, "document", doc_id, event="created", source_kind="manual",
                             confidence=1.0, actor=actor, actor_id=actor_id,
                             note="Загружен файл: " + (file.filename or "документ"))
    # анализ документа → риск для Директора/брифинга (по тексту, детерминированно)
    signals.react(bid, "document", {"text": text, "filename": file.filename})
    return {"ok": True, "doc_id": doc_id, "chunks": n}


@app.post("/api/documents/{doc_id}/delete")
def api_documents_delete(doc_id: int, business_id: int = 0,
                         x_auth: str = Header(default="")):
    """Удалить документ и его чанки."""
    bid = _resolve_bid(x_auth, business_id)
    database.delete_document(doc_id, bid)
    return {"ok": True}


# ---------- ФИНАНСЫ (модуль AI-директор) ----------

class FinanceIn(BaseModel):
    kind: str                              # 'income' | 'expense'
    category: str | None = None
    amount: int = 0
    note: str | None = None
    # Остальные поля операции приходят одним словарём: их набор описан в
    # entities.py, и дублировать его здесь значило бы завести второе описание,
    # которое рано или поздно разойдётся с первым.
    data: dict | None = None
    business_id: int = 0


def _finance_origin(link) -> dict:
    """Происхождение операции одним объектом: кем записана и с какой уверенностью."""
    if not link:
        return {"by": "", "by_ru": "", "actor_ru": "", "confidence": None, "item": None}
    by = _link_by(link)
    return {"by": by, "by_ru": BY_RU.get(by, ""),
            "actor_ru": ACTOR_RU.get(link.get("actor"), link.get("actor") or ""),
            "confidence": link.get("confidence"),
            "item": (link.get("item_title") or link.get("item_filename")) or None}


def _finance_public(row: dict) -> dict:
    """Операция наружу: с именами тех, с кем она была, и с понятным документом."""
    out = dict(row)
    out["doc_type_ru"] = database.DOC_TYPES.get(row.get("doc_type") or "", "")
    out["who"] = (row.get("employee_name") or row.get("supplier_name")
                  or row.get("client_name") or row.get("counterparty") or "")
    return out


@app.get("/api/finance")
def api_finance(business_id: int = 0, limit: int = 100, kind: str = "",
                doc_type: str = "", x_auth: str = Header(default="")):
    """
    Сводка по деньгам, последние операции и справочники для формы.

    Проценты (маржа, доля расходов) считает база, а не страница: одна формула
    на весь продукт — один ответ владельцу.
    """
    bid = _resolve_bid(x_auth, business_id)
    rows = database.finance_rows(bid, limit=max(1, min(int(limit or 100), 500)),
                                 kind=kind or None, doc_type=doc_type or None)
    # Кто записал операцию — VELOR или человек — видно в списке, а не только в
    # карточке: доверять цифре, не зная её происхождения, нельзя.
    origins = {}
    for k in ("income", "expense"):
        origins.update({(k, i): l for i, l in database.memory_origins(
            bid, k, [r["id"] for r in rows if r["kind"] == k]).items()})
    entries = []
    for r in rows:
        pub = _finance_public(r)
        pub["origin"] = _finance_origin(origins.get((r["kind"], r["id"])))
        entries.append(pub)
    return {"summary": database.finance_summary(bid),
            "entries": entries,
            "fields": entities.schema("expense", bid)["fields"],
            "docs": database.DOC_TYPES}


@app.post("/api/finance")
def api_finance_add(body: FinanceIn, x_auth: str = Header(default="")):
    """
    Добавить доход или расход руками.

    Идёт той же дорогой, что и подтверждённый разбор: entities.create проверит
    поля, приведёт типы и создаст ОДНУ запись в finance_entries.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    kind = body.kind if body.kind in ("income", "expense") else "expense"
    values = dict(body.data or {})
    # Короткая форма (kind/category/amount/note) остаётся рабочей: ею
    # пользуются старые экраны и бот.
    if body.amount:
        values.setdefault("amount", body.amount)
    if body.category is not None:
        values.setdefault("category", (body.category or "").strip())
    if body.note is not None:
        values.setdefault("note", (body.note or "").strip())
    values.setdefault("source", "manual")
    try:
        eid, detail, clean = entities.create(bid, kind, values)
    except entities.EntityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    actor, actor_id = _actor(x_auth, bid)
    database.add_memory_link(bid, kind, eid, event="created", source_kind="manual",
                             confidence=1.0, actor=actor, actor_id=actor_id,
                             note=detail)
    signals.react(bid, "finance")   # деньги → прибыль, прогноз, Директор, риски
    return {"ok": True, "id": eid, "detail": detail, "entry": _finance_public(
        database.finance_row(eid, bid) or {}), "summary": database.finance_summary(bid)}


@app.post("/api/finance/{entry_id}/delete")
def api_finance_delete(entry_id: int, business_id: int = 0,
                       x_auth: str = Header(default="")):
    """Удалить запись — только свою. След в истории остаётся."""
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    row = database.get_finance_entry(entry_id, bid)
    if not row:
        raise HTTPException(status_code=404, detail="Операция не найдена.")
    database.delete_finance_entry(entry_id, bid)
    actor, actor_id = _actor(x_auth, bid)
    # Операции больше нет, но того, что она была и кто её убрал, из истории
    # не вычеркнуть: деньги — это то место, где «удалил и забыл» недопустимо.
    database.add_memory_link(bid, row["kind"], entry_id, event="removed",
                             source_kind="manual", actor=actor, actor_id=actor_id,
                             note="Удалено: {} ₽ · {}".format(
                                 row.get("amount"), row.get("category") or "без категории"))
    signals.react(bid, "finance")
    return {"ok": True, "summary": database.finance_summary(bid)}


@app.get("/api/finance/insights")
def api_finance_insights(business_id: int = 0, x_auth: str = Header(default="")):
    """AI-инсайты по финансам: выводы и рекомендации от VELOR."""
    import ai
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    s = database.finance_summary(bid)
    if not ai.ai_available():
        return {"ok": False, "answer": None}
    if s["income"] == 0 and s["expense"] == 0:
        return {"ok": True, "answer": "Пока нет данных. Внесите доходы и расходы — и я разберу цифры."}
    cats = "; ".join(f"{c['kind']}/{c['category']}: {c['total']}₽" for c in s["by_category"][:12])
    summary_text = (
        f"Выручка: {s['income']}₽. Расходы: {s['expense']}₽. Прибыль: {s['profit']}₽. "
        f"По категориям — {cats}."
    )
    business = database.get_business(bid) or {"name": "VELOR AI"}
    try:
        return {"ok": True, "answer": ai.finance_insights(business, summary_text)}
    except Exception:
        return {"ok": False, "answer": None}


class FinanceEdit(BaseModel):
    data: dict
    business_id: int = 0


def _finance_kind(bid: int, entry_id: int) -> str:
    row = database.get_finance_entry(entry_id, bid)
    if not row:
        raise HTTPException(status_code=404, detail="Операция не найдена.")
    return "income" if row.get("kind") == "income" else "expense"


@app.get("/api/finance/{entry_id}")
def api_finance_entry(entry_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """Одна операция целиком: поля, связи и вся её история."""
    bid = _resolve_bid(x_auth, business_id)
    kind = _finance_kind(bid, entry_id)
    values = entities.read(bid, kind, entry_id)
    if values is None:
        raise HTTPException(status_code=404, detail="Операция не найдена.")
    history = [_memory_link_public(l)
               for l in database.memory_links(bid, kind, entry_id)]
    sch = entities.schema(kind, bid)
    ai = _ai_values(history)
    created = next((h for h in history if h.get("event") == "created"), None)
    return {"kind": kind, "id": entry_id, "values": values,
            "schema": sch,
            "entry": _finance_public(database.finance_row(entry_id, bid) or {}),
            "origin": _finance_origin(created),
            "ai": ai, "corrections": _corrections(sch["fields"], ai, values),
            "history": history}


@app.post("/api/finance/{entry_id}")
def api_finance_edit(entry_id: int, body: FinanceEdit, x_auth: str = Header(default="")):
    """
    Изменить операцию. Сумма, дата, категория, контрагент, описание,
    сотрудник, заявка, источник — всё правится здесь, и всё попадает в
    историю: по деньгам должно быть видно, кто и что поменял.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    kind = _finance_kind(bid, entry_id)
    try:
        detail, now, before = entities.update(bid, kind, entry_id, body.data or {})
    except entities.EntityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logging.exception("Финансы: правка не удалась (biz %s, запись %s)", bid, entry_id)
        raise HTTPException(status_code=502, detail="Не удалось сохранить правку.")
    changes = understanding.diff(before, now)
    if changes:
        actor, actor_id = _actor(x_auth, bid)
        database.add_memory_link(bid, kind, entry_id, event="edited", source_kind="manual",
                                 changes=changes, actor=actor, actor_id=actor_id, note=detail)
    signals.react(bid, "finance")   # исправили сумму — аналитика считает заново
    history = [_memory_link_public(l)
               for l in database.memory_links(bid, kind, entry_id)]
    ai = _ai_values(history)
    return {"ok": True, "detail": detail, "changes": changes, "values": now,
            "entry": _finance_public(database.finance_row(entry_id, bid) or {}),
            "summary": database.finance_summary(bid),
            "ai": ai, "corrections": _corrections(
                entities.schema(kind, bid)["fields"], ai, now),
            "history": history}


# ---------- GROWTH (AI-маркетолог / контент) ----------

class GrowthAnalyzeIn(BaseModel):
    text: str
    business_id: int = 0


class GrowthGenerateIn(BaseModel):
    task: str = "post"                     # plan | post | ad
    topic: str | None = None
    business_id: int = 0


@app.post("/api/growth/analyze")
def api_growth_analyze(body: GrowthAnalyzeIn, x_auth: str = Header(default="")):
    """Разбор поста/описания AI-маркетологом."""
    import ai
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    if not ai.ai_available() or not (body.text or "").strip():
        return {"ok": False, "answer": None}
    business = database.get_business(bid) or {"name": "VELOR AI"}
    try:
        return {"ok": True, "answer": ai.analyze_content(business, body.text)}
    except Exception:
        return {"ok": False, "answer": None}


@app.post("/api/growth/generate")
def api_growth_generate(body: GrowthGenerateIn, x_auth: str = Header(default="")):
    """Генерация контент-плана / поста / рекламы под бизнес."""
    import ai
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    if not ai.ai_available():
        return {"ok": False, "answer": None}
    business = database.get_business(bid) or {"name": "VELOR AI"}
    try:
        answer = ai.generate_content(business, body.task, body.topic or "")
        database.log_event(bid, "content", "Готов материал для продвижения",
                           (body.topic or body.task or "")[:120])
        return {"ok": True, "answer": answer}
    except Exception:
        return {"ok": False, "answer": None}


# ---------- ИИ-ОТВЕТ ЯДРА НА САЙТЕ ----------

class AskIn(BaseModel):
    question: str
    role: str | None = None            # роль AI-сотрудника в кабинете
    business_id: int = 0


def _biz_snapshot(bid: int) -> str:
    """Короткая сводка бизнеса — чтобы AI-сотрудник отвечал по свежим данным.

    Все числа считает база (COUNT/SUM), а не длина выборки. Раньше здесь брались
    последние 20 заказов и в промпт уходило «заказов всего 20» для любой компании —
    ИИ строил выводы и рекомендации на заведомо неверных цифрах.
    """
    o = database.orders_overview(bid)
    clients = database.count_clients(bid)
    parts = [f"заказов всего {o['total']}", f"новых {o['new']}",
             f"выполнено {o['done']}", f"сегодня {o['today']}",
             f"клиентов {clients}"]
    if o["turnover"]:
        parts.append(f"оборот по заказам {o['turnover']} руб.")
        if o["total"]:
            parts.append(f"средний чек {o['turnover'] // o['total']} руб.")
    return ", ".join(parts)


def _ai_failure_reason(err) -> str:
    """Перевести отказ провайдера на человеческий, ничего не разглашая."""
    t = str(err)
    low = t.lower()
    if "401" in t or "unauthorized" in low or "credentials" in low \
            or "api key is invalid" in low:
        return ("Модель не приняла ключ — он отозван, истёк или выдан другому "
                "аккаунту. Нужен новый ключ в .env.")
    if "429" in t or "quota" in low or "rate limit" in low or "insufficient" in low:
        return "У модели кончился лимит запросов или баланс."
    if "timeout" in low or "timed out" in low:
        return "Модель не ответила вовремя."
    if "connection" in low or "resolve" in low or "network" in low:
        return "До модели нет связи с этого сервера."
    return "Модель сейчас не отвечает."


@app.post("/api/ask")
def api_ask(body: AskIn, request: Request, x_auth: str = Header(default="")):
    """Вопрос ядру — отвечает ИИ (если ключ настроен).

    На лендинге запрос идёт без токена → короткий ответ от лица бизнеса.
    В кабинете panel-auth.js добавляет X-Auth → отвечает AI-сотрудник выбранной
    роли, зная базу знаний и свежую сводку бизнеса.
    """
    import ai
    payload = _auth_payload(x_auth)
    # Токен передан, но не распознан (истёк/битый) → отдаём 401, чтобы panel-auth.js
    # обновил access по refresh и повторил запрос. Иначе кабинетный вопрос молча уходил
    # в лендинг-ветку без базы знаний, и ассистент отвечал «не знаю, чем занимаешься»
    # после истечения access-токена (30 мин). Лендинг (без X-Auth) идёт как прежде.
    if x_auth and not payload:
        raise HTTPException(status_code=401, detail="Сессия истекла — обновите вход")
    bid = payload["bid"] if payload and payload.get("role") == "business" else None

    # Лимит защищает БАЗОВЫЙ ключ VELOR — тот, за который платит платформа. Кто
    # спрашивает своим ключом, тратит свои запросы и свои деньги: считать их за
    # него незачем, а ограничивать — просто неправильно.
    #
    # Порядок проверок поменялся намеренно: чтобы узнать, чей ключ, нужно сперва
    # понять, кто спрашивает. Побочно стало лучше — просроченный токен больше не
    # съедает лимит: раньше он тратил попытку и только потом получал 401.
    if not (bid is not None and ai.own_key(bid)):
        wait = ratelimit.ask_retry_after("ask:" + _client_ip(request))
        if wait:
            raise HTTPException(status_code=429, detail=(
                "Слишком много запросов подряд. Подождите " + ratelimit.human_wait(wait)
                + " и попробуйте снова."))

    if not ai.ai_available(bid):
        return {"ok": False, "answer": None,
                "detail": "Модель не настроена: ни базового ключа, ни своего."}
    if bid is not None:
        st = trial.access(database.get_business(bid))
        # Онбординг + реальное использование ИИ = старт триала (как при сохранении
        # настроек в /api/business). Иначе аккаунт мог бы бесконечно пользоваться ИИ,
        # формально оставаясь в онбординге и не запуская отсчёт 14 дней. launch
        # идемпотентен и наполняет trial_registry/owner_identity — совместимость цела.
        if st["phase"] == "onboarding":
            try:
                trial.launch(bid)
            except Exception:
                logging.exception("Не удалось запустить триал по первому запросу ИИ (biz %s)", bid)
            st = trial.access(database.get_business(bid))
        if st["read_only"]:
            return {"ok": False, "answer": None, "locked": True,
                    "detail": "Пробный период завершён — оформите подписку, чтобы ИИ снова отвечал."}
    try:
        if bid is not None:                    # кабинет бизнеса — роль + данные
            role = body.role or "assistant"
            snapshot = _biz_snapshot(bid)
            # НОВЫЙ СЛОЙ: Context Engine собирает полный контекст (компания, знания,
            # документы-RAG, память, CRM, финансы) и сам зовёт LLM. Обратная
            # совместимость: при ЛЮБОЙ ошибке — прежний путь ai.assistant_answer.
            try:
                import context_engine
                return {"ok": True, "answer": context_engine.respond(
                    bid, body.question, role=role, snapshot=snapshot)}
            except Exception:
                logging.exception("Context Engine упал — откат на assistant_answer (biz %s)", bid)
            business = database.get_business(bid) or {"name": "VELOR AI"}
            docs = database.search_chunks(bid, body.question)
            persona = None
            if role.startswith("agent:") and role[6:].isdigit():   # свой сотрудник — характер из БД
                agent = database.get_agent(int(role[6:]), bid)
                if agent:
                    persona = f"Сейчас ты — {agent['name']}. {agent['persona']}"
            return {"ok": True, "answer": ai.assistant_answer(
                business, body.question, role, snapshot, docs, persona,
                database.timeline_digest(bid))}
        # лендинг — общий короткий ответ. Компанию берём только если её явно
        # указали и она существует, иначе отвечаем от лица VELOR AI (а не молча
        # от лица случайного бизнеса №1).
        business = None
        if body.business_id and body.business_id > 0:
            business = database.get_business(body.business_id)
        business = business or {"name": "VELOR AI"}
        return {"ok": True, "answer": ai.site_answer(business, body.question)}
    except Exception as e:
        # Причину надо назвать. «Не могу ответить» лечится по-разному: отозванный
        # ключ, кончившаяся квота и упавшая сеть — три разные беды, и владелец
        # должен знать, какая у него. Текст ошибки провайдера сюда не тащим
        # целиком: в нём бывает эхо запроса, а сам ключ в сообщениях 401 не
        # приходит — но полагаться на это не будем.
        logging.exception("Вопрос к модели не прошёл (biz %s)", bid)
        return {"ok": False, "answer": None, "detail": _ai_failure_reason(e)}


# ---------- Отдаём сайт ----------

# ============================================================
#  INSTAGRAM: ВХОД, СОБЫТИЯ, ПЕРЕПИСКА
# ============================================================
# Один вебхук на всё приложение Meta и много компаний внутри: какому бизнесу
# принадлежит событие, определяется по id аккаунта в самом событии. Поэтому
# здесь нет ни одного места, где business_id брался бы «по умолчанию».


@app.get("/api/instagram/setup")
def api_instagram_setup(business_id: int = 0, x_auth: str = Header(default="")):
    """
    Чего не хватает для подключения канала и что вписать в кабинет Meta.

    Адрес вебхука и слово-пароль нужны тому, кто настраивает приложение Meta,
    а без них Instagram просто не станет звонить — и канал будет выглядеть
    подключённым, оставаясь немым.
    """
    _resolve_bid(x_auth, business_id)
    st = instagram.setup_state()
    # Слово-пароль вебхука — настройка приложения VELOR, общая для всех компаний.
    # Показывать его каждому арендатору незачем: чужой компании оно не помогает
    # ничем, а знать общий секрет платформы ей не положено.
    if require_business(x_auth) != -1:
        st = {**st, "verify_token": ""}
    return {**st, "limits": {
        "reply_window_hours": instagram.REPLY_WINDOW_HOURS,
        "human_window_days": instagram.HUMAN_WINDOW_DAYS,
        "history_limit": instagram.HISTORY_LIMIT,
    }}


@app.post("/api/instagram/login")
def api_instagram_login(business_id: int = 0, x_auth: str = Header(default="")):
    """
    Начать вход. Возвращаем адрес НАСТОЯЩЕЙ страницы Instagram.

    Своей формы входа у VELOR нет и быть не может: пароль от Instagram должен
    вводиться только в Instagram. Мы получаем не пароль, а доступ, который
    владелец выдал сам и может отозвать у себя.
    """
    bid = _resolve_bid(x_auth, business_id)
    require_active(bid)
    try:
        return {"ok": True, "url": instagram.authorize_url(bid)}
    except connectors.ConnectorError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _ig_back(ok: bool, message: str):
    """Вернуть человека в кабинет с понятным итогом (Meta ведёт сюда браузером)."""
    return RedirectResponse(
        "/connections.html?ig=" + ("ok" if ok else "err")
        + "&msg=" + _urlquote(message[:200]), status_code=303)


@app.get("/api/instagram/callback")
def api_instagram_callback(code: str = "", state: str = "",
                           error: str = "", error_description: str = ""):
    """
    Возврат от Meta после входа.

    Токена кабинета здесь нет — браузер приходит со стороны Instagram. Поэтому
    компанию берём из подписанного state: подделать его нельзя, а живёт он
    четверть часа, чтобы старую ссылку нельзя было разыграть повторно.
    """
    if error:
        return _ig_back(False, error_description or "Доступ не выдан.")
    bid = instagram.read_state(state)
    if not bid or not database.get_business(bid):
        return _ig_back(False, "Ссылка входа устарела. Начните подключение заново.")
    try:
        tok = instagram.exchange_code(code)
        who = instagram.me(tok["access_token"]) or {}
        ig_id = str(who.get("user_id") or who.get("id") or "")
        if not ig_id:
            return _ig_back(False, "Instagram не сказал, чей это аккаунт.")
        username = who.get("username") or ""
        sub = {}
        try:
            sub = instagram.subscribe(tok["access_token"])
        except connectors.ConnectorError as e:
            # Доступ выдан, но события не подписаны: сохранить подключение и
            # промолчать было бы худшим вариантом — канал выглядел бы живым и
            # не принимал бы ни одного сообщения.
            instagram.save_token(bid, tok["access_token"], tok["expires_in"],
                                 {"ig_id": ig_id, "username": username,
                                  "account_type": who.get("account_type"),
                                  "scopes": instagram.SCOPES, "webhook_fields": []},
                                 config={"Аккаунт": "@" + username if username else ig_id})
            database.mark_connection_synced(bid, "instagram", error=str(e))
            return _ig_back(False, "Аккаунт подключён, но события не подписаны: "
                                   + str(e))
        instagram.save_token(bid, tok["access_token"], tok["expires_in"],
                             {"ig_id": ig_id, "username": username,
                              "account_type": who.get("account_type"),
                              "scopes": instagram.SCOPES,
                              "webhook_fields": sub.get("fields") or []},
                             config={"Аккаунт": "@" + username if username else ig_id})
        database.log_event(bid, "integration", "Instagram подключён",
                           "Директ приходит в VELOR" + (f" · @{username}" if username else ""))
    except connectors.ConnectorError as e:
        return _ig_back(False, str(e))
    except Exception:
        logging.exception("Instagram: подключение не удалось (biz %s)", bid)
        return _ig_back(False, "Не удалось завершить подключение.")

    # Первую порцию переписок забираем в фоне: доступ уже проверен, держать
    # человека перед крутящейся страницей незачем.
    if _os.getenv("DISABLE_SYNC_WORKER"):
        instagram.pull_recent(bid)
    else:
        import threading
        threading.Thread(target=instagram.pull_recent, args=(bid,),
                         name="velor-ig-first-pull", daemon=True).start()
    return _ig_back(True, "Instagram подключён" + (f": @{username}" if username else ""))


@app.get("/api/instagram/webhook")
def api_instagram_webhook_verify(request: Request):
    """
    Проверка адреса при настройке в кабинете Meta.

    Meta присылает своё слово-пароль и число; вернуть нужно ровно это число и
    только если слово совпало. Отвечать на чужую проверку нельзя: так посторонний
    смог бы подписать наш адрес на свои события.
    """
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == instagram.verify_token():
        return Response(content=q.get("hub.challenge") or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="Проверочное слово не совпало.")


@app.post("/api/instagram/webhook")
async def api_instagram_webhook(request: Request):
    """
    Событие из Instagram: новое сообщение, эхо ответа, прочтение.

    Подпись обязательна. Адрес публичный, и без неё кто угодно мог бы прислать
    «сообщение от клиента» и заставить VELOR ответить постороннему человеку от
    имени бизнеса.

    Meta отвечает на ошибки повторной доставкой, поэтому наружу мы всегда
    отдаём 200: разбираться с нашими бедами повторным звонком бессмысленно, а
    дубли уже отсечены по номеру сообщения.
    """
    raw = await request.body()
    if not instagram.verify_signature(raw, request.headers.get("x-hub-signature-256") or ""):
        raise HTTPException(status_code=403, detail="Подпись не совпала.")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {"ok": True}
    try:
        instagram.handle_webhook(payload if isinstance(payload, dict) else {})
    except Exception:
        logging.exception("Instagram: разбор события не удался")
    return {"ok": True}


class AiPolicyIn(BaseModel):
    channel: str = "instagram"
    level: int | None = None
    grants: list[str] | None = None
    business_id: int = 0


@app.get("/api/ai/policy")
def api_ai_policy(channel: str = "instagram", business_id: int = 0,
                  x_auth: str = Header(default="")):
    """
    Насколько самостоятельно VELOR разговаривает с клиентами в этом канале.

    Отдаём и текущую настройку, и весь справочник: владелец должен читать не
    коды разрешений, а что именно они позволяют. «send_price» ему ни о чём не
    говорит, «называть цены из каталога» — говорит всё.
    """
    bid = _resolve_bid(x_auth, business_id)
    pol = sales.policy(bid, channel)
    return {
        "policy": pol,
        "levels": sales.levels_public(),
        "permissions": sales.permissions_public(),
        "dangerous": list(sales.DANGEROUS),
        "default_level": sales.DEFAULT_LEVEL,
        # Продавец силён ровно настолько, насколько полна память бизнеса.
        # Пустой каталог — это не «ИИ плохой», это нечего продавать, и увидеть
        # это надо здесь, а не по молчанию канала.
        "memory": sales.memory_health(bid),
    }


@app.post("/api/ai/policy")
def api_ai_policy_set(body: AiPolicyIn, x_auth: str = Header(default="")):
    """
    Изменить автономию.

    Опасные действия включаются только отсюда и только поимённо: ни один
    уровень их не выдаёт. Каждое включение попадает в историю компании — чтобы
    на вопрос «кто разрешил VELOR давать скидки» был ответ.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    who, _ = _actor(x_auth, bid)
    try:
        pol = sales.set_policy(bid, body.channel or "instagram",
                               level=body.level, grants=body.grants, actor=who)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "policy": pol}


@app.get("/api/instagram/threads")
def api_instagram_threads(business_id: int = 0, x_auth: str = Header(default="")):
    """Все переписки директа: кто написал, когда, сколько осталось на ответ."""
    bid = _resolve_bid(x_auth, business_id)
    st = connections.state(bid, "instagram")
    items = []
    for t in database.ig_threads_list(bid):
        items.append({**t, "window": instagram.window_state(bid, t["igsid"])})
    return {"connection": st, "threads": items,
            "limits": {"reply_window_hours": instagram.REPLY_WINDOW_HOURS,
                       "human_window_days": instagram.HUMAN_WINDOW_DAYS,
                       "history_limit": instagram.HISTORY_LIMIT}}


@app.get("/api/instagram/thread/{igsid}")
def api_instagram_thread(igsid: str, business_id: int = 0,
                         x_auth: str = Header(default="")):
    """Одна переписка целиком — так, как её сохранил VELOR."""
    bid = _resolve_bid(x_auth, business_id)
    t = database.ig_thread(bid, igsid)
    if not t:
        raise HTTPException(status_code=404, detail="Такой переписки нет.")
    return {"thread": t,
            "client": database.get_client(t["client_id"], bid),
            "messages": database.get_client_messages(t["client_id"], bid, limit=200),
            "window": instagram.window_state(bid, igsid),
            "policy": sales.policy(bid, "instagram")}


class IgReplyIn(BaseModel):
    text: str = ""
    business_id: int = 0


@app.post("/api/instagram/thread/{igsid}/reply")
def api_instagram_reply(igsid: str, body: IgReplyIn, x_auth: str = Header(default="")):
    """
    Ответ владельца своими словами.

    Отправляем как сообщение человека: только у такого сообщения Instagram
    разрешает тег, продлевающий срок ответа до семи суток. На ответ модели
    ставить этот тег запрещено правилами Meta, поэтому решение принимается
    здесь — там, где точно известно, что писал человек.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    try:
        res = instagram.reply_as_human(bid, igsid, body.text)
    except connectors.AuthError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except connectors.ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **res, "window": instagram.window_state(bid, igsid)}


class IgHandoffIn(BaseModel):
    paused: bool = True
    business_id: int = 0


@app.post("/api/instagram/thread/{igsid}/handoff")
def api_instagram_handoff(igsid: str, body: IgHandoffIn,
                          x_auth: str = Header(default="")):
    """
    Взять разговор на себя — или вернуть его VELOR.

    Передача треда между приложениями через Meta (Handover Protocol) в этой
    сборке Instagram API не документирована, поэтому мы её не изображаем.
    Переключатель честно означает одно: отвечает VELOR или отвечает человек.
    """
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    if not database.ig_thread(bid, igsid):
        raise HTTPException(status_code=404, detail="Такой переписки нет.")
    database.ig_thread_pause(bid, igsid, bool(body.paused), by="owner")
    return {"ok": True, "paused": bool(body.paused),
            "thread": database.ig_thread(bid, igsid)}


# ============================================================
#  TELEGRAM-БОТ ЧЕРЕЗ WEBHOOK (живёт внутри веб-сервиса, без отдельного воркера)
# ============================================================
# Для бесплатного хостинга: не поднимаем всегда-включённый процесс bot.py, а
# принимаем апдейты Telegram прямо в веб-сервис. У каждого бизнеса свой токен →
# свой URL /api/tg/webhook/{token}, поэтому сообщения разных ботов не смешиваются.

def _public_base() -> str:
    """Публичный адрес сервиса. Render кладёт его в RENDER_EXTERNAL_URL; можно
    переопределить PUBLIC_URL. Без него webhook не зарегистрировать."""
    base = (_os.getenv("RENDER_EXTERNAL_URL") or _os.getenv("PUBLIC_URL") or "").strip()
    return base.rstrip("/")


def _tg_secret(token: str) -> str:
    """Секрет для заголовка X-Telegram-Bot-Api-Secret-Token: подтверждает, что
    апдейт пришёл от Telegram, а не подделан. Стабилен per-deploy, нигде не хранится."""
    return hashlib.sha256((token + "|" + auth._secret().hex()).encode()).hexdigest()[:48]


def _tg_api(token: str, method: str, **params):
    """Вызов Telegram Bot API (без python-telegram-bot — просто HTTPS)."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json=params, timeout=15)
        return r.json()
    except Exception:
        logging.exception("Telegram API %s не удался", method)
        return None


def _tg_files(msg: dict) -> list[dict]:
    """
    Что за файлы в сообщении Telegram: [{file_id, name, size}].

    Фото приходит несколькими размерами одного снимка — берём самый крупный,
    иначе в память бизнеса уедет превью, на котором цену не прочитать. Имя у
    фото Telegram не передаёт, поэтому даём своё: расширение решает, примем мы
    материал или нет, и оставлять его пустым нельзя.
    """
    out = []
    photos = msg.get("photo") or []
    if isinstance(photos, list) and photos:
        big = max(photos, key=lambda p: (p or {}).get("file_size") or 0)
        if big.get("file_id"):
            out.append({"file_id": big["file_id"], "name": "Фото из Telegram.jpg",
                        "size": big.get("file_size") or 0})
    for key, default in (("document", None), ("voice", "Голосовое.ogg"),
                         ("audio", "Аудио.mp3"), ("video", "Видео.mp4")):
        part = msg.get(key)
        if not isinstance(part, dict) or not part.get("file_id"):
            continue
        name = (part.get("file_name") or default or "").strip()
        if not name:
            continue                      # без имени не узнать тип — не берём
        out.append({"file_id": part["file_id"], "name": name,
                    "size": part.get("file_size") or 0})
    return out


def _tg_download(token: str, file_id: str) -> bytes | None:
    """
    Скачать файл Telegram. None — не вышло, и это не повод падать.

    Размер проверяем ДО скачивания по ответу getFile и ещё раз после: первое
    бережёт трафик, второе защищает от того, что сервер сказал одно, а отдал
    другое. Путь приходит от Telegram и в наше хранилище не попадает — там своё
    имя из storage.new_key().
    """
    info = _tg_api(token, "getFile", file_id=file_id)
    if not info or not info.get("ok"):
        return None
    path = ((info.get("result") or {}).get("file_path") or "").strip()
    size = (info.get("result") or {}).get("file_size") or 0
    if not path or (size and size > intake.MAX_BYTES):
        return None
    try:
        r = requests.get(f"https://api.telegram.org/file/bot{token}/{path}",
                         timeout=60, stream=True)
        if r.status_code != 200:
            return None
        data = r.raw.read(intake.MAX_BYTES + 1, decode_content=True)
    except Exception:
        logging.exception("Telegram: файл не скачался")
        return None
    return None if len(data) > intake.MAX_BYTES else data


def set_webhook_for(token: str):
    """Зарегистрировать webhook для одного бота на наш публичный адрес."""
    base = _public_base()
    if not base or not token:
        return {"ok": False, "error": "no_public_url"}
    url = f"{base}/api/tg/webhook/{token}"
    return _tg_api(token, "setWebhook", url=url, secret_token=_tg_secret(token),
                   allowed_updates=["message"], drop_pending_updates=True) or {"ok": False}


@app.post("/api/tg/webhook/{token}")
async def api_tg_webhook(token: str, request: Request):
    """Приём апдейтов Telegram: находим бизнес по токену, отвечаем как сотрудник."""
    # чужой POST (даже зная токен) не пройдёт без подписи, которую ставит Telegram
    if request.headers.get("x-telegram-bot-api-secret-token") != _tg_secret(token):
        return JSONResponse({"ok": True}, status_code=200)
    biz = database.find_business_by_token(token)
    if not biz:
        return {"ok": True}
    bid = biz["id"]
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    # Защита от повторной доставки: тот же update_id уже обработан → тихо выходим,
    # не создавая повторную заявку и не отправляя повторный ответ.
    try:
        if database.tg_update_seen(bid, update.get("update_id")):
            return {"ok": True}
    except Exception:
        logging.exception("Проверка дубля update_id не удалась (biz %s)", bid)
    msg = update.get("message") or update.get("edited_message") or {}
    if not isinstance(msg, dict):
        return {"ok": True}
    chat_id = (msg.get("chat") or {}).get("id")
    # Подпись к фото — такой же текст, как обычное сообщение: человек написал
    # «вот новый прайс» и приложил файл одной отправкой, и разрывать их нельзя.
    text = msg.get("text") or msg.get("caption") or ""
    frm = msg.get("from") or {}
    files = _tg_files(msg)
    if not chat_id or (not text and not files):
        return {"ok": True}                       # стикер, прочтение, служебное — молчим
    full_name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) \
        or frm.get("username") or "клиент"

    # Привязка личности владельца: если пришёл ожидаемый код — этот отправитель и
    # есть владелец. Фиксируем его ЛИЧНЫЙ Telegram id (устойчив к смене бота).
    pending = (biz.get("tg_verify_code") or "").strip()
    if pending and text.strip() == pending:
        try:
            identity.link_telegram(bid, frm)
            database.update_business(bid, owner_verified=1, tg_verify_code=None)
        except Exception:
            logging.exception("Не удалось привязать владельца (biz %s)", bid)
        _tg_api(token, "sendMessage", chat_id=chat_id,
                text="✓ Готово! Вы подтверждены как владелец. Вернитесь в кабинет и "
                     "нажмите «Запустить VELOR».")
        return {"ok": True}

    # ── ВЛАДЕЛЕЦ ИЛИ КЛИЕНТ ────────────────────────────────────────────────
    # Развилка одна и проходит здесь. Владелец рассказывает о своём деле — это
    # материал, и он идёт в ту же дверь, что и загрузка из кабинета. Клиент
    # разговаривает — его по-прежнему ведёт продавец, и Lead Engine к этому
    # маршруту отношения пока не имеет.
    #
    # Команды остаются командами для обоих: «/start» — это не сведения о
    # бизнесе, и записывать его в память было бы нелепо.
    owner = botcore.is_owner(bid, frm.get("id"))
    is_command = text.strip().startswith("/")

    try:
        if text.strip() == "/start":
            reply = botcore.greeting_text(bid)
        elif owner and not is_command:
            got, missed, huge = [], [], []
            for f in files:
                # Размер, заявленный в самом сообщении, проверяем ДО обращения
                # к Telegram: тянуть сорок мегабайт, чтобы затем отказать, —
                # это трата и нашего времени, и чужого канала.
                if f["size"] and f["size"] > intake.MAX_BYTES:
                    huge.append(f["name"])
                    continue
                data = _tg_download(token, f["file_id"])
                if data is None:
                    missed.append(f["name"])
                    continue
                got.append((f["name"], data))
            if got or text:
                reply = botcore.from_owner(bid, text=text, files=got,
                                           actor_id=frm.get("id"))
            else:
                # Всё, что прислали, отсеялось ещё до приёма. Звать приём не за
                # чем — ему нечего принимать, и «нечего принимать» звучало бы
                # как наша ошибка, а не как объяснение.
                reply = "Не принял:"
            # О несработавших файлах говорим отдельными строками, а не молчим:
            # иначе владелец решит, что чек принят.
            if huge:
                reply += ("\n• " + ", ".join(huge) + ": больше %d МБ — не принимаю."
                          % (intake.MAX_BYTES // (1024 * 1024)))
            if missed:
                reply += "\n• " + ", ".join(missed) + ": не смог скачать из Telegram."
        elif owner:
            reply = ("Это команда, а не сведения о бизнесе — я её не записываю. "
                     "Пришлите текстом, фото или файлом то, что хотите мне рассказать.")
        elif files and not text:
            # Клиент прислал вложение без слов. В разговор это не превратить, а
            # в память бизнеса чужой файл не кладут — принимаем по-человечески.
            reply = "Спасибо! Получили. Сотрудник посмотрит и ответит."
        else:
            reply = botcore.handle_message(bid, frm.get("id"), full_name, text)
    except Exception:
        logging.exception("Ошибка обработки webhook (biz %s)", bid)
        reply = "Ой, я на секунду задумалась. Напишите ещё раз, пожалуйста."
    if reply:
        _tg_api(token, "sendMessage", chat_id=chat_id, text=reply)
    return {"ok": True}


@app.post("/api/tg/register")
def api_tg_register(x_auth: str = Header(default="")):
    """Подключить/переподключить webhook для бота текущего бизнеса (кнопка в кабинете)."""
    bid = _resolve_bid(x_auth, 0)
    token = ((database.get_business(bid) or {}).get("tg_bot_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Сначала сохраните токен бота в настройках.")
    if not _public_base():
        raise HTTPException(status_code=400,
                            detail="Публичный адрес сервиса не задан (RENDER_EXTERNAL_URL/PUBLIC_URL).")
    res = set_webhook_for(token)
    if res and res.get("ok"):
        return {"ok": True, "detail": "Бот подключён — клиенты пишут прямо в кабинет."}
    return {"ok": False, "detail": (res or {}).get("description") or "Не удалось подключить бота."}


class FreshFiles(StaticFiles):
    """
    Как обычная раздача файлов, но страницы, стили и скрипты браузер обязан
    перепроверять на сервере. Иначе после правок кабинета человек продолжает
    видеть старую версию, пока не нажмёт Ctrl+F5.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        path = str(args[0] if args else "")
        if path.endswith((".html", ".css", ".js")):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


@app.get("/")
def index():
    return FileResponse("web/index.html", headers={"Cache-Control": "no-cache, must-revalidate"})

# Всё остальное из папки web/ (стили, скрипты, dashboard.html)
app.mount("/", FreshFiles(directory="web"), name="web")


if __name__ == "__main__":
    # 0.0.0.0 — сайт доступен другим устройствам в сети.
    # Порт берём из окружения ($PORT) — так требуют облачные хостинги (Render и др.);
    # локально по умолчанию 8000.
    uvicorn.run(app, host="0.0.0.0", port=int(_os.getenv("PORT", "8000")))
