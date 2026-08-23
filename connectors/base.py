# -*- coding: utf-8 -*-
"""
Общая основа для всех коннекторов.

Каждый коннектор — отдельный модуль в этой папке, который умеет ровно две вещи:

    check(creds, meta) -> meta      проверить ключи «живым» запросом
    sync(bid, creds, meta, cursor)  забрать новое и разложить по нашим таблицам

Больше он не знает НИЧЕГО: ни про HTTP-эндпоинты VELOR, ни про фронт, ни про
шифрование. Добавить новый сервис = добавить один файл и строку в реестр.

Данные всегда ложатся в уже существующие сущности через
database.upsert_external_* — поэтому CRM, финансы, AI-директор, экспорт и цели
видят их сразу, без единой правки в своих модулях.
"""
from __future__ import annotations

import datetime
import logging

import requests

import safeurl

log = logging.getLogger("velor.connectors")

TIMEOUT = 25            # секунд на запрос: внешний сервис не должен вешать наш поток
MAX_ITEMS = 500         # сколько записей забираем за один прогон
DEFAULT_WINDOW_DAYS = 60  # как глубоко смотрим назад при первом подключении


class ConnectorError(Exception):
    """Понятная человеку ошибка подключения (её увидит владелец в кабинете)."""


def field(key, label, hint="", secret=False, required=True, placeholder=""):
    """Описание одного поля формы подключения — из него фронт рисует модалку."""
    return {"key": key, "label": label, "hint": hint, "secret": secret,
            "required": required, "placeholder": placeholder}


def public_url(url: str, *, require_https: bool = True) -> str:
    """
    Проверить адрес, который ввёл владелец (вебхук, домен магазина).

    Без этой проверки коннектор становится инструментом SSRF: достаточно
    указать вебхуком http://169.254.169.254/ — и наш сервер сам сходит за
    облачными метаданными. Поэтому любой пользовательский адрес проходит здесь.
    """
    try:
        return safeurl.normalize(url, require_https=require_https)
    except safeurl.UnsafeUrl as e:
        raise ConnectorError(str(e))


def need(creds: dict, *keys):
    """Достать обязательные значения; при отсутствии — понятная ошибка."""
    out = []
    for k in keys:
        v = (creds.get(k) or "").strip() if isinstance(creds.get(k), str) else creds.get(k)
        if not v:
            raise ConnectorError(f"Не заполнено поле «{k}».")
        out.append(v)
    return out[0] if len(out) == 1 else out


def request(method: str, url: str, *, headers=None, params=None, json=None,
            auth=None, data=None) -> dict:
    """
    HTTP-запрос к внешнему API с одинаковой обработкой ошибок.

    Смысл в том, чтобы владелец бизнеса видел «Неверный ключ», а не стектрейс:
    401/403 — ключ не тот, 429 — слишком часто, 5xx — у них авария.
    """
    try:
        r = requests.request(method, url, headers=headers, params=params, json=json,
                             auth=auth, data=data, timeout=TIMEOUT)
    except requests.Timeout:
        raise ConnectorError("Сервис не ответил вовремя. Попробуйте ещё раз через минуту.")
    except requests.RequestException as e:
        raise ConnectorError("Не удалось связаться с сервисом. Проверьте адрес и доступ в интернет.") from e

    if r.status_code in (401, 403):
        raise ConnectorError("Ключ доступа не подошёл. Проверьте, что скопировали его целиком "
                             "и что у ключа есть нужные права.")
    if r.status_code == 404:
        raise ConnectorError("Сервис не нашёл такой адрес. Проверьте домен или номер магазина.")
    if r.status_code == 429:
        raise ConnectorError("Сервис просит подождать — слишком много запросов. "
                             "Синхронизация продолжится позже сама.")
    if r.status_code >= 500:
        raise ConnectorError("На стороне сервиса сейчас сбой. Повторим позже автоматически.")
    if r.status_code >= 400:
        raise ConnectorError(_reason(r))
    try:
        return r.json() if r.text else {}
    except ValueError:
        raise ConnectorError("Сервис вернул неожиданный ответ — возможно, адрес указан неверно.")


def _short(text: str, limit: int = 160) -> str:
    t = " ".join((text or "").split())
    return t[:limit]


# Слова, по которым понятно, что дело в ключе, даже если сервис прислал не 401.
# Пример: Ozon на неверный Api-Key отвечает кодом 400 с текстом «Invalid Api-Key».
_KEY_WORDS = ("api-key", "api key", "apikey", "unauthorized", "invalid key",
              "invalid token", "access denied", "forbidden", "credential")


def _reason(r) -> str:
    """Достать из ответа человеческую причину отказа вместо сырого JSON."""
    detail = ""
    try:
        data = r.json()
        if isinstance(data, dict):
            for key in ("message", "description", "detail", "error_description", "error_message"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    detail = v.strip()
                    break
            else:
                err = data.get("error")
                if isinstance(err, dict) and isinstance(err.get("message"), str):
                    detail = err["message"].strip()
                elif isinstance(err, str):
                    detail = err.strip()
    except ValueError:
        pass
    if not detail:
        detail = _short(r.text)
    if any(w in detail.lower() for w in _KEY_WORDS):
        return ("Ключ доступа не подошёл. Проверьте, что скопировали его целиком "
                "и что у ключа есть нужные права. Ответ сервиса: " + _short(detail, 120))
    return (f"Сервис отклонил запрос (код {r.status_code}). " + _short(detail, 140)).strip()


# ---------- время ----------

def now():
    return datetime.datetime.utcnow()


def since(cursor: str | None, days: int = DEFAULT_WINDOW_DAYS) -> datetime.datetime:
    """С какого момента забирать. Есть курсор — с него, иначе окно назад."""
    if cursor:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.datetime.strptime(cursor[:19], fmt)
            except ValueError:
                continue
    return now() - datetime.timedelta(days=days)


def iso_z(dt: datetime.datetime) -> str:
    """Формат, который понимают почти все API: 2026-08-01T00:00:00.000Z."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def stamp(dt: datetime.datetime = None) -> str:
    """Наш внутренний формат времени — тот же, что во всей базе."""
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def to_stamp(value) -> str | None:
    """Привести чужую дату (ISO, с Z, с таймзоной) к нашему формату. None — если не разобрали."""
    if not value:
        return None
    s = str(value).strip().replace("T", " ").replace("Z", "")
    s = s.split("+")[0].split(".")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def day_of(value) -> str | None:
    st = to_stamp(value)
    return st[:10] if st else None


def rub(value, cents=False) -> int:
    """
    Величина суммы в целых рублях. cents=True — на входе копейки/центы.

    Возвращаем именно ВЕЛИЧИНУ (по модулю): направление денег у нас несёт поле
    kind (income/expense), а не знак числа. Раньше здесь стояло max(0, …), и
    возвраты, которые сервисы отдают отрицательными (WB: forPay = -1200),
    превращались в ноль и молча пропадали — расход не появлялся, прибыль
    оказывалась завышенной.
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if cents:
        v = v / 100.0
    return abs(int(round(v)))
