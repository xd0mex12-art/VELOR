# -*- coding: utf-8 -*-
"""
Шифрование чужих секретов (ключи API подключённых сервисов).

Зачем отдельный модуль: пароли мы храним ХЕШЕМ — их не нужно читать обратно.
А ключ Ozon или ЮKassa читать обратно нужно на каждую синхронизацию, поэтому
его нельзя хешировать — только шифровать. Утечка базы не должна отдавать
злоумышленнику доступ к кассам и складам наших клиентов.

Ключ шифрования берётся из переменной окружения SECRET_KEY (или JWT_SECRET, если
отдельный не задан), а локально — из файла .secret_box, как это уже сделано для
подписи токенов в auth.py. Смена ключа делает старые записи нечитаемыми: это
осознанно (лучше попросить владельца ввести ключ заново, чем молча работать
с испорченными данными).
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_KEY_FILE = ".secret_box"
_box = None


def _material() -> bytes:
    """Секретный материал: из окружения или из локального файла (создаём при первом запуске)."""
    env = (os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET") or "").strip()
    if env:
        return env.encode()
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read()
    raw = os.urandom(32)
    with open(_KEY_FILE, "wb") as f:
        f.write(raw)
    return raw


def _fernet() -> Fernet:
    global _box
    if _box is None:
        # Растягиваем произвольный секрет в 32-байтный ключ Fernet.
        digest = hashlib.pbkdf2_hmac("sha256", _material(), b"velor-connections-v1", 200_000)
        _box = Fernet(base64.urlsafe_b64encode(digest))
    return _box


def seal(text: str) -> str:
    """Зашифровать секрет для хранения в базе."""
    return _fernet().encrypt((text or "").encode()).decode()


def open_(blob: str) -> str:
    """Расшифровать секрет. Пустая строка, если запись повреждена или ключ сменился."""
    if not blob:
        return ""
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ""


def mask(text: str, keep: int = 4) -> str:
    """Как показать ключ в интерфейсе: «…a1b2». Целиком наружу не отдаём никогда."""
    t = (text or "").strip()
    if not t:
        return ""
    return "…" + t[-keep:] if len(t) > keep else "…"
