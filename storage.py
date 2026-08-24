"""
Хранилище оригиналов файлов.

Зачем отдельный модуль. До Universal Inbox проект НЕ хранил файлы вообще:
/api/documents/upload вытаскивал из PDF текст и выбрасывал оригинал, а папка
uploads/ существовала только в docker-томе и в скрипте бэкапа. Inbox обязан
сохранить присланное как есть — значит слой хранения нужно завести, и лучше
один, чем по копии в каждой будущей фиче.

Почему два backend'а, а не просто папка. Боевой деплой — Render, где диск
эфемерный: файлы на нём исчезают при каждом передеплое, и «сохранённый
оригинал» оказывался бы ложью. Постоянно там живёт только Postgres из
DATABASE_URL. Поэтому:

    DATABASE_URL задан  → backend "db":  оригинал лежит в таблице inbox_blobs
                                         (переживает передеплой, попадает в
                                          те же бэкапы, что и остальные данные);
    DATABASE_URL пуст   → backend "fs":  файл лежит в UPLOAD_DIR (локальная
                                         разработка и Docker с томом).

Выбор можно перебить переменной INBOX_STORAGE=fs|db — например, если у Render
появится постоянный диск.

Ключ файла (storage_key) — это ИМЯ, а не путь: 32 шестнадцатеричных знака плюс
расширение. Имя, присланное пользователем, в путь не попадает никогда, поэтому
«../../etc/passwd» в filename не может увести запись за пределы каталога.
"""
import os
import re
import uuid

from config import DB_PATH  # noqa: F401  (импорт держит единый источник настроек)

# Каталог для backend'а "fs". В docker-compose это том ./uploads:/app/uploads.
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")

# Ключ: 32 hex + необязательное короткое расширение. Всё остальное — отказ.
_KEY_RE = re.compile(r"^[0-9a-f]{32}(\.[a-z0-9]{1,8})?$")


class StorageError(Exception):
    """Не удалось сохранить, прочитать или удалить файл."""


def backend() -> str:
    """
    Какой backend активен. Признак «мы на Postgres» берём у database, а не из
    окружения повторно: иначе два модуля могли бы разойтись во мнении и файл
    ушёл бы в базу, а искали бы его на диске.
    """
    forced = (os.getenv("INBOX_STORAGE") or "").strip().lower()
    if forced in ("fs", "db"):
        return forced
    import database
    return "db" if database._PG else "fs"


def new_key(filename: str = "") -> str:
    """Придумать имя для хранения. Расширение берём из оригинала — только
    буквы и цифры, не длиннее восьми знаков; всё прочее отбрасываем."""
    ext = ""
    dot = (filename or "").rfind(".")
    if dot != -1:
        raw = filename[dot + 1:].lower()
        if raw.isalnum() and len(raw) <= 8:
            ext = "." + raw
    return uuid.uuid4().hex + ext


def _check(key: str) -> str:
    if not key or not _KEY_RE.match(key):
        raise StorageError("Недопустимый ключ файла")
    return key


def _fs_path(business_id: int, key: str) -> str:
    """Путь на диске. business_id приводится к int — в путь не попадает строка."""
    return os.path.join(UPLOAD_DIR, "inbox", str(int(business_id)), _check(key))


def put(business_id: int, key: str, data: bytes) -> str:
    """Сохранить оригинал под заданным ключом. Возвращает тот же ключ."""
    _check(key)
    if backend() == "fs":
        path = _fs_path(business_id, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Пишем во временный файл и переименовываем: если процесс упадёт на
        # середине, в хранилище не останется обрезанного «оригинала».
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return key
    import database
    database.put_blob(business_id, key, data)
    return key


def get(business_id: int, key: str) -> bytes:
    """Прочитать оригинал. Чужой файл прочитать нельзя: business_id входит
    и в путь, и в условие выборки."""
    _check(key)
    if backend() == "fs":
        path = _fs_path(business_id, key)
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise StorageError("Файл не найден в хранилище")
    import database
    data = database.get_blob(business_id, key)
    if data is None:
        raise StorageError("Файл не найден в хранилище")
    return data


def delete(business_id: int, key: str) -> None:
    """Удалить оригинал. Отсутствие файла ошибкой не считаем — удаление
    должно быть идемпотентным, иначе запись в Inbox нельзя стереть повторно."""
    if not key:
        return
    try:
        _check(key)
    except StorageError:
        return
    if backend() == "fs":
        try:
            os.remove(_fs_path(business_id, key))
        except FileNotFoundError:
            pass
        return
    import database
    database.delete_blob(business_id, key)
