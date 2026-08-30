# -*- coding: utf-8 -*-
"""
VELOR замечает сам — находки, а не отчёты.

До этого слоя VELOR отвечал на события: клиент написал — ответил, материал
пришёл — разобрал, разговор оборвался — подготовил касание. На вопрос «что
вообще происходит, пока я не смотрю» отвечал только Директор, и то по запросу
и только про деньги.

Здесь появляется третий распорядок: VELOR сам, по расписанию, смотрит на
состояние бизнеса, находит сигнал, взвешивает его и приносит владельцу
готовую мысль — «вот что я заметил, вот доказательства, вот что предлагаю».

Три уровня, которые нельзя смешивать:

    НАХОДКА обнаруживает.   Она ничего не делает и делать не вправе.
    ДЕЙСТВИЕ исполняет.     Через тот же actions.py, с теми же полномочиями.
    ВЛАДЕЛЕЦ решает.        Между находкой и действием стоит он.

Отсюда все ограничения этого файла:

  • обнаружение — не действие. Оно не пишется в журнал действий и не требует
    полномочий: посмотреть на свои же данные VELOR вправе всегда;
  • ни одной находки без чисел, которые можно пересчитать. «Кажется, продажи
    падают» — это не находка, а настроение;
  • догадка ИИ живёт в отдельном поле и подписана догадкой. Смешать её с
    посчитанным фактом — самый быстрый способ обесценить и то, и другое;
  • мало данных — молчим. Вывод по двум продажам вреднее отсутствия вывода:
    по нему принимают решения;
  • одна проблема — одна запись. Обход идёт каждые полчаса, и если бы каждый
    раз заводилась новая находка, владелец перестал бы читать их к обеду;
  • проблема исчезла — запись закрывается сама, с результатом. Старая
    тревога на экране учит не доверять экрану.

Порогов «на все случаи» здесь нет. Там, где нужный порог уже назван и объяснён
в Директоре или в оценке возможностей, берётся он — второй ответ на тот же
вопрос был бы не строгостью, а расхождением.
"""
import datetime
import logging

import database
import director
import qualify

log = logging.getLogger("velor.initiatives")


# ── два уровня внимания ────────────────────────────────────────────────────
# Не всё замеченное стоит того, чтобы прерывать владельца. Разница не в
# важности сигнала, а в том, есть ли что решать: WATCH описывает состояние,
# ACT ждёт ответа.
WATCH = "watch"
ACT = "act"

LEVEL_RU = {WATCH: "Наблюдение", ACT: "Требует решения"}

# ── важность ───────────────────────────────────────────────────────────────
# Шкала не новая: ровно та, по которой уже ранжируются возможности. Заводить
# вторую значило бы получить «высокий приоритет» в двух разных смыслах на
# соседних экранах.
LOW, MEDIUM, HIGH, URGENT = qualify.LOW, qualify.MEDIUM, qualify.HIGH, qualify.URGENT
PRIORITY_ORDER = qualify.PRIORITY_ORDER
PRIORITY_RU = qualify.PRIORITY_RU

# ── уверенность ────────────────────────────────────────────────────────────
# Без процентов. «Проблема на 92%» — фальшивая точность: за ней нет ни
# измерения, ни способа её проверить.
SOLID = "solid"     # выборки хватает, цифры сходятся
LIKELY = "likely"   # основание есть, но данных немного — сказано вслух

CONFIDENCE_RU = {SOLID: "Оснований достаточно",
                 LIKELY: "Основание есть, но данных немного"}

# ── о чём находка ──────────────────────────────────────────────────────────
GROUPS = [("lead", "Возможности"), ("sales", "Продажи"),
          ("retention", "Возвраты"), ("operations", "Операции"),
          ("finance", "Финансы"), ("data", "Данные")]


# ── пороги ─────────────────────────────────────────────────────────────────
# Каждый назван и объяснён. Молчаливая константа — это решение, которое никто
# не принимал; а по этим решениям владельца будят.

# Через сколько часов молчания разговор начинает стоить денег — тот же порог,
# по которому оценка возможностей поднимает приоритет.
GAP_HOURS = qualify.GAP_HIGH_HOURS

# Насколько просроченным должен быть готовый черновик, чтобы это стало
# завалом, а не «ещё не дошли руки».
BACKLOG_LATE_H = 6
MIN_BACKLOG = 2

# Окно сравнения. Две недели против двух предыдущих — самый короткий период,
# в котором у маленького бизнеса вообще набирается что сравнивать.
WINDOW_DAYS = 14

# Минимальный объём выборки. Меньше — это не тренд, а совпадение; берём тот
# же порог, по которому молчит Директор.
MIN_CLOSED = director.MIN_ENTRIES_TREND * 2   # закрытых возможностей в периоде
MIN_LOST = director.MIN_ENTRIES_TREND         # потерь, чтобы говорить о потерях
LOST_JUMP = 50                                # % роста потерь, с которого это событие
CONV_DROP = 10                                # процентных пунктов падения конверсии

# Возвраты. Раньше 30 дней клиент ещё не «пропал», позже полугода — это уже не
# пауза, а уход, и звать его «повторной покупкой» нечестно.
RETURN_FROM_DAYS = director.SLEEP_DAYS
RETURN_TO_DAYS = 180
MIN_RETURN_CLIENTS = 3

# Деньги. Оба порога — из Директора: сколько операций делает период
# сравнимым и с какой суммы разговор вообще имеет смысл.
MIN_FIN_ENTRIES = director.MIN_ENTRIES_TREND
MIN_FIN_BASE = director.MIN_BASE_MONEY
FIN_JUMP = 30                                 # % изменения расходов

# Сколько живёт находка, которую перестали подтверждать. Не «срок годности
# проблемы»: пока обход видит её снова, срок продлевается. Это защита от
# записи, о которой все забыли, — обход сломался, а тревога висит.
TTL_DAYS = 14

# Сколько молчим о том, от чего владелец отмахнулся. Пока картина не изменится
# существенно — а «существенно» здесь означает другой отпечаток повода.
COOLDOWN_DAYS = 14

# Сколько действий может вырасти из одной находки за раз. Ограничение не
# техническое: «VELOR решил написать пятистам клиентам» не должно стать
# возможным ни при каких настройках.
ACT_CAP = 10


# ── реестр находок ─────────────────────────────────────────────────────────
# Только то, что VELOR действительно умеет обнаружить по своим же данным.
# Каждая строка отвечает на три вопроса владельца: что произошло, почему это
# важно и что можно сделать.

HOT_LEADS_UNANSWERED = "hot_leads_unanswered"
FOLLOWUP_BACKLOG = "followup_backlog"
LOST_LEADS_CLUSTER = "lost_leads_cluster"
CONVERSION_DROP = "conversion_drop"
RETURNING_CLIENT = "returning_client_opportunity"
DATA_CONFLICT = "data_conflict"
FINANCIAL_ANOMALY = "financial_anomaly"

TYPES = {
    HOT_LEADS_UNANSWERED: {
        "title": "Горячие клиенты ждут ответа",
        "group": "lead", "href": "leads.html",
        # Действие, которое VELOR умеет: подготовить черновик касания. Не
        # «написать за вас» — отправка спрашивается отдельно и своим правом.
        "action": "prepare_followup",
        "action_note": "Подготовить, что им написать",
        # Догадка ИИ здесь не нужна: причина известна и она одна — не ответили.
        "guess": False},
    FOLLOWUP_BACKLOG: {
        "title": "Готовые сообщения так и не ушли",
        "group": "lead", "href": "leads.html",
        "action": "send_followup",
        "action_note": "Отправить подготовленное",
        "guess": False},
    LOST_LEADS_CLUSTER: {
        "title": "Возможностей теряется больше обычного",
        "group": "sales", "href": "leads.html",
        "action": None, "action_note": "Посмотреть, на чём именно теряем",
        "guess": True},
    CONVERSION_DROP: {
        "title": "Доля покупок упала",
        "group": "sales", "href": "leads.html",
        "action": None, "action_note": "Посмотреть закрытые возможности",
        "guess": True},
    RETURNING_CLIENT: {
        "title": "Клиенты, которые покупали и перестали",
        "group": "retention", "href": "clients.html",
        # Действия нет намеренно. Рассылка по базе — не то, что VELOR вправе
        # предложить одной кнопкой: у неё нет ни повода в разговоре, ни
        # адресата, который чего-то ждёт.
        "action": None, "action_note": "Посмотреть, кому есть что предложить",
        "guess": False},
    DATA_CONFLICT: {
        "title": "В памяти бизнеса две разные правды",
        "group": "data", "href": "knowledge.html",
        "action": None, "action_note": "Открыть и оставить одну запись",
        "guess": False},
    FINANCIAL_ANOMALY: {
        "title": "Расходы изменились заметно",
        "group": "finance", "href": "finance.html",
        "action": None, "action_note": "Посмотреть, из чего сложилось",
        "guess": True},
}

GROUP_RU = dict(GROUPS)


class InitiativeError(Exception):
    """Сделать по находке нельзя, и человеку сказано почему."""


# ── время и числа ──────────────────────────────────────────────────────────

def _now():
    """
    Сейчас — по тем же часам, по которым пишет база.

    База ставит время в UTC (`database.now`), окна в SQL считаются от `date('now')`,
    то есть тоже в UTC. Взять здесь местное время значило бы получить сдвиг на
    часовой пояс в каждом «ждёт пять часов» — и разговор, длящийся три часа,
    показывался бы шестичасовым.
    """
    return datetime.datetime.utcnow()


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(when):
    try:
        return datetime.datetime.strptime(str(when)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _hours_since(when, now=None):
    dt = _parse(when)
    if not dt:
        return None
    return max(0.0, ((now or _now()) - dt).total_seconds() / 3600)


_money = director._money
_num = director._num
_plural = director._plural


def _pct(now_v, was_v):
    """Изменение в процентах. Не от чего считать — значит, нечего сказать."""
    if not was_v:
        return None
    return round((now_v - was_v) * 100 / was_v)


def _bucket(n):
    """
    Грубая мера повода. Нарочно грубая: одним клиентом больше — та же
    проблема, и заводить о ней вторую запись значит начать спамить.
    """
    n = int(n or 0)
    if n <= 3:
        return "1-3"
    if n <= 9:
        return "4-9"
    return "10+"


def _pct_bucket(p):
    p = abs(int(p or 0))
    if p < 25:
        return "10-24"
    if p < 50:
        return "25-49"
    return "50+"


def _window_ru(days, now=None):
    """«последние 14 дней (16.08–30.08)» — период словами и датами."""
    end = (now or _now()).date()
    start = end - datetime.timedelta(days=days - 1)
    return "%s %s (%s–%s)" % (
        days, _plural(days, "день", "дня", "дней"),
        start.strftime("%d.%m"), end.strftime("%d.%m"))


def _found(kind, *, title, summary, why, priority=MEDIUM, level=ACT,
           confidence=SOLID, evidence=None, impact=None, impact_note=None,
           fingerprint="", action=False, action_note=None):
    """
    Одна находка до записи.

    fingerprint — отпечаток ПОВОДА, не доказательств: по нему решается только
    один вопрос — «это та же самая проблема, от которой владелец уже
    отмахнулся, или уже другая».
    """
    meta = TYPES[kind]
    return {
        "type": kind, "title": title, "summary": summary, "why": why,
        "priority": priority, "level": level, "confidence": confidence,
        "evidence": evidence or {}, "impact": impact,
        "impact_note": impact_note or "",
        # Обычно предложение берётся из реестра. Детектор вправе его снять:
        # предлагать «подготовить черновик» там, где черновик уже лежит, —
        # значит обещать работу, которой не будет, и получить в ответ «повод
        # уже неактуален» вместо результата.
        "action": (meta["action"] if action is False else action),
        "action_note": action_note or meta["action_note"],
        "href": meta["href"],
        "fingerprint": "%s:%s" % (kind, fingerprint or "-"),
    }


# ═══ ОБНАРУЖЕНИЕ ═══════════════════════════════════════════════════════════
#
# Все семь детекторов — обычная арифметика по своим же таблицам. ИИ здесь не
# участвует и участвовать не должен: «сколько горячих клиентов не получили
# ответа» — вопрос с точным ответом, и спрашивать его у модели значило бы
# платить за то, что уже посчитано, и получить это с ошибкой.

def _hot_leads(bid, now):
    """Клиент с высоким намерением написал — и ему не ответили."""
    rows = database.list_leads(bid, status="open", limit=200)
    if not rows:
        return None
    gaps = database.last_exchanges(bid, [r.get("client_id") for r in rows])
    hot, worst, value = [], 0.0, 0
    for lead in rows:
        gap = gaps.get(lead.get("client_id")) or {}
        if not gap.get("unanswered"):
            continue
        waited = _hours_since(gap.get("last_in_at"), now)
        if waited is None or waited < GAP_HOURS:
            continue
        # Приоритет НЕ читается из строки: он там всегда пуст и пуст намеренно —
        # записанный, он врал бы уже через час, потому что зависит от того,
        # сколько бизнес молчит прямо сейчас. Спрашиваем ровно ту же функцию,
        # по которой приоритет считается на экране возможности.
        level, _why, _risks = qualify.priority_of(
            lead, intent=lead.get("intent") or qualify.LOW,
            fit=lead.get("fit") or qualify.UNKNOWN,
            value=lead.get("value"), gap=gap, signals=lead.get("signals"),
            value_estimated=not lead.get("value"))
        if level not in (HIGH, URGENT):
            continue
        hot.append(lead)
        worst = max(worst, waited)
        value += int(lead.get("value") or lead.get("estimated_value") or 0)
    if not hot:
        return None

    n = len(hot)
    hours = int(worst)
    # Что VELOR уже сделал по этим разговорам. Если черновики готовы для всех,
    # предлагать подготовить их ещё раз нечестно: работа сделана, и ждут
    # теперь не его, а владельца.
    drafted = {int(r["lead_id"]) for r in
               database.list_followups(bid, live=True, limit=200) if r.get("lead_id")}
    ready = all(int(l["id"]) in drafted for l in hot)
    return _found(
        HOT_LEADS_UNANSWERED,
        title="%s %s ответа" % (
            _num(n), _plural(n, "горячий клиент ждёт", "горячих клиента ждут",
                             "горячих клиентов ждут")),
        summary="Они написали сами и говорят о покупке прямо. Дольше всех ждёт "
                "%s %s." % (hours, _plural(hours, "час", "часа", "часов")),
        why="Пока разговор живой, его ещё можно закрыть. Каждый час молчания "
            "уменьшает эту вероятность — люди уходят к тем, кто ответил.",
        # Срочно — не от количества, а от времени: один человек, ждущий сутки,
        # хуже пятерых, написавших полчаса назад.
        priority=URGENT if hours >= GAP_HOURS * 3 or n >= 5 else HIGH,
        level=ACT, confidence=SOLID,
        evidence={"kind": "lead", "ids": [int(l["id"]) for l in hot[:20]],
                  "window": "сейчас", "numbers": {"count": n, "max_hours": hours}},
        impact=value or None,
        impact_note=("Столько стоят эти возможности по суммам из самих "
                     "разговоров. Это оценка, а не выручка."),
        action=(None if ready else False),
        action_note=("Черновики уже готовы — осталось отправить" if ready else None),
        fingerprint=_bucket(n))


def _followup_backlog(bid, now):
    """Черновик готов, время пришло — и он всё ещё лежит."""
    rows = database.list_followups(bid, live=True, limit=200)
    late = []
    for row in rows:
        if row["status"] not in (database.FU_DRAFT, database.FU_APPROVED):
            continue
        waited = _hours_since(row.get("recommended_at"), now)
        if waited is None or waited < BACKLOG_LATE_H:
            continue
        late.append(row)
    if len(late) < MIN_BACKLOG:
        return None

    n = len(late)
    hours = int(max(_hours_since(r.get("recommended_at"), now) or 0 for r in late))
    return _found(
        FOLLOWUP_BACKLOG,
        title="%s %s ждут отправки" % (
            _num(n), _plural(n, "сообщение", "сообщения", "сообщений")),
        summary="Текст готов, подходящее время прошло. Самое старое лежит "
                "%s %s." % (hours, _plural(hours, "час", "часа", "часов")),
        why="Смысл напоминания — во времени. Через неделю то же самое письмо "
            "читается как «про нас забыли, а потом вспомнили».",
        priority=HIGH, level=ACT, confidence=SOLID,
        evidence={"kind": "followup", "ids": [int(r["id"]) for r in late[:20]],
                  "window": "сейчас", "numbers": {"count": n, "max_hours": hours}},
        impact=None,
        impact_note="",
        fingerprint=_bucket(n))


def _lost_cluster(bid, now):
    """Потерь стало заметно больше, чем в предыдущие две недели."""
    cur = database.leads_period(bid, WINDOW_DAYS, 0)
    prev = database.leads_period(bid, WINDOW_DAYS, WINDOW_DAYS)
    if cur["lost"] < MIN_LOST:
        return None

    growth = _pct(cur["lost"], prev["lost"])
    if prev["lost"] and (growth is None or growth < LOST_JUMP):
        return None
    if not prev["lost"] and cur["closed"] < MIN_CLOSED:
        # Не с чем сравнивать и выборка мала — это ещё не рост, а начало.
        return None

    top = sorted(cur["lost_reasons"].items(), key=lambda kv: -kv[1])
    reason_ru = ""
    if top:
        import leads as leads_mod
        reason_ru = leads_mod.LOST_REASONS.get(top[0][0], "")
    n = cur["lost"]
    grew = ("на %d%% больше, чем за предыдущие %d" % (growth, WINDOW_DAYS)
            if growth is not None else
            "за предыдущие %d дней не было ни одной" % WINDOW_DAYS)
    return _found(
        LOST_LEADS_CLUSTER,
        title="Потеряно %s %s" % (
            _num(n), _plural(n, "возможность", "возможности", "возможностей")),
        summary="За %s — %s. %s" % (
            _window_ru(WINDOW_DAYS, now), grew,
            ("Чаще всего причина: «%s»." % reason_ru) if reason_ru else
            "Причины в карточках записаны."),
        why="Потери — единственное место, где видно, на чём именно бизнес "
            "теряет деньги. Одинаковая причина у нескольких подряд — это уже "
            "не невезение.",
        priority=HIGH if n >= 5 else MEDIUM,
        level=ACT if n >= 5 else WATCH,
        confidence=SOLID if cur["closed"] >= MIN_CLOSED else LIKELY,
        evidence={"kind": "lead", "ids": cur["lost_ids"][:20],
                  "window": _window_ru(WINDOW_DAYS, now),
                  "numbers": {"lost": n, "was": prev["lost"], "change": growth,
                              "reasons": cur["lost_reasons"]}},
        impact=None, impact_note="",
        fingerprint=_bucket(n))


def _conversion_drop(bid, now):
    """Доля покупок среди закрытых возможностей упала — при достаточной выборке."""
    cur = database.leads_period(bid, WINDOW_DAYS, 0)
    prev = database.leads_period(bid, WINDOW_DAYS, WINDOW_DAYS)
    # Процент, посчитанный по двум сделкам, — не конверсия. Требуем выборку в
    # ОБОИХ периодах: иначе «упало с 100% до 50%» означало бы «было одно, стало
    # два, одно из них не купило».
    if cur["closed"] < MIN_CLOSED or prev["closed"] < MIN_CLOSED:
        return None
    drop = (prev["conversion"] or 0) - (cur["conversion"] or 0)
    if drop < CONV_DROP:
        return None

    return _found(
        CONVERSION_DROP,
        title="Покупают реже: %d%% вместо %d%%" % (cur["conversion"],
                                                   prev["conversion"]),
        summary="За %s закрыто %s, купили %s. За предыдущие %d дней было "
                "%d%% из %s." % (
                    _window_ru(WINDOW_DAYS, now),
                    _num(cur["closed"]), _num(cur["won"]), WINDOW_DAYS,
                    prev["conversion"], _num(prev["closed"])),
        why="Столько же разговоров приносят меньше денег. Разница в %d "
            "%s — это те заказы, которые раньше доходили до оплаты." % (
                drop, _plural(drop, "пункт", "пункта", "пунктов")),
        priority=HIGH, level=ACT,
        confidence=SOLID if cur["closed"] >= MIN_CLOSED * 2 else LIKELY,
        evidence={"kind": "lead", "ids": cur["lost_ids"][:20],
                  "window": _window_ru(WINDOW_DAYS, now),
                  "numbers": {"conversion": cur["conversion"],
                              "was": prev["conversion"], "drop": drop,
                              "closed": cur["closed"], "closed_was": prev["closed"]}},
        impact=None, impact_note="",
        fingerprint=_pct_bucket(drop))


def _returning_clients(bid, now):
    """Люди с историей покупок, которые перестали возвращаться."""
    rows = database.sleeping_clients(bid, RETURN_FROM_DAYS, limit=30)
    edge = (now.date() - datetime.timedelta(days=RETURN_TO_DAYS)).isoformat()
    # Ушедшие полгода назад — это уже не пауза. Звать это «повторной покупкой»
    # значило бы выдать давно потерянных за почти вернувшихся.
    warm = [c for c in rows if str(c.get("last_at") or "")[:10] >= edge and c.get("spent")]
    if len(warm) < MIN_RETURN_CLIENTS:
        return None

    n = len(warm)
    spent = sum(int(c["spent"] or 0) for c in warm)
    names = ", ".join((c.get("name") or "без имени") for c in warm[:3])
    return _found(
        RETURNING_CLIENT,
        title="%s %s перестали возвращаться" % (
            _num(n), _plural(n, "клиент", "клиента", "клиентов")),
        summary="Последний заказ у каждого был больше %d дней назад, но не "
                "дальше полугода. Например: %s." % (RETURN_FROM_DAYS, names),
        why="Эти люди уже покупали и знают вас. Вернуть их обычно дешевле, чем "
            "найти новых, — но повод для разговора придумывать вам.",
        priority=MEDIUM, level=WATCH, confidence=SOLID,
        evidence={"kind": "client", "ids": [int(c["id"]) for c in warm[:20]],
                  "window": "заказы старше %d дней" % RETURN_FROM_DAYS,
                  "numbers": {"count": n, "spent": spent}},
        impact=spent,
        impact_note=("Столько они принесли раньше. Это факт о прошлом, а не "
                     "обещание, что вернутся."),
        fingerprint=_bucket(n))


def _data_conflict(bid, now):
    """Две записи об одном и том же говорят разное."""
    try:
        import entities
    except Exception:
        log.exception("Реестр знаний не прочитался (biz %s)", bid)
        return None

    # Смотрим на то, что сотрудник цитирует клиенту: услуги, товары, правила.
    # Расхождение в «О компании» стоит недоразумения, расхождение в цене —
    # денег.
    seen, clash = {}, []
    for kind in ("service", "product", "rule"):
        for fact in database.list_facts(bid, kind=kind):
            key = (kind, entities.norm_title(fact.get("title") or ""))
            if not key[1]:
                continue
            body = (fact.get("body") or "").strip()
            first = seen.get(key)
            if first is None:
                seen[key] = fact
                continue
            if (first.get("body") or "").strip() == body:
                continue     # дубль без расхождения — это неаккуратность, не риск
            clash.append((kind, fact.get("title") or "", first, fact))
    if not clash:
        return None

    kind, title, one, two = clash[0]
    hot = kind in ("service", "product")
    n = len(clash)
    return _found(
        DATA_CONFLICT,
        title="«%s» записано по-разному" % _short(title, 60),
        summary="Две записи с одним названием говорят разное: «%s» и «%s»."
                % (_short(one.get("body")), _short(two.get("body")))
                + ("" if n == 1 else " Всего таких пар: %d." % n),
        why=("Сотрудник берёт условия из памяти. Пока записей две, он может "
             "назвать клиенту не ту цену — и это обещание придётся выполнять."
             if hot else
             "Пока правило записано дважды и по-разному, сотрудник отвечает "
             "клиентам то одно, то другое."),
        priority=HIGH if hot else MEDIUM, level=ACT, confidence=SOLID,
        evidence={"kind": "fact",
                  "ids": [int(one["id"]), int(two["id"])],
                  "window": "текущая память бизнеса",
                  "numbers": {"pairs": n, "fact_kind": kind}},
        impact=None, impact_note="",
        fingerprint="%s:%s" % (kind, entities.norm_title(title)[:60]))


def _financial_anomaly(bid, now):
    """Расходы изменились так, что это уже не колебание."""
    cur = database.money_period(bid, WINDOW_DAYS * 2, 0)
    prev = database.money_period(bid, WINDOW_DAYS * 2, WINDOW_DAYS * 2)
    if cur["entries"] < MIN_FIN_ENTRIES or prev["entries"] < MIN_FIN_ENTRIES:
        return None
    if prev["expense"] < MIN_FIN_BASE:
        # Рост с 300 до 600 ₽ — не событие, а шум. Порог тот же, что у Директора.
        return None
    change = _pct(cur["expense"], prev["expense"])
    if change is None or abs(change) < FIN_JUMP:
        return None

    up = change > 0
    return _found(
        FINANCIAL_ANOMALY,
        title="Расходы %s на %d%%" % ("выросли" if up else "упали", abs(change)),
        summary="За последние %d дней — %s против %s за предыдущие %d. "
                "Операций в периоде: %s." % (
                    WINDOW_DAYS * 2, _money(cur["expense"]), _money(prev["expense"]),
                    WINDOW_DAYS * 2, _num(cur["entries"])),
        why=("Прибыль — это разница. Расход, выросший быстрее выручки, съедает "
             "её, даже когда продажи в порядке."
             if up else
             "Резкое падение расходов чаще означает, что что-то не занесено, а "
             "не что стало дешевле."),
        priority=HIGH if up else MEDIUM, level=ACT if up else WATCH,
        confidence=SOLID if cur["entries"] >= MIN_FIN_ENTRIES * 2 else LIKELY,
        evidence={"kind": "finance", "ids": [],
                  "window": "%d дней против предыдущих %d" % (WINDOW_DAYS * 2,
                                                             WINDOW_DAYS * 2),
                  "numbers": {"expense": cur["expense"], "was": prev["expense"],
                              "change": change, "entries": cur["entries"]}},
        impact=None,
        impact_note="",
        fingerprint=("up:" if up else "down:") + _pct_bucket(change))


def _short(s, n=70):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


# Порядок важен: он же порядок разбора. Дешёвое и денежное — первым.
DETECTORS = [
    (HOT_LEADS_UNANSWERED, _hot_leads),
    (FOLLOWUP_BACKLOG, _followup_backlog),
    (LOST_LEADS_CLUSTER, _lost_cluster),
    (CONVERSION_DROP, _conversion_drop),
    (DATA_CONFLICT, _data_conflict),
    (FINANCIAL_ANOMALY, _financial_anomaly),
    (RETURNING_CLIENT, _returning_clients),
]


# ═══ ЗАПИСЬ И ПОВТОРЫ ══════════════════════════════════════════════════════

def _enough_data(bid):
    """
    Есть ли вообще на что смотреть. У бизнеса, который завёлся вчера, нет ни
    трендов, ни проблем — есть пустая база, и говорить ему «конверсия упала»
    значит врать в первый же день.
    """
    try:
        span = database.data_span(bid)
        if span.get("rows"):
            return True
        # Память бизнеса — тоже данные, и противоречие в ней стоит денег ещё до
        # первого клиента: сотрудник цитирует её с первого же разговора.
        return bool(database.count_leads(bid) or database.count_clients(bid)
                    or database.count_facts(bid))
    except Exception:
        log.exception("Объём данных не прочитался (biz %s)", bid)
        return False


def _live_of_type(bid, kind):
    rows = database.list_initiatives(bid, kind=kind, live=True, limit=1)
    return rows[0] if rows else None


def _muffled(bid, fingerprint, now):
    """
    Не отмахивался ли владелец от ЭТОГО же повода недавно.

    Отпечаток здесь — единственный судья: пока картина та же, повторять
    отклонённое нельзя. Стала существенно другой — отпечаток другой, и находка
    имеет право появиться снова хоть на следующем обходе.
    """
    old = database.find_initiative(bid, fingerprint,
                                   statuses=(database.IN_DISMISSED,))
    if not old:
        return False
    when = _parse(old.get("decided_at") or old.get("updated_at"))
    if not when:
        return False
    return (now - when).days < COOLDOWN_DAYS


def _note(bid, cand, now):
    """
    Положить находку — или обновить ту, что уже есть.

    Одна проблема — одна запись. Обход идёт каждые полчаса; заводить новую
    строку на каждом означало бы к вечеру завалить владельца сорока
    одинаковыми сообщениями и научить его их не читать.
    """
    live = _live_of_type(bid, cand["type"])
    expires = _stamp(now + datetime.timedelta(days=TTL_DAYS))

    if live:
        fields = {"title": cand["title"], "summary": cand["summary"],
                  "why": cand["why"], "priority": cand["priority"],
                  "level": cand["level"], "confidence": cand["confidence"],
                  "evidence": cand["evidence"], "impact": cand["impact"],
                  "impact_note": cand["impact_note"],
                  # Предложение тоже пересматривается: пока запись висела,
                  # VELOR мог уже сделать то, что она предлагала.
                  "action": cand["action"], "action_note": cand["action_note"],
                  "fingerprint": cand["fingerprint"],
                  "last_seen_at": _stamp(now), "expires_at": expires}
        if live.get("fingerprint") != cand["fingerprint"]:
            # Картина изменилась существенно — это уже другой разговор.
            # Возвращаем находку владельцу на глаза, но не заводим вторую:
            # проблема-то та же самая.
            if live["status"] in (database.IN_SEEN, database.IN_ACKNOWLEDGED):
                fields["status"] = database.IN_NEW
        database.update_initiative(live["id"], bid, **fields)
        return {"id": live["id"], "new": False}

    if _muffled(bid, cand["fingerprint"], now):
        return {"id": None, "new": False, "muffled": True}

    iid = database.add_initiative(
        bid, cand["type"], title=cand["title"], level=cand["level"],
        priority=cand["priority"], confidence=cand["confidence"],
        summary=cand["summary"], why=cand["why"], evidence=cand["evidence"],
        impact=cand["impact"], impact_note=cand["impact_note"],
        action=cand["action"], action_note=cand["action_note"],
        href=cand["href"], fingerprint=cand["fingerprint"], expires_at=expires)

    # Обнаружение — событие компании, а не действие VELOR. В журнал действий
    # оно не попадает намеренно: там записано то, что VELOR СДЕЛАЛ, и мешать
    # с этим то, что он ЗАМЕТИЛ, значит потерять разницу между наблюдением и
    # поступком.
    database.log_event(bid, "risk" if cand["level"] == ACT else "opportunity",
                       "VELOR заметил: " + cand["title"], cand["summary"],
                       level="important" if cand["priority"] in (HIGH, URGENT)
                       else "info", once_key="initiative:" + cand["fingerprint"])
    return {"id": iid, "new": True}


def _resolved_note(row, now):
    """Чем кончилось. Без причинных заявлений — только что изменилось."""
    nums = (row.get("evidence") or {}).get("numbers") or {}
    was = nums.get("count") or nums.get("lost") or nums.get("pairs")
    if was:
        return "Было %s — сейчас ни одного." % _num(was)
    return "Сигнал больше не подтверждается."


def _close_gone(bid, scanned, alive_ids, now):
    """
    Проблема исчезла — закрываем находку сама собой, с результатом.

    Показывать вчерашнюю тревогу после того, как её разобрали, — самый
    надёжный способ научить владельца не верить этому экрану.
    """
    closed = 0
    for kind in scanned:
        for row in database.list_initiatives(bid, kind=kind, live=True, limit=20):
            if row["id"] in alive_ids:
                continue
            database.update_initiative(
                row["id"], bid, status=database.IN_RESOLVED,
                outcome=_resolved_note(row, now), closed_at=_stamp(now))
            closed += 1
    return closed


# ═══ ДОГАДКА ИИ ════════════════════════════════════════════════════════════
#
# Модель зовётся в одном-единственном случае: когда «почему» действительно
# нельзя посчитать. Обход идёт круглосуточно у всех бизнесов — платить за
# пересказ уже посчитанного нельзя ни технически, ни экономически.
#
# И то, что модель скажет, остаётся ДОГАДКОЙ: отдельное поле, отдельная
# подпись на экране. Гипотеза, записанная как факт, — это не помощь, а
# дезинформация с интонацией уверенности.

def _may_guess(row):
    meta = TYPES.get(row.get("type")) or {}
    return bool(meta.get("guess")) and row.get("level") == ACT \
        and row.get("priority") in (HIGH, URGENT) and not (row.get("hypothesis") or "")


def _guess(bid, row):
    """Спросить у ИИ возможную причину. Молча ничего не делаем, если нечем."""
    try:
        import ai
        if not ai.ai_available():
            return None
        business = database.get_business(bid) or {}
        facts = "\n".join([
            row.get("title") or "", row.get("summary") or "",
            "Числа: %s" % ((row.get("evidence") or {}).get("numbers") or {}),
        ])
        said = (ai.initiative_hypothesis(business, facts) or "").strip()
        if not said:
            return None
        database.update_initiative(row["id"], bid, hypothesis=said[:600])
        return said
    except Exception:
        log.exception("Догадка о причине не собралась (biz %s, находка %s)",
                      bid, row.get("id"))
        return None


# ═══ ОБХОД ═════════════════════════════════════════════════════════════════

def scan(business_id, *, now=None, use_ai=True):
    """
    Один взгляд на состояние бизнеса.

    Отдельного планировщика под это нет и не будет: он уже есть — тот же
    получасовой обход, что обслуживает касания. Второй ритм означал бы второй
    ответ на вопрос «что сейчас происходит», и первое же расхождение между
    ними владелец увидел бы как две разные правды на одном экране.
    """
    now = now or _now()
    out = {"checked": 0, "found": 0, "new": 0, "resolved": 0, "quiet": 0,
           "guessed": 0}
    if not _enough_data(business_id):
        # Молчание — тоже ответ, и здесь он единственный честный.
        return out

    scanned, alive, fresh_ids = [], [], []
    for kind, detect in DETECTORS:
        out["checked"] += 1
        try:
            cand = detect(business_id, now)
        except Exception:
            log.exception("Детектор %s сорвался (biz %s)", kind, business_id)
            # Сорвавшийся детектор не должен закрывать свои же находки как
            # «проблема исчезла»: мы не знаем, исчезла ли она.
            continue
        scanned.append(kind)
        if not cand:
            continue
        out["found"] += 1
        got = _note(business_id, cand, now)
        if got.get("muffled"):
            out["quiet"] += 1
            # Отклонённое остаётся отклонённым: закрывать по нему нечего, но и
            # «исчезло» тут неправда — поэтому тип из разбора не выпадает.
            scanned.remove(kind)
            continue
        alive.append(got["id"])
        if got["new"]:
            out["new"] += 1
            fresh_ids.append(got["id"])

    out["resolved"] = _close_gone(business_id, scanned, set(alive), now)

    # Дорогая часть — только для того, что уже прошло весь дешёвый отбор.
    if use_ai and fresh_ids:
        try:
            import trial
            business = database.get_business(business_id) or {}
            if trial.access(business)["read_only"]:
                return out
        except Exception:
            log.exception("Состояние подписки не прочиталось (biz %s)", business_id)
            return out
        for iid in fresh_ids:
            row = database.get_initiative(iid, business_id)
            if row and _may_guess(row) and _guess(business_id, row):
                out["guessed"] += 1
    return out


def settle(business_id, *, now=None):
    """
    Прибрать находки, которые перестали подтверждаться.

    Не «срок годности проблемы»: пока обход видит сигнал снова, срок
    продлевается. Это страховка от записи, о которой забыли, — детектор
    сломался, данные ушли, а тревога висит перед владельцем месяцами.
    """
    now = now or _now()
    gone = 0
    for row in database.list_initiatives(business_id, live=True, limit=100):
        seen = _parse(row.get("last_seen_at") or row.get("created_at"))
        if seen and (now - seen).days >= TTL_DAYS:
            database.update_initiative(
                row["id"], business_id, status=database.IN_EXPIRED,
                outcome="Сигнал не подтверждался %d дней." % TTL_DAYS,
                closed_at=_stamp(now))
            gone += 1
    return gone


# ═══ РЕШЕНИЯ ВЛАДЕЛЬЦА ═════════════════════════════════════════════════════

def _get(business_id, initiative_id):
    row = database.get_initiative(initiative_id, business_id)
    if not row:
        raise InitiativeError("Находка не найдена.")
    return row


def seen(business_id, initiative_id):
    """Показали на экране. Не решение — просто «уже не новость»."""
    row = _get(business_id, initiative_id)
    if row["status"] != database.IN_NEW:
        return row
    return database.update_initiative(initiative_id, business_id,
                                      status=database.IN_SEEN)


def acknowledge(business_id, initiative_id):
    """
    «Понял».

    Это не «разобрался»: шесть клиентов, которые ждут ответа, никуда не делись
    оттого, что владелец о них прочитал. Поэтому находка остаётся живой и
    закроется только тогда, когда исчезнет сам сигнал.
    """
    row = _get(business_id, initiative_id)
    if row["status"] in database.INITIATIVE_CLOSED:
        return row
    return database.update_initiative(initiative_id, business_id,
                                      status=database.IN_ACKNOWLEDGED,
                                      decided_at=database.now())


def dismiss(business_id, initiative_id):
    """
    «Не интересно» — и мы замолкаем. Но не навсегда: если картина изменится
    существенно, VELOR скажет снова. Молчать о выросшей вдвое проблеме потому,
    что о маленькой однажды отмахнулись, — это не деликатность, а сбой.
    """
    row = _get(business_id, initiative_id)
    if row["status"] in database.INITIATIVE_CLOSED:
        return row
    return database.update_initiative(initiative_id, business_id,
                                      status=database.IN_DISMISSED,
                                      outcome="Вы сказали, что это не важно.",
                                      decided_at=database.now(),
                                      closed_at=database.now())


# ── от находки к действию ──────────────────────────────────────────────────

def _targets(row):
    """По каким объектам действовать. Не «по всем клиентам» — по этим."""
    ev = row.get("evidence") or {}
    kind = ev.get("kind")
    ids = [int(i) for i in (ev.get("ids") or [])][:ACT_CAP]
    return kind, ids


def _settled(business_id, got, dedupe_key):
    """
    Что на самом деле получилось. Не то, что решили, — то, чем кончилось.

    Отдельная функция потому, что решение и результат расходятся ровно в одном
    месте, и место это важное: если такое предложение уже ждёт владельца,
    новая запись снимает себя сама. Отчитаться в этот момент словом
    «поставлено в очередь» значило бы соврать дважды — предложение не новое, а
    указывать надо на то, которое действительно ждёт.
    """
    import actions

    aid = got["id"]
    row = database.get_action(aid, business_id)
    if row and row["status"] == database.AC_CANCELLED and dedupe_key:
        live = database.find_live_action(business_id, dedupe_key)
        if live and int(live["id"]) != int(aid):
            return int(live["id"]), "already"
    status = (row or {}).get("status")
    if status == database.AC_SUCCEEDED:
        return int(aid), "done"
    if status in (database.AC_PROPOSED, database.AC_APPROVED):
        return int(aid), "approval"
    if status == database.AC_BLOCKED:
        return int(aid), "deny"
    if status == database.AC_FAILED:
        return int(aid), "failed"
    return int(aid), "skipped"


def _make_action(business_id, row, action_id, kind, target_id):
    """Одно действие из находки — через общий путь, без единого обхода."""
    import actions

    reason = "%s: %s" % (row["title"], row.get("action_note") or "")
    based = {"initiative_id": int(row["id"]), "initiative": row["type"]}
    based[kind + "_id"] = target_id

    if action_id == "send_followup":
        import followup
        fu = database.get_followup(target_id, business_id)
        if not fu:
            return None
        based["lead_id"] = fu.get("lead_id")
        # Ключ тот же, которым касания уже метят свои предложения: иначе одно
        # сообщение получило бы два предложения об отправке.
        key = followup.ACTION_KEY % target_id
        got = actions.execute(
            business_id, action_id, channel=fu.get("channel"),
            target_type="followup", target_id=target_id,
            reason=reason, based_on=based,
            payload={"followup_id": target_id, "message": fu.get("message") or ""},
            dedupe_key=key)
        return _settled(business_id, got, key)

    lead = database.get_lead(target_id, business_id)
    if not lead:
        return None
    key = "initiative:%s:%s:%s" % (row["type"], action_id, target_id)
    got = actions.execute(
        business_id, action_id, channel=lead.get("channel"),
        target_type="lead", target_id=target_id, reason=reason, based_on=based,
        dedupe_key=key)
    return _settled(business_id, got, key)


def act(business_id, initiative_id):
    """
    Сделать то, что находка предлагает.

    Ни одного обхода полномочий: каждое действие идёт тем же путём, что и все
    остальные, — реестр, полномочия, исполнитель, журнал. Находка не даёт
    права; она только называет повод и объект.

    Из одной находки может вырасти несколько действий — по одному на объект.
    Заводить на каждое отдельную находку значило бы шесть раз сообщить
    владельцу об одной проблеме.

    Исполнителем в журнале остаётся VELOR, а не владелец: работу делает он.
    То, что попросил владелец, видно по самой находке — у неё записано, когда
    он нажал; связь держится через based_on каждого действия.
    """
    import actions

    row = _get(business_id, initiative_id)
    action_id = row.get("action")
    if not action_id or action_id not in actions.REGISTRY:
        raise InitiativeError(
            "У этой находки нет действия, которое VELOR мог бы выполнить сам. "
            "Посмотрите сами — я показал, где.")
    kind, ids = _targets(row)
    if not ids:
        raise InitiativeError("Не осталось объектов, по которым можно действовать.")

    made = []
    decisions = {"done": 0, "approval": 0, "deny": 0, "failed": 0,
                 "already": 0, "skipped": 0}
    for target_id in ids:
        try:
            got = _make_action(business_id, row, action_id, kind, target_id)
        except Exception:
            log.exception("Действие из находки не получилось (biz %s, %s→%s)",
                          business_id, initiative_id, target_id)
            continue
        if not got:
            continue
        aid, outcome = got
        made.append(aid)
        decisions[outcome] = decisions.get(outcome, 0) + 1

    if not made:
        raise InitiativeError("Ни одного действия создать не удалось.")

    database.update_initiative(
        initiative_id, business_id, status=database.IN_ACTED,
        actions_ids=list((row.get("actions_ids") or [])) + made,
        decided_at=database.now(),
        outcome=_act_note(decisions))
    return {"initiative": public(database.get_initiative(initiative_id, business_id)),
            "actions": made, "decisions": decisions}


def _act_note(d):
    """Итог словами владельца. Каждая цифра — про то, что действительно вышло."""
    bits = []
    if d.get("done"):
        bits.append("выполнено %d" % d["done"])
    if d.get("approval"):
        bits.append("%d ждут вашего разрешения" % d["approval"])
    if d.get("already"):
        bits.append("%d уже было в работе" % d["already"])
    if d.get("deny"):
        bits.append("%d не разрешено" % d["deny"])
    if d.get("failed"):
        bits.append("%d не получилось" % d["failed"])
    if d.get("skipped"):
        bits.append("у %d повод уже неактуален" % d["skipped"])
    return ("По этой находке: " + ", ".join(bits) + "."
            if bits else "Действия созданы.")


# ═══ КАК ЭТО ЧИТАЕТСЯ ══════════════════════════════════════════════════════

def _tag(row):
    """Одно слово, по которому владелец понимает, куда смотреть первым."""
    if row.get("level") == WATCH:
        return "Возможность" if row.get("type") == RETURNING_CLIENT else "Наблюдение"
    return {URGENT: "Срочно", HIGH: "Важно"}.get(row.get("priority"), "Стоит знать")


def public(row):
    """Одна находка человеческими словами — так, как она попадёт на экран."""
    if not row:
        return None
    meta = TYPES.get(row.get("type")) or {}
    ev = row.get("evidence") or {}
    impact = row.get("impact")
    return {
        "id": row["id"],
        "type": row["type"],
        "kind_title": meta.get("title") or "",
        "group": meta.get("group") or "",
        "group_ru": GROUP_RU.get(meta.get("group"), ""),
        "tag": _tag(row),
        "level": row.get("level"),
        "level_ru": LEVEL_RU.get(row.get("level"), ""),
        "priority": row.get("priority"),
        "priority_ru": PRIORITY_RU.get(row.get("priority"), ""),
        "confidence": row.get("confidence"),
        "confidence_ru": CONFIDENCE_RU.get(row.get("confidence"), ""),
        "title": row.get("title") or "",
        "summary": row.get("summary") or "",
        "why": row.get("why") or "",
        # Догадка приходит на экран подписанной. Без подписи она читается как
        # вывод, а вывода здесь нет — есть предположение.
        "hypothesis": row.get("hypothesis") or "",
        "hypothesis_note": ("Возможная причина — это догадка, а не факт."
                            if (row.get("hypothesis") or "") else ""),
        "evidence": {"kind": ev.get("kind"), "ids": ev.get("ids") or [],
                     "window": ev.get("window") or "",
                     "numbers": ev.get("numbers") or {}},
        "evidence_ru": _evidence_ru(ev),
        "impact": impact,
        "impact_ru": (_money(impact) if impact else ""),
        # Подпись у суммы разная, и это не косметика. «Потенциальная ценность»
        # — про деньги, которых ещё нет; у вернувшихся клиентов сумма про
        # прошлое, и назвать её потенциальной значило бы пообещать повтор.
        "impact_label": ("Принесли раньше" if row.get("type") == RETURNING_CLIENT
                         else "Потенциальная ценность"),
        "impact_note": row.get("impact_note") or "",
        "action": row.get("action") or "",
        "action_note": row.get("action_note") or "",
        "can_act": bool(row.get("action")),
        "href": row.get("href") or "",
        "status": row.get("status"),
        "status_ru": STATUS_RU.get(row.get("status"), ""),
        "outcome": row.get("outcome") or "",
        "actions_ids": row.get("actions_ids") or [],
        "created_at": row.get("created_at"),
        "last_seen_at": row.get("last_seen_at"),
        "closed_at": row.get("closed_at"),
        "open": row.get("status") in database.INITIATIVE_LIVE,
        "fresh": row.get("status") == database.IN_NEW,
    }


STATUS_RU = {
    database.IN_NEW: "новое",
    database.IN_SEEN: "просмотрено",
    database.IN_ACKNOWLEDGED: "вы это видели",
    database.IN_ACTED: "по ней уже действуем",
    database.IN_DISMISSED: "вы сказали, что не важно",
    database.IN_RESOLVED: "решилось",
    database.IN_EXPIRED: "повод устарел",
}

_EV_KIND_RU = {"lead": "возможность", "followup": "сообщение",
               "client": "клиент", "fact": "запись в памяти",
               "finance": "операция"}

# Когда перечислять нечего (деньги считаются суммой, а не списком), называем
# источник целиком — иначе выходит «считано из раздела «операция»».
_EV_SOURCE_RU = {"lead": "Возможности", "followup": "Подготовленные сообщения",
                 "client": "Клиенты с заказами", "fact": "Память бизнеса",
                 "finance": "Финансовые операции"}


def _evidence_ru(ev):
    """
    Основание словами. Не пересказ данных — адрес, по которому владелец может
    пойти и пересчитать всё сам.
    """
    ids = ev.get("ids") or []
    kind = ev.get("kind")
    if not kind:
        return ""
    if not ids:
        return "%s: %s." % (_EV_SOURCE_RU.get(kind, kind),
                            ev.get("window") or "текущий период")
    word = _EV_KIND_RU.get(kind, kind)
    shown = ", ".join("№%s" % i for i in ids[:5])
    tail = "" if len(ids) <= 5 else " и ещё %d" % (len(ids) - 5)
    return "%s: %s%s. Период: %s." % (
        word.capitalize(), shown, tail, ev.get("window") or "сейчас")


# Порядок на экране — не по времени находки, а по цене вопроса (§: что может
# стоить денег сейчас, потом что приведёт к проблеме, потом что улучшить).
_RANK = {URGENT: 0, HIGH: 1, MEDIUM: 2, LOW: 3}


def _order(row):
    return (0 if row.get("level") == ACT else 1,
            _RANK.get(row.get("priority"), 4),
            -int(row["id"]))


def feed(business_id, *, only_open=True, limit=50):
    """Находки в том порядке, в каком их стоит читать."""
    rows = database.list_initiatives(business_id, live=only_open, limit=limit) \
        if only_open else database.list_initiatives(business_id, limit=limit)
    rows.sort(key=_order)
    return [public(r) for r in rows]


def overview(business_id):
    """Короткая сводка для главной: сколько ждёт взгляда и что важнее всего."""
    try:
        rows = database.list_initiatives(business_id, live=True, limit=50)
    except Exception:
        log.exception("Находки не прочитались (biz %s)", business_id)
        return {"open": 0, "fresh": 0, "act": 0, "top": None}
    rows.sort(key=_order)
    act_rows = [r for r in rows if r.get("level") == ACT]
    return {
        "open": len(rows),
        "fresh": sum(1 for r in rows if r["status"] == database.IN_NEW),
        "act": len(act_rows),
        "top": public(rows[0]) if rows else None,
    }


def brief(business_id, *, limit=4):
    """
    То, что показывается на главной. Не всё найденное, а то, о чём стоит
    говорить: лучше три настоящих находки, чем тридцать средних.
    """
    return feed(business_id, only_open=True, limit=30)[:limit]


def attention(business_id, *, limit=3):
    """
    Строки для блока «Что требует внимания». Только то, что ждёт решения:
    наблюдения туда не попадают — иначе список дел перестанет быть списком дел.
    """
    out = []
    for row in feed(business_id, only_open=True, limit=20):
        if row["level"] != ACT or row["priority"] not in (HIGH, URGENT):
            continue
        note = row["summary"]
        if row["impact_ru"]:
            note = "%s ≈ %s. %s" % (row["impact_label"], row["impact_ru"], note)
        out.append({"title": row["title"], "note": note,
                    "href": "dashboard.html#notice",
                    "level": "urgent" if row["priority"] == URGENT else "warn"})
        if len(out) >= limit:
            break
    return out
