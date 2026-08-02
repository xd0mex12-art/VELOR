# -*- coding: utf-8 -*-
"""
Защита от перебора паролей и спама регистраций — в памяти, без внешних служб.

Две простые механики:
  1. Блокировка входа по НЕУДАЧНЫМ попыткам. Считаем промахи с одного ключа
     (обычно IP) в скользящем окне; после превышения порога временно
     возвращаем «подождите». Удачный вход сразу обнуляет счётчик.
  2. Ограничение частоты регистраций с одного ключа за окно (антиспам).

Хранится всё в оперативной памяти процесса. Для одного сервера этого достаточно;
при рестарте счётчики сбрасываются — это нормально и даже удобно.
"""
import threading
import time

from config import (LOGIN_MAX_FAILS, LOGIN_FAIL_WINDOW_MIN, LOGIN_BLOCK_MIN,
                    REGISTER_MAX, REGISTER_WINDOW_MIN, ASK_MAX, ASK_WINDOW_MIN)

_lock = threading.Lock()
_fails: dict[str, list[float]] = {}    # ключ -> метки времени неудачных попыток
_blocked: dict[str, float] = {}        # ключ -> момент, до которого заблокирован
_events: dict[str, list[float]] = {}   # ключ -> метки времени событий (регистрации)

_LOGIN_WINDOW = LOGIN_FAIL_WINDOW_MIN * 60
_LOGIN_BLOCK = LOGIN_BLOCK_MIN * 60
_REG_WINDOW = REGISTER_WINDOW_MIN * 60
_ASK_WINDOW = ASK_WINDOW_MIN * 60


def _clean(seq: list[float], horizon: float) -> list[float]:
    """Оставить только метки времени свежее горизонта."""
    return [t for t in seq if t >= horizon]


def login_retry_after(key: str) -> int:
    """
    Сколько секунд ещё нужно ждать этому ключу перекрытым (0 — можно пробовать).
    Вызывать ДО проверки пароля.
    """
    now = time.time()
    with _lock:
        until = _blocked.get(key)
        if until and until > now:
            return int(until - now) + 1
        if until:  # срок блокировки истёк — снимаем и обнуляем историю
            _blocked.pop(key, None)
            _fails.pop(key, None)
    return 0


def note_login_fail(key: str) -> None:
    """Записать неудачную попытку входа; при превышении порога — заблокировать."""
    now = time.time()
    with _lock:
        seq = _clean(_fails.get(key, []), now - _LOGIN_WINDOW)
        seq.append(now)
        _fails[key] = seq
        if len(seq) >= LOGIN_MAX_FAILS:
            _blocked[key] = now + _LOGIN_BLOCK
            _fails.pop(key, None)


def note_login_success(key: str) -> None:
    """Удачный вход — стираем историю промахов и блокировку по этому ключу."""
    with _lock:
        _fails.pop(key, None)
        _blocked.pop(key, None)


def register_retry_after(key: str) -> int:
    """
    Сколько секунд ждать до следующей разрешённой регистрации с этого ключа
    (0 — можно регистрировать). Учитывает попытку как совершённую.
    """
    now = time.time()
    with _lock:
        seq = _clean(_events.get(key, []), now - _REG_WINDOW)
        if len(seq) >= REGISTER_MAX:
            oldest = seq[0]
            return int(oldest + _REG_WINDOW - now) + 1
        seq.append(now)
        _events[key] = seq
    return 0


def ask_retry_after(key: str) -> int:
    """
    Сколько секунд ждать до следующего разрешённого запроса к ИИ с этого ключа
    (обычно IP); 0 — можно спрашивать. Та же оконная механика, что у регистраций
    (общий счётчик _events, ключи не пересекаются: «ask:…» vs «reg:…»). Вызывать
    ДО обращения к модели — при превышении к ИИ не идём вовсе.
    """
    now = time.time()
    with _lock:
        seq = _clean(_events.get(key, []), now - _ASK_WINDOW)
        if len(seq) >= ASK_MAX:
            oldest = seq[0]
            return int(oldest + _ASK_WINDOW - now) + 1
        seq.append(now)
        _events[key] = seq
    return 0


def human_wait(seconds: int) -> str:
    """Понятная человеку подсказка, сколько ждать."""
    if seconds <= 90:
        return "около минуты"
    minutes = (seconds + 59) // 60
    if minutes < 5:
        return f"{minutes} минуты"
    return f"{minutes} минут"
