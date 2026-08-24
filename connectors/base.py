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

import contextlib
import datetime
import logging
import os
import threading
import time

import requests

import safeurl

log = logging.getLogger("velor.connectors")

TIMEOUT = 25            # секунд на запрос: внешний сервис не должен вешать наш поток
MAX_ITEMS = 500         # сколько записей забираем за один прогон
DEFAULT_WINDOW_DAYS = 60  # как глубоко смотрим назад при первом подключении

TRIES = 3               # попыток на запрос: сеть и 5xx лечатся повтором
RETRY_WAIT_MAX = 20     # дольше этого ждать перед повтором не станем

# ---------- темп запросов ----------
#
# Наши коннекторы ходят страницами в цикле (у amoCRM ещё и контакт на каждую
# сделку), а сервисы считают запросы в секунду. Без паузы первая же синхронизация
# у активного магазина упирается в 429 и половина данных не доезжает.
#
# Поэтому пауза встроена в сам request(): любой цикл в любом коннекторе
# автоматически идёт вежливо, и про это не надо помнить в 11 модулях.
HOST_GAP = float(os.getenv("CONNECTOR_HOST_GAP", "0.34"))   # секунд между запросами к хосту
HOST_GAP_SPECIAL = {
    # Статистика Wildberries пускает примерно один запрос в минуту на аккаунт.
    # Пытаться чаще бессмысленно: в ответ прилетает 429, а не данные.
    "statistics-api.wildberries.ru": 61.0,
}
PATIENT_WAIT_MAX = 180   # сколько готов ждать фоновый поток
IMPATIENT_WAIT_MAX = 25  # сколько готов ждать человек, который смотрит на спиннер

_gap_lock = threading.Lock()
_next_ok: dict[str, float] = {}      # хост -> когда ему можно снова
_local = threading.local()


@contextlib.contextmanager
def patient():
    """
    Пометить поток как фоновый: здесь можно спокойно ждать своей очереди.

    В обработчике HTTP-запроса ждать минуту нельзя — человек смотрит на кнопку.
    А фоновой синхронизации спешить некуда, и она дожидается окна Wildberries
    вместо того, чтобы бросать ошибку.
    """
    old = getattr(_local, "patient", False)
    _local.patient = True
    try:
        yield
    finally:
        _local.patient = old


def _wait_budget() -> float:
    return PATIENT_WAIT_MAX if getattr(_local, "patient", False) else IMPATIENT_WAIT_MAX


def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].lower()


def _pace(url: str) -> None:
    """Дождаться своей очереди к хосту. Очередь общая на процесс и на потоки."""
    if HOST_GAP <= 0:                # CONNECTOR_HOST_GAP=0 — выключено (тесты, отладка)
        return
    host = _host_of(url)
    gap = HOST_GAP_SPECIAL.get(host, HOST_GAP)
    with _gap_lock:
        now = time.monotonic()
        slot = max(now, _next_ok.get(host, 0.0))
        wait = slot - now
        if wait > _wait_budget():
            # Место в очереди не занимаем — иначе накажем и следующего.
            # Текст должен быть честным и при подключении, и в карточке
            # источника: в первом случае ничего ещё не подключено, обещать
            # «данные подтянутся сами» нельзя.
            raise ConnectorError("Сервис разрешает запросы редко и сейчас просит паузу. "
                                 "Попробуйте через минуту.")
        _next_ok[host] = slot + gap
    if wait > 0:
        time.sleep(wait)


class ConnectorError(Exception):
    """Понятная человеку ошибка подключения (её увидит владелец в кабинете)."""


class AuthError(ConnectorError):
    """
    Сервис не принял ключ: отозвали, истёк, не хватает прав.

    Отдельный класс, потому что это другой ответ владельцу. «Сервис лежит» —
    подождать; «ключ не подошёл» — переподключить. Одинаковый статус для
    обоих случаев заставлял бы человека гадать, что делать.
    """


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


def _retry_after(r, attempt: int) -> float:
    """Сколько ждать перед повтором: сервис попросил сам — слушаем его."""
    raw = (r.headers.get("Retry-After") or "").strip() if r is not None else ""
    try:
        if raw:
            return max(0.0, float(raw))
    except ValueError:
        pass                      # бывает и дата — тогда считаем сами
    return min(RETRY_WAIT_MAX, 1.5 * (2 ** attempt))


def request(method: str, url: str, *, headers=None, params=None, json=None,
            auth=None, data=None, timeout=None) -> dict:
    """
    HTTP-запрос к внешнему API с одинаковой обработкой ошибок и повторами.

    Смысл в том, чтобы владелец бизнеса видел «Неверный ключ», а не стектрейс:
    401/403 — ключ не тот, 404 — адрес, 4xx — причина словами сервиса.

    Обрыв связи, таймаут, 429 и 5xx — это не ошибка настройки, а рябь на линии.
    Раньше любая такая рябь роняла весь прогон и данные ждали полчаса до
    следующего круга; теперь запрос повторяется, а 429 ждёт ровно столько,
    сколько попросил сам сервис в заголовке Retry-After.

    Все наши запросы — чтение, поэтому повтор безопасен: дублей он не создаёт.
    """
    last = None
    for attempt in range(TRIES):
        _pace(url)
        try:
            r = requests.request(method, url, headers=headers, params=params, json=json,
                                 auth=auth, data=data, timeout=timeout or TIMEOUT)
        except requests.Timeout as e:
            last = ConnectorError("Сервис не ответил вовремя. Попробуйте ещё раз через минуту.")
            e_wait = min(RETRY_WAIT_MAX, 1.5 * (2 ** attempt))
        except requests.RequestException as e:
            last = ConnectorError("Не удалось связаться с сервисом. "
                                  "Проверьте адрес и доступ в интернет.")
            e_wait = min(RETRY_WAIT_MAX, 1.5 * (2 ** attempt))
        else:
            # Ошибки настройки повторять бессмысленно — ключ не станет верным.
            if r.status_code in (401, 403):
                raise AuthError("Ключ доступа не подошёл. Проверьте, что скопировали его целиком "
                                "и что у ключа есть нужные права.")
            if r.status_code == 404:
                raise ConnectorError("Сервис не нашёл такой адрес. Проверьте домен или номер магазина.")
            if r.status_code == 429 or r.status_code >= 500:
                busy = r.status_code == 429
                last = ConnectorError(
                    "Сервис просит подождать — слишком много запросов. "
                    "Синхронизация продолжится позже сама." if busy else
                    "На стороне сервиса сейчас сбой. Повторим позже автоматически.")
                e_wait = _retry_after(r, attempt)
            elif r.status_code >= 400:
                raise ConnectorError(_reason(r))
            else:
                try:
                    return r.json() if r.text else {}
                except ValueError:
                    raise ConnectorError("Сервис вернул неожиданный ответ — "
                                         "возможно, адрес указан неверно.")

        # Сюда попадаем только с временной бедой. Ждём, если ожидание разумное.
        if attempt + 1 >= TRIES or e_wait > _wait_budget():
            break
        log.info("Повтор запроса к %s через %.1f с (попытка %s из %s)",
                 _host_of(url), e_wait, attempt + 2, TRIES)
        time.sleep(e_wait)
    raise last


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
