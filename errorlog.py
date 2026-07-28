# -*- coding: utf-8 -*-
"""
Чтение и разбор файла errors.log для «Журнала ошибок» в кабинете владельца.

Формат строки-записи (см. logging.basicConfig в server.py):
    2026-07-26 11:35:33,536 WARNING velor: текст сообщения
Трассировка исключения (log.exception) идёт следующими строками БЕЗ даты —
их приклеиваем к предыдущей записи как полный текст ошибки.

Файл может содержать легаси-строки в cp1251 (старое логирование на Windows),
поэтому декодируем каждую строку устойчиво: сначала utf-8, при ошибке — cp1251.
"""
import os
import re

import config

# Тот же файл, куда пишет логгер в server.py (каталог из config.LOG_DIR).
LOG_FILE = os.path.join(config.LOG_DIR, "errors.log")

# Начало записи: дата, время, УРОВЕНЬ, логгер: сообщение
_ENTRY_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})[,.]?\d* (\w+) ([\w.]+): (.*)$"
)


def _decode(raw: bytes) -> str:
    """Устойчивое декодирование строки лога (utf-8, иначе cp1251)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def _read_lines() -> list[str]:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "rb") as f:
        return [_decode(line).rstrip("\r\n") for line in f]


def parse_entries() -> list[dict]:
    """
    Разобрать лог на записи (новые — первыми). Каждая запись:
    {date, time, ts, level, logger, message, full}
    """
    entries: list[dict] = []
    cur: dict | None = None
    for line in _read_lines():
        m = _ENTRY_RE.match(line)
        if m:
            date, tm, level, logger, msg = m.groups()
            cur = {
                "date": date,
                "time": tm,
                "ts": f"{date} {tm}",
                "level": level.upper(),
                "logger": logger,
                "message": msg,
                "full": line,
            }
            entries.append(cur)
        elif cur is not None:
            # продолжение (трассировка) — доклеиваем к текущей записи
            cur["full"] += "\n" + line
        # строки до первой валидной записи игнорируем
    entries.reverse()   # новые сверху
    return entries


def query(date: str = "", q: str = "", level: str = "", limit: int = 500) -> dict:
    """
    Отфильтровать записи по дате (точное YYYY-MM-DD), уровню и подстроке поиска.
    Возвращает {items, total, shown, levels, exists, size}.
    """
    exists = os.path.exists(LOG_FILE)
    size = os.path.getsize(LOG_FILE) if exists else 0
    entries = parse_entries()
    levels = sorted({e["level"] for e in entries})

    date = (date or "").strip()
    level = (level or "").strip().upper()
    ql = (q or "").strip().lower()

    def keep(e: dict) -> bool:
        if date and e["date"] != date:
            return False
        if level and e["level"] != level:
            return False
        if ql and ql not in e["full"].lower():
            return False
        return True

    filtered = [e for e in entries if keep(e)]
    total = len(filtered)
    items = filtered[: max(1, limit)]
    return {
        "items": items,
        "total": total,
        "shown": len(items),
        "levels": levels,
        "exists": exists,
        "size": size,
    }
