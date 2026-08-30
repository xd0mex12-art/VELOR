# -*- coding: utf-8 -*-
"""
FOLLOW-UP — удержание возможности, а не напоминание о себе.

Разница между этими двумя вещами и есть весь модуль. «Напомнить о себе» —
это когда бизнес пишет, потому что давно не писал. «Удержать возможность» —
это когда есть конкретное незакрытое обязательство: человек спросил и не
получил ответа, мы назвали условия и не услышали решения, человек назвал
дату, и она подходит. Первое клиент считает спамом справедливо. Второе он
считает работой — тоже справедливо.

Поэтому здесь нет ни одной причины написать «просто так». Касание существует
только если существует событие, которое можно назвать вслух, и оно записано
вместе со ссылкой на сообщение, из которого взято.

Четыре границы, за которые модуль не заходит
────────────────────────────────────────────
1. ЧЕРНОВИК ≠ ОТПРАВЛЕНО. Состояний восемь, и «запланировано» среди них не
   значит «ушло». Отправленным считается только то, что подтвердил канал.

2. ПО УМОЛЧАНИЮ VELOR ГОТОВИТ, А НЕ ОТПРАВЛЯЕТ. Право писать клиенту первым
   не входит ни в один уровень автономии — как скидки и возвраты, оно
   включается владельцем поимённо. Пока оно выключено, всё, что делает
   VELOR, — кладёт владельцу готовый текст и объяснение.

3. НОВОЕ СЛОВО КЛИЕНТА ОТМЕНЯЕТ СТАРОЕ РАСПИСАНИЕ. Человек ответил — значит
   запланированное касание больше не нужно, и отправить его позже было бы
   разговором с самим собой.

4. ТРИ КАСАНИЯ И СТОП. Не «пока не купит»: после третьего молчания продолжать
   некуда, и четвёртое сообщение говорит уже не о сделке, а о нас.
"""
from __future__ import annotations

import datetime
import logging
import re

import database
import qualify

log = logging.getLogger("velor.followup")


# ── зачем пишем ────────────────────────────────────────────────────────────
# Причина — не украшение карточки. Это то, что владелец прочитает, прежде чем
# нажать «Отправить», и то, чем сообщение оправдано перед клиентом. Причин
# ровно столько, сколько событий мы умеем показать пальцем в переписке.

BUSINESS_GAP = "business_gap"                  # клиент спросил — мы молчим
CUSTOMER_NO_RESPONSE = "customer_no_response"  # мы ответили — молчит клиент
QUOTE_PENDING = "quote_pending"                # мы назвали условия — решения нет
RETURN_OPPORTUNITY = "return_opportunity"      # разговор оборвался до покупки
TIMING = "timing"                              # подходит дата, которую назвал клиент

REASONS = (BUSINESS_GAP, CUSTOMER_NO_RESPONSE, QUOTE_PENDING,
           RETURN_OPPORTUNITY, TIMING)

REASON_RU = {
    BUSINESS_GAP: "Клиент ждёт ответа",
    CUSTOMER_NO_RESPONSE: "Клиент не ответил",
    QUOTE_PENDING: "Клиент не ответил на предложение",
    RETURN_OPPORTUNITY: "Разговор оборвался до покупки",
    TIMING: "Подходит дата, которую назвал клиент",
}

# ── почему остановились ────────────────────────────────────────────────────
STOP_RU = {
    "won": "клиент купил",
    "lost": "возможность закрыта как несостоявшаяся",
    "refused": "клиент сказал, что не нужно",
    "do_not_contact": "клиент попросил больше не писать",
    "new_conversation": "клиент снова написал сам",
    "owner_cancelled": "вы отменили касание",
    "offer_unavailable": "то, чем интересовались, снято с продажи",
    "limit_reached": "три касания уже было — больше не пишем",
    "business_limit": "дневной предел автоматических касаний исчерпан",
    "no_channel": "писать некуда: канал этого разговора недоступен",
    "closed_window": "канал больше не пропустит сообщение",
    "no_policy": "VELOR не разрешено писать клиентам первым",
    "read_only": "пробный период завершён",
    "no_reason": "повода написать нет",
    "replied": "клиент ответил",
}

# ── чем кончилось ──────────────────────────────────────────────────────────
REPLIED, CONVERTED, REJECTED, IGNORED, FAILED_OUT = (
    "replied", "converted", "rejected", "ignored", "failed")

OUTCOME_RU = {
    REPLIED: "клиент ответил",
    CONVERTED: "появилась заявка",
    REJECTED: "клиент отказался",
    IGNORED: "ответа не было",
    FAILED_OUT: "сообщение не ушло",
}

# ── время ──────────────────────────────────────────────────────────────────
# Стартовые правила первой версии, а не вечная истина. Ставим их здесь одним
# списком, чтобы менять их можно было в одном месте и осознанно.
#
# Отсчёт идёт от последнего касания, а не от создания возможности: иначе три
# сообщения могли бы уйти в один день просто потому, что разговор старый.
DELAYS_H = (24, 72, 168)      # первое — через сутки, второе — трое, третье — неделя
MAX_ATTEMPTS = 3
MIN_GAP_H = 20                # минимум между двумя касаниями по одной возможности

# Сколько часов молчания бизнеса делают ответ клиенту срочным. Совпадает с
# порогом оценки не случайно: два разных числа для одного события означали бы,
# что система спорит сама с собой.
GAP_HOURS = qualify.GAP_HIGH_HOURS

# Разговор старше этого — уже не «мы не ответили», а «всё давно остыло».
STALE_DAYS = qualify.FRESH_DAYS

# За сколько дней до названной клиентом даты уместно напомнить.
TIMING_LEAD_DAYS = 2

# Общий предел на бизнес: защита не от одного назойливого касания, а от того,
# что однажды сойдётся сразу всё и уйдёт сотня сообщений за час.
BUSINESS_DAILY_CAP = 20

# Сколько ждать ответа на отправленное касание, прежде чем записать «не ответил».
IGNORED_AFTER_DAYS = 7

# Сорвавшуюся отправку повторяем, но считанное число раз: бесконечные попытки
# в чужой недоступный API — это не надёжность, а долбёжка.
MAX_TRIES = 3
# Сколько предложение о касании ждёт владельца. Дольше недели — уже не то же
# самое предложение: разрыв в разговоре, из-за которого оно появилось, к тому
# времени вырос настолько, что повод надо считать заново.
FOLLOWUP_TTL_DAYS = 7

# Часы работы бизнеса считаем по его же исходящим. Меньше этого — не считаем
# вовсе: по трём сообщениям рабочее окно не восстановить.
QUIET_MIN_HISTORY = 10


class FollowupError(Exception):
    pass


# ── мелочи времени ─────────────────────────────────────────────────────────

def _now():
    return datetime.datetime.utcnow()


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(when):
    return qualify._parse(when)


def _hours_since(when):
    return qualify._hours_since(when)


def _days_since(when):
    return qualify._days_since(when)


# ── канал ──────────────────────────────────────────────────────────────────

def channel_of(lead):
    """
    Каким каналом шёл этот разговор.

    Канал возможности не выбирается заново: где человек написал, там ему и
    отвечают. Искать его в другом месте — отдельное решение, которого на этом
    этапе никто не принимал.
    """
    for key in ("channel", "source"):
        value = (lead.get(key) or "").strip().lower()
        if value in ("telegram", "instagram"):
            return value
    return ""


def can_reach(business_id, lead, *, auto=True):
    """
    Можно ли вообще доставить сообщение по этому каналу. (можно, почему нет).

    auto=False — отправляет человек из кабинета: у Instagram у него окно шире,
    и это не поблажка, а разные правила Meta для автоответа и для сотрудника.
    """
    channel = channel_of(lead)
    if not channel:
        return False, "no_channel"
    client = database.get_client(lead.get("client_id"), business_id) if lead.get("client_id") else None
    if not client:
        return False, "no_channel"

    if channel == "telegram":
        business = database.get_business(business_id) or {}
        if not (business.get("tg_bot_token") or "").strip():
            return False, "no_channel"
        if not (str(client.get("tg_user_id") or "").strip()):
            return False, "no_channel"
        return True, ""

    # Instagram: писать первым нельзя, и это правило площадки, а не наше.
    try:
        import instagram
        thread = database.ig_thread_of_client(business_id, client["id"]) \
            if hasattr(database, "ig_thread_of_client") else None
        igsid = (thread or {}).get("igsid") or (client.get("external_id") or "")
        if not igsid:
            return False, "no_channel"
        win = instagram.window_state(business_id, igsid)
        if not win.get("can_reply"):
            return False, "closed_window"
        # Автоматике доступно только окно 24 часов: тег HUMAN_AGENT ставится
        # на сообщение, написанное человеком, и подписывать им автоответ —
        # нарушение правил Meta, а не хитрость.
        if auto and win.get("needs_human_tag"):
            return False, "closed_window"
        return True, ""
    except Exception:
        log.exception("Канал Instagram не проверился (biz %s)", business_id)
        return False, "no_channel"


# ── права ──────────────────────────────────────────────────────────────────

def may_autosend(business_id, channel):
    """
    Разрешено ли VELOR отправлять касание самому.

    Вопрос тот же, что был, но задаётся он теперь в одном месте на весь
    продукт. Внутри — по-прежнему право «Писать клиенту первым», которое ни
    один уровень автономии не выдаёт; сверху добавилось положение тумблера,
    которое владелец выставил на странице автономности.

    Не прочиталось — считаем, что нельзя. Неотправленное сообщение владелец
    увидит в кабинете, отправленное по ошибке — уже нет.
    """
    try:
        import actions
        return actions.allowed_auto(business_id, "send_followup",
                                    channel=channel or "telegram")
    except Exception:
        log.exception("Полномочия не прочитались (biz %s)", business_id)
        return False


# ── след в общем журнале ───────────────────────────────────────────────────
# У касаний своя очередь, свои состояния и свой экран, и переносить их в общий
# исполнитель значило бы написать эту машинерию второй раз. Общему журналу
# нужно другое: чтобы владелец видел предстоящую отправку в одном списке с
# остальным, что ждёт его решения, и чтобы отправленное осталось в истории
# наравне с остальным, что VELOR делал.

ACTION_KEY = "send_followup:%s"


def _propose_action(business_id, row):
    """Показать готовящееся касание в общей очереди решений."""
    if not row:
        return None
    state = {database.FU_DRAFT: database.AC_PROPOSED,
             database.FU_APPROVED: database.AC_APPROVED,
             database.FU_SCHEDULED: database.AC_APPROVED,
             database.FU_BLOCKED: database.AC_BLOCKED}.get(row["status"])
    if not state:
        return None
    try:
        import actions
        key = ACTION_KEY % row["id"]
        if database.find_live_action(business_id, key):
            return None
        lead = database.get_lead(row["lead_id"], business_id) or {}
        return database.add_action(
            business_id, "send_followup",
            mode=(actions.AUTOMATIC if state == database.AC_APPROVED
                  else actions.APPROVED_BY_OWNER),
            status=state, channel=row.get("channel"),
            target_type="followup", target_id=row["id"],
            reason=_action_reason(row, lead),
            based_on=dict(row.get("based_on") or {}, lead_id=row["lead_id"],
                          client_id=row.get("client_id")),
            payload={"followup_id": row["id"], "message": row.get("message") or ""},
            error=row.get("stop_reason") or None, dedupe_key=key,
            # «Когда уместно написать» и «до каких пор предложение в силе» —
            # разные даты, и путать их дорого: recommended_at часто уже в
            # прошлом («ответить надо было час назад»), и предложение погасло
            # бы, не успев попасться владельцу на глаза.
            expires_at=_stamp((_parse(row.get("recommended_at")) or _now())
                              + datetime.timedelta(days=FOLLOWUP_TTL_DAYS)))
    except Exception:
        log.exception("Касание не попало в журнал действий (biz %s)", business_id)
        return None


def _action_reason(row, lead):
    """Человеческое «почему» — то же, что видит владелец в карточке."""
    who = (lead.get("title") or "").strip()
    why = REASON_RU.get(row.get("reason"), row.get("reason") or "")
    trig = (row.get("trigger") or "").strip()
    said = "Написать клиенту: " + (why or "повод из переписки")
    if trig:
        said += " — " + trig
    if who:
        said += " (%s)" % who[:80]
    return said


def _mark_action(business_id, followup_id, status, *, result=None, error=None):
    """Чем кончилось касание — тем же кончилась и запись в журнале."""
    try:
        import actions
        row = database.find_live_action(business_id, ACTION_KEY % followup_id)
        if not row:
            return
        if status == database.AC_CANCELLED:
            actions.cancel(business_id, row["id"], reason=result or "Повод исчез.")
            return
        if row["status"] != database.AC_RUNNING:
            database.claim_action(row["id"], business_id)
        database.finish_action(row["id"], business_id, status,
                               result=result, error=error)
    except Exception:
        log.exception("Судьба касания не записалась в журнал (biz %s)", business_id)


# ── тихие часы ─────────────────────────────────────────────────────────────

def work_hours(business_id):
    """
    В какие часы этому бизнесу нормально писать клиентам.

    Часового пояса в системе нет, и придумывать его за владельца нельзя: чужой
    ночью ошибиться дороже, чем подождать до утра. Зато есть факт — время, в
    которое бизнес сам отвечает своим клиентам. По нему и считаем.

    Мало истории — говорим «не знаем», и автоматическая отправка не идёт вовсе.
    """
    hours = database.outbound_hours(business_id)
    if len(hours) < QUIET_MIN_HISTORY:
        return {"known": False, "from": None, "to": None, "n": len(hours)}
    lo, hi = min(hours), max(hours)
    return {"known": True, "from": lo, "to": hi, "n": len(hours)}


def in_work_hours(business_id, when=None):
    """Сейчас подходящее время? (можно, окно). Не знаем часов — значит нет."""
    win = work_hours(business_id)
    if not win["known"]:
        return False, win
    hour = (when or _now()).hour
    return win["from"] <= hour <= win["to"], win


def next_work_time(business_id, when=None):
    """Ближайший подходящий момент. Не знаем рабочих часов — не переносим."""
    when = when or _now()
    win = work_hours(business_id)
    if not win["known"]:
        return when
    if win["from"] <= when.hour <= win["to"]:
        return when
    target = when.replace(minute=0, second=0, microsecond=0)
    if when.hour < win["from"]:
        return target.replace(hour=win["from"])
    return (target + datetime.timedelta(days=1)).replace(hour=win["from"])


# ── стоп-условия ───────────────────────────────────────────────────────────

def _negative(signals, key=None):
    for s in signals or []:
        if s.get("kind") != "negative":
            continue
        if key is None or s.get("key") == key:
            return s
    return None


def stop_reason(business_id, lead, *, gap=None):
    """
    Почему писать нельзя. None — можно.

    Порядок важен: сначала то, что решил человек (купил, отказался, попросил
    не писать), потом то, что решили мы. Человек всегда раньше.

    gap не передали — спросим сами. Один и тот же вопрос не должен получать
    разные ответы в зависимости от того, что было под рукой у вызывающего.
    """
    if gap is None:
        gap = database.last_exchange(business_id, lead.get("client_id"))
    status = (lead.get("status") or "").strip()
    if status == "won":
        return "won"
    if status == "lost":
        return "lost"
    if status not in database.LEAD_OPEN:
        return "lost"

    signals = lead.get("signals") or []
    if _negative(signals, "do_not_contact"):
        return "do_not_contact"
    if _negative(signals, "refuse") or _negative(signals, "competitor"):
        return "refused"

    # Позиция снята с продажи — писать про неё нельзя: предложение больше не
    # наше. Спрашиваем ту же память, что и оценка, а не заводим вторую.
    try:
        hay = " ".join(x for x in (lead.get("title"), lead.get("interest")) if x)
        fit, why, _found, _risks = qualify.fit_of(business_id, hay)
        if fit == qualify.LOW and any("снято с продажи" in w for w in why):
            return "offer_unavailable"
    except Exception:
        log.exception("Соответствие каталогу не проверилось (biz %s)", business_id)

    # «Не писать» сказано про эту возможность, а не про один текст. Предложить
    # завтра то же самое другими словами — значит не услышать владельца; из
    # трёх таких «предложений» и складывается ощущение, что система спорит.
    # Запрет снимается тем единственным, что может его снять: человек написал
    # сам, и разговор пошёл дальше.
    said_id = (gap or {}).get("last_in_id") or 0
    for row in database.list_followups(business_id, lead_id=lead["id"],
                                       status=database.FU_CANCELLED, limit=20):
        if row.get("stop_reason") != "owner_cancelled":
            continue
        mark = (row.get("based_on") or {}).get("cancelled_after")
        if mark and said_id and int(said_id) > int(mark):
            continue
        return "owner_cancelled"

    if database.followup_attempts(business_id, lead["id"]) >= MAX_ATTEMPTS:
        return "limit_reached"
    return None


# ── решение: нужно ли касание и какое ──────────────────────────────────────

def _last_touch(business_id, lead, gap):
    """
    Когда по этой возможности в последний раз что-то происходило.

    Берём позднее из двух: последнее сообщение в переписке и последнее
    отправленное касание. Иначе отправленный вчера follow-up не мешал бы
    отправить сегодня следующий.

    Правка карточки владельцем сюда НЕ входит, хотя она и обновляет активность
    возможности. Он смотрел на неё, а не разговаривал с человеком; считать это
    касанием значило бы откладывать письмо каждый раз, когда владелец открыл
    карточку.
    """
    marks = [gap.get("last_in_at"), gap.get("last_out_at")]
    for row in database.list_followups(business_id, lead_id=lead["id"],
                                       status=database.FU_SENT, limit=10):
        marks.append(row.get("sent_at"))
    stamps = [_parse(m) for m in marks]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def _quoted(lead, gap):
    """Слова клиента, к которым мы возвращаемся. Только его собственные."""
    for source in (lead.get("interest"), lead.get("title")):
        text = (source or "").strip()
        if text:
            return text[:140]
    return ""


def decide(business_id, lead, *, gap=None, now=None):
    """
    Нужно ли сейчас касание по этой возможности и какое именно.

    Возвращает решение либо None. Решение всегда содержит причину, событие,
    на которое она опирается, и провенанс — id сообщений и даты, по которым
    его можно проверить руками.
    """
    now = now or _now()
    if gap is None:
        gap = database.last_exchange(business_id, lead.get("client_id"))
    # Возможность, заведённая рукой владельца, не выросла из переписки — и
    # молчание в чужом разговоре к ней не относится. Правило то же, что в
    # оценке: одно событие не должно означать разного в двух модулях.
    if not lead.get("first_message_id"):
        gap = {}
    stop = stop_reason(business_id, lead, gap=gap)
    if stop:
        return {"stop": stop}

    attempts = database.followup_attempts(business_id, lead["id"])
    attempt = attempts + 1
    last = _last_touch(business_id, lead, gap)
    since_h = (now - last).total_seconds() / 3600 if last else None
    if since_h is not None and attempts and since_h < MIN_GAP_H:
        return None                       # только что писали — рано

    base = {
        "lead_id": lead["id"],
        "lead_status": lead.get("status"),
        "last_in_at": gap.get("last_in_at"),
        "last_out_at": gap.get("last_out_at"),
        "last_in_id": gap.get("last_in_id"),
        "first_message_id": lead.get("first_message_id"),
        "last_message_id": lead.get("last_message_id"),
    }
    signals = lead.get("signals") or []
    if signals:
        base["signals"] = [
            {k: s.get(k) for k in ("key", "title", "quote", "message_id", "at")}
            for s in signals[-4:]]

    stale = (since_h or 0) / 24 >= STALE_DAYS

    # 1. Клиент спросил — бизнес молчит. Это не follow-up в обычном смысле:
    #    здесь мы не напоминаем о себе, а закрываем свой же долг.
    if gap.get("unanswered") and not stale:
        waiting = _hours_since(gap.get("last_in_at")) or 0
        if waiting >= GAP_HOURS:
            return {"reason": BUSINESS_GAP, "attempt": attempt,
                    "recommended_at": _stamp(now),
                    "trigger": "клиент написал %s назад, ответа не было"
                               % qualify._hours_ru(waiting),
                    "based_on": dict(base, waiting_hours=round(waiting, 1))}
        return None

    # 2. Наступает дата, которую назвал сам клиент. Своей даты не выдумываем:
    #    её либо сказали вслух, либо её нет.
    wanted = _parse(lead.get("wanted_at"))
    if wanted:
        days_left = (wanted.date() - now.date()).days
        if 0 <= days_left <= TIMING_LEAD_DAYS:
            return {"reason": TIMING, "attempt": attempt,
                    "recommended_at": _stamp(now),
                    "trigger": "клиент называл дату %s — до неё %s"
                               % (lead["wanted_at"],
                                  "остался день" if days_left == 1 else
                                  "сегодня" if days_left == 0 else
                                  "%s дня" % days_left),
                    "based_on": dict(base, wanted_at=lead.get("wanted_at"))}
        if days_left < 0:
            # Дата прошла, а сделки нет. Напоминать про вчера бессмысленно —
            # это уже разговор о новой задаче, и заводить его должен человек.
            return None

    if since_h is None:
        return None

    # 3. Разговор оборвался давно. Тут уже не «вы не ответили», а «мы к вам
    #    возвращаемся», и звучать это должно иначе.
    if stale:
        if attempts >= 1:
            return None                   # напоминали — второй раз не догоняем
        return {"reason": RETURN_OPPORTUNITY, "attempt": attempt,
                "recommended_at": _stamp(now),
                "trigger": "разговор остановился %s дней назад"
                           % int((since_h or 0) / 24),
                "based_on": dict(base, silent_days=int((since_h or 0) / 24))}

    # 4. Мы ответили — молчит клиент. Ждём столько, сколько положено этому
    #    касанию по счёту.
    need_h = DELAYS_H[min(attempt, len(DELAYS_H)) - 1]
    if since_h < need_h:
        return None

    reason = QUOTE_PENDING if _we_quoted(business_id, lead, gap) else CUSTOMER_NO_RESPONSE
    return {"reason": reason, "attempt": attempt,
            "recommended_at": _stamp(now),
            "trigger": "мы написали последними %s назад, ответа нет"
                       % qualify._hours_ru(since_h),
            "based_on": dict(base, silent_hours=round(since_h, 1))}


_PRICE_IN_REPLY = re.compile(r"\d[\d\s]*\s*(?:₽|руб\w*|р\.)", re.I)


def _we_quoted(business_id, lead, gap):
    """
    Мы называли условия и ждём решения?

    Смотрим последнее наше сообщение этому человеку. Сумма в нём означает, что
    мяч на стороне клиента, и это другая ситуация, чем просто молчание.

    Спрашиваем именно последнее исходящее, а не переписку лида: границы лида
    кончаются на последней реплике КЛИЕНТА, и наш ответ — тот самый, в котором
    названа цена, — в них не попадает никогда.
    """
    if lead.get("order_id"):
        return True
    try:
        row = database.last_outbound(business_id, lead.get("client_id"))
    except Exception:
        return False
    return bool(row and _PRICE_IN_REPLY.search(row.get("content") or ""))


# ── черновик ───────────────────────────────────────────────────────────────

def _name(business_id, lead):
    who = (lead.get("client_name") or "").strip()
    if not who and lead.get("client_id"):
        client = database.get_client(lead["client_id"], business_id) or {}
        who = (client.get("name") or "").strip()
    # @-логин из директа именем не считается: обращение «Здравствуйте,
    # ivan_2007» звучит хуже, чем просто «Здравствуйте».
    if not who or who.startswith("@") or "_" in who or who.isdigit():
        return ""
    return who.split()[0]


def _hello(business_id, lead):
    who = _name(business_id, lead)
    return "Здравствуйте, %s!" % who if who else "Здравствуйте!"


def template(business_id, lead, decision):
    """
    Черновик из фактов и только из них.

    Ни одной цифры, которой мы не знаем, ни одного обещания, которого никто не
    давал. Конкретность берётся не из выдумки, а из слов самого человека: о чём
    он спрашивал и какую дату называл. Этого достаточно, чтобы сообщение не
    было тем самым «напоминаем о себе».
    """
    hello = _hello(business_id, lead)
    about = _quoted(lead, {})
    # Слова клиента ставим в кавычки и отдельным предложением: так видно, что
    # это цитата, а не наш пересказ его задачи. Пересказывать чужую просьбу
    # своими словами — первый шаг к тому, чтобы что-нибудь в неё добавить.
    said = (" Вы писали: «%s»." % about.rstrip(" .")) if about else ""
    reason = decision.get("reason")

    if reason == BUSINESS_GAP:
        return ("%s Извините за задержку — возвращаюсь к вашему вопросу.%s "
                "Отвечу сейчас." % (hello, said))
    if reason == TIMING:
        return ("%s Вы называли дату %s.%s Подскажите, актуально — оформляем?"
                % (hello, lead.get("wanted_at") or "которую ждёте", said))
    if reason == QUOTE_PENDING:
        return ("%s Мы отправляли вам условия и ждём вашего решения.%s "
                "Подскажите, подходит — или что-то нужно поменять?" % (hello, said))
    if reason == RETURN_OPPORTUNITY:
        return ("%s Возвращаюсь к нашему разговору.%s Если задача ещё "
                "актуальна — напишите, подскажу, что можем." % (hello, said))
    return ("%s Возвращаюсь к вашему вопросу.%s Подскажите, ещё актуально?"
            % (hello, said))


_FORBIDDEN_CLAIMS = re.compile(
    r"я\s+лично|я\s+сам\w*\s+провер\w+|живой\s+человек|я\s+не\s+бот", re.I)


def _ai_draft(business_id, lead, decision):
    """
    Черновик от модели — если она есть и если сказанное ею подтверждается.

    Проверка та же, что у продавца: каждая сумма, срок и утверждение о наличии
    должны находиться в памяти бизнеса или в словах самого клиента. Не нашлось —
    берём шаблон. Красивое сообщение с выдуманной цифрой хуже сухого.
    """
    try:
        import ai
        import sales
        if not ai.ai_available():
            return None, ["модель не подключена"]
        business = database.get_business(business_id) or {}
        pol = sales.policy(business_id, channel_of(lead) or "telegram")
        allowed = set(pol["allowed"])
        mem = sales.memory(business_id, _quoted(lead, {}), allowed)
        rows = database.lead_messages(business_id, lead["id"], limit=20)
        said = " ".join(r.get("content") or "" for r in rows
                        if (r.get("role") or "") == "user")
        history = [{"role": ("user" if (r.get("role") or "") == "user" else "assistant"),
                    "content": r.get("content") or ""} for r in rows][-10:]

        system = (
            "Ты пишешь ОДНО короткое сообщение клиенту от лица компании «%s». "
            % (business.get("name") or "компания") +
            "Это возвращение к незакрытому разговору, а не рассылка.\n"
            "ПОВОД (он настоящий, придумывать другой нельзя): %s.\n"
            % (decision.get("trigger") or REASON_RU.get(decision.get("reason"), "")) +
            "ЖЕЛЕЗНЫЕ ПРАВИЛА:\n"
            "1. Цены, сроки, наличие и скидки бери ТОЛЬКО из памяти бизнеса "
            "ниже и цитируй дословно. Нет цифры в памяти — не называй её.\n"
            "2. Никакой выдуманной срочности: «последний шанс», «осталось "
            "мало», «только сегодня» — запрещены, если этого нет в памяти.\n"
            "3. Не дави и не спрашивай «вы ещё заинтересованы?» — напомни, о "
            "чём был разговор, и задай один конкретный вопрос.\n"
            "4. Не приписывай себе действий, которых не было, и не утверждай, "
            "что ты человек.\n"
            "5. Две-три строки, без приветственных штампов и без подписи.\n")
        if mem["empty"]:
            system += ("\nПАМЯТЬ БИЗНЕСА ПУСТА: ни услуг, ни цен ты не знаешь. "
                       "Ничего не перечисляй — просто вернись к вопросу клиента.")
        else:
            system += "\nПАМЯТЬ БИЗНЕСА:\n" + mem["text"][:3000]

        raw = ai._ask(system, history or [{"role": "user", "content": said or "—"}])
        text = re.sub(r"\s+\n", "\n", (raw or "").strip())
        if not text:
            return None, ["модель не сформулировала сообщение"]
        problems = sales.unproven(text, mem["grounding"] + "\n" + said, allowed)
        if _FORBIDDEN_CLAIMS.search(text):
            problems.append("сообщение выдаёт автоматику за человека")
        if problems:
            return None, problems
        return text[:900], []
    except Exception:
        log.exception("Черновик касания не написался (biz %s)", business_id)
        return None, ["модель не ответила"]


def draft(business_id, lead, decision):
    """Текст касания и то, чем он подтверждён. Никогда не бросает."""
    text, problems = _ai_draft(business_id, lead, decision)
    if text:
        return text, {"draft_by": "ai"}
    return template(business_id, lead, decision), {
        "draft_by": "template",
        # Почему не модель — владельцу это важнее, чем нам: по этим строкам
        # видно, какой цены или какого условия не хватает в памяти бизнеса.
        "model_rejected": problems[:4],
    }


# ── планирование ───────────────────────────────────────────────────────────

def plan(business_id, lead, *, gap=None, now=None):
    """
    Посмотреть на одну возможность и, если есть повод, подготовить касание.

    Возвращает запись касания, либо None (повода нет), либо запись со
    статусом «нельзя» — с причиной, по которой писать не будем.
    """
    now = now or _now()
    live = database.list_followups(business_id, lead_id=lead["id"], live=True, limit=5)
    decision = decide(business_id, lead, gap=gap, now=now)

    # Готовить черновики — тоже полномочие, пусть и самое безобидное: владелец
    # вправе сказать «не занимайся этим вовсе», и тогда очередь не должна
    # наполняться предложениями, которых он не просил.
    if decision and not decision.get("stop"):
        try:
            import actions
            if not actions.allowed_auto(business_id, "prepare_followup",
                                        channel=channel_of(lead)):
                return None
        except Exception:
            log.exception("Полномочия на подготовку не прочитались (biz %s)",
                          business_id)
            return None

    if decision and decision.get("stop"):
        # Повод исчез — то, что уже лежало в очереди, отменяем и говорим почему.
        for row in live:
            _close(business_id, row, database.FU_CANCELLED, decision["stop"])
        return None
    if live:
        return live[-1]                    # уже готово — второе не заводим
    if not decision:
        return None

    text, extra = draft(business_id, lead, decision)
    based = dict(decision.get("based_on") or {}, **extra)
    channel = channel_of(lead)

    status = database.FU_DRAFT
    stop = None
    recommended = decision["recommended_at"]

    if may_autosend(business_id, channel):
        ok, why = can_reach(business_id, lead, auto=True)
        if not ok:
            status, stop = database.FU_BLOCKED, why
        else:
            status = database.FU_SCHEDULED
            recommended = _stamp(next_work_time(business_id, _parse(recommended) or now))
    else:
        ok, why = can_reach(business_id, lead, auto=False)
        if not ok:
            status, stop = database.FU_BLOCKED, why

    fid = database.add_followup(
        business_id, lead["id"], reason=decision["reason"], channel=channel,
        client_id=lead.get("client_id"), trigger=decision.get("trigger"),
        status=status, attempt=decision["attempt"], recommended_at=recommended,
        message=text, based_on=based, stop_reason=stop)
    database.log_event(business_id, "client",
                       "Готово касание: " + REASON_RU.get(decision["reason"], ""),
                       (lead.get("title") or "")[:180],
                       once_key="followup:%s" % fid)
    made = database.get_followup(fid, business_id)
    _propose_action(business_id, made)
    return made


def scan(business_id, *, limit=200):
    """
    Обойти открытые возможности и подготовить то, что назрело.

    Возвращает сводку — сколько подготовлено, сколько отменено, сколько
    заблокировано. Ничего не отправляет: отправка — отдельный шаг.
    """
    out = {"planned": 0, "blocked": 0, "skipped": 0}
    try:
        rows = database.list_leads(business_id, status="open", limit=limit)
    except Exception:
        log.exception("Возможности не прочитались (biz %s)", business_id)
        return out
    gaps = database.last_exchanges(business_id, [r.get("client_id") for r in rows])
    # Что уже лежало до обхода. Без этого «подготовлено 3» означало бы «нашлось
    # три», и цифра в журнале росла бы каждый час, ничего не сообщая.
    before = {r["id"] for r in database.list_followups(business_id, live=True, limit=500)}
    now = _now()
    for lead in rows:
        try:
            row = plan(business_id, lead,
                       gap=gaps.get(lead.get("client_id")) or {}, now=now)
        except Exception:
            log.exception("Касание не спланировалось (biz %s, lead %s)",
                          business_id, lead.get("id"))
            continue
        if not row or row["id"] in before:
            out["skipped"] += 1
        elif row["status"] == database.FU_BLOCKED:
            out["blocked"] += 1
        else:
            out["planned"] += 1
    return out


# ── проверка перед отправкой ───────────────────────────────────────────────

def may_send(business_id, row, *, auto=True, now=None):
    """
    Можно ли отправить это касание прямо сейчас. (можно, причина отказа).

    Разрешения канала мало. Здесь сходится ВСЁ: живая ли возможность, не
    появилось ли нового слова клиента, не исчерпан ли лимит, подтверждён ли
    текст, доступен ли канал, не ночь ли на дворе. Не сошлось хоть одно —
    не отправляем и пишем, что именно.
    """
    now = now or _now()
    if row.get("status") not in (database.FU_DRAFT, database.FU_APPROVED,
                                 database.FU_SCHEDULED):
        return False, "уже не в работе"
    if not (row.get("message") or "").strip():
        return False, "нет текста"

    business = database.get_business(business_id) or {}
    try:
        import trial
        if trial.access(business)["read_only"]:
            return False, STOP_RU["read_only"]
    except Exception:
        log.exception("Состояние подписки не прочиталось (biz %s)", business_id)
        return False, "состояние подписки неизвестно"

    lead = database.get_lead(row["lead_id"], business_id)
    if not lead:
        return False, "возможности больше нет"
    gap = database.last_exchange(business_id, lead.get("client_id"))
    stop = stop_reason(business_id, lead, gap=gap)
    # Лимит касаний проверяем по уже потраченным, а это касание ещё не
    # потрачено: иначе третье касание блокировало бы само себя.
    if stop == "limit_reached":
        if database.followup_attempts(business_id, lead["id"]) >= MAX_ATTEMPTS:
            return False, STOP_RU["limit_reached"]
    elif stop:
        return False, STOP_RU.get(stop, stop)

    # Клиент написал после того, как мы это придумали, — значит, разговор пошёл
    # дальше, и запланированное сообщение говорило бы о вчерашнем дне.
    planned_at = _parse(row.get("created_at"))
    last_in = _parse(gap.get("last_in_at"))
    if planned_at and last_in and last_in > planned_at:
        return False, STOP_RU["new_conversation"]
    # То же и про владельца: если он ответил сам, наше касание лишнее.
    last_out = _parse(gap.get("last_out_at"))
    if planned_at and last_out and last_out > planned_at:
        return False, "вы уже ответили сами"

    if auto:
        # Право «писать первым» отвечает за сообщения, которых владелец не
        # видел. Это — видел и разрешил именно его, поэтому отправка здесь не
        # самовольная, а поручённая, и спрашивать про автономию не за что.
        approved = row.get("status") == database.FU_APPROVED
        if not approved and not may_autosend(business_id, row.get("channel")):
            return False, STOP_RU["no_policy"]
        sent_today = database.followups_sent_since(
            business_id, _stamp(now - datetime.timedelta(days=1)))
        if sent_today >= BUSINESS_DAILY_CAP:
            return False, STOP_RU["business_limit"]
        fits, win = in_work_hours(business_id, now)
        if not fits:
            # Рабочих часов не знаем: своей волей в такое время не пишем, а
            # порученное отправляем — момент выбрал человек, и придумывать за
            # него «наверное, там ночь» не на чем.
            if win["known"] or not approved:
                return False, ("сейчас нерабочее время" if win["known"] else
                               "не знаю, в какие часы вам можно писать клиентам")

    ok, why = can_reach(business_id, lead, auto=auto)
    if not ok:
        return False, STOP_RU.get(why, why)
    return True, ""


# ── отправка ───────────────────────────────────────────────────────────────

def _deliver(business_id, lead, text, *, auto):
    """
    Отдать сообщение каналу. (ушло, ошибка, id сообщения в переписке).

    Канал сам решает, как доставить; здесь важно одно: пока он не подтвердил,
    сообщение не отправлено. Молчание чужого API — это «не знаем», а «не
    знаем» считается неудачей.
    """
    channel = channel_of(lead)
    client = database.get_client(lead.get("client_id"), business_id) or {}
    if channel == "telegram":
        import botcore
        sent, error = botcore.send_text(business_id, client.get("tg_user_id"), text)
        if not sent:
            return False, error, None
        # Отправленное касание — обычное сообщение бизнеса, и жить оно должно
        # там же, где остальные. Иначе переписка в карточке врёт: клиент видел
        # наш текст, а в истории его нет — и «мы не ответили» остаётся правдой
        # для системы после того, как перестало быть ею.
        mid = database.save_message(business_id, lead["client_id"], "assistant",
                                    text, channel="telegram")
        return True, "", mid
    if channel == "instagram":
        import instagram
        thread = database.ig_thread_of_client(business_id, client["id"])
        igsid = (thread or {}).get("igsid") or (client.get("external_id") or "")
        if not igsid:
            return False, "переписка в Instagram не найдена", None
        try:
            if auto:
                token = instagram.ensure_fresh_token(business_id)
                if not token:
                    return False, "Instagram отключён", None
                res = instagram.send_text(token, igsid, text, human=False)
                if not (res or {}).get("message_id"):
                    return False, "Instagram не подтвердил отправку", None
                mid = database.save_message(business_id, lead["client_id"],
                                            "assistant", text, channel="instagram")
                database.ig_thread_mark(business_id, igsid,
                                        last_out_at=database.now())
                return True, "", mid
            # Отправляет человек — тогда и правила площадки другие, и запись
            # в переписке делает сам канал.
            instagram.reply_as_human(business_id, igsid, text)
            last = database.last_outbound(business_id, lead["client_id"]) or {}
            return True, "", last.get("id")
        except Exception as e:
            return False, str(e) or "Instagram не принял сообщение", None
    return False, "канал недоступен", None


def send(business_id, followup_id, *, auto=True, actor="owner", actor_id=None):
    """
    Отправить касание. Возвращает запись в новом состоянии.

    Захват атомарный: два одновременных планировщика не отправят одно и то же
    дважды, потому что выиграть захват может ровно один. После неудачи запись
    возвращается в очередь — но считанное число раз.
    """
    row = database.get_followup(followup_id, business_id)
    if not row:
        raise FollowupError("Касание не найдено.")
    # Уже ушедшее не трогаем ничем: ни повторной отправкой, ни новой причиной
    # отказа. Переписывать состояние отправленного сообщения — значит однажды
    # объяснить владельцу, что то, что клиент прочитал, «заблокировано».
    #
    # Отменённое — по той же причине. У него записано, ПОЧЕМУ оно отменено
    # («клиент снова написал сам»), и попытка отправить его заново стёрла бы
    # эту причину, заменив её на «сейчас нельзя». История разговора важнее
    # опрятного состояния: без неё непонятно, что вообще произошло.
    if row["status"] in (database.FU_SENT, database.FU_SENDING,
                         database.FU_CANCELLED):
        return row
    ok, why = may_send(business_id, row, auto=auto)
    if not ok:
        _close(business_id, row, database.FU_BLOCKED, why)
        _mark_action(business_id, followup_id, database.AC_BLOCKED, error=why)
        return database.get_followup(followup_id, business_id)

    # Ручная отправка идёт из черновика напрямую: владелец нажал «Отправить» —
    # это и есть подтверждение, отдельного «одобрить» ему не нужно.
    was = row["status"]
    if was == database.FU_DRAFT:
        was = database.FU_APPROVED
        database.update_followup(followup_id, business_id, status=was)
    if not database.claim_followup(followup_id, business_id):
        # Захват не удался — значит, кто-то другой уже занят этим касанием.
        return database.get_followup(followup_id, business_id)

    lead = database.get_lead(row["lead_id"], business_id)
    sent, error, mid = _deliver(business_id, lead, row["message"], auto=auto)
    if not sent:
        tries = int(database.get_followup(followup_id, business_id)["tries"])
        # Возвращаем ровно туда, откуда взяли. Вывести состояние из того, кто
        # пробовал отправить, — значит однажды показать «уйдёт само» там, где
        # автономия выключена, и владелец решит, что разрешил лишнее.
        back = database.FU_FAILED if tries >= MAX_TRIES else was
        database.update_followup(followup_id, business_id, status=back,
                                 error=(error or "")[:300],
                                 outcome=FAILED_OUT if back == database.FU_FAILED else None)
        log.warning("Касание %s не ушло (biz %s): %s", followup_id, business_id, error)
        if back == database.FU_FAILED:
            _mark_action(business_id, followup_id, database.AC_FAILED, error=error)
        return database.get_followup(followup_id, business_id)

    database.mark_followup_sent(followup_id, business_id)
    database.update_followup(followup_id, business_id, error=None)
    try:
        database.add_memory_link(
            business_id, "lead", row["lead_id"], event="edited",
            source_kind=row.get("channel") or "auto",
            actor=("velor" if auto else actor), actor_id=actor_id,
            note="Отправлено касание №%s: %s" % (row["attempt"],
                                                 REASON_RU.get(row["reason"], "")))
    except Exception:
        log.exception("Касание не записалось в историю (biz %s)", business_id)
    database.log_event(business_id, "reply", "Отправлено касание клиенту",
                       (row.get("message") or "")[:200],
                       once_key="followup-sent:%s" % followup_id)
    _mark_action(business_id, followup_id, database.AC_SUCCEEDED,
                 result="Сообщение доставлено в " + (row.get("channel") or "канал"))
    try:
        import leads as leads_mod
        # Граница разговора двигается вместе с ним: отправленное касание —
        # часть этой возможности, и в её переписке оно должно быть видно.
        leads_mod.touch(business_id, row["lead_id"], message_id=mid)
    except Exception:
        log.exception("Активность возможности не обновилась (biz %s)", business_id)
    return database.get_followup(followup_id, business_id)


def _close(business_id, row, status, reason):
    database.update_followup(row["id"], business_id, status=status,
                             stop_reason=(reason or "")[:200])
    if status == database.FU_CANCELLED:
        _mark_action(business_id, row["id"], database.AC_CANCELLED,
                     result=STOP_RU.get(reason or "", reason or "Повод исчез."))


# ── решения владельца ──────────────────────────────────────────────────────

def approve(business_id, followup_id):
    """Владелец разрешил. Отправит планировщик — в рабочее время."""
    row = database.get_followup(followup_id, business_id)
    if not row:
        raise FollowupError("Касание не найдено.")
    if row["status"] not in (database.FU_DRAFT, database.FU_BLOCKED):
        raise FollowupError("Это касание уже не черновик.")
    ok, why = may_send(business_id, dict(row, status=database.FU_DRAFT), auto=False)
    if not ok:
        _close(business_id, row, database.FU_BLOCKED, why)
        return database.get_followup(followup_id, business_id)
    database.update_followup(followup_id, business_id, status=database.FU_APPROVED,
                             stop_reason=None, error=None)
    made = database.get_followup(followup_id, business_id)
    # Владелец сказал «да» — предложение в очереди больше не ждёт его: оно
    # ждёт подходящего времени. Если записи ещё не было (касание завели до
    # того, как появился журнал), заводим её теперь.
    got = database.find_live_action(business_id, ACTION_KEY % followup_id)
    if got:
        database.update_action(got["id"], business_id,
                               status=database.AC_APPROVED,
                               decided_at=database.now())
    else:
        _propose_action(business_id, made)
    return made


def edit(business_id, followup_id, text):
    """Владелец переписал текст. Своё сообщение он отправляет своими словами."""
    row = database.get_followup(followup_id, business_id)
    if not row:
        raise FollowupError("Касание не найдено.")
    text = (text or "").strip()
    if not text:
        raise FollowupError("Пустое сообщение отправить нельзя.")
    if row["status"] in (database.FU_SENT, database.FU_SENDING):
        raise FollowupError("Отправленное сообщение изменить нельзя.")
    based = dict(row.get("based_on") or {}, draft_by="owner")
    database.update_followup(followup_id, business_id, message=text[:900],
                             based_on=based)
    return database.get_followup(followup_id, business_id)


def stop_all(business_id, lead_id, *, reason="lost"):
    """
    Прекратить все живые касания по возможности. Возвращает, сколько отменено.

    Отправленное не трогаем: его уже видел клиент, и переписывать историю
    задним числом нельзя — можно только записать, чем она кончилась.
    """
    n = 0
    for row in database.list_followups(business_id, lead_id=lead_id, live=True,
                                       limit=20):
        _close(business_id, row, database.FU_CANCELLED, reason)
        n += 1
    return n


def cancel(business_id, followup_id, *, reason="owner_cancelled"):
    """«Не писать». Отмена — тоже решение, и она записывается как решение."""
    row = database.get_followup(followup_id, business_id)
    if not row:
        raise FollowupError("Касание не найдено.")
    if row["status"] == database.FU_SENT:
        raise FollowupError("Это сообщение уже отправлено.")
    if reason == "owner_cancelled":
        # Запоминаем, на каком сообщении клиента владелец сказал «не писать».
        # По времени это не отличить: отмена и последняя реплика могут попасть
        # в одну секунду. По номеру сообщения — отличить всегда, и та же
        # причина заставила считать по id разрыв «спросил / ответили».
        lead = database.get_lead(row["lead_id"], business_id) or {}
        gap = database.last_exchange(business_id, lead.get("client_id"))
        database.update_followup(
            followup_id, business_id,
            based_on=dict(row.get("based_on") or {},
                          cancelled_after=gap.get("last_in_id")))
    _close(business_id, row, database.FU_CANCELLED, reason)
    return database.get_followup(followup_id, business_id)


# ── жизнь после отправки ───────────────────────────────────────────────────

def on_client_message(business_id, client_id, *, message_id=None, text=""):
    """
    Клиент написал сам. Всё запланированное по нему больше не нужно.

    Это правило важнее расписания: новое слово человека всегда свежее нашего
    вчерашнего плана. И оно же — первая половина атрибуции: если касание уже
    ушло, ответ на него засчитывается именно ему.
    """
    if not client_id:
        return 0
    changed = 0
    try:
        rows = database.list_followups(business_id, limit=200, live=True)
        for row in rows:
            if int(row.get("client_id") or 0) != int(client_id):
                continue
            _close(business_id, row, database.FU_CANCELLED, "new_conversation")
            changed += 1
        # Атрибуция: последнее отправленное касание этому человеку получает
        # ответ. Утверждать, что оно принесло деньги, ещё рано — записываем
        # только то, что видно: клиент откликнулся.
        sent = [r for r in database.list_followups(business_id,
                                                   status=database.FU_SENT, limit=200)
                if int(r.get("client_id") or 0) == int(client_id) and not r.get("outcome")]
        if sent:
            last = sent[-1]
            negative = qualify.read(text or "", message_id=message_id)
            rejected = any(s["kind"] == "negative" for s in negative)
            database.update_followup(
                last["id"], business_id,
                outcome=REJECTED if rejected else REPLIED,
                outcome_at=database.now(), reply_message_id=message_id)
    except Exception:
        log.exception("Касания не отменились по ответу клиента (biz %s)", business_id)
    return changed


def on_order(business_id, client_id, order_id):
    """
    Появилась заявка. Если ей предшествовало касание — цепочка сохраняется.

    Сохраняется именно цепочка «касание → ответ → заявка», а не утверждение,
    что заявку принесло касание. Разница станет считаемой, когда таких цепочек
    накопится достаточно; выдумывать выводы сейчас — значит поверить в них.
    """
    if not client_id:
        return None
    try:
        sent = [r for r in database.list_followups(business_id,
                                                   status=database.FU_SENT, limit=200)
                if int(r.get("client_id") or 0) == int(client_id)]
        if not sent:
            return None
        last = sent[-1]
        database.update_followup(last["id"], business_id, outcome=CONVERTED,
                                 outcome_at=database.now(), order_id=int(order_id))
        return last["id"]
    except Exception:
        log.exception("Заявка не связалась с касанием (biz %s)", business_id)
        return None


def settle(business_id, *, now=None):
    """
    Записать «ответа не было» тем касаниям, на которые уже не ответят.

    Без этого шага «проигнорировано» не существовало бы как факт: молчание
    само себя не записывает, а разница между «ждём» и «не ответили» — это
    ровно то, чем потом будет считаться польза касаний.
    """
    now = now or _now()
    edge = _stamp(now - datetime.timedelta(days=IGNORED_AFTER_DAYS))
    n = 0
    for row in database.list_followups(business_id, status=database.FU_SENT, limit=300):
        if row.get("outcome") or not row.get("sent_at"):
            continue
        if row["sent_at"] <= edge:
            database.update_followup(row["id"], business_id, outcome=IGNORED,
                                     outcome_at=database.now())
            n += 1
    return n


# ── обход ──────────────────────────────────────────────────────────────────

def run(business_id, *, now=None):
    """
    Полный проход по бизнесу: подготовить, отправить созревшее, подытожить.

    Идемпотентен: второй запуск подряд не создаёт вторых касаний и не
    отправляет уже отправленное — каждое состояние проверяется на входе.
    """
    now = now or _now()
    out = {"planned": 0, "blocked": 0, "skipped": 0, "sent": 0, "failed": 0,
           "settled": 0}
    try:
        business = database.get_business(business_id) or {}
        import trial
        if trial.access(business)["read_only"]:
            return out
    except Exception:
        log.exception("Состояние подписки не прочиталось (biz %s)", business_id)
        return out

    out.update(scan(business_id))
    for row in database.list_followups(
            business_id, status=(database.FU_SCHEDULED, database.FU_APPROVED),
            due_before=_stamp(now), limit=100):
        # Уходит и запланированное (это разрешила автономия), и одобренное
        # (это разрешил владелец про конкретное сообщение). Разница между ними
        # не в том, отправлять ли, а в том, кто за текст отвечает.
        try:
            after = send(business_id, row["id"], auto=True)
        except Exception:
            log.exception("Отправка касания сорвалась (biz %s, fu %s)",
                          business_id, row["id"])
            continue
        if after and after["status"] == database.FU_SENT:
            out["sent"] += 1
        elif after and after["status"] in (database.FU_FAILED, database.FU_BLOCKED):
            out["failed"] += 1
    out["settled"] = settle(business_id, now=now)
    return out


# ── наружу ─────────────────────────────────────────────────────────────────

def public(row):
    """Касание словами, которые можно показать человеку."""
    if not row:
        return None
    d = dict(row)
    d["reason_ru"] = REASON_RU.get(d.get("reason"), d.get("reason") or "")
    d["stop_ru"] = STOP_RU.get(d.get("stop_reason") or "", d.get("stop_reason") or "")
    d["outcome_ru"] = OUTCOME_RU.get(d.get("outcome") or "", "")
    d["sent"] = d.get("status") == database.FU_SENT
    d["live"] = d.get("status") in database.FOLLOWUP_LIVE
    based = d.get("based_on") or {}
    # Провенанс словами: владелец должен видеть не «reason=quote_pending», а
    # то, на каком сообщении это основано.
    why = []
    if d.get("trigger"):
        why.append(d["trigger"])
    if based.get("last_in_at"):
        why.append("последнее сообщение клиента: %s" % based["last_in_at"])
    if based.get("last_out_at"):
        why.append("последний наш ответ: %s" % based["last_out_at"])
    if based.get("last_in_id"):
        why.append("сообщение №%s" % based["last_in_id"])
    d["why"] = why
    d["by_ai"] = based.get("draft_by") == "ai"
    d["model_rejected"] = list(based.get("model_rejected") or [])
    d["attempt_ru"] = "касание %s из %s" % (d.get("attempt") or 1, MAX_ATTEMPTS)
    return d


# ── как это выполняется по кнопке из общего списка ─────────────────────────
# Общий исполнитель не знает, что такое касание, и знать не должен. Он умеет
# одно: спросить полномочия, захватить действие и записать результат. Работу
# делает тот, кто ей владеет.

def _run_action(business_id, row):
    """Выполнить касание по подтверждению владельца из общей очереди."""
    fid = int((row.get("payload") or {}).get("followup_id") or row.get("target_id") or 0)
    if not fid:
        return {"ok": False, "error": "Непонятно, какое сообщение отправлять."}
    text = ((row.get("payload") or {}).get("message") or "").strip()
    cur = database.get_followup(fid, business_id)
    if not cur:
        return {"ok": False, "status": database.AC_STALE,
                "error": "Этого касания больше нет."}
    # Владелец мог переписать текст прямо в очереди — тогда отправляем его
    # слова, а не наши. Правка сохраняется в самом касании, чтобы в карточке
    # возможности было видно ровно то, что ушло клиенту.
    if text and text != (cur.get("message") or "").strip():
        edit(business_id, fid, text)
    after = send(business_id, fid, auto=False, actor="owner")
    if after and after["status"] == database.FU_SENT:
        return {"ok": True, "target_id": fid,
                "result": "Сообщение доставлено в " + (after.get("channel") or "канал")}
    if after and after["status"] == database.FU_BLOCKED:
        return {"ok": False, "status": database.AC_BLOCKED,
                "error": after.get("stop_reason") or "Сейчас отправлять нельзя."}
    # Касание вернулось в очередь на новую попытку — значит, и предложение в
    # журнале должно вернуться туда же. Иначе владелец увидел бы «не
    # получилось» у сообщения, которое через полчаса всё-таки уйдёт.
    retry = bool(after and after["status"] in database.FOLLOWUP_LIVE)
    return {"ok": False, "retry": retry,
            "error": (after or {}).get("error") or "Сообщение не ушло."}


def _check_action(business_id, row):
    """
    Не устарело ли предложение. Проверяем ровно перед выполнением, а не при
    показе: между «показали» и «нажали» проходит время, и именно в нём клиент
    успевает ответить сам.
    """
    fid = int((row.get("payload") or {}).get("followup_id") or row.get("target_id") or 0)
    cur = database.get_followup(fid, business_id) if fid else None
    if not cur:
        return False, "Этого касания больше нет."
    if cur["status"] == database.FU_SENT:
        return False, "Это сообщение уже отправлено."
    if cur["status"] in (database.FU_CANCELLED, database.FU_FAILED):
        return False, STOP_RU.get(cur.get("stop_reason") or "",
                                  "Касание больше не в работе.")
    ok, why = may_send(business_id, cur, auto=False)
    return ok, why


def _run_prepare(business_id, row):
    """
    Подготовить черновик касания по одной возможности.

    Раньше черновики появлялись только сплошным обходом: «посмотри все живые
    возможности и реши, где назрело». Находке нужен другой вход — по одному
    конкретному разговору, который она и назвала. Логика та же самая, plan()
    один на оба входа: второй способ решать, что писать клиенту, означал бы
    два разных ответа на один вопрос.
    """
    lead_id = int(row.get("target_id") or 0)
    # Ту же проверку, что стоит перед подтверждением, делаем и здесь. Не для
    # порядка: разрешённое действие выполняется сразу, без остановки у
    # владельца, — и если не спросить об актуальности тут, второй обход
    # подготовит второй черновик по тому же разговору.
    ok, why = _check_prepare(business_id, row)
    if not ok:
        return {"ok": False, "status": database.AC_STALE, "error": why}
    lead = database.get_lead(lead_id, business_id)
    gap = database.last_exchange(business_id, lead.get("client_id"))
    got = plan(business_id, lead, gap=gap)
    if not got:
        return {"ok": False, "status": database.AC_STALE,
                "error": "Повода писать сейчас нет."}
    if got["status"] == database.FU_BLOCKED:
        return {"ok": False, "status": database.AC_BLOCKED,
                "error": got.get("stop_reason") or "Сейчас писать нельзя."}
    return {"ok": True, "target_id": lead_id,
            "after": {"followup_id": got["id"]},
            "result": "Черновик готов: " + _short_text(got.get("message"))}


def _check_prepare(business_id, row):
    """Не готово ли уже. Дважды один и тот же черновик никому не нужен."""
    lead_id = int(row.get("target_id") or 0)
    lead = database.get_lead(lead_id, business_id) if lead_id else None
    if not lead:
        return False, "Этой возможности больше нет."
    if lead["status"] not in database.LEAD_OPEN:
        return False, "Возможность уже закрыта."
    live = database.list_followups(business_id, lead_id=lead_id, live=True, limit=1)
    if live:
        return False, "Черновик по этой возможности уже готов."
    return True, ""


def _short_text(s, n=80):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _register():
    """Объявить себя исполнителем. Ленивo — чтобы не заводить кольцо импортов."""
    try:
        import actions
        actions.RUNNERS["send_followup"] = _run_action
        actions.CHECKERS["send_followup"] = _check_action
        actions.RUNNERS["prepare_followup"] = _run_prepare
        actions.CHECKERS["prepare_followup"] = _check_prepare
    except Exception:
        log.exception("Касания не объявились исполнителем")


_register()


def overview(business_id):
    """
    Короткая сводка для владельца: что готово, что запланировано, что стоит.

    Считается по той же таблице, что и всё остальное. Второго способа узнать,
    сколько у нас касаний, в системе нет.
    """
    counts = database.followups_overview(business_id)
    return {
        "ready": counts.get(database.FU_DRAFT, 0),
        "approved": counts.get(database.FU_APPROVED, 0),
        "scheduled": counts.get(database.FU_SCHEDULED, 0),
        "blocked": counts.get(database.FU_BLOCKED, 0),
        "sent": counts.get(database.FU_SENT, 0),
        "failed": counts.get(database.FU_FAILED, 0),
        "live": counts.get("live", 0),
    }
