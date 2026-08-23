# -*- coding: utf-8 -*-
"""
Проверка адреса, который ввёл пользователь, перед тем как мы по нему пойдём.

Любое место, где сервер сам открывает ссылку из формы (анализ конкурента,
вебхук Bitrix24, адрес магазина WooCommerce), — это потенциальный SSRF:
пользователь пишет http://169.254.169.254/ или http://localhost:8000/, и наш
сервер послушно читает СВОИ внутренние адреса и облачные метаданные с ключами.

Поэтому правило одно на весь проект: только http(s) и только имя, которое
резолвится в публичный IP. Проверка живёт здесь, чтобы её нельзя было забыть
в новом коннекторе.
"""
import ipaddress
import socket
import urllib.parse


class UnsafeUrl(ValueError):
    """Адрес ведёт во внутреннюю сеть или составлен неверно."""


def normalize(url: str, *, require_https: bool = False) -> str:
    """Проверить адрес и вернуть его нормализованным. Иначе — UnsafeUrl."""
    raw = (url or "").strip()
    if not raw:
        raise UnsafeUrl("Адрес не указан.")
    if "://" not in raw:
        raw = "https://" + raw

    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("Поддерживаются только адреса http:// и https://.")
    if require_https and parts.scheme != "https":
        raise UnsafeUrl("Нужен адрес по https:// — по http ключи шли бы открытым текстом.")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("Не разобрал адрес.")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        raise UnsafeUrl("Такой адрес не найден — проверьте написание.")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise UnsafeUrl("Этот адрес ведёт во внутреннюю сеть — укажите публичный адрес.")
    return urllib.parse.urlunsplit(parts)
