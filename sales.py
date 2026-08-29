# -*- coding: utf-8 -*-
"""
AI SALES CHANNEL — продавец, который не умеет выдумывать.

Задача этого слоя проста на словах и трудна на деле: превратить канал общения в
канал продаж, не позволив модели сказать ни одной цифры, которой нет в памяти
бизнеса. Выдуманная цена дороже упущенной сделки: по ней клиент придёт, а
владелец будет объясняться.

Три опоры
─────────
1. ПАМЯТЬ БИЗНЕСА — единственный источник. Каталог услуг и товаров, правила
   работы, сведения о компании и фрагменты документов. Ничего сверх этого в
   промпт не попадает, и придумать «примерную» цену модели просто не из чего.

2. ПРАВА МЕНЯЮТ НЕ ТЕКСТ ИНСТРУКЦИИ, А ТО, ЧТО МОДЕЛЬ ВИДИТ. Запретить словами
   «не называй цены» — значит понадеяться на послушание. Здесь без права
   send_price цены физически не уходят в промпт: назвать их неоткуда. Так же с
   каталогом, скидками и возвратами.

3. ПРОВЕРКА ПОСЛЕ ОТВЕТА. Всё, что модель написала, разбирается обратно: каждая
   сумма, процент, срок и утверждение о наличии должны находиться в памяти
   дословно. Не нашлось — ответ клиенту НЕ уходит, разговор передаётся человеку,
   а владельцу показывают, что VELOR хотел сказать и почему это остановлено.

Что означает «не уверен»
────────────────────────
Не настроение модели, а перечислимые случаи: она сама попросила человека;
в ответе есть неподтверждённый факт; клиент просит скидку, возврат или особую
цену без соответствующего разрешения; клиент прямо зовёт живого сотрудника;
памяти по вопросу нет вовсе. Во всех этих случаях — HUMAN HANDOFF, и клиент
получает честную фразу без единой цифры.
"""
from __future__ import annotations

import json
import logging
import re

import ai
import database

log = logging.getLogger("velor.sales")

# ── права ──────────────────────────────────────────────────────────────────
# Каждое право отвечает на вопрос «что VELOR может сделать сам». Опасные — те,
# после которых бизнес теряет деньги, обязательства или доверие: скидка,
# возврат, цена мимо прайса и сообщение, отправленное клиенту первым. Последнее
# денег не стоит, но стоит отношений: репутацию тратит не выдуманная цена, а
# ощущение, что тебе пишут без спроса.
# Они не входят ни в один уровень автономии, включая четвёртый:
# «полная разрешённая автономия» — это всё, что владелец разрешил, а не всё,
# что бывает на свете.

ANSWER_FAQ = "answer_faq"
SEND_PRICE = "send_price"
RECOMMEND_SERVICE = "recommend_service"
COLLECT_CUSTOMER = "collect_customer"
CREATE_LEAD = "create_lead"
CREATE_ORDER = "create_order"
DISCOUNT = "discount"
REFUND = "refund"
MODIFY_PRICE = "modify_price"
FOLLOW_UP = "follow_up"

PERMISSIONS = {
    ANSWER_FAQ: {
        "title": "Отвечать на вопросы",
        "means": "Отвечает по документам и правилам компании. Чего в памяти нет — "
                 "не отвечает, а зовёт человека.",
        "danger": False},
    SEND_PRICE: {
        "title": "Называть цены",
        "means": "Цитирует цены из каталога дословно. Без этого права цены в "
                 "разговор вообще не попадают — модель их не видит.",
        "danger": False},
    RECOMMEND_SERVICE: {
        "title": "Подбирать услуги",
        "means": "Показывает каталог и предлагает подходящее. Без этого права "
                 "каталог модели не показывается.",
        "danger": False},
    COLLECT_CUSTOMER: {
        "title": "Собирать контакты",
        "means": "Спрашивает имя и телефон и записывает их в карточку клиента.",
        "danger": False},
    CREATE_LEAD: {
        "title": "Заводить лида",
        "means": "Создаёт карточку клиента и записывает, чем он интересовался.",
        "danger": False},
    CREATE_ORDER: {
        "title": "Оформлять заявки",
        "means": "Создаёт заявку с суммой из каталога. Без этого права доводит "
                 "до готовности и передаёт вам.",
        "danger": False},
    DISCOUNT: {
        "title": "Говорить о скидках",
        "means": "Называет скидку, ЗАПИСАННУЮ в правилах работы. Придумать свою "
                 "не может и с этим правом: проверка не пропустит.",
        "danger": True},
    REFUND: {
        "title": "Обсуждать возвраты",
        "means": "Объясняет условия возврата из правил и оформляет обращение на "
                 "возврат. Денег не двигает никогда — это делает человек.",
        "danger": True},
    MODIFY_PRICE: {
        "title": "Ставить цену мимо прайса",
        "means": "Может записать в заявку сумму, отличную от каталога, — например "
                 "когда скидка из правил уже учтена. Считать «на глаз» не будет.",
        "danger": True},
    FOLLOW_UP: {
        "title": "Писать клиенту первым",
        "means": "Сам отправляет напоминание по незакрытой возможности — не "
                 "чаще трёх раз и только в ваши рабочие часы. Без этого права "
                 "VELOR готовит текст и ждёт вашего «Отправить».",
        "danger": True},
}

DANGEROUS = [k for k, v in PERMISSIONS.items() if v["danger"]]

# ── уровни автономии ───────────────────────────────────────────────────────
LEVELS = {
    1: {"title": "Только отвечает",
        "means": "Отвечает на вопросы по памяти бизнеса. Ничего не создаёт и "
                 "ничего не обещает.",
        "grants": [ANSWER_FAQ]},
    2: {"title": "Отвечает и собирает лидов",
        "means": "Рассказывает об услугах и ценах, подбирает подходящее, "
                 "спрашивает контакт и заводит карточку клиента. Заявку "
                 "оформляете вы.",
        "grants": [ANSWER_FAQ, SEND_PRICE, RECOMMEND_SERVICE, COLLECT_CUSTOMER,
                   CREATE_LEAD]},
    3: {"title": "Отвечает и оформляет заявки",
        "means": "Всё предыдущее плюс сам оформляет заявку, когда всё ясно. "
                 "Сумму берёт из каталога, свою не придумывает.",
        "grants": [ANSWER_FAQ, SEND_PRICE, RECOMMEND_SERVICE, COLLECT_CUSTOMER,
                   CREATE_LEAD, CREATE_ORDER]},
    4: {"title": "Полная разрешённая автономия",
        "means": "Всё, что вы разрешили. Опасные действия — скидки, возвраты, "
                 "цена мимо прайса — сюда НЕ входят: каждое включается "
                 "отдельно и только вами.",
        "grants": [ANSWER_FAQ, SEND_PRICE, RECOMMEND_SERVICE, COLLECT_CUSTOMER,
                   CREATE_LEAD, CREATE_ORDER]},
}

# Осторожный старт: канал отвечает и собирает лидов, но заявки не оформляет,
# пока владелец сам этого не разрешит. Всё, что создаётся от его имени без
# спроса, однажды создастся не так.
DEFAULT_LEVEL = 2


def levels_public() -> list[dict]:
    return [{"level": n, **{k: v for k, v in meta.items() if k != "grants"},
             "grants": list(meta["grants"])} for n, meta in sorted(LEVELS.items())]


def permissions_public() -> list[dict]:
    return [{"key": k, **v} for k, v in PERMISSIONS.items()]


def policy(business_id: int, channel: str = "instagram") -> dict:
    """
    Что каналу разрешено прямо сейчас.

    Итоговый набор = права уровня ПЛЮС то, что владелец включил поимённо.
    Опасные права приходят только вторым путём: уровень их не даёт никогда.
    """
    row = database.ai_policy(business_id, channel) or {}
    level = int(row.get("level") or DEFAULT_LEVEL)
    if level not in LEVELS:
        level = DEFAULT_LEVEL
    grants = [g for g in (row.get("grants") or []) if g in PERMISSIONS]
    allowed = set(LEVELS[level]["grants"]) | set(grants)
    return {
        "channel": channel,
        "level": level,
        "level_title": LEVELS[level]["title"],
        "level_means": LEVELS[level]["means"],
        "granted": sorted(grants),
        "allowed": sorted(allowed),
        "dangerous_on": sorted(g for g in grants if PERMISSIONS[g]["danger"]),
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
    }


def set_policy(business_id: int, channel: str, level=None, grants=None,
               actor: str = "owner") -> dict:
    """
    Изменить автономию. Включение опасного права — событие в истории бизнеса.

    Отдельная запись нужна не для порядка ради порядка: если завтра VELOR
    назовёт скидку, владелец должен видеть, что он сам её разрешил, когда и
    какую. Без этого спор «я такого не включал» неразрешим.
    """
    before = policy(business_id, channel)
    if level is not None:
        level = int(level)
        if level not in LEVELS:
            raise ValueError("Такого уровня автономии нет.")
    if grants is not None:
        bad = [g for g in grants if g not in PERMISSIONS]
        if bad:
            raise ValueError("Неизвестное разрешение: " + ", ".join(bad))
        grants = sorted(set(grants))
    database.save_ai_policy(business_id, channel, level=level, grants=grants,
                            actor=actor)
    after = policy(business_id, channel)

    if after["level"] != before["level"]:
        database.log_event(business_id, "settings",
                           f"Автономия VELOR: уровень {after['level']}",
                           LEVELS[after['level']]["title"] + " · " + channel)
    turned_on = set(after["granted"]) - set(before["granted"])
    turned_off = set(before["granted"]) - set(after["granted"])
    for g in sorted(turned_on):
        database.log_event(
            business_id, "settings",
            ("Разрешено опасное действие: " if PERMISSIONS[g]["danger"]
             else "Разрешено действие: ") + PERMISSIONS[g]["title"],
            PERMISSIONS[g]["means"],
            level="important" if PERMISSIONS[g]["danger"] else "info")
    for g in sorted(turned_off):
        database.log_event(business_id, "settings",
                           "Запрещено действие: " + PERMISSIONS[g]["title"], channel)
    return after


# ── память бизнеса ─────────────────────────────────────────────────────────

def _fact_line(row: dict, with_body: bool) -> str:
    title = (row.get("title") or "").strip()
    body = (row.get("body") or "").strip()
    # Знание, которое VELOR понял сам и человек не подтверждал, показывается —
    # выбросить его значило бы отвечать хуже, чем можем, — но помечается. Модель
    # обязана оговориться, а не выдавать догадку за факт. Проверка ниже такую
    # строку по-прежнему считает основанием: она из памяти бизнеса, а не из
    # головы модели. Разница между «непроверено» и «выдумано» здесь и проходит.
    mark = "" if row.get("verified", 1) else "  [не подтверждено владельцем]"
    if with_body and body:
        return f"— {title}: {body}{mark}"
    return f"— {title}{mark}"


def _mentions(row: dict, roots) -> bool:
    hay = ((row.get("title") or "") + " " + (row.get("body") or "")).lower()
    return any(r in hay for r in roots)


DISCOUNT_ROOTS = ("скидк", "акци", "промокод", "бонус", "дешевл")
REFUND_ROOTS = ("возврат", "возвращ", "компенсац", "обмен товара")
# В ответе ищем более узкий набор: «вернусь с ответом» — не про возврат денег,
# а корень «верн» поймал бы и его. Правило работы прятать по широкому набору
# можно, обвинять готовый ответ — только по однозначному.
REFUND_IN_REPLY = ("возврат", "возвращ", "компенсац")


def memory(business_id: int, question: str, allowed: set) -> dict:
    """
    Собрать то — и только то, — что каналу разрешено видеть.

    Здесь и происходит настоящее ограничение прав. Без права называть цены
    каталог уходит в промпт одними названиями: тела записей, где живут суммы и
    условия, не приезжают вовсе. Придумать цену из ничего модель может, но
    проверка ниже такой ответ не выпустит.
    """
    services = database.list_facts(business_id, "service") or []
    products = database.list_facts(business_id, "product") or []
    rules = database.list_facts(business_id, "rule") or []
    company = database.list_facts(business_id, "company") or []

    show_catalog = RECOMMEND_SERVICE in allowed or SEND_PRICE in allowed
    with_price = SEND_PRICE in allowed

    # Правила о скидках и возвратах — тоже право. Без него VELOR не знает, что
    # скидка вообще бывает, и не может её «случайно» вспомнить.
    visible_rules = []
    for r in rules:
        if _mentions(r, DISCOUNT_ROOTS) and DISCOUNT not in allowed:
            continue
        if _mentions(r, REFUND_ROOTS) and REFUND not in allowed:
            continue
        visible_rules.append(r)

    docs = []
    if ANSWER_FAQ in allowed:
        docs = database.search_chunks(business_id, question or "", k=4) or []

    catalog = []
    if show_catalog:
        for row in services:
            catalog.append(_fact_line(row, with_price))
        for row in products:
            catalog.append(_fact_line(row, with_price))

    block = []
    if company:
        block.append("О КОМПАНИИ:\n" + "\n".join(_fact_line(r, True) for r in company))
    if catalog:
        head = ("КАТАЛОГ — цены и условия здесь и больше нигде. Цитируй дословно:"
                if with_price else
                "КАТАЛОГ — только названия. Цены называть НЕ разрешено:")
        block.append(head + "\n" + "\n".join(catalog))
    if visible_rules:
        block.append("ПРАВИЛА РАБОТЫ:\n"
                     + "\n".join(_fact_line(r, True) for r in visible_rules))
    if docs:
        block.append("ФРАГМЕНТЫ ДОКУМЕНТОВ КОМПАНИИ:\n"
                     + "\n---\n".join(d[:800] for d in docs))

    # Текст для сверки. Сюда идёт ВСЁ, что модели показали, — по нему потом
    # проверяется каждая цифра из ответа.
    grounding = "\n".join(block)
    # Есть ли среди показанного непроверенное. Считаем по тем же записям, что
    # ушли в промпт: правило, спрятанное правами, на осторожность ответа влиять
    # не должно — его модель всё равно не видела.
    shown = list(company) + list(visible_rules) + (
        list(services) + list(products) if show_catalog else [])
    has_unverified = any(not r.get("verified", 1) for r in shown)

    return {
        "text": "\n\n".join(block),
        "grounding": grounding,
        "has_unverified": has_unverified,
        "has_catalog": bool(catalog),
        "has_prices": bool(with_price and catalog),
        "counts": {"services": len(services), "products": len(products),
                   "rules": len(visible_rules), "docs": len(docs),
                   "company": len(company)},
        "empty": not block,
    }


def memory_health(business_id: int) -> dict:
    """
    Чем продавцу вообще торговать.

    Продавец не умнее памяти бизнеса: пустой каталог означает, что на вопрос
    «сколько стоит» он не ответит никогда — не потому что плох, а потому что
    цены никто не внёс. Владелец должен видеть это заранее, а не догадываться
    по молчанию канала.
    """
    services = database.list_facts(business_id, "service") or []
    products = database.list_facts(business_id, "product") or []
    rules = database.list_facts(business_id, "rule") or []
    company = database.list_facts(business_id, "company") or []
    docs = database.count_documents(business_id) or 0

    offers = services + products
    # Цена в памяти живёт внутри текста записи: «Букет пионов: от 4500 ₽».
    # Считаем не «есть ли поле», а есть ли в тексте число — по нему и пройдёт
    # проверка ответа, так что это ровно та цифра, которую продавец сможет назвать.
    priced = [o for o in offers if _NUM.search((o.get("body") or ""))]
    gaps = []
    if not offers:
        gaps.append("В памяти нет ни услуг, ни товаров — рассказывать не о чем.")
    elif not priced:
        gaps.append("Ни у одной услуги не записана цена — назвать её продавцу неоткуда.")
    elif len(priced) < len(offers):
        gaps.append(f"Без цены записей: {len(offers) - len(priced)} из {len(offers)}.")
    if not rules:
        gaps.append("Нет правил работы — условия, сроки и возвраты подтвердить нечем.")
    return {
        "services": len(services), "products": len(products),
        "rules": len(rules), "company": len(company), "documents": docs,
        "priced": len(priced), "offers": len(offers),
        "ready": bool(offers and priced),
        "gaps": gaps,
    }


# ── проверка на выдумку ────────────────────────────────────────────────────
# Обратный разбор ответа. Всё, что похоже на обещание, ищем в памяти дословно.

_SPACES = "    "
_NUM = re.compile(r"\d[\d" + _SPACES + r"]*(?:[.,]\d+)?")
_MONEY = re.compile(r"(\d[\d" + _SPACES + r"]*(?:[.,]\d+)?)\s*(?:₽|руб\w*|р\.)", re.I)
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_TERM = re.compile(r"(\d+)\s*(?:рабоч\w+\s+)?"
                   r"(дн\w*|сут\w*|недел\w*|час\w*|минут\w*|месяц\w*)", re.I)

STOCK_CLAIMS = (("в наличии", "налич"), ("нет в наличии", "налич"),
                ("под заказ", "под заказ"), ("закончил", "законч"),
                ("распродан", "распродан"), ("в ассортименте", "ассортимент"))


def _norm_num(raw: str) -> set:
    """Одно число во всех видах, в каких его пишут люди: 3 200, 3200, 3200.00."""
    s = re.sub("[" + _SPACES + "]", "", raw).replace(",", ".").rstrip(".")
    out = {s}
    try:
        v = float(s)
        if v == int(v):
            out.add(str(int(v)))
    except ValueError:
        pass
    return out


def _numbers(text: str) -> set:
    out = set()
    for m in _NUM.finditer(text or ""):
        out |= _norm_num(m.group(0))
    return out


def unproven(reply: str, grounding: str, allowed: set) -> list[str]:
    """
    Что в ответе не подтверждается памятью бизнеса.

    Пустой список — ответ можно отправлять. Иначе отправлять нельзя: каждая
    строка здесь это обещание, за которое некому отвечать.

    Считаем подтверждённым и то, что назвал сам клиент: если он сказал «бюджет
    5000», повторить эту цифру не выдумка. Поэтому слова клиента приходят в
    grounding вместе с памятью.
    """
    problems = []
    known = _numbers(grounding)
    low = (reply or "").lower()
    ground_low = (grounding or "").lower()

    for m in _MONEY.finditer(reply or ""):
        if not (_norm_num(m.group(1)) & known):
            problems.append(f"суммы «{m.group(0).strip()}» нет в памяти бизнеса")

    for m in _PERCENT.finditer(reply or ""):
        if not (_norm_num(m.group(1)) & known):
            problems.append(f"процента «{m.group(0).strip()}» нет в памяти бизнеса")

    for m in _TERM.finditer(reply or ""):
        if not (_norm_num(m.group(1)) & known):
            problems.append(f"срока «{m.group(0).strip()}» нет в памяти бизнеса")

    for phrase, root in STOCK_CLAIMS:
        if phrase in low and root not in ground_low:
            problems.append(f"о наличии («{phrase}») в памяти ничего не сказано")

    # Опасные темы: даже подтверждённую скидку нельзя называть без разрешения.
    if DISCOUNT not in allowed and any(r in low for r in DISCOUNT_ROOTS):
        problems.append("речь о скидке, а разрешения говорить о скидках нет")
    if REFUND not in allowed and any(r in low for r in REFUND_IN_REPLY):
        problems.append("речь о возврате, а разрешения обсуждать возвраты нет")
    if SEND_PRICE not in allowed and _MONEY.search(reply or ""):
        problems.append("названа цена, а разрешения называть цены нет")

    # Один и тот же промах не надо повторять трижды.
    return list(dict.fromkeys(problems))


# ── когда зовём человека ───────────────────────────────────────────────────

ASK_HUMAN = ("оператор", "менеджер", "живой человек", "с человеком", "позовите",
             "хочу поговорить с", "соедините", "директор", "владелец")

HOLD_REPLY = ("Спасибо! Уточню у коллеги и вернусь с точным ответом — "
              "чтобы не сказать лишнего.")


def wants_human(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in ASK_HUMAN)


# ── промпт ─────────────────────────────────────────────────────────────────

def _rights_block(allowed: set) -> str:
    can = [PERMISSIONS[k]["title"] for k in PERMISSIONS if k in allowed]
    cannot = [PERMISSIONS[k]["title"] for k in PERMISSIONS if k not in allowed]
    out = "\n\nЧТО ТЕБЕ РАЗРЕШЕНО СЕЙЧАС: " + (", ".join(can) if can else "только отвечать") + "."
    if cannot:
        out += ("\nЧТО ЗАПРЕЩЕНО: " + ", ".join(cannot) +
                ". Если клиент просит именно это — не отказывай сухо и не "
                "придумывай: скажи, что позовёшь сотрудника, и поставь HANDOFF.")
    return out


def _system(business: dict, client_info: dict, mem: dict, allowed: set) -> str:
    name = business.get("name") or "компания"
    about = (business.get("about") or "").strip()
    who = f"«{name}»" + (f" ({about})" if about else "")

    p = (f"Ты — продавец-консультант компании {who}. " + ai._identity(business)
         + "Ты переписываешься с клиентом от лица бизнеса.\n"
         "Твоя работа: понять, что нужно клиенту, ответить по фактам компании и "
         "довести до заявки.\n"
         "ЖЕЛЕЗНЫЕ ПРАВИЛА:\n"
         "1. Цены, наличие, условия, скидки и сроки берутся ТОЛЬКО из памяти "
         "бизнеса ниже и цитируются дословно. Не считай в уме, не складывай, не "
         "прикидывай «примерно», не округляй. Нет цифры в памяти — значит, её "
         "называть нельзя.\n"
         "2. Не знаешь — так и скажи и поставь HANDOFF. Это правильный ответ, а "
         "не поражение.\n"
         "3. Пиши коротко и по-человечески, 1–3 предложения. Один вопрос за раз.\n"
         "4. Уточняй только то, что нужно этому бизнесу, а не по шаблону.\n")
    p += _rights_block(allowed)

    if mem["empty"]:
        p += ("\n\nПАМЯТЬ БИЗНЕСА ПУСТА: ты не знаешь ни услуг, ни цен, ни условий "
              "этой компании. Ничего не перечисляй и не предполагай — спроси, что "
              "нужно клиенту, и поставь HANDOFF.")
    else:
        p += ("\n\nПАМЯТЬ БИЗНЕСА — единственный источник правды. Всё, чего здесь "
              "нет, для тебя не существует:\n" + mem["text"][:5000])
        if mem.get("has_unverified"):
            p += ("\n\nСтроки с пометкой «не подтверждено владельцем» VELOR понял "
                  "сам из присланных материалов, и человек их не проверял. "
                  "Пользоваться ими можно, но выдавать за точные — нельзя: "
                  "назови цифру и сразу оговорись, что уточнишь, а в служебной "
                  "строке поставь handoff.")

    if client_info:
        known = ", ".join(f"{k}: {v}" for k, v in client_info.items() if v)
        if known:
            p += f"\n\nЧто мы знаем о клиенте: {known}. Постоянного узнавай по имени."
    p += ai._tone_line(business)

    p += (
        "\n\nСЛУЖЕБНАЯ СТРОКА. После ответа клиенту добавь отдельной последней "
        "строкой JSON — клиент её не увидит:\n"
        'VELOR: {"customer": {"name": null, "phone": null}, '
        '"lead": {"interest": "что нужно клиенту"}, '
        '"order": {"text": null, "amount": null, "phone": null, "address": null, '
        '"date_wanted": null}, "handoff": null}\n'
        "— customer заполняй, когда клиент назвал имя или телефон;\n"
        "— lead.interest — чем клиент интересуется, своими словами, всегда;\n"
        "— order заполняй ТОЛЬКО когда заявка полностью ясна и подтверждена "
        "клиентом; amount — цена ИЗ КАТАЛОГА или названная клиентом, иначе null;\n"
        "— handoff — короткая причина позвать человека, если ты не уверен, если "
        "просят скидку/возврат/особые условия или если данных нет.\n"
        "Ставь строку один раз, в самом конце."
    )
    return p


_JSON_LINE = re.compile(r"VELOR:\s*(\{.*\})", re.S)


def _parse(raw: str):
    """Отделить ответ клиенту от служебной строки."""
    data = {}
    m = _JSON_LINE.search(raw or "")
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            data = {}
        raw = _JSON_LINE.sub("", raw)
    return (raw or "").strip(), data


def _clean(v):
    if isinstance(v, str):
        s = v.strip()
        return None if s.lower() in ("", "null", "none", "-") else s
    return v


# ── главный вход ───────────────────────────────────────────────────────────

def answer(business_id: int, client: dict, text: str, channel: str = "instagram",
           history=None) -> dict:
    """
    Ответить клиенту как продавец и сделать то, что разрешено.

    Возвращает решение целиком, а не только текст: канал сам решает, как
    показать передачу человеку, — у Instagram это пауза в переписке, у другого
    канала будет иначе. Здесь остаётся общая правда: что ответить, что создать
    и почему остановились.
    """
    pol = policy(business_id, channel)
    allowed = set(pol["allowed"])
    business = database.get_business(business_id) or {}
    client_id = client.get("id")

    out = {"reply": None, "handoff": False, "reason": None, "draft": None,
           "actions": [], "policy": pol, "unproven": []}

    # Клиент прямо зовёт человека — тут не о чем размышлять.
    if wants_human(text):
        out.update(handoff=True, reason="Клиент попросил живого сотрудника.",
                   reply=HOLD_REPLY)
        return out

    if not ai.ai_available():
        # Модели нет — значит, нет и продавца. Молча принимаем обращение.
        out.update(handoff=True, reason="ИИ не подключён — отвечает человек.",
                   reply="Спасибо! Мы получили сообщение и скоро ответим.")
        return out

    mem = memory(business_id, text, allowed)
    client_info = {"имя": client.get("name"), "телефон": client.get("phone"),
                   "предпочтения": client.get("favorite"),
                   "заметки": client.get("notes")}
    hist = list(history or database.get_history(business_id, client_id) or [])
    if not hist or hist[-1].get("content") != text:
        hist = hist + [{"role": "user", "content": text}]

    try:
        raw = ai._ask(_system(business, client_info, mem, allowed), hist)
    except Exception:
        log.exception("AI-продавец не ответил (biz %s)", business_id)
        out.update(handoff=True, reason="Модель не ответила.", reply=HOLD_REPLY)
        return out

    reply, data = _parse(raw)

    # Слова клиента — тоже подтверждение: назвать в ответ его же бюджет или его
    # же срок не выдумка. Берём всю переписку, а не только последнюю реплику.
    said_by_client = " ".join(m.get("content") or "" for m in hist
                              if m.get("role") == "user")
    grounding = mem["grounding"] + "\n" + said_by_client

    problems = unproven(reply, grounding, allowed)
    out["unproven"] = problems
    asked_handoff = _clean((data or {}).get("handoff"))

    if problems:
        # Ответ уже написан, но отправить его нельзя. Показываем владельцу и
        # черновик, и причину: по ним видно, чего не хватает в памяти бизнеса.
        out.update(handoff=True, draft=reply, reply=HOLD_REPLY,
                   reason="Ответ не подтверждён памятью бизнеса: "
                          + "; ".join(problems) + ".")
        # Контакт и интерес всё равно записываем: их назвал сам клиент, а не
        # придумала модель. Терять телефон из-за того, что в том же ответе
        # оказалась непроверенная цена, — значит наказывать клиента за нашу
        # осторожность. А вот заявку по такому разговору не создаём: она
        # опиралась бы на цифру, которой никто не подтверждал.
        out["actions"] = _apply(business_id, client, data,
                                allowed - {CREATE_ORDER}, channel, out, text)
        return out

    if not reply:
        out.update(handoff=True, reason="Модель не сформулировала ответ.",
                   reply=HOLD_REPLY)
        return out

    # Действия делаем ДО отправки: если заявка не создалась, обещать её нельзя.
    out["actions"] = _apply(business_id, client, data, allowed, channel, out, text)

    if asked_handoff:
        out.update(handoff=True, reason=asked_handoff, reply=reply)
        return out

    out["reply"] = reply
    return out


def _apply(business_id, client, data, allowed, channel, out, said="") -> list[dict]:
    """
    Сделать то, что разрешено, и честно отметить то, что не разрешено.

    Порядок важен: сначала контакт, потом лид, потом заявка. Заявка без клиента
    осиротеет, а лид без контакта — это всё ещё лид, просто без телефона.
    """
    done = []
    client_id = client.get("id")
    customer = (data or {}).get("customer") or {}
    lead = (data or {}).get("lead") or {}
    order = (data or {}).get("order") or {}

    name = _clean(customer.get("name"))
    phone = _clean(customer.get("phone")) or _clean(order.get("phone"))
    interest = _clean(lead.get("interest"))

    if (name or phone) and COLLECT_CUSTOMER in allowed:
        fields = {}
        # Имя: слово самого клиента сильнее того, что стояло раньше. В директе
        # там обычно @-логин, и «Меня зовут Ольга» — лучший источник, какой у
        # нас вообще бывает.
        if name and name != (client.get("name") or "").strip():
            fields["name"] = name
        # Телефон: заполняем пустое, но НЕ затираем существующий. Владелец мог
        # его проверить и исправить руками; молча заменить подтверждённый
        # контакт разбором переписки — ровно тот случай, против которого
        # существует ручное управление. Второй номер уходит в заметку.
        old_phone = (client.get("phone") or "").strip()
        if phone and not old_phone:
            fields["phone"] = phone
        elif phone and phone != old_phone:
            note = (client.get("notes") or "").strip()
            line = "Назвал ещё телефон: " + phone
            if line not in note:
                fields["notes"] = (note + "\n" + line).strip()[:2000]
        if fields:
            database.update_client(client_id, business_id, **fields)
            database.add_memory_link(
                business_id, "client", client_id, event="edited", source_kind="ai",
                actor="velor", changes=json.dumps(fields, ensure_ascii=False),
                note=f"AI-продавец записал контакт из канала «{channel}»")
            done.append({"action": COLLECT_CUSTOMER, "fields": sorted(fields)})
    elif (name or phone):
        done.append({"action": COLLECT_CUSTOMER, "skipped": "нет разрешения"})

    if interest:
        if CREATE_LEAD in allowed:
            # Лид — отдельная запись, а не строчка в заметках клиента. Раньше
            # «создать лида» означало дописать «Интерес: пионы» в notes: у такой
            # записи не было ни состояния, ни причины проигрыша, и посчитать её
            # было нельзя. Теперь этим занимается leads — одна дверь и для
            # продавца, и для бота, и для руки владельца.
            #
            # Решает при этом не модель: она заполняет интерес всегда, даже
            # когда человек спросил адрес. Заводить возможность или нет —
            # проверяют правила по словам самого клиента.
            import leads
            lead_id = leads.from_message(
                business_id, client, said or interest, source=channel,
                channel=channel, interest=interest)
            if lead_id:
                done.append({"action": CREATE_LEAD, "interest": interest,
                             "lead_id": lead_id})
            else:
                done.append({"action": CREATE_LEAD, "interest": interest,
                             "skipped": "разговор пока не о покупке"})
        else:
            done.append({"action": CREATE_LEAD, "skipped": "нет разрешения"})

    order_text = _clean(order.get("text"))
    if order_text:
        if CREATE_ORDER in allowed:
            amount = order.get("amount")
            oid = database.add_order(
                business_id, order_text, client_id=client_id,
                phone=phone or client.get("phone"),
                address=_clean(order.get("address")),
                date_wanted=_clean(order.get("date_wanted")),
                amount=amount if isinstance(amount, (int, float)) else 0,
                source=channel)
            database.add_memory_link(
                business_id, "order", oid, event="created", source_kind="ai",
                actor="velor", note=f"AI-продавец оформил заявку из канала «{channel}»")
            # Заявка появилась — значит, возможность стала сделкой. Новой
            # системы заказов для этого не заводим: лид просто ссылается на
            # существующую заявку, и конверсия считается по факту, а не по
            # ощущению, что «вроде купил».
            try:
                import leads
                won = leads.on_order(business_id, client_id, oid,
                                     amount=amount if isinstance(amount, (int, float)) else None,
                                     channel=channel)
                if won:
                    done.append({"action": CREATE_LEAD, "lead_id": won, "won": True})
            except Exception:
                log.exception("Заявка не связалась с возможностью (biz %s)", business_id)
            done.append({"action": CREATE_ORDER, "order_id": oid})
        else:
            # Клиент готов, а права оформить заявку нет. Это не «ничего не
            # произошло» — это момент, когда нужен человек, и как можно быстрее.
            done.append({"action": CREATE_ORDER, "skipped": "нет разрешения"})
            out["handoff"] = True
            # Причину не перебиваем: если разговор уже остановлен из-за
            # неподтверждённой цифры, владельцу нужна именно та причина —
            # она объясняет, чего не хватает в памяти бизнеса.
            if not out.get("reason"):
                out["reason"] = ("Клиент готов оформить заявку, а разрешения "
                                 "оформлять их у VELOR нет.")
            database.log_event(business_id, "order", "Клиент готов оформить заявку",
                               order_text[:200], level="important")
    return done
