# -*- coding: utf-8 -*-
"""
АВТОНОМНОСТЬ И ЖУРНАЛ ДЕЙСТВИЙ — граница между «ИИ что-то делает» и «ИИ
действует в рамках полномочий, которые ему дал владелец».

До этого слоя вопрос «можно ли?» задавался в четырёх местах на четырёх разных
языках: продавец спрашивал у `sales.policy`, приём материала — у
`intake.may_autoapply`, воронка — у `leads.may_create`, касания — у
`followup.may_autosend`. Каждый отвечал правильно и никто не знал про
остальных, поэтому на вопрос владельца «а что VELOR вообще может делать сам?»
ответа не существовало: его пришлось бы собирать из четырёх экранов, три из
которых он никогда не открывал.

Здесь тот же вопрос задаётся один раз и на одном языке.

Четыре опоры
────────────
1. РЕЕСТР НАЗЫВАЕТ ТО, ЧТО ЕСТЬ. Ни одного действия «на будущее»: если в коде
   нет исполнителя, в реестре нет строки. Список полномочий, обещающий
   несуществующее, хуже отсутствия списка — по нему принимают решения.

2. НОВЫХ ПРАВ НЕ ЗАВОДИТСЯ. Настройка живёт в той же `ai_policy`, что уровень
   и поимённые разрешения, и решение всегда — САМОЕ СТРОГОЕ из того, что уже
   действовало. Слой сверху может только сузить свободу, не расширить: иначе
   обновление продукта однажды разрешило бы то, чего владелец не разрешал.

3. ПОТОЛОК ВЫШЕ ВОЛИ ВЛАДЕЛЬЦА. Есть действия, которые не станут
   автоматическими даже по кнопке «Разрешить всё»: запись денег и переписывание
   знания, за которое поручился человек. Потолок взят не из соображений
   красоты — ровно эти два запрета уже стоят в коде (`understanding.ACTIONS`
   с safe=False и `entities.create_or_update`, возвращающий «blocked»), и
   реестр их называет, а не выдумывает.

4. ЗАПРЕЩЁННОЕ ПИШЕТСЯ НАРАВНЕ С ВЫПОЛНЕННЫМ. Сработавшая защита выглядит
   снаружи ровно как поломка: «ничего не произошло». Разницу между «сломалось»
   и «не разрешено» видно только в журнале, поэтому BLOCKED — полноценная
   запись, а не молчание.

Чего здесь нет
──────────────
Бизнес-логики. Этот модуль спрашивает, разрешено ли, находит исполнителя и
записывает, чем кончилось. Что именно значит «отправить касание», знает
followup.py; что значит «записать в память бизнеса» — understanding.py. Стянуть
их сюда значило бы получить один файл, знающий всё, и четыре, не знающих
ничего.
"""
from __future__ import annotations

import datetime
import logging

import database

log = logging.getLogger("velor.actions")


# ── три положения тумблера ─────────────────────────────────────────────────
# Технических уровней владелец не видит никогда: он выбирает из трёх слов,
# каждое из которых означает понятное ему поведение.

AUTO = "auto"            # делает сам
APPROVAL = "approval"    # готовит и ждёт вашего слова
DENY = "deny"            # не делает

MODES = (DENY, APPROVAL, AUTO)          # порядок = по возрастанию свободы
MODE_RU = {AUTO: "Автоматически", APPROVAL: "Спрашивать разрешение",
           DENY: "Запрещено"}


def _min(*modes):
    """Самое строгое из положений. Свобода — это минимум по всем ограничениям."""
    best = AUTO
    for m in modes:
        if m is None:
            continue
        if MODES.index(m) < MODES.index(best):
            best = m
    return best


# ── риск ───────────────────────────────────────────────────────────────────
# Не для красоты и не для сортировки: риск объясняет владельцу, ПОЧЕМУ у
# действия такой потолок, и почему одно включается кнопкой, а другое нет.

LOW = "low"            # внутреннее: черновик, оценка. Отменяется без следа.
MEDIUM = "medium"      # запись в базу, которую легко исправить.
HIGH = "high"          # уходит наружу или двигает обязательства и деньги.
CRITICAL = "critical"  # необратимое. Сегодня таких у VELOR нет — см. ниже.

RISK_RU = {LOW: "Внутреннее", MEDIUM: "Меняет данные",
           HIGH: "Наружу или про деньги", CRITICAL: "Необратимое"}


# ── реестр ─────────────────────────────────────────────────────────────────
# Каждая строка — то, что VELOR действительно умеет делать сегодня. Полей
# немного, и каждое отвечает на отдельный вопрос владельца:
#
#   title/means  — что это, его словами;
#   risk         — почему у этого такой потолок;
#   default      — как ведёт себя, пока владелец ничего не менял;
#   ceiling      — выше этого не поднимется НИКОГДА, включая «Разрешить всё»;
#   choices      — какие положения у этого действия вообще существуют;
#   permission   — какое уже существующее право (sales) им управляет;
#   runner       — какой модуль умеет его выполнить по подтверждению.
#
# «choices» — не украшение. У ответа клиенту нет положения «спросить»: живой
# разговор нельзя поставить на паузу до вечера, и предлагать это владельцу
# значило бы обещать очередь, которой не существует. Лучше честные два
# положения, чем красивые три.

GROUPS = [("sales", "Продажи"), ("data", "Данные"),
          ("ops", "Операции"), ("money", "Финансы")]

REGISTRY = {
    # ── продажи ────────────────────────────────────────────────────────────
    "reply_to_customer": {
        "title": "Отвечать клиентам",
        "means": "Сам ведёт разговор в Telegram и директе. Цены и условия берёт "
                 "только из памяти бизнеса; чего там нет — зовёт вас. "
                 "Запретить — значит, что все разговоры ведёте вы.",
        "group": "sales", "risk": HIGH, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, DENY), "permission": "answer_faq", "runner": None},
    "qualify_lead": {
        "title": "Оценивать возможности",
        "means": "Считает, насколько человек близок к покупке и кому отвечать "
                 "первым. Ничего никому не пишет и не создаёт.",
        "group": "sales", "risk": LOW, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, DENY), "permission": None, "runner": None},
    "create_lead": {
        "title": "Заводить возможности",
        "means": "Из разговора, в котором слышно намерение купить, создаёт "
                 "карточку возможности. Сообщения при этом никуда не переносятся.",
        "group": "sales", "risk": MEDIUM, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, DENY), "permission": "create_lead", "runner": None},
    "update_lead": {
        "title": "Дополнять возможности",
        "means": "Дописывает в карточку то, что клиент сказал позже: срок, "
                 "количество, сумму. То, что вы правили руками, не трогает.",
        "group": "sales", "risk": MEDIUM, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, DENY), "permission": "create_lead", "runner": None},
    "mark_lead_won": {
        "title": "Закрывать возможность сделкой",
        "means": "Когда по возможности появилась заявка, помечает её выигранной "
                 "и связывает с заявкой. Сам заявок не выдумывает.",
        "group": "sales", "risk": MEDIUM, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, DENY), "permission": None, "runner": None},
    "prepare_followup": {
        "title": "Готовить напоминания",
        "means": "Замечает, что разговор оборвался, и пишет черновик касания. "
                 "Черновик никуда не уходит без отдельного разрешения ниже.",
        "group": "sales", "risk": LOW, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, DENY), "permission": None, "runner": "followup"},
    "send_followup": {
        "title": "Писать клиенту первым",
        "means": "Отправляет подготовленное касание сам — не чаще трёх раз на "
                 "возможность и только в ваши рабочие часы.",
        "group": "sales", "risk": HIGH, "default": APPROVAL, "ceiling": AUTO,
        "choices": (AUTO, APPROVAL, DENY), "permission": "follow_up",
        "runner": "followup"},

    # ── данные ─────────────────────────────────────────────────────────────
    "create_client": {
        "title": "Заводить клиентов",
        "means": "Создаёт карточку человека из переписки или из присланного "
                 "материала.",
        "group": "data", "risk": MEDIUM, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, APPROVAL, DENY), "permission": "collect_customer",
        "runner": "understanding"},
    "update_client": {
        "title": "Записывать контакты",
        "means": "Дописывает имя и телефон, которые человек назвал сам. "
                 "Проверенный вами телефон не затирает — второй уходит в заметку.",
        "group": "data", "risk": MEDIUM, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, DENY), "permission": "collect_customer", "runner": None},
    "save_knowledge": {
        "title": "Пополнять память бизнеса",
        "means": "Добавляет услуги, товары, цены, правила работы и сведения о "
                 "компании из того, что вы прислали.",
        "group": "data", "risk": MEDIUM, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, APPROVAL, DENY), "permission": None,
        "runner": "understanding"},
    # Потолок APPROVAL взят не с потолка: `entities.create_or_update` уже
    # отказывается переписывать запись, которую подтвердил человек. Здесь это
    # правило просто названо вслух и распространено на кнопку «Разрешить всё».
    "update_knowledge": {
        "title": "Изменять подтверждённые данные",
        "means": "Переписывает то, за что вы уже поручились. Сам этого не делает "
                 "никогда — только показывает, что предлагает изменить.",
        "group": "data", "risk": HIGH, "default": APPROVAL, "ceiling": APPROVAL,
        "choices": (APPROVAL, DENY), "permission": None, "runner": "understanding"},

    # ── операции ───────────────────────────────────────────────────────────
    "create_order": {
        "title": "Оформлять заявки",
        "means": "Создаёт заявку, когда с клиентом всё оговорено. Сумму берёт "
                 "из каталога, свою не придумывает.",
        "group": "ops", "risk": HIGH, "default": AUTO, "ceiling": AUTO,
        "choices": (AUTO, APPROVAL, DENY), "permission": "create_order",
        "runner": "understanding"},
    "add_goal": {
        "title": "Ставить цели",
        "means": "Заводит цель бизнеса из того, что вы написали или прислали.",
        "group": "ops", "risk": MEDIUM, "default": APPROVAL, "ceiling": AUTO,
        "choices": (AUTO, APPROVAL, DENY), "permission": None,
        "runner": "understanding"},

    # ── финансы ────────────────────────────────────────────────────────────
    # Потолок APPROVAL — то же самое правило, что уже стоит в
    # `understanding.ACTIONS`: create_expense и create_income помечены
    # safe=False, то есть VELOR не выполняет их сам ни при какой уверенности.
    # Деньги считает человек. Кнопка «Разрешить всё» этого не меняет.
    "record_finance": {
        "title": "Записывать расходы и доходы",
        "means": "Заносит сумму из чека или выписки в финансы. Сам не заносит "
                 "никогда — показывает разобранное и ждёт вашего подтверждения.",
        "group": "money", "risk": HIGH, "default": APPROVAL, "ceiling": APPROVAL,
        "choices": (APPROVAL, DENY), "permission": None,
        "runner": "understanding"},
}

# Действий с потолком CRITICAL сегодня нет, и придумывать их нечестно: VELOR не
# двигает деньги, не удаляет данные, не меняет цены и не рассылает ничего
# массово. Уровень описан и проверяется — если такое действие появится, ему
# будет куда встать; пустым он остаётся потому, что таких возможностей нет.

# Ключ действия по имени права продавца — чтобы две страницы кабинета не
# отвечали на один вопрос по-разному.
ACTION_BY_PERMISSION = {v["permission"]: k for k, v in REGISTRY.items()
                        if v.get("permission")}

# Что случилось помимо работы: владелец поменял настройку. В реестр это не
# входит — реестр про то, что делает VELOR, а не про то, что делают с VELOR, —
# но в журнале должно быть видно наравне.
SETTING_CHANGED = "autonomy_changed"
SETTING_TITLES = {SETTING_CHANGED: "Изменена автономность VELOR"}

# Строка политики, в которой живут положения тумблеров. Отдельная от каналов:
# «может ли VELOR писать клиенту первым» — одно решение владельца, а не два
# разных для Telegram и директа. Уровень и поимённые права при этом остаются у
# каждого канала своими, как были.
ALL = "*"


# ── как VELOR действовал ───────────────────────────────────────────────────
AUTOMATIC = "automatic"          # сам, в рамках разрешённого
APPROVED_BY_OWNER = "approved"   # владелец разрешил именно это
MANUAL = "manual"                # сделал человек

MODE_ON_RECORD_RU = {AUTOMATIC: "автоматически", APPROVED_BY_OWNER: "с вашего разрешения",
                     MANUAL: "вручную"}

ACTOR_RU = {"velor": "VELOR", "owner": "Владелец", "system": "Система"}

STATUS_RU = {
    database.AC_PROPOSED: "ждёт вашего решения",
    database.AC_APPROVED: "разрешено",
    database.AC_RUNNING: "выполняется",
    database.AC_SUCCEEDED: "выполнено",
    database.AC_FAILED: "не получилось",
    database.AC_BLOCKED: "заблокировано",
    database.AC_CANCELLED: "отклонено",
    database.AC_STALE: "устарело",
}

# Сколько живёт предложение, если ему не назначили собственного срока. Не
# «магическая цифра ради цифры»: подтверждать позавчерашнее предложение
# бессмысленно — обстоятельства, из-за которых оно появилось, к тому времени
# успевают смениться, и проверка актуальности всё равно его отклонит.
DEFAULT_TTL_DAYS = 7


class ActionError(Exception):
    """Действие выполнить нельзя, и человеку сказано почему."""


def _now():
    return datetime.datetime.now()


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(when):
    try:
        return datetime.datetime.strptime(str(when)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


# ── исполнители ────────────────────────────────────────────────────────────
# Модуль, который умеет делать работу, сам говорит об этом — а не наоборот.
# Так бизнес-логика остаётся там, где живёт, и этот файл не превращается в
# один большой знающий всё.

RUNNERS = {}
CHECKERS = {}


def runner(action_id):
    """Объявить исполнителя действия. Возвращает (ok, текст, id, ошибка)."""
    def wrap(fn):
        RUNNERS[action_id] = fn
        return fn
    return wrap


def checker(action_id):
    """Объявить проверку актуальности. Возвращает (актуально, почему нет)."""
    def wrap(fn):
        CHECKERS[action_id] = fn
        return fn
    return wrap


def _load(action_id):
    """Подтянуть модуль-исполнитель. Импорт ленивый — иначе кольцо импортов."""
    if action_id in RUNNERS:
        return RUNNERS[action_id]
    mod = (REGISTRY.get(action_id) or {}).get("runner")
    if not mod:
        return None
    try:
        __import__(mod)
    except Exception:
        log.exception("Модуль %s не загрузился — действие %s выполнить некому",
                      mod, action_id)
        return None
    return RUNNERS.get(action_id)


# ── настройка владельца ────────────────────────────────────────────────────

def raw_modes(business_id) -> dict:
    """Что владелец выставил своей рукой. Пусто — значит, не трогал."""
    try:
        row = database.ai_policy(business_id, ALL) or {}
    except Exception:
        log.exception("Настройка автономности не прочиталась (biz %s)", business_id)
        return {}
    out = {}
    for key, val in (row.get("modes") or {}).items():
        if key in REGISTRY and val in MODES:
            out[key] = val
    return out


# Каналы, по которым VELOR разговаривает с клиентами. Список нужен, чтобы
# спросить «а не разрешено ли это уже где-нибудь», и держится здесь, а не
# перебирается по базе: канал, которого нет в коде, всё равно ничего не умеет.
CHANNELS = ("telegram", "vk", "instagram")


def _granted_anywhere(business_id) -> set:
    """
    Права продавца, которые уже действуют хотя бы в одном канале.

    Читаем разом: этот вопрос задаётся на каждое сообщение клиента, и десять
    отдельных походов в базу за одним и тем же ответом — цена, которую платил
    бы каждый разговор.
    """
    try:
        import sales
        out = set()
        for channel in CHANNELS:
            row = database.ai_policy(business_id, channel) or {}
            level = int(row.get("level") or sales.DEFAULT_LEVEL)
            out |= set(row.get("grants") or [])
            out |= set((sales.LEVELS.get(level) or {}).get("grants") or ())
        return out
    except Exception:
        log.exception("Права каналов не прочитались (biz %s)", business_id)
        return set()


def _permission_default(meta, granted):
    """
    Как действие ведёт себя, пока владелец не высказался.

    Умолчание реестра — не единственный источник: если право уже выдано
    существующей настройкой канала, значит, владелец УЖЕ разрешил это раньше,
    просто на другой странице. Начать с «запрещено» здесь означало бы молча
    отобрать разрешение при обновлении продукта.
    """
    perm = meta.get("permission")
    if not perm:
        return meta["default"]
    if perm in granted:
        return AUTO
    # Права нет ни в одном канале — значит, само по себе оно не действует.
    # Для действий с очередью это «спрашивать», для остальных «запрещено»:
    # обещать подтверждение там, где его негде нажать, нельзя.
    if meta["default"] != AUTO:
        return meta["default"]
    return APPROVAL if APPROVAL in meta["choices"] else DENY


def modes(business_id) -> dict:
    """Итоговое положение каждого тумблера: воля владельца или умолчание."""
    said = raw_modes(business_id)
    granted = _granted_anywhere(business_id)
    out = {}
    for action_id, meta in REGISTRY.items():
        mode = said.get(action_id) or _permission_default(meta, granted)
        if mode not in meta["choices"]:
            mode = _min(mode, meta["ceiling"])
            if mode not in meta["choices"]:
                mode = meta["choices"][-1] if meta["choices"] else DENY
        out[action_id] = _min(mode, meta["ceiling"])
    return out


def muted(business_id) -> set:
    """
    Права продавца, которые владелец приглушил на странице автономности.

    Существует ровно для того, чтобы страница директа и страница автономности
    не отвечали на один вопрос по-разному: приглушённое право перестаёт быть
    разрешённым везде, а не только там, где его выключили.

    Считается ТОЛЬКО по тому, что владелец сказал своей рукой. Умолчания сюда
    не входят и входить не могут: умолчание вычисляется из этих же прав, и
    подмешать его обратно значило бы получить круг, в котором право отбирает
    само себя.
    """
    out = set()
    try:
        said = raw_modes(business_id)
    except Exception:
        log.exception("Автономность не прочиталась (biz %s)", business_id)
        return out
    for action_id, mode in said.items():
        perm = (REGISTRY.get(action_id) or {}).get("permission")
        if perm and mode != AUTO:
            out.add(perm)
    return out


def _channel_gate(business_id, action_id, channel, meta):
    """
    Что об этом действии думает КАНАЛ.

    Тумблер на странице автономности один на весь бизнес — «может ли VELOR
    писать клиенту первым» это одно решение владельца, а не два разных для
    Telegram и директа. Но уровень доверия каналу так и остался у каждого
    канала свой, и он старше: поставив Telegram «только отвечает», владелец
    пообещал себе, что там ничего не создаётся. Общий тумблер этого обещания
    отменять не вправе.

    Не прочиталось — считаем, что нельзя. Несостоявшееся действие видно в
    кабинете, самовольное — уже нет.
    """
    perm = meta.get("permission")
    if not perm:
        return AUTO
    try:
        import sales
        pol = sales.policy(business_id, channel or "telegram")
        if perm in set(pol.get("allowed") or ()):
            return AUTO
    except Exception:
        log.exception("Права канала %s не прочитались (biz %s)", channel, business_id)
        return DENY
    # Права у канала нет. Для действий с очередью это «спрашивать», для
    # остальных — «нельзя»: обещать подтверждение там, где его негде нажать,
    # значит обещать несуществующее.
    return APPROVAL if APPROVAL in meta["choices"] else DENY


def set_mode(business_id, action_id, mode, *, actor="owner", actor_id=None):
    """
    Изменить одно положение. Само изменение — тоже событие журнала.

    Отдельная запись нужна не для порядка: если завтра VELOR напишет клиенту
    сам, владелец должен видеть, что он это разрешил, когда именно и что было
    до того. Без этого спор «я такого не включал» неразрешим.
    """
    meta = REGISTRY.get(action_id)
    if not meta:
        raise ActionError("Такого действия у VELOR нет.")
    if mode not in MODES:
        raise ActionError("Неизвестный режим.")
    if mode not in meta["choices"]:
        raise ActionError("У этого действия такого режима не бывает: "
                          + MODE_RU.get(mode, mode))
    want = _min(mode, meta["ceiling"])
    before = modes(business_id)
    if before.get(action_id) == want and action_id in raw_modes(business_id):
        return before

    said = dict(raw_modes(business_id))
    said[action_id] = want
    database.save_ai_policy(business_id, ALL, modes=said, actor=actor)
    _sync_permission(business_id, action_id, want, actor=actor)

    after = modes(business_id)
    _record_setting(business_id, {action_id: (before.get(action_id), after.get(action_id))},
                    actor=actor, actor_id=actor_id)
    return after


def _sync_permission(business_id, action_id, mode, *, actor="owner"):
    """
    Разрешил на этой странице — разрешено и в канале. Но только опасное.

    Опасные права (писать первым, скидки, возвраты) не входят ни в один уровень
    автономии: они включаются поимённо, и этот тумблер — единственное место, где
    их включают. Не выдать право здесь значило бы сделать тумблер декоративным.

    Всем остальным распоряжается УРОВЕНЬ канала, и трогать его отсюда нельзя.
    Владелец, поставивший Telegram «только отвечает», пообещал себе, что там
    ничего не создаётся; общий тумблер, тихо выдающий право в обход уровня,
    отменял бы это обещание — и владелец узнал бы об этом, увидев запись,
    которую не заказывал. Слой полномочий сужает свободу, а не расширяет.
    """
    perm = (REGISTRY.get(action_id) or {}).get("permission")
    if not perm or mode != AUTO:
        return
    try:
        import sales
        if not (sales.PERMISSIONS.get(perm) or {}).get("danger"):
            return
        for channel in CHANNELS:
            pol = database.ai_policy(business_id, channel) or {}
            grants = set(pol.get("grants") or [])
            if perm in grants:
                continue
            grants.add(perm)
            database.save_ai_policy(business_id, channel,
                                    grants=sorted(grants), actor=actor)
    except Exception:
        log.exception("Право %s не выдалось каналам (biz %s)", perm, business_id)


def sync_from_permissions(business_id, changed):
    """
    Включили право на странице канала — снимаем приглушение здесь.

    Обратная сторона `muted`. Без неё владелец включил бы «Писать клиенту
    первым» в директе, а автономность продолжала бы держать его выключенным, и
    настройка молча не работала бы.
    """
    said = dict(raw_modes(business_id))
    touched = False
    for perm in changed or ():
        action_id = ACTION_BY_PERMISSION.get(perm)
        if action_id and said.get(action_id) not in (None, AUTO):
            said[action_id] = AUTO
            touched = True
    if touched:
        database.save_ai_policy(business_id, ALL, modes=said, actor="owner")


def set_all(business_id, mode, *, actor="owner", actor_id=None):
    """
    «Разрешить всё» и «Запретить всё».

    «Разрешить всё» не означает «игнорировать потолок»: каждое действие
    поднимается настолько, насколько ему вообще позволено, и запись денег
    остаётся на подтверждении, сколько бы раз кнопку ни нажали. Иначе одна
    кнопка отменяла бы все продуманные ограничения разом — а именно так и
    случаются неприятности, о которых потом никто не помнит, что разрешал.
    """
    if mode not in MODES:
        raise ActionError("Неизвестный режим.")
    before = modes(business_id)
    said = dict(raw_modes(business_id))
    for action_id, meta in REGISTRY.items():
        want = _min(mode, meta["ceiling"])
        if want not in meta["choices"]:
            # Просят «спрашивать» там, где спрашивать негде, — оставляем самое
            # строгое из возможного, а не самое близкое по звучанию.
            want = DENY if DENY in meta["choices"] else meta["choices"][-1]
        said[action_id] = want
    database.save_ai_policy(business_id, ALL, modes=said, actor=actor)
    for action_id, want in said.items():
        _sync_permission(business_id, action_id, want, actor=actor)

    after = modes(business_id)
    diff = {a: (before.get(a), after.get(a)) for a in REGISTRY
            if before.get(a) != after.get(a)}
    _record_setting(business_id, diff, actor=actor, actor_id=actor_id, bulk=mode)
    return after


def _record_setting(business_id, diff, *, actor="owner", actor_id=None, bulk=None):
    """Изменение настройки — запись в журнале, с тем, что было и что стало."""
    if not diff and not bulk:
        return
    if bulk == AUTO:
        said = ("Разрешена максимальная самостоятельность — насколько позволяют "
                "ограничения безопасности")
    elif bulk == DENY:
        said = "Самостоятельность VELOR отключена полностью"
    elif bulk == APPROVAL:
        said = "VELOR будет спрашивать разрешение перед действиями"
    else:
        one = next(iter(diff)) if len(diff) == 1 else None
        said = ("«%s»: %s → %s" % (REGISTRY[one]["title"],
                                   MODE_RU.get(diff[one][0], "—"),
                                   MODE_RU.get(diff[one][1], "—"))
                if one else "Изменено действий: %d" % len(diff))
    try:
        database.add_action(
            business_id, SETTING_CHANGED, actor=actor, actor_id=actor_id,
            mode=MANUAL, status=database.AC_SUCCEEDED, reason=said,
            before={a: v[0] for a, v in diff.items()},
            after={a: v[1] for a, v in diff.items()},
            result=said)
    except Exception:
        log.exception("Изменение автономности не записалось (biz %s)", business_id)
    try:
        database.log_event(business_id, "settings", "Автономность VELOR изменена",
                           said[:400], level="important")
    except Exception:
        pass


# ── единственная проверка полномочий ───────────────────────────────────────

def can_execute(business_id, action_id, *, channel=None, limit=None,
                actor="velor") -> dict:
    """
    Можно ли VELOR сделать это прямо сейчас. Один ответ на весь продукт.

    Возвращает {"decision": auto|approval|deny, "why": …}. Решение — САМОЕ
    СТРОГОЕ из четырёх ограничений, и порядок между ними неважен именно
    потому, что берётся минимум: ни одно из них не может ослабить другое.

      1. потолок действия     — выше не поднимается никогда;
      2. воля владельца       — то, что он выставил на странице автономности;
      3. уровень доверия каналу — то, что уже обещано в настройках канала;
      4. потолок вызывающего  — то, что вызывающий знает про своё действие;
      5. состояние подписки   — в read-only не меняем ничего.

    Дублировать эту логику по модулям нельзя: расхождение между двумя копиями
    проверки прав — это не разные ответы, а дыра.
    """
    meta = REGISTRY.get(action_id)
    if not meta:
        return {"decision": DENY, "why": "Такого действия у VELOR нет.",
                "mode": DENY, "ceiling": DENY}

    owner_mode = modes(business_id).get(action_id, meta["default"])
    gate = _channel_gate(business_id, action_id, channel, meta)
    decision = _min(owner_mode, meta["ceiling"], limit, gate)

    why = ""
    if decision == DENY:
        why = ("Вы запретили VELOR это действие." if owner_mode == DENY
               else "Каналу это действие не разрешено.")
    elif decision == APPROVAL:
        if meta["ceiling"] == APPROVAL:
            why = "Такое VELOR не делает сам — нужно ваше подтверждение."
        elif owner_mode == APPROVAL:
            why = "Вы попросили спрашивать перед этим действием."
        else:
            why = "Каналу это действие пока не разрешено делать самому."

    # Триал закончился — данные целы, но менять их нельзя. То же правило, что
    # в кабинете и в приёме материала, и формулировка та же.
    if decision != DENY:
        try:
            import trial
            business = database.get_business(business_id) or {}
            if trial.access(business)["read_only"]:
                decision, why = DENY, "Пробный период завершён — VELOR ничего не меняет."
        except Exception:
            log.exception("Состояние подписки не прочиталось (biz %s)", business_id)
            decision, why = DENY, "Состояние подписки неизвестно."

    return {"decision": decision, "why": why, "mode": owner_mode,
            "ceiling": meta["ceiling"], "action": action_id,
            "channel": channel or None}


def decide(business_id, action_id, **kw) -> str:
    """Короткая форма: только решение. Для мест, где причина не нужна."""
    return can_execute(business_id, action_id, **kw)["decision"]


def allowed_auto(business_id, action_id, **kw) -> bool:
    """Можно ли сделать это САМОМУ. Ровно тот вопрос, который задавали раньше."""
    return decide(business_id, action_id, **kw) == AUTO


# ── актуальность ───────────────────────────────────────────────────────────

def fresh(business_id, row) -> tuple:
    """
    Не устарело ли предложение, пока оно ждало решения. (актуально, почему нет).

    Час назад VELOR предложил написать клиенту. За этот час клиент мог ответить
    сам, владелец мог передумать, право могли отобрать. Выполнить старое
    решение по старым основаниям — самый простой способ сделать глупость с
    формально правильным журналом.
    """
    action_id = row.get("action")
    meta = REGISTRY.get(action_id)
    if not meta:
        return False, "Такого действия у VELOR больше нет."

    due = _parse(row.get("expires_at"))
    if due and _now() > due:
        return False, "Срок предложения истёк — обстоятельства уже другие."

    verdict = can_execute(business_id, action_id, channel=row.get("channel"))
    if verdict["decision"] == DENY:
        return False, verdict["why"] or "Действие больше не разрешено."

    check = CHECKERS.get(action_id) or (_load(action_id) and CHECKERS.get(action_id))
    if check:
        try:
            ok, why = check(business_id, row)
        except Exception:
            log.exception("Проверка актуальности сорвалась (biz %s, action %s)",
                          business_id, row.get("id"))
            return False, "Не удалось проверить, актуально ли это ещё."
        if not ok:
            return False, why or "Обстоятельства изменились."
    return True, ""


# ── исполнение ─────────────────────────────────────────────────────────────

def _dedupe(business_id, key, mine_id):
    """
    Один повод — одно предложение.

    Проверяем ПОСЛЕ вставки, а не до: между «посмотрел, ничего нет» и «записал»
    есть щель, и два обхода подряд успевают проскочить в неё оба. Сравнение по
    id разрешает спор одинаково у обоих — выигрывает тот, кто записался первым,
    и второй убирает за собой сам.
    """
    if not key:
        return True
    older = database.find_live_action(business_id, key)
    if older and int(older["id"]) < int(mine_id):
        database.update_action(mine_id, business_id, status=database.AC_CANCELLED,
                               result="Такое предложение уже есть.",
                               decided_at=database.now())
        return False
    return True


def execute(business_id, action_id, *, channel=None, target_type=None,
            target_id=None, reason="", based_on=None, payload=None, before=None,
            dedupe_key=None, expires_at=None, limit=None, actor="velor",
            actor_id=None, run=None):
    """
    Единственный путь от намерения до записи в журнале.

        намерение → реестр → полномочия → авто / подтверждение / отказ
                  → исполнитель → результат → журнал

    Возвращает {"decision", "id", "ok", "row", "why"}. Ничего не бросает:
    сорвавшееся действие — это запись FAILED, а не падение вызывающего.
    """
    meta = REGISTRY.get(action_id)
    if not meta:
        raise ActionError("Такого действия у VELOR нет.")
    verdict = can_execute(business_id, action_id, channel=channel, limit=limit)
    decision = verdict["decision"]

    if decision == DENY:
        aid = database.add_action(
            business_id, action_id, actor=actor, actor_id=actor_id,
            mode=AUTOMATIC, status=database.AC_BLOCKED, channel=channel,
            target_type=target_type, target_id=target_id, reason=reason,
            based_on=based_on, payload=payload, before=before,
            error=verdict["why"], dedupe_key=dedupe_key)
        return {"decision": DENY, "id": aid, "ok": False, "why": verdict["why"],
                "row": database.get_action(aid, business_id)}

    if not expires_at:
        expires_at = _stamp(_now() + datetime.timedelta(days=DEFAULT_TTL_DAYS))

    if decision == APPROVAL:
        aid = database.add_action(
            business_id, action_id, actor=actor, actor_id=actor_id,
            mode=APPROVED_BY_OWNER, status=database.AC_PROPOSED, channel=channel,
            target_type=target_type, target_id=target_id, reason=reason,
            based_on=based_on, payload=payload, before=before,
            dedupe_key=dedupe_key, expires_at=expires_at)
        if not _dedupe(business_id, dedupe_key, aid):
            return {"decision": APPROVAL, "id": aid, "ok": False,
                    "why": "Такое предложение уже ждёт вашего решения.",
                    "row": database.get_action(aid, business_id)}
        return {"decision": APPROVAL, "id": aid, "ok": False, "why": verdict["why"],
                "row": database.get_action(aid, business_id)}

    aid = database.add_action(
        business_id, action_id, actor=actor, actor_id=actor_id, mode=AUTOMATIC,
        status=database.AC_PROPOSED, channel=channel, target_type=target_type,
        target_id=target_id, reason=reason, based_on=based_on, payload=payload,
        before=before, dedupe_key=dedupe_key, expires_at=expires_at)
    if not _dedupe(business_id, dedupe_key, aid):
        return {"decision": AUTO, "id": aid, "ok": False,
                "why": "Такое действие уже выполняется.",
                "row": database.get_action(aid, business_id)}
    return _run(business_id, aid, run=run)


def _run(business_id, action_id_row, *, run=None):
    """Захватить и выполнить. Захват атомарный: выиграть его может только один."""
    row = database.get_action(action_id_row, business_id)
    if not row:
        raise ActionError("Действие не найдено.")
    if row["status"] in (database.AC_SUCCEEDED, database.AC_RUNNING):
        return {"decision": AUTO, "id": row["id"], "ok": False,
                "why": "Уже выполняется.", "row": row}
    if not database.claim_action(row["id"], business_id):
        return {"decision": AUTO, "id": row["id"], "ok": False,
                "why": "Действие уже взял другой процесс.",
                "row": database.get_action(row["id"], business_id)}

    fn = run or _load(row["action"])
    if not fn:
        database.finish_action(row["id"], business_id, database.AC_FAILED,
                               error="Выполнить это действие сейчас некому.")
        return {"decision": AUTO, "id": row["id"], "ok": False,
                "why": "Выполнить это действие сейчас некому.",
                "row": database.get_action(row["id"], business_id)}
    try:
        got = fn(business_id, row) or {}
    except Exception as e:
        log.exception("Действие %s сорвалось (biz %s)", row["action"], business_id)
        database.finish_action(row["id"], business_id, database.AC_FAILED,
                               error=str(e) or "Не получилось выполнить.")
        return {"decision": AUTO, "id": row["id"], "ok": False, "why": str(e),
                "row": database.get_action(row["id"], business_id)}

    ok = bool(got.get("ok"))
    if not ok and got.get("retry"):
        # Не вышло, но работа не кончена: канал не ответил, а попытки ещё
        # остались. Закрыть запись «не получилось» значило бы убрать её с
        # глаз владельца в тот момент, когда она всё ещё живёт и будет
        # повторена, — и он узнал бы об отправке из переписки, а не отсюда.
        database.update_action(row["id"], business_id,
                               status=database.AC_PROPOSED,
                               error=(got.get("error") or "")[:400] or None)
        return {"decision": AUTO, "id": row["id"], "ok": False,
                "why": got.get("error") or "",
                "row": database.get_action(row["id"], business_id)}
    # Молчание исполнителя — это «не знаем», а «не знаем» здесь считается
    # неудачей. Записать успех на непроверенном результате значит однажды
    # показать владельцу «отправлено» там, где ничего не ушло.
    database.finish_action(
        row["id"], business_id,
        database.AC_SUCCEEDED if ok else (got.get("status") or database.AC_FAILED),
        result=got.get("result"), error=None if ok else got.get("error"),
        after=got.get("after"), target_id=got.get("target_id"))
    return {"decision": AUTO, "id": row["id"], "ok": ok,
            "why": "" if ok else (got.get("error") or ""),
            "row": database.get_action(row["id"], business_id)}


def record(business_id, action_id, *, status=database.AC_SUCCEEDED, actor="velor",
           actor_id=None, mode=AUTOMATIC, channel=None, target_type=None,
           target_id=None, reason="", based_on=None, before=None, after=None,
           result=None, error=None):
    """
    Записать то, что уже случилось.

    Для действий, у которых есть собственная машинерия и собственная очередь
    (касания, разбор входящих): пропускать их через исполнитель заново значило
    бы выполнить работу дважды. Полномочия у них спрошены на месте — здесь
    только след в общем журнале.
    """
    try:
        return database.add_action(
            business_id, action_id, actor=actor, actor_id=actor_id, mode=mode,
            status=status, channel=channel, target_type=target_type,
            target_id=target_id, reason=reason, based_on=based_on, before=before,
            after=after, result=result, error=error)
    except Exception:
        log.exception("Действие %s не записалось в журнал (biz %s)",
                      action_id, business_id)
        return None


# ── решения владельца ──────────────────────────────────────────────────────

def approve(business_id, action_id, *, actor="owner", actor_id=None, payload=None):
    """
    Владелец разрешил ЭТО действие. Не такие действия вообще — именно это.

    Постоянное разрешение живёт на странице автономности и включается там же.
    Смешивать одно с другим нельзя: нажатие «Разрешить» под конкретным
    сообщением не должно однажды означать «пиши всем клиентам всегда».
    """
    row = database.get_action(action_id, business_id)
    if not row:
        raise ActionError("Действие не найдено.")
    if row["status"] != database.AC_PROPOSED:
        return row
    ok, why = fresh(business_id, row)
    if not ok:
        database.update_action(action_id, business_id, status=database.AC_STALE,
                               error=why, decided_at=database.now())
        return database.get_action(action_id, business_id)
    fields = {"status": database.AC_APPROVED, "mode": APPROVED_BY_OWNER,
              "decided_at": database.now()}
    if payload is not None:
        fields["payload"] = dict(row.get("payload") or {}, **payload)
    database.update_action(action_id, business_id, **fields)
    got = _run(business_id, action_id)
    return got["row"]


def reject(business_id, action_id, *, actor="owner", actor_id=None):
    """Владелец отказал. Причина отказа — сам отказ, и он тоже в журнале."""
    row = database.get_action(action_id, business_id)
    if not row:
        raise ActionError("Действие не найдено.")
    if row["status"] not in (database.AC_PROPOSED, database.AC_APPROVED):
        return row
    database.update_action(action_id, business_id, status=database.AC_CANCELLED,
                           result="Вы отклонили это действие.",
                           decided_at=database.now())
    return database.get_action(action_id, business_id)


def cancel(business_id, action_id, *, reason="Повод исчез."):
    """Отменить предложение изнутри: обстоятельства изменились."""
    row = database.get_action(action_id, business_id)
    if not row or row["status"] not in database.ACTION_LIVE:
        return row
    database.update_action(action_id, business_id, status=database.AC_CANCELLED,
                           result=reason, decided_at=database.now())
    return database.get_action(action_id, business_id)


def settle(business_id, *, now=None):
    """
    Прибрать просроченное. Устаревшее предложение не выполняется никогда.

    Отдельным проходом, а не при показе: иначе просроченное висело бы в очереди
    ровно до того момента, когда кто-нибудь на него нажмёт, — и тогда бы
    выяснилось, что оно уже неактуально.
    """
    now = now or _now()
    gone = 0
    for row in database.list_actions(business_id, waiting=True, limit=200):
        due = _parse(row.get("expires_at"))
        if due and now > due:
            database.update_action(row["id"], business_id, status=database.AC_STALE,
                                   error="Срок предложения истёк.",
                                   decided_at=_stamp(now))
            gone += 1
    return gone


# ── как это читается ───────────────────────────────────────────────────────

def title_of(row):
    meta = REGISTRY.get(row.get("action"))
    if meta:
        return meta["title"]
    return SETTING_TITLES.get(row.get("action"), row.get("action") or "Действие")


def public(row):
    """Одно действие человеческими словами — так, как оно попадёт на экран."""
    if not row:
        return None
    meta = REGISTRY.get(row.get("action")) or {}
    return {
        "id": row["id"],
        "action": row["action"],
        "title": title_of(row),
        "means": meta.get("means") or "",
        "group": meta.get("group") or "system",
        "risk": meta.get("risk"),
        "actor": row.get("actor"),
        "actor_ru": ACTOR_RU.get(row.get("actor"), row.get("actor") or ""),
        "mode": row.get("mode"),
        "mode_ru": MODE_ON_RECORD_RU.get(row.get("mode"), ""),
        "status": row.get("status"),
        "status_ru": STATUS_RU.get(row.get("status"), row.get("status") or ""),
        "channel": row.get("channel"),
        "target_type": row.get("target_type"),
        "target_id": row.get("target_id"),
        "reason": row.get("reason") or "",
        "based_on": row.get("based_on") or {},
        "before": row.get("before") or {},
        "after": row.get("after") or {},
        "result": row.get("result") or "",
        "error": row.get("error") or "",
        "created_at": row.get("created_at"),
        "done_at": row.get("done_at"),
        "waiting": row.get("status") == database.AC_PROPOSED,
        "editable": bool((row.get("payload") or {}).get("message")),
        "message": (row.get("payload") or {}).get("message") or "",
    }


FILTERS = {
    "all": None,
    "auto": None,          # разбирается ниже: фильтр по mode, а не по статусу
    "approved": None,
    "manual": None,
    "done": (database.AC_SUCCEEDED,),
    "blocked": (database.AC_BLOCKED, database.AC_STALE),
    "failed": (database.AC_FAILED,),
    "waiting": (database.AC_PROPOSED,),
}

FILTER_RU = {"all": "Все", "auto": "Автоматически", "approved": "С подтверждением",
             "manual": "Вручную", "done": "Успешные", "blocked": "Заблокированные",
             "failed": "Ошибки", "waiting": "Ждут решения"}


def feed(business_id, *, kind="all", limit=80):
    """Журнал: что VELOR делал, чего ему не дали и что не получилось."""
    kind = kind if kind in FILTERS else "all"
    status = FILTERS.get(kind)
    rows = database.list_actions(business_id, status=status,
                                limit=limit if kind in ("all", "auto", "approved",
                                                        "manual") else limit)
    if kind in ("auto", "approved", "manual"):
        want = {"auto": AUTOMATIC, "approved": APPROVED_BY_OWNER,
                "manual": MANUAL}[kind]
        rows = [r for r in rows if r.get("mode") == want][:limit]
    return [public(r) for r in rows]


def pending(business_id, *, limit=50):
    """Очередь подтверждений. Это не отдельная система — это те же записи."""
    return [public(r) for r in
            database.list_actions(business_id, waiting=True, limit=limit)]


def overview(business_id):
    """Короткая сводка для главной: сколько ждёт и что было сегодня."""
    since = _stamp(_now() - datetime.timedelta(days=1))
    try:
        return {
            "waiting": database.count_actions(business_id, waiting=True),
            "done": database.count_actions(business_id, status=database.AC_SUCCEEDED,
                                           since=since),
            "blocked": database.count_actions(
                business_id, status=(database.AC_BLOCKED, database.AC_STALE),
                since=since),
            "failed": database.count_actions(business_id, status=database.AC_FAILED,
                                             since=since),
            "last": [public(r) for r in
                     database.list_actions(business_id, limit=5)],
        }
    except Exception:
        log.exception("Сводка действий не собралась (biz %s)", business_id)
        return {"waiting": 0, "done": 0, "blocked": 0, "failed": 0, "last": []}


def settings(business_id):
    """Всё, что нужно странице автономности: положения, смысл, потолки."""
    cur = modes(business_id)
    said = raw_modes(business_id)
    items = []
    for action_id, meta in REGISTRY.items():
        # Тумблер общий, а уровень доверия — у каждого канала свой, и он старше.
        # Показать «Автоматически» там, где канал этого всё равно не позволит,
        # значило бы соврать в собственных настройках: владелец решил бы, что
        # включил, и ждал бы поведения, которого не будет.
        held = [ch for ch in CHANNELS
                if cur.get(action_id) == AUTO
                and _channel_gate(business_id, action_id, ch, meta) != AUTO]
        items.append({
            "action": action_id,
            "title": meta["title"],
            "means": meta["means"],
            "group": meta["group"],
            "risk": meta["risk"],
            "risk_ru": RISK_RU.get(meta["risk"], ""),
            "mode": cur.get(action_id),
            "mode_ru": MODE_RU.get(cur.get(action_id), ""),
            "choices": [{"mode": m, "title": MODE_RU[m]} for m in meta["choices"]],
            "ceiling": meta["ceiling"],
            # Владельцу важно не «потолок APPROVAL», а почему кнопка «Разрешить
            # всё» не сделала это автоматическим. Объясняем на месте.
            "capped": (meta["ceiling"] != AUTO),
            "held_by": held,
            "held_note": ("Разрешено здесь, но уровень доверия каналу этого не "
                          "позволяет: " + ", ".join(held)) if held else "",
            "touched": action_id in said,
        })
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    items.sort(key=lambda x: (order.get(x["group"], 9), x["title"]))
    return {
        "groups": [{"key": k, "title": t} for k, t in GROUPS],
        "actions": items,
        "modes": [{"mode": m, "title": MODE_RU[m]} for m in (AUTO, APPROVAL, DENY)],
        "filters": [{"key": k, "title": FILTER_RU[k]} for k in FILTERS],
    }
