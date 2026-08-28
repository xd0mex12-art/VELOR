# -*- coding: utf-8 -*-
"""
Единое окно входящих: одна дверь для всего, что касается бизнеса.

Зачем ещё один слой. Приёмник, разбор, реестр записей и журнал происхождения
уже написаны и работают — но входов в них было два: заметка отдельным
эндпоинтом, файлы отдельным, а всё, что придёт завтра из бота или с сайта,
стало бы третьим и четвёртым. Тогда правила «что принимаем», «сколько за раз»
и «не принимали ли это раньше» пришлось бы повторять в каждом входе, и они
разошлись бы уже на втором.

Поэтому здесь ровно одно: превратить ЛЮБОЙ вход в одинаковые события приёма и
отдать их разбору. Ниже по течению никто не знает, откуда пришёл материал:
кабинет, бот, письмо и выгрузка выглядят одинаково, отличаясь только пометкой
source — так канал добавляется без единой правки в понимании и в памяти.

Что этот модуль НЕ делает, сознательно:
  • не разбирает материал сам — это understanding.py;
  • не создаёт записи — это entities.py;
  • не решает, что показать человеку — это server.py;
  • не знает про HTTP: ни Request, ни UploadFile сюда не попадают.

Главное обещание: принятое не теряется и не удваивается. Первое обеспечивает
storage, второе — отпечаток содержимого: тот же чек, присланный дважды, — это
один расход, даже если файл переименовали.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

import database
import storage
import understanding

log = logging.getLogger("velor.intake")

# ── что принимаем ──────────────────────────────────────────────────────────
# Список закрытый и живёт здесь, а не в обработчике запроса: правило «этот тип
# мы принимаем» одинаково для кабинета, бота и почты. Тип определяется по
# расширению, а не по заголовку от клиента: заголовок присылает отправитель.
TYPES = {
    # изображения и скриншоты
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "heic": "image/heic", "heif": "image/heif", "tif": "image/tiff", "tiff": "image/tiff",
    # документы
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "rtf": "application/rtf", "odt": "application/vnd.oasis.opendocument.text",
    # таблицы
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "csv": "text/csv", "tsv": "text/tab-separated-values",
    # текст и данные
    "txt": "text/plain", "md": "text/markdown", "json": "application/json",
    "xml": "application/xml", "zip": "application/zip",
    # голосовые: расшифровки пока нет, но принять и сохранить обязаны —
    # иначе тип VOICE_INFORMATION недостижим, а материал теряется
    "ogg": "audio/ogg", "oga": "audio/ogg", "mp3": "audio/mpeg",
    "m4a": "audio/mp4", "wav": "audio/wav", "amr": "audio/amr",
}

# Чего мы честно НЕ умеем прочитать, даже когда принимаем и храним. Человеку
# это говорится сразу, а не после разбора: «принял, но ничего не понял» —
# худший вид ответа, потому что выглядит как поломка.
UNREADABLE = {
    "audio": ("ogg", "oga", "mp3", "m4a", "wav", "amr"),
    "archive": ("zip",),
    "legacy": ("doc", "rtf", "odt", "xls", "ods"),
}
UNREADABLE_WHY = {
    "audio": "Голосовое сохраню, но расшифровывать речь VELOR пока не умеет — "
             "напишите словами, что в нём.",
    "archive": "Архив сохраню целиком, но заглянуть внутрь не умею — "
               "пришлите файлы из него по отдельности.",
    "legacy": "Формат сохраню, но прочитать текст из него не умею — "
              "пересохраните в PDF, DOCX, XLSX или CSV.",
}

MAX_BYTES = int(os.getenv("INBOX_MAX_MB", "25")) * 1024 * 1024
MAX_FILES = 10          # за один заход — чтобы одна форма не легла на минуту
NOTE_MAX = 20000        # знаков в заметке


class IntakeError(Exception):
    """Принять нельзя — с объяснением для человека."""


def ext_of(filename: str) -> str:
    """Расширение из имени файла — в нижнем регистре, без точки."""
    name = (filename or "").strip().replace("\\", "/").split("/")[-1]
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot > 0 else ""


def safe_name(filename: str) -> str:
    """
    Имя для показа и для скачивания. Путь убираем полностью: имя приходит от
    отправителя, а в файловую систему оно и так не попадает — там своё имя из
    storage.new_key(). Здесь важно другое: не дать управляющим символам и
    переводам строк уехать в заголовок ответа.
    """
    name = (filename or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f\"]", "", name)[:180]
    return name or "файл"


def unreadable_note(filename: str) -> str | None:
    """Честная строка о том, что содержимое прочитать не выйдет. Нет — None."""
    ext = ext_of(filename)
    for group, exts in UNREADABLE.items():
        if ext in exts:
            return UNREADABLE_WHY[group]
    return None


# ── отпечаток ──────────────────────────────────────────────────────────────

_WS = re.compile(r"\s+", re.U)


def fingerprint(*, data: bytes | None = None, text: str | None = None) -> str:
    """
    Отпечаток содержимого — чтобы не принять одно и то же дважды.

    Файл считается по байтам: переименование не меняет содержимого, а значит,
    не делает из одного чека два. Заметка — по тексту, приведённому к общему
    виду (регистр, пробелы, переводы строк): человек, отправивший ту же мысль
    ещё раз с другой расстановкой пробелов, имел в виду одно и то же.

    Отпечаток отличается для файла и для текста намеренно: файл с текстом
    «Стрижка 1500» и заметка «Стрижка 1500» — разные материалы, у первого есть
    оригинал, который можно открыть.
    """
    h = hashlib.sha256()
    if data is not None:
        h.update(b"file\x00")
        h.update(data)
    else:
        norm = _WS.sub(" ", (text or "").strip().lower())
        h.update(b"text\x00")
        h.update(norm.encode("utf-8"))
    return h.hexdigest()


# ── разбор после приёма ────────────────────────────────────────────────────

def may_autoapply(business_id: int, channel: str) -> bool:
    """
    Разрешено ли VELOR записывать понятое САМ, без нажатия человека.

    Отдельной настройки для этого не заводим: право уже описано уровнем
    автономии канала (ai_policy), и заводить рядом второе значило бы завести
    вторую правду. Первый уровень — «только отвечает»: разбирать материал он
    по-прежнему разбирает, но записывать в память бизнеса без человека не
    вправе. Со второго — вправе, и то лишь безопасное и при высокой
    уверенности: это решают пороги в understanding, а не мы.

    Кабинет сюда не попадает: там человек и так стоит перед экраном.
    """
    try:
        import sales
        return int(sales.policy(business_id, channel).get("level") or 0) >= 2
    except Exception:
        # Политику не прочитали — считаем, что права нет. Ошибка в защите
        # должна оборачиваться лишним вопросом человеку, а не лишней записью.
        log.exception("Приём: не удалось прочитать автономию канала %s (biz %s)",
                      channel, business_id)
        return False


def understand(business_id: int, item_id: int, background: bool = True,
               auto=None) -> None:
    """
    Запустить разбор принятого материала.

    В обычной работе — отдельным потоком: чтение PDF и ответ модели занимают
    секунды, а отправитель должен получить ответ сразу. В тестах и при
    отключённых фоновых задачах считаем на месте, чтобы поведение было
    предсказуемым, а не «когда-нибудь потом».
    """
    if not background or os.getenv("DISABLE_SYNC_WORKER"):
        _understand_safely(business_id, item_id, auto)
        return
    import threading
    threading.Thread(target=_understand_safely, args=(business_id, item_id, auto),
                     name=f"velor-intake-{item_id}", daemon=True).start()


def _understand_safely(business_id: int, item_id: int, auto=None) -> None:
    """Разбор может упасть, а уронить приём — нет: материал уже сохранён."""
    try:
        understanding.process(business_id, item_id, auto=auto)
    except Exception:
        log.exception("Приём: разбор упал (biz %s, материал %s)", business_id, item_id)
        try:
            database.set_inbox_status(item_id, business_id, "FAILED", "Разбор не удался")
        except Exception:
            pass


# ── сам приём ──────────────────────────────────────────────────────────────

def _existing(business_id, digest):
    """Уже принимали такое? Возвращает запись или None."""
    try:
        return database.find_inbox_by_hash(business_id, digest)
    except Exception:
        # Отпечаток — защита, а не условие работы. Если он почему-то не
        # считался, принять материал всё равно правильнее, чем отказать.
        log.exception("Приём: не удалось проверить повтор (biz %s)", business_id)
        return None


def receive_text(business_id: int, text: str, *, title: str | None = None,
                 source: str = "web", actor: str | None = None,
                 actor_id: int | None = None, meta: dict | None = None,
                 background: bool = True, auto=None) -> dict:
    """
    Принять текст: заметку, пересланное сообщение, вставленный кусок письма.

    Возвращает {"item_id", "duplicate", "of"} — где of указывает на ранее
    принятый материал, если это он же.
    """
    text = (text or "").strip()
    if not text:
        raise IntakeError("Пустая заметка — напишите хоть слово.")
    if len(text) > NOTE_MAX:
        raise IntakeError("Заметка длиннее %d знаков — сократите или приложите файлом."
                          % NOTE_MAX)

    digest = fingerprint(text=text)
    was = _existing(business_id, digest)
    if was:
        return {"item_id": was["id"], "duplicate": True, "of": was["id"]}

    head = (title or "").strip() or text.splitlines()[0][:120]
    item_id = database.add_inbox_item(
        business_id, kind="text", title=head, body=text,
        size=len(text.encode("utf-8")), source=source,
        content_hash=digest, actor=actor, actor_id=actor_id, meta=meta)
    understand(business_id, item_id, background=background, auto=auto)
    return {"item_id": item_id, "duplicate": False, "of": None}


def receive_file(business_id: int, filename: str, data: bytes, *,
                 source: str = "web", actor: str | None = None,
                 actor_id: int | None = None, meta: dict | None = None,
                 background: bool = True, auto=None) -> dict:
    """
    Принять один файл. Проверки те же для любого канала, поэтому они здесь, а
    не в обработчике запроса.
    """
    name = safe_name(filename)
    ext = ext_of(filename)
    if not data:
        raise IntakeError("Файл пустой.")
    if len(data) > MAX_BYTES:
        raise IntakeError("Больше %d МБ." % (MAX_BYTES // (1024 * 1024)))
    if ext not in TYPES:
        raise IntakeError("Такой тип файла пока не принимаем.")

    digest = fingerprint(data=data)
    was = _existing(business_id, digest)
    if was:
        return {"item_id": was["id"], "duplicate": True, "of": was["id"]}

    key = storage.new_key(name)
    try:
        storage.put(business_id, key, data)
    except Exception:
        log.exception("Приём: не удалось сохранить файл (biz %s)", business_id)
        raise IntakeError("Хранилище недоступно.")

    item_id = database.add_inbox_item(
        business_id, kind="file", title=name, filename=name, mime=TYPES[ext],
        size=len(data), storage_key=key, source=source,
        content_hash=digest, actor=actor, actor_id=actor_id, meta=meta)
    understand(business_id, item_id, background=background, auto=auto)
    return {"item_id": item_id, "duplicate": False, "of": None}


def receive(business_id: int, *, text: str | None = None, files=None,
            source: str = "web", actor: str | None = None,
            actor_id: int | None = None, meta: dict | None = None,
            background: bool = True, auto=None) -> dict:
    """
    Одна дверь: принять текст и файлы одной отправкой.

    files — последовательность пар (имя, байты). Один плохой файл в пачке не
    отменяет остальные: человек, приславший десять чеков, не должен потерять
    девять из-за одного повреждённого.

    Возвращает
        {"accepted": [id…], "duplicates": [{id, of, what}…],
         "rejected": [{what, error}…]}
    — и ничего про интерфейс: как это показать, решает вызывающая сторона.
    """
    files = list(files or [])
    if not (text or "").strip() and not files:
        raise IntakeError("Нечего принимать: ни текста, ни файлов.")
    if len(files) > MAX_FILES:
        raise IntakeError("За один раз — не больше %d файлов." % MAX_FILES)

    accepted, duplicates, rejected = [], [], []

    # Текст первым: когда его прислали вместе с файлами, он обычно объясняет,
    # что это за файлы, — и в ленте должен стоять рядом, а не после них.
    if (text or "").strip():
        try:
            got = receive_text(business_id, text, source=source, actor=actor,
                               actor_id=actor_id, meta=meta, background=background,
                               auto=auto)
            (duplicates if got["duplicate"] else accepted).append(
                {"id": got["item_id"], "of": got["of"], "what": "заметка"}
                if got["duplicate"] else got["item_id"])
        except IntakeError as e:
            rejected.append({"what": "заметка", "error": str(e)})
        except Exception:
            # База или хранилище отказали. Отправка могла нести ещё файлы, и
            # ронять её целиком нельзя — но и делать вид, что заметка принята,
            # тоже: она не принята, и человек должен это увидеть.
            log.exception("Приём: заметка не принята (biz %s)", business_id)
            rejected.append({"what": "заметка",
                             "error": "Не удалось сохранить — попробуйте ещё раз."})

    for name, data in files:
        shown = safe_name(name)
        try:
            got = receive_file(business_id, name, data, source=source, actor=actor,
                               actor_id=actor_id, meta=meta, background=background,
                               auto=auto)
        except IntakeError as e:
            rejected.append({"what": shown, "error": str(e)})
            continue
        except Exception:
            log.exception("Приём: файл не принят (biz %s)", business_id)
            rejected.append({"what": shown, "error": "Не удалось принять файл."})
            continue
        if got["duplicate"]:
            duplicates.append({"id": got["item_id"], "of": got["of"], "what": shown})
        else:
            accepted.append(got["item_id"])

    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


# ── что сказать отправителю ────────────────────────────────────────────────
# Кабинет показывает карточки, бот — текст, завтра появится третий канал. Но
# СЛОВА должны быть одни: если «нужно подтверждение» в кабинете и «сохранено»
# в боте означают одно и то же событие, доверять нельзя ни тому, ни другому.
# Поэтому формулировка живёт здесь, рядом с приёмом, а не в каждом канале.

def _plural(n, one, few, many):
    a, b = abs(n) % 100, abs(n) % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    return one if b == 1 else many


def describe(business_id: int, item_id: int) -> str:
    """Одна строка о судьбе одного материала: что понял и что с этим сделал."""
    item = database.get_inbox_item(item_id, business_id) or {}
    # У файла есть имя, у заметки — только её же текст. Пересказывать человеку
    # целиком то, что он сам минуту назад написал, незачем: хватит начала,
    # чтобы он узнал свою мысль в списке.
    name = (item.get("filename") or "").strip()
    if not name:
        name = (item.get("title") or "заметка").strip()
        if len(name) > 48:
            name = name[:47].rstrip() + "…"
    res = database.get_inbox_result(business_id, item_id)
    if not res:
        return f"• {name}: принял, разбираю."

    kind = understanding.TYPE_RU.get(res.get("type"), "Не разобрал")
    data = res.get("extracted_data") or {}
    applied = res.get("applied") or []
    bits = [f"• {name}: {kind.lower()}"]
    if data.get("items_count"):
        n = int(data["items_count"])
        bits.append("нашёл %d %s" % (n, _plural(n, "позицию", "позиции", "позиций")))
    elif data.get("amount"):
        bits.append("сумма %s" % "{:,}".format(int(data["amount"])).replace(",", " ") + " ₽")

    line = ", ".join(bits)
    notes = [n for n in (res.get("notes") or []) if str(n).strip()]
    if notes:
        # Оговорка важнее итога: она объясняет, почему итог такой.
        return line + ".\n  " + " ".join(str(n) for n in notes)
    if applied:
        # Что именно сделано, словами самого действия: «Товары: добавлено 2,
        # обновлено 4». Пересказывать это своими словами значит завести второе
        # описание одного события.
        line += ".\n  " + "; ".join(a.get("detail") or "" for a in applied if a.get("detail"))
    elif res.get("type") == "UNKNOWN":
        line += ".\n  Не понял, что это. Загляните во «Входящие» и подскажите."
    else:
        line += ".\n  " + (understanding.NEEDS_RU.get(res.get("level"), "нужно уточнение")
                           .capitalize() + " — откройте «Входящие».")
    return line


def report(business_id: int, got: dict) -> str:
    """
    Человеческий ответ на отправку: что принято, что уже было, что не вышло.

    Ни одного слова о внутреннем устройстве: отправитель не должен знать ни
    про отпечатки, ни про уровни уверенности — он должен понять, изменились
    ли его данные и нужно ли ему что-то сделать.
    """
    lines = []
    accepted = got.get("accepted") or []
    if accepted:
        lines.append("Готово. Принял %d %s:" % (
            len(accepted), _plural(len(accepted), "материал", "материала", "материалов")))
        lines += [describe(business_id, i) for i in accepted]

    for d in (got.get("duplicates") or []):
        lines.append("• %s: это вы уже присылали — ничего не менял." % d.get("what"))

    for r in (got.get("rejected") or []):
        lines.append("• %s: не принял. %s" % (r.get("what"), r.get("error")))

    if not lines:
        return "Ничего не принял — нечего было обрабатывать."
    return "\n".join(lines)
