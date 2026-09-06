# -*- coding: utf-8 -*-
"""
VELOR пишет владельцу первым.

До сих пор продукт молчал, пока его не спросят. Разбор считался, риски
находились, брифинг собирался — и всё это лежало за дверью, в которую надо
войти самому. Владелец маленького бизнеса заходит в кабинет не каждый день, а
зависшая заявка стареет каждый час.

Канал уже есть и новый заводить не нужно: личный Telegram владельца
подтверждён при запуске триала и лежит в `owner_identity`, а бот компании
подключён её же руками. Пишем тем же ботом в тот же личный чат — то есть туда,
куда владелец и так смотрит.

Три правила, от которых здесь ничего не должно отступать.

ПЕРВОЕ: молчать, если сказать нечего. Ежедневное «всё спокойно» — самый
короткий путь к тому, чтобы сообщения перестали читать. Письмо уходит, только
если в нём есть хотя бы одна строка, требующая внимания, или заметное движение
денег.

ВТОРОЕ: не повторяться. Одно письмо в сутки, и то же самое не повторяется на
следующий день: у каждой строки есть отпечаток, и вчерашние строки в сегодняшнее
письмо не попадают. Иначе «три заявки висят» будет приходить неделю подряд.

ТРЕТЬЕ: выключается в одно движение, и об этом сказано в первом же письме.
Рассылка, из которой не выйти, — это спам, чем бы она ни была по смыслу.
"""
from __future__ import annotations

import datetime
import json
import logging

import database

log = logging.getLogger("velor.outreach")

# По умолчанию ВЫКЛЮЧЕНО. Включать рассылку чужим владельцам без их ведома
# нельзя: это сообщения живым людям, и решение принимает не код.
PUSH_OFF = "off"
DEFAULT_HOUR = 9
MIN_HOUR, MAX_HOUR = 6, 22


def setting(business: dict) -> tuple[bool, int]:
    """(включено ли, в котором часу). Пусто — значит выключено."""
    raw = ((business or {}).get("morning_push") or "").strip().lower()
    if not raw or raw == PUSH_OFF:
        return False, DEFAULT_HOUR
    if raw == "on":
        return True, DEFAULT_HOUR
    try:
        hour = int(raw)
    except ValueError:
        return False, DEFAULT_HOUR
    return True, max(MIN_HOUR, min(MAX_HOUR, hour))


def owner_chat(business_id: int) -> str:
    """Личный Telegram владельца — тот, что подтверждён, а не любой знакомый."""
    try:
        row = database.owner_identity(business_id) or {}
    except Exception:
        return ""
    return str(row.get("telegram_user_id") or "").strip()


# ── ЧТО ПИСАТЬ ──────────────────────────────────────────────────────────────

def _money(n):
    return "{:,}".format(int(n or 0)).replace(",", " ") + " ₽"


def lines_of(payload: dict) -> list[str]:
    """
    Строки письма. Только то, что посчитано, — и ни одной ради объёма.

    Возвращаем список, а не готовый текст, потому что каждую строку надо
    сверить с вчерашним письмом по отдельности: повторяется обычно одна из
    них, а не всё сразу.
    """
    p = payload or {}
    out = []
    for a in (p.get("attention") or [])[:4]:
        a = " ".join(str(a).split())
        if a:
            out.append("• " + a[0].upper() + a[1:])

    risk = p.get("risk") or {}
    if risk.get("title"):
        out.append("• " + str(risk["title"]).strip())
    opp = p.get("opportunity") or {}
    if opp.get("title"):
        out.append("• " + str(opp["title"]).strip())

    # Деньги — отдельной строкой и только когда вчера что-то происходило.
    if p.get("income_yday") or p.get("expense_yday"):
        out.append("Вчера: доход %s, расход %s." % (_money(p.get("income_yday")),
                                                    _money(p.get("expense_yday"))))
    return out


def _fingerprints(lines: list[str]) -> list[str]:
    """Отпечаток строки без чисел: «3 заявки висят» и «4 заявки висят» — одно
    и то же сообщение, и приходить оно должно не каждый день."""
    import re
    return [re.sub(r"\d+", "#", l).strip().lower() for l in lines]


def compose(business: dict, payload: dict, said_before: list[str]) -> str:
    """Готовое письмо или пустая строка, если писать не о чем."""
    lines = lines_of(payload)
    seen = set(said_before or [])
    fresh = [l for l, f in zip(lines, _fingerprints(lines)) if f not in seen]
    # Строка про деньги сама по себе поводом для письма не является: она
    # приходила бы каждый день и вытеснила бы всё остальное.
    worth = [l for l in fresh if l.startswith("•")]
    if not worth:
        return ""

    name = (business or {}).get("name") or "ваш бизнес"
    head = (payload or {}).get("today") or ""
    parts = ["VELOR · %s, коротко:" % name]
    if head:
        parts.append(" ".join(str(head).split())[:200])
    parts.append("")
    parts += fresh
    parts.append("")
    parts.append("Подробнее — в кабинете. Чтобы не писать по утрам: "
                 "«Ещё → Настройки → Утреннее письмо».")
    return "\n".join(parts)


# ── ОТПРАВКА ────────────────────────────────────────────────────────────────

MARK = "morning-push"


def _said_before(business_id: int) -> list[str]:
    """Отпечатки строк вчерашнего письма — чтобы не повторяться."""
    try:
        row = database.get_push_state(business_id) or {}
        return json.loads(row.get("lines") or "[]")
    except Exception:
        return []


def send_morning(business_id: int, *, now=None, force=False):
    """
    Отправить утреннее письмо. Возвращает (ушло, почему нет).

    Ничего не делает молча: у каждого отказа есть причина, и она возвращается
    наружу — иначе «почему мне не пришло» становится неотвечаемым вопросом.
    """
    business = database.get_business(business_id) or {}
    on, hour = setting(business)
    if not on and not force:
        return False, "выключено владельцем"

    now = now or datetime.datetime.now()
    today = now.date().isoformat()
    if not force and now.hour < hour:
        return False, "ещё рано"

    state = database.get_push_state(business_id) or {}
    if not force and state.get("sent_on") == today:
        return False, "сегодня уже писали"

    chat = owner_chat(business_id)
    if not chat:
        return False, "личный Telegram владельца не подтверждён"

    payload = database.get_briefing(business_id, today) or {}
    try:
        payload = json.loads(payload.get("payload") or "{}")
    except (ValueError, AttributeError):
        payload = {}
    if not payload:
        return False, "брифинг за сегодня ещё не собран"

    text = compose(business, payload, _said_before(business_id))
    if not text:
        # Не ошибка, а решение: писать «всё спокойно» каждый день значит
        # приучить владельца не открывать эти сообщения.
        database.save_push_state(business_id, sent_on=None,
                                 lines=json.dumps(_fingerprints(lines_of(payload)),
                                                  ensure_ascii=False))
        return False, "нечего сообщить"

    import botcore
    okay, err = botcore.send_text(business_id, chat, text)
    if not okay:
        return False, err or "Telegram не принял сообщение"

    database.save_push_state(business_id, sent_on=today,
                             lines=json.dumps(_fingerprints(lines_of(payload)),
                                              ensure_ascii=False))
    database.log_event(business_id, "briefing", "Утреннее письмо отправлено",
                       text.split("\n")[0][:120], level="info")
    return True, ""


def round_all(now=None):
    """Обход всех компаний. Одна упавшая не должна ронять остальные."""
    sent = 0
    for b in database.list_businesses_with_stats():
        try:
            okay, _ = send_morning(b["id"], now=now)
            sent += 1 if okay else 0
        except Exception:
            log.exception("Утреннее письмо не ушло (biz %s)", b.get("id"))
    return sent
