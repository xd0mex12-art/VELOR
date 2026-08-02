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

import requests

from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, Response, JSONResponse
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
import config
import botcore
import trial
import identity
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

    bid = database.create_business(
        name=name,
        about=(body.about or "").strip() or "малый бизнес: приём заказов и заявок",
        greeting="Здравствуйте! Напишите, что вам нужно — я приму заявку и всё оформлю.",
    )
    database.update_business(bid, login=login, password=password)

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
    bid = database.create_business(name=body.name, about=body.about, greeting=body.greeting)
    extra = {k: v for k, v in {"plan": body.plan, "fee": body.fee,
                               "login": body.login, "password": body.password}.items() if v is not None}
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
    database.update_business(bid, **body.model_dump(exclude_none=True))
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
def api_orders(business_id: int = 0, x_auth: str = Header(default="")):
    """Заказы бизнеса — для его панели."""
    return database.get_orders(_resolve_bid(x_auth, business_id))


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
    )
    signals.react(bid, "order")   # заказ влияет на Директора, брифинг, риски
    return {"ok": True, "order_id": order_id}


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
def api_clients(business_id: int = 0, q: str = "",
                limit: int = 50, offset: int = 0, x_auth: str = Header(default="")):
    """Список клиентов бизнеса с поиском и постраничной загрузкой."""
    bid = _resolve_bid(x_auth, business_id)
    query = q.strip() or None
    limit = max(1, min(limit, 200))
    items = database.list_clients(bid, query=query, limit=limit, offset=max(0, offset))
    stats = database.clients_overview(bid, query=query)
    return {"items": items, "total": stats["total"],
            "with_phone": stats["with_phone"], "orders_total": stats["orders_total"]}


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
    if not ai.ai_available():
        return client.get("ai_summary") or "", client.get("ai_advice") or ""
    business = database.get_business(bid) or {}
    facts = _client_facts_text(client, orders, messages)
    res = ai.client_summary(business, facts)
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
    return {"client": client, "orders": orders, "messages": messages,
            "summary": summary, "advice": advice}


@app.post("/api/clients/{client_id}/summary/refresh")
def api_client_summary_refresh(client_id: int, business_id: int = 0, x_auth: str = Header(default="")):
    """Пересобрать резюме клиента принудительно."""
    bid = _resolve_bid(x_auth, business_id)
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
    database.update_business(bid, **fields)
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


@app.get("/api/stats")
def api_stats(business_id: int = 0, x_auth: str = Header(default="")):
    """Аналитика пользы VELOR: обработано сообщений, заказов, сэкономлено времени."""
    bid = _resolve_bid(x_auth, business_id)
    s = database.business_stats(bid)
    # оценка сэкономленного времени: ~2 мин на обработанное сообщение клиента
    s["minutes_saved"] = s["messages"] * 2
    return s


# ---------- ГЛАВНАЯ: всё одним запросом ----------

@app.get("/api/home")
def api_home(business_id: int = 0, x_auth: str = Header(default="")):
    """
    Сводка для главной страницы. Один запрос вместо шести и ни одного обращения
    к ИИ: берём то, что уже посчитано и сохранено, — главная должна открываться мгновенно.
    """
    bid = _resolve_bid(x_auth, business_id)
    business = database.get_business(bid) or {}
    sig = database.risk_signals(bid)
    orders = database.get_orders(bid, limit=100)
    today = datetime.date.today().isoformat()

    risks = [r for r in database.list_risks(bid) if r["status"] == "new"]
    opps = [o for o in database.list_opportunities(bid) if o["status"] == "new"]
    journal = database.list_journal(bid, limit=1)
    advice = journal[0]["advice"] if journal and journal[0]["advice"] else ""

    orders_new = [o for o in orders if o.get("status") == "новый"]
    orders_today = [o for o in orders if (o.get("created_at") or "").startswith(today)]

    # «Сегодня важно»: одна главная мысль. Сначала то, что горит.
    money_insight = signals.top_insight(bid)   # живое денежное следствие (без ИИ)
    if risks and risks[0]["level"] == 1:
        focus = {"text": risks[0]["title"], "note": risks[0]["why"], "kind": "риск", "href": "risks.html"}
    elif money_insight:
        focus = money_insight
    elif orders_new:
        focus = {"text": f"Разберите {database._plural(len(orders_new), 'новую заявку', 'новые заявки', 'новых заявок')}",
                 "note": (orders_new[0].get("text") or "")[:140], "kind": "заявки", "href": "orders.html"}
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
        "orders": {"new": len(orders_new), "today": len(orders_today), "total": len(orders)},
        "money": {"income": sig["current"]["income"], "profit": sig["current"]["profit"],
                  "income_change": sig["change"]["income"], "profit_change": sig["change"]["profit"]},
        "clients": {"new": sig["current"]["clients"], "change": sig["change"]["clients"]},
        "forecast": signals.forecast(bid),   # прогноз на конец месяца (обновляется с расходами)
        "advice": advice,
        "advice_day": journal[0]["day"] if journal else None,
        "risks": {"count": len(risks), "top": risks[0] if risks else None},
        "opportunities": {"count": len(opps), "top": opps[0] if opps else None},
        "activity": database.list_events(bid, limit=6),
        "health": database.business_health(bid)["score"],
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
        f"сообщений обработано {stats['messages']}.",
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
    if dirty or (business.get("board_day") != day and not database.list_board_recs(bid)):
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
            "SELECT COUNT(*) FROM orders WHERE business_id = ? AND status = 'new'", bid)
        orders_stale = one(
            """SELECT COUNT(*) FROM orders WHERE business_id = ? AND status = 'new'
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


def _build_briefing(bid, day):
    """Собрать брифинг за день и сохранить. Модель зовём один раз в сутки."""
    import ai
    numbers = _briefing_numbers(bid)
    words = {}
    if ai.ai_available():
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
    database.save_briefing(bid, day, json.dumps(payload, ensure_ascii=False))
    return payload


def _load_briefing(bid, day, force=False):
    # Реактивность: если после сборки менялись данные — пересобираем брифинг,
    # чтобы Директор учёл свежие риски (напр. из документов) и цифры.
    if signals.is_dirty(bid, "briefing"):
        force = True
    row = None if force else database.get_briefing(bid, day)
    if row and row.get("payload"):
        try:
            return json.loads(row["payload"]), row.get("shown_on")
        except json.JSONDecodeError:
            pass
    payload = _build_briefing(bid, day)
    signals.settle(bid, "briefing")
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
    row = None if force else database.get_weekly_review(bid, week_start)
    if row and row.get("payload"):
        try:
            payload = json.loads(row["payload"])
            # Если прошлый сбор не получил формулировок из-за сбоя модели, а модель
            # снова доступна — пересобираем, чтобы не залипал пустой обзор.
            empty = not any(payload.get(k) for k in _WEEKLY_NARRATIVE)
            if not (empty and ai.ai_available()):
                return payload
        except json.JSONDecodeError:
            pass
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


# ---------- ИНСТРУМЕНТЫ ----------
#
# Один список — один источник правды. Чтобы инструмент заработал, достаточно
# поставить "ready": True и добавить обработчик в TOOL_HANDLERS: страница
# перерисуется сама, править вёрстку не нужно.

TOOLS = [
    {"id": "pdf",      "name": "Создать PDF",
     "about": "Прайс, счёт или отчёт одной кнопкой — из ваших данных, готовый к отправке клиенту.",
     "group": "Документы", "ready": False},
    {"id": "excel",    "name": "Экспорт Excel",
     "about": "Выгрузка клиентов, заказов и финансов в таблицу — для бухгалтера или своего анализа.",
     "group": "Документы", "ready": False},
    {"id": "contract", "name": "Сгенерировать договор",
     "about": "Договор под вашу услугу: сотрудник подставит реквизиты, предмет и сроки из памяти компании.",
     "group": "Документы", "ready": False},
    {"id": "offer",    "name": "Коммерческое предложение",
     "about": "КП под конкретного клиента — с вашими услугами, ценами и обоснованием выгоды.",
     "group": "Продажи", "ready": False},
    {"id": "letter",   "name": "Написать письмо",
     "about": "Письмо клиенту или партнёру в вашем тоне: напоминание, извинение, предложение вернуться.",
     "group": "Продажи", "ready": False},
]

TOOL_HANDLERS: dict = {}          # id → функция(bid, params); пока пусто


@app.get("/api/tools")
def api_tools(business_id: int = 0, x_auth: str = Header(default="")):
    """Список инструментов. Готовые к работе помечены ready."""
    _resolve_bid(x_auth, business_id)
    return {"tools": [{**t, "ready": t["ready"] and t["id"] in TOOL_HANDLERS} for t in TOOLS]}


@app.post("/api/tools/{tool_id}")
def api_tool_run(tool_id: int | str, business_id: int = 0,
                 x_auth: str = Header(default="")):
    """Запуск инструмента. Пока ни один не подключён — отвечаем честно."""
    bid = _resolve_bid(x_auth, business_id)
    handler = TOOL_HANDLERS.get(str(tool_id))
    if not handler:
        raise HTTPException(status_code=501, detail="Этот инструмент ещё готовится")
    return handler(bid)


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

def _fetch_url_text(url: str) -> str:
    """Скачать страницу и вытащить видимый текст (без тегов). '' при ошибке."""
    import re as _re
    import urllib.request
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (VELOR Research)"})
    with urllib.request.urlopen(req, timeout=8) as r:
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
    if not ai.ai_available():
        return {"ok": False, "answer": None}
    material = (body.text or "").strip()
    if not material and body.url:
        try:
            material = _fetch_url_text(body.url)
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
    business_id: int = 0


@app.get("/api/finance")
def api_finance(business_id: int = 0, x_auth: str = Header(default="")):
    """Сводка по деньгам + последние записи доходов/расходов."""
    bid = _resolve_bid(x_auth, business_id)
    return {"summary": database.finance_summary(bid),
            "entries": database.list_finance_entries(bid)}


@app.post("/api/finance")
def api_finance_add(body: FinanceIn, x_auth: str = Header(default="")):
    """Добавить доход или расход."""
    bid = _resolve_bid(x_auth, body.business_id)
    require_active(bid)
    kind = body.kind if body.kind in ("income", "expense") else "expense"
    database.add_finance_entry(bid, kind, (body.category or "").strip() or None,
                               body.amount, (body.note or "").strip() or None)
    return {"ok": True}


@app.post("/api/finance/{entry_id}/delete")
def api_finance_delete(entry_id: int, business_id: int = 0,
                       x_auth: str = Header(default="")):
    """Удалить запись — только свою."""
    bid = _resolve_bid(x_auth, business_id)
    database.delete_finance_entry(entry_id, bid)
    return {"ok": True}


@app.get("/api/finance/insights")
def api_finance_insights(business_id: int = 0, x_auth: str = Header(default="")):
    """AI-инсайты по финансам: выводы и рекомендации от VELOR."""
    import ai
    bid = _resolve_bid(x_auth, business_id)
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
    """Короткая сводка бизнеса — чтобы AI-сотрудник отвечал по свежим данным."""
    from datetime import date
    orders = database.get_orders(bid) or []
    clients = database.list_clients(bid) or []
    today = date.today().isoformat()
    new = sum(1 for o in orders if (o.get("status") if isinstance(o, dict) else o["status"]) == "новый")
    tod = sum(1 for o in orders if str((o.get("created_at") if isinstance(o, dict) else o["created_at"]) or "").startswith(today))
    return f"заказов всего {len(orders)}, новых {new}, сегодня {tod}, клиентов {len(clients)}"


@app.post("/api/ask")
def api_ask(body: AskIn, request: Request, x_auth: str = Header(default="")):
    """Вопрос ядру — отвечает ИИ (если ключ настроен).

    На лендинге запрос идёт без токена → короткий ответ от лица бизнеса.
    В кабинете panel-auth.js добавляет X-Auth → отвечает AI-сотрудник выбранной
    роли, зная базу знаний и свежую сводку бизнеса.
    """
    import ai
    # Защита общего ключа ИИ: лимит запросов по IP. Проверяем ДО обращения к модели —
    # при превышении отдаём понятную 429 и к ИИ не идём вовсе (никаких лишних вызовов).
    wait = ratelimit.ask_retry_after("ask:" + _client_ip(request))
    if wait:
        raise HTTPException(status_code=429, detail=(
            "Слишком много запросов подряд. Подождите " + ratelimit.human_wait(wait)
            + " и попробуйте снова."))
    if not ai.ai_available():
        return {"ok": False, "answer": None}   # сайт покажет заготовленную фразу

    payload = _auth_payload(x_auth)
    # Токен передан, но не распознан (истёк/битый) → отдаём 401, чтобы panel-auth.js
    # обновил access по refresh и повторил запрос. Иначе кабинетный вопрос молча уходил
    # в лендинг-ветку без базы знаний, и ассистент отвечал «не знаю, чем занимаешься»
    # после истечения access-токена (30 мин). Лендинг (без X-Auth) идёт как прежде.
    if x_auth and not payload:
        raise HTTPException(status_code=401, detail="Сессия истекла — обновите вход")
    bid = payload["bid"] if payload and payload.get("role") == "business" else None
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
    except Exception:
        return {"ok": False, "answer": None}


# ---------- Отдаём сайт ----------

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
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text")
    frm = msg.get("from") or {}
    if not chat_id or not text:
        return {"ok": True}                       # не текст (фото/стикер) — тихо пропускаем
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

    try:
        reply = (botcore.greeting_text(bid) if text.strip() == "/start"
                 else botcore.handle_message(bid, frm.get("id"), full_name, text))
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
