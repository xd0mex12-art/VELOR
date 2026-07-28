# -*- coding: utf-8 -*-
"""
JWT-аутентификация без внешних зависимостей: короткоживущий access-токен
(подписанный HS256) и долгоживущий refresh-токен (случайная строка, хранится
в базе только в виде хеша и может быть отозван).

Здесь — только выпуск и проверка токенов. Хранение refresh-токенов и логика
эндпоинтов живут в database.py и server.py, чтобы архитектура не менялась.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from config import JWT_SECRET, ACCESS_TTL_MIN

_SECRET_FILE = ".jwt_secret"
_secret_cache = None


def _secret() -> bytes:
    """Ключ подписи: из .env, иначе постоянный из файла (переживает перезапуск)."""
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    if JWT_SECRET:
        _secret_cache = JWT_SECRET.encode()
        return _secret_cache
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE, "rb") as f:
            _secret_cache = f.read()
    else:
        _secret_cache = secrets.token_bytes(32)
        with open(_SECRET_FILE, "wb") as f:
            f.write(_secret_cache)
    return _secret_cache


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def make_access_token(claims: dict, ttl_min: int = None) -> str:
    """Собрать подписанный access-токен (HS256) с истечением."""
    ttl = (ttl_min if ttl_min is not None else ACCESS_TTL_MIN) * 60
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = dict(claims)
    payload["iat"] = now
    payload["exp"] = now + ttl
    seg = (_b64(json.dumps(header, separators=(",", ":")).encode()) + "." +
           _b64(json.dumps(payload, separators=(",", ":")).encode()))
    sig = hmac.new(_secret(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64(sig)


def decode_access_token(token: str):
    """Проверить подпись и срок. Возвращает payload или None."""
    if not token or token.count(".") != 2:
        return None
    try:
        seg, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret(), seg.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        payload = json.loads(_unb64(seg.split(".", 1)[1]))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def new_refresh_token() -> str:
    """Сгенерировать случайный refresh-токен (в базе хранится только его хеш)."""
    return secrets.token_urlsafe(32)
