"""
Мозг ассистента. Умеет работать с двумя ИИ:

  1. Claude (Anthropic)  — ключ ANTHROPIC_API_KEY в .env
  2. GigaChat (Сбер)     — ключ GIGACHAT_AUTH_KEY в .env (принимает
                           российские карты, есть бесплатный тариф)

Логика выбора: пробуем Claude, если он недоступен (нет ключа, нет
денег на балансе) — автоматически переключаемся на GigaChat.
Если не работает ничего — бот падает в простой режим (анкета).

Функции:
  ping()        — быстрая проверка при старте: какой ИИ живой
  chat_reply()  — диалог с клиентом в Telegram (+ оформление заказа)
  site_answer() — короткий ответ ядра на сайте
"""
import json
import re
import time
import uuid

import requests

import prompt_engine
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, GIGACHAT_AUTH_KEY

try:
    import anthropic
    _claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
except ImportError:
    _claude = None

# какой провайдер сейчас рабочий: 'claude' / 'gigachat' / None (ещё не знаем)
_active = None


# ============================================================
#  Системные промпты (общие для обоих ИИ)
# ============================================================

_TONE = {
    "professional": "Тон общения: профессиональный, деловой, по существу, на «вы».",
    "friendly": "Тон общения: дружелюбный, тёплый, живой, на «вы», но по-человечески.",
    "strict": "Тон общения: строгий, чёткий, без лишних слов и эмоций.",
}


def _tone_line(business: dict) -> str:
    t = (business.get("tone") or "").strip()
    return "\n" + _TONE[t] if t in _TONE else ""


def _knowledge_block(business: dict) -> str:
    """База знаний бизнеса, которую подкладываем ядру перед ответом.

    Это и есть «заточка под бизнес»: модель отвечает не вообще, а по фактам
    конкретного дела — прайс, услуги, условия. Пока кладём целиком; когда у
    кого-то знаний станет много — добавим нарезку и поиск нужного куска.
    """
    kb = (business.get("knowledge") or "").strip()
    facts = (business.get("facts") or "").strip()   # услуги, товары, правила, цели
    if facts:
        kb = (facts + "\n\n" + kb).strip() if kb else facts
    if not kb:
        # База знаний пустая. Без этого явного запрета модель на вопрос «какой у вас
        # ассортимент?» выдумывает чужой каталог — что и ловил владелец. Затыкаем.
        return (
            "\n\nВНИМАНИЕ: у тебя НЕТ базы знаний этого бизнеса — ты не знаешь его "
            "товары, услуги, цены и ассортимент. КАТЕГОРИЧЕСКИ НЕЛЬЗЯ перечислять или "
            "придумывать какие-либо товары, услуги, каталог, ассортимент или цены. "
            "На такие вопросы честно отвечай, что уточнишь у владельца/сотрудника, "
            "и предложи помочь оформить заявку. Никогда не выдавай предположение за факт."
        )
    return (
        "\n\nБАЗА ЗНАНИЙ БИЗНЕСА — единственный источник правды о товарах, услугах, "
        "ценах и условиях. Отвечай про ассортимент и цены СТРОГО по этим фактам и "
        "НИЧЕГО не добавляй от себя. Если чего-то здесь нет — не выдумывай, честно "
        "скажи, что уточнит сотрудник:\n"
        + kb[:4000]
    )


def _docs_block(docs: list[str] | None) -> str:
    """Фрагменты из загруженных документов бизнеса, найденные под вопрос (RAG)."""
    if not docs:
        return ""
    body = "\n---\n".join(d[:900] for d in docs)
    return (
        "\n\nФРАГМЕНТЫ ИЗ ДОКУМЕНТОВ БИЗНЕСА — это официальные факты компании. "
        "Если во фрагментах есть ответ на вопрос — дай его ПРЯМО и с конкретными "
        "цифрами (цены, сроки, условия), НЕ отправляй уточнять то, что здесь написано. "
        "Выдумывать сверх фрагментов нельзя:\n" + body[:3500]
    )


def _system_chat(business: dict, client_info: dict | None, docs: list[str] | None = None) -> str:
    name = business.get("name") or "компания"
    about = business.get("about")
    who = f"«{name}»" + (f" ({about})" if about else "")
    p = (
        f"Ты — VELOR AI, живой и дружелюбный ассистент бизнеса {who}. "
        + _identity(business) +
        "Ты общаешься с клиентом в Telegram от лица этого бизнеса.\n"
        "Твоя главная задача — принять заявку/заказ: узнать, что нужно клиенту, "
        "уточнить телефон, адрес (или самовывоз/онлайн) и желаемую дату или время.\n"
        "Правила:\n"
        "- Пиши коротко и по-человечески, 1–3 предложения.\n"
        "- Один вопрос за раз, не анкетируй.\n"
        "- Не выдумывай цены, наличие и услуги, которых не знаешь: "
        "точные детали подтвердит сотрудник при связи.\n"
        "- КОГДА ЗАКАЗ ПОЛНОСТЬЮ ЯСЕН — добавь В КОНЦЕ ответа отдельной строкой:\n"
        'ORDER_JSON: {"text": "что нужно клиенту", "phone": "телефон или null", '
        '"address": "адрес или null", "date_wanted": "дата/время или null"}\n'
        "Эту строку клиент не увидит — она для системы. Добавляй её только один раз, "
        "когда заявка готова к записи, и вместе с ней напиши клиенту подтверждение."
    )
    if client_info:
        known = ", ".join(f"{k}: {v}" for k, v in client_info.items() if v)
        if known:
            p += f"\nЧто мы знаем об этом клиенте: {known}. Постоянного клиента узнавай и приветствуй по имени."
    p += _tone_line(business)
    p += _knowledge_block(business)
    p += _docs_block(docs)
    return p


# ---- Роли AI-сотрудников (кабинет владельца) ----
# Один движок и одна база знаний, но каждый «сотрудник» отвечает в своей роли.
_ROLE_PERSONA = {
    "assistant": (
        "Сейчас ты — универсальный ассистент владельца: помогаешь по любым "
        "вопросам бизнеса, от заказов до идей."
    ),
    "sales": (
        "Сейчас ты — AI-продавец. Помогаешь отвечать клиентам, снимать возражения, "
        "доводить до заказа и поднимать средний чек. Предлагай допродажи и готовые "
        "формулировки ответов клиенту."
    ),
    "marketing": (
        "Сейчас ты — AI-маркетолог. Придумываешь посты, акции, рекламные тексты и "
        "идеи продвижения под этот бизнес. Давай конкретные тексты и предложения, "
        "а не общие советы."
    ),
    "analyst": (
        "Сейчас ты — AI-аналитик. Смотришь на цифры бизнеса (заказы, клиенты, оборот), "
        "находишь закономерности, точки роста и риски. Если данных не хватает — прямо "
        "скажи, какие нужны."
    ),
    "admin": (
        "Сейчас ты — AI-администратор. Помогаешь с задачами, расписанием, организацией "
        "процессов и порядком в делах. Предлагай понятные, выполнимые шаги."
    ),
}


def _identity(business: dict) -> str:
    """Личность сотрудника, собранная владельцем при создании (имя + характер)."""
    parts = []
    ai_name = (business.get("ai_name") or "").strip()
    traits = (business.get("ai_traits") or "").strip()
    desc = (business.get("ai_desc") or "").strip()
    if ai_name:
        parts.append(f"Тебя зовут {ai_name} — так тебя назвал владелец, представляйся этим именем.")
    if traits:
        parts.append(f"Твои черты характера: {traits}. Они должны быть слышны в манере речи.")
    if desc:
        parts.append(f"Владелец описал тебя так: {desc}")
    return (" ".join(parts) + " ") if parts else ""


def _timeline_block(timeline: str | None) -> str:
    """История бизнеса — дополнительная память: что происходило в компании и когда."""
    if not timeline:
        return ""
    return ("\n\nИСТОРИЯ КОМПАНИИ (последние события, новые сверху). Это твоя память о том, "
            "что уже произошло: ссылайся на неё, замечай изменения и не предлагай то, "
            "что владелец уже сделал:\n" + timeline[:1800])


def _system_assistant(business: dict, role: str | None, snapshot: str | None,
                      docs: list[str] | None = None, persona: str | None = None,
                      timeline: str | None = None, question: str = "") -> str:
    """Собрать системный промпт через Prompt Engine.

    Профессию (маркетолог, продавец, CFO, юрист, HR, руководитель…) движок
    определяет САМ по тексту запроса владельца — пользователю не нужно писать
    промпты. persona (если задан) — свой AI-сотрудник владельца из БД, тогда
    берём его характер вместо автоопределённой роли.
    """
    name = business.get("name") or "компания"
    about = business.get("about")
    who = f"«{name}»" + (f" ({about})" if about else "")
    base_identity = (
        f"Ты — VELOR AI, цифровой сотрудник бизнеса {who}. "
        + _identity(business) +
        "Ты не языковая модель и не чат-бот, а опытный член команды: пользователь "
        "должен чувствовать, что говорит с живым профессионалом. Ты партнёр, "
        "ориентированный на результат: видишь проблему или возможность — коротко "
        "предлагаешь, но не навязываешься."
    )

    role_key, mandate = prompt_engine.build_persona(
        business, question, ui_role=role, forced_persona=persona)

    # Контекст компании — единым блоком (движок отвечает за архитектуру, ai.py —
    # за доступ к данным бизнеса).
    ctx = _tone_line(business)
    if snapshot:
        ctx += "\n\nТекущие данные бизнеса: " + snapshot + "."
    ctx += _knowledge_block(business)
    ctx += _timeline_block(timeline)
    ctx += _docs_block(docs)

    return prompt_engine.compose(business, base_identity, mandate, role_key, ctx)


def assistant_answer(business: dict, question: str, role: str | None = None,
                     snapshot: str | None = None, docs: list[str] | None = None,
                     persona: str | None = None, timeline: str | None = None) -> str:
    """Ответ AI-сотрудника: авто-определение профессии + знания + история + документы."""
    return _ask(_system_assistant(business, role, snapshot, docs, persona, timeline,
                                  question=question),
                [{"role": "user", "content": question[:800]}], max_tokens=600).strip()


_OPP_CATEGORIES = "расходы, цены, клиенты, услуга, реклама, процессы"


def find_opportunities(business: dict, signals_text: str) -> list[dict]:
    """
    Найти способы улучшить бизнес по его же цифрам. Возвращает список
    словарей {category, title, why, action, priority 1..3}. Если модель
    ответила не по форме — вернём пустой список, страница это переживёт.
    """
    system = (
        f"Ты — AI-директор бизнеса {_biz_who(business)}. Тебе дают реальные цифры компании. "
        "Найди 4–6 возможностей улучшить дело: снизить расходы, поднять цены, вернуть "
        "спящих клиентов, добавить услугу, запустить акцию, починить процесс.\n"
        "Всё, что помечено словом ТРЕВОЖНО, — самое больное место: разбери это в первую "
        "очередь и поставь таким пунктам priority 1.\n"
        "Требования: опирайся ТОЛЬКО на эти цифры и ссылайся на них в обосновании; "
        "не предлагай то, для чего в данных нет оснований; каждое действие должно быть "
        "выполнимо владельцем небольшого бизнеса за неделю без бюджета на разработку.\n"
        f"Категория — одно слово из списка: {_OPP_CATEGORIES}.\n"
        "priority: 1 — сделать первым, 2 — важно, 3 — когда дойдут руки.\n"
        "Ответь ТОЛЬКО массивом JSON, без пояснений вокруг, в формате:\n"
        '[{"category":"расходы","title":"коротко суть","why":"почему — со ссылкой на цифру",'
        '"action":"что конкретно сделать","priority":1}]'
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": signals_text[:1500]}], max_tokens=1100).strip()

    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []

    clean = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not (it.get("title") or "").strip():
            continue
        try:
            pr = int(it.get("priority") or 2)
        except (TypeError, ValueError):
            pr = 2
        clean.append({
            "category": (it.get("category") or "").strip().lower()[:20] or "процессы",
            "title": str(it["title"]).strip()[:120],
            "why": str(it.get("why") or "").strip()[:400],
            "action": str(it.get("action") or "").strip()[:400],
            "priority": min(3, max(1, pr)),
        })
    return clean[:6]


def board_recommendations(business: dict, facts_text: str, avoid=None) -> list[dict]:
    """
    Совет директоров: комплексный разбор бизнеса → не более 5 главных рекомендаций.
    Возвращает список {problem, why, effect, priority 1..3}. priority: 1 высокий,
    2 средний, 3 низкий. avoid — уже решённые рекомендации, их не повторять.
    """
    avoid_block = ""
    if avoid:
        lines = []
        for a in avoid[:40]:
            mark = "уже сделано" if a.get("status") == "accepted" else "владелец отклонил"
            lines.append(f"- {str(a.get('problem'))[:90]} ({mark})")
        avoid_block = ("\nЭти рекомендации уже разобраны — НЕ повторяй их, если в данных не "
                       "появилось новых оснований:\n" + "\n".join(lines))
    system = (
        f"Ты — совет директоров бизнеса {_biz_who(business)}. Раз в день ты проводишь "
        "комплексный разбор: финансы, клиенты, контент, документы, история дел, цели и "
        "прошлые решения. На основе всего этого дай НЕ БОЛЕЕ ПЯТИ самых важных "
        "рекомендаций — только то, что действительно двигает бизнес.\n"
        "Требования: опирайся ТОЛЬКО на приведённые данные и ссылайся на конкретные цифры и "
        "факты; не выдумывай того, чего в данных нет; каждая рекомендация выполнима "
        "владельцем небольшого бизнеса; без общих слов вроде «улучшите сервис»; пиши ТОЛЬКО "
        "по-русски.\n"
        "Для каждой рекомендации:\n"
        "problem — краткое описание проблемы или возможности;\n"
        "why — почему ты пришёл к этому выводу, со ссылкой на цифру или факт из данных;\n"
        "effect — какой ожидается эффект, по возможности с оценкой;\n"
        "priority — уровень важности: 1 высокий, 2 средний, 3 низкий.\n"
        "Расставь по убыванию важности. Если данных мало, дай меньше рекомендаций — не "
        "добивай до пяти пустыми.\n"
        "Ответь ТОЛЬКО массивом JSON, без пояснений вокруг:\n"
        '[{"problem":"суть","why":"почему — со ссылкой на данные","effect":"ожидаемый эффект","priority":1}]'
        + avoid_block
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": facts_text[:2600]}], max_tokens=1500).strip()

    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []

    clean = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not (it.get("problem") or "").strip():
            continue
        try:
            pr = int(it.get("priority") or 2)
        except (TypeError, ValueError):
            pr = 2
        clean.append({
            "problem": str(it["problem"]).strip()[:200],
            "why": str(it.get("why") or "").strip()[:400],
            "effect": str(it.get("effect") or "").strip()[:400],
            "priority": min(3, max(1, pr)),
        })
    return clean[:5]


_IDEA_CATEGORIES = "услуга, акция, контент, автоматизация, партнёрство, прибыль"


def generate_ideas(business: dict, signals_text: str, avoid_titles=None) -> list[dict]:
    """
    Накидать идеи развития бизнеса по его данным. Возвращает список словарей
    {category, title, benefit, how, effort 1..3}. effort — сложность внедрения:
    1 легко, 2 средне, 3 сложно. avoid_titles — что уже предлагали, не повторять.
    """
    avoid = ""
    if avoid_titles:
        avoid = ("\nУже предлагали (НЕ повторяй эти идеи, придумай другие): "
                 + "; ".join(str(t)[:80] for t in avoid_titles[:40]))
    system = (
        f"Ты — AI-директор бизнеса {_biz_who(business)}. Придумай 6 разных идей для "
        "развития дела. Идеи должны покрывать разные направления, по одной из каждого:\n"
        "новая услуга, акция, контент для продвижения, автоматизация рутины, партнёрство, "
        "способ увеличить прибыль.\n"
        "Требования: опирайся на то, чем бизнес занимается и на его цифры; идея должна быть "
        "выполнима владельцем небольшого бизнеса, без больших вложений и команды разработки; "
        "конкретно, без общих слов вроде «улучшить сервис»; пиши ТОЛЬКО по-русски.\n"
        f"category — одно слово из списка: {_IDEA_CATEGORIES}.\n"
        "benefit — какая ожидается польза (что это даст: больше заказов, выше чек, экономия "
        "времени и т.п.), по возможности с опорой на цифры бизнеса.\n"
        "how — что конкретно сделать, чтобы это внедрить, в одну-две фразы.\n"
        "effort — сложность внедрения: 1 — легко (за пару часов), 2 — средне (за несколько "
        "дней), 3 — сложно (потребует заметных усилий).\n"
        "Ответь ТОЛЬКО массивом JSON, без пояснений вокруг, в формате:\n"
        '[{"category":"услуга","title":"коротко суть","benefit":"ожидаемая польза",'
        '"how":"как внедрить","effort":1}]'
        + avoid
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": signals_text[:1500]}], max_tokens=1400).strip()

    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []

    clean = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not (it.get("title") or "").strip():
            continue
        try:
            ef = int(it.get("effort") or 2)
        except (TypeError, ValueError):
            ef = 2
        clean.append({
            "category": (it.get("category") or "").strip().lower()[:20] or "прибыль",
            "title": str(it["title"]).strip()[:120],
            "benefit": str(it.get("benefit") or "").strip()[:400],
            "how": str(it.get("how") or "").strip()[:400],
            "effort": min(3, max(1, ef)),
        })
    return clean[:6]


_RISK_CATEGORIES = "деньги, клиенты, спрос, зависимость, процессы"


def find_risks(business: dict, signals_text: str) -> list[dict]:
    """
    Предупредить владельца о проблемах: что уже пошло не так и чем это грозит.
    Возвращает список {category, title, why, action, level 1..3}.
    """
    system = (
        f"Ты — AI-директор бизнеса {_biz_who(business)}. Тебе дают сравнение последних "
        "30 дней с предыдущими 30 и показатели зависимости. Найди 3–5 РИСКОВ: "
        "что уже ухудшается или чем бизнес уязвим.\n"
        "Правила: только по этим цифрам, каждый риск объясняй числом из данных; "
        "не пугай без оснований — если данных мало, так и напиши в объяснении; "
        "рекомендация должна быть выполнима владельцем за неделю.\n"
        "Помеченное словом ОПАСНО — самое серьёзное, ставь таким level 1.\n"
        f"Категория — одно слово из списка: {_RISK_CATEGORIES}.\n"
        "level: 1 — угроза сейчас, 2 — тревожный сигнал, 3 — держать в поле зрения.\n"
        "Ответь ТОЛЬКО массивом JSON:\n"
        '[{"category":"деньги","title":"коротко суть","why":"чем это грозит — с цифрой",'
        '"action":"что сделать, чтобы не выстрелило","level":1}]'
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": signals_text[:1500]}], max_tokens=1100).strip()

    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []

    clean = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not (it.get("title") or "").strip():
            continue
        try:
            lv = int(it.get("level") or 2)
        except (TypeError, ValueError):
            lv = 2
        clean.append({
            "category": (it.get("category") or "").strip().lower()[:20] or "процессы",
            "title": str(it["title"]).strip()[:120],
            "why": str(it.get("why") or "").strip()[:400],
            "action": str(it.get("action") or "").strip()[:400],
            "level": min(3, max(1, lv)),
        })
    return clean[:5]


def journal_entry(business: dict, facts_text: str) -> tuple[str, str]:
    """
    Ежедневная запись журнала: что произошло + одна главная рекомендация.
    Возвращает (что произошло, рекомендация). При сбое ИИ — пустые строки,
    цифры в журнале всё равно сохранятся.
    """
    system = (
        f"Ты — AI-директор бизнеса {_biz_who(business)}. Тебе дают сухие цифры за один день. "
        "Напиши короткий дневник владельца строго в двух частях:\n"
        "ИТОГ: 1–2 предложения простым языком — что за день произошло. Только по этим данным. "
        "Не добавляй статусы, которых нет в цифрах («выполнен», «оплачен», «клиент доволен»). "
        "Доход и прибыль — не одно и то же: прибыль = доход минус расход, "
        "и если расход есть, не называй доход прибылью. "
        "Если день пустой — так и скажи спокойно, без драмы.\n"
        "СОВЕТ: одна самая важная рекомендация на завтра, конкретная и выполнимая "
        "за день, вытекающая именно из этих цифр. Без общих слов вроде «развивайте бизнес»."
        + _knowledge_block(business)
    )
    out = _ask(system, [{"role": "user", "content": facts_text[:1200]}], max_tokens=400).strip()
    # модель может назвать вторую часть по-разному — ловим любой из вариантов
    m = re.search(r"(СОВЕТ|РЕКОМЕНДАЦ\w*|ГЛАВНОЕ)\b[^:\n]*:?", out, re.IGNORECASE)
    if m:
        happened, advice = out[:m.start()], out[m.end():]
    else:
        happened, advice = out, ""
    clean = lambda s: re.sub(r"^\s*(ИТОГ|ЧТО ПРОИЗОШЛО)\b[^:\n]*:?", "", s, flags=re.IGNORECASE).strip(" :\n-*")
    return clean(happened), clean(advice)


def _biz_who(business: dict) -> str:
    name = business.get("name") or "компания"
    about = business.get("about")
    return f"«{name}»" + (f" ({about})" if about else "")


def analyze_content(business: dict, text: str) -> str:
    """AI-маркетолог разбирает пост/описание: заголовок, оффер, текст, CTA, шанс привлечь клиента."""
    system = (
        f"Ты — AI-маркетолог VELOR AI при бизнесе {_biz_who(business)}. "
        "Тебе дают текст поста, описания товара или рекламы. Разбери его как маркетолог:\n"
        "1) Заголовок / первое предложение — цепляет ли, есть ли крючок.\n"
        "2) Оффер — понятна ли выгода для клиента.\n"
        "3) Текст — читаемость, лишнее, конкретика.\n"
        "4) Призыв к действию (CTA) — есть ли, сильный ли.\n"
        "5) Вероятность привлечь клиента — низкая/средняя/высокая и почему.\n"
        "В конце — 2–3 конкретные правки, что улучшить. "
        "Пиши по-русски, коротко и по делу, без воды."
    )
    system += _knowledge_block(business)
    return _ask(system, [{"role": "user", "content": text[:2000]}], max_tokens=700).strip()


_GROWTH_TASKS = {
    "plan": "Составь контент-план на месяц (по неделям): темы постов, форматы и цель каждого. "
            "Учитывай продукты и аудиторию этого бизнеса.",
    "post": "Напиши готовый пост для соцсетей этого бизнеса: цепляющее первое предложение, "
            "польза для клиента и призыв к действию.",
    "ad": "Напиши короткий рекламный текст (для таргета/объявления): сильный оффер, "
          "выгода и призыв. Дай 2 варианта.",
}


def generate_content(business: dict, task: str, topic: str = "") -> str:
    """AI-контент-стратег: контент-план, пост или реклама под конкретный бизнес."""
    instr = _GROWTH_TASKS.get(task, _GROWTH_TASKS["post"])
    system = (
        f"Ты — AI-контент-стратег VELOR AI при бизнесе {_biz_who(business)}. "
        "Ты учитываешь специфику этого бизнеса, его продукты и клиентов. "
        + instr + " Пиши по-русски, живо и по делу, без канцелярита."
    )
    system += _tone_line(business)
    system += _knowledge_block(business)
    user = topic.strip() or "Тема на твоё усмотрение — исходя из того, чем занимается бизнес."
    return _ask(system, [{"role": "user", "content": user[:800]}], max_tokens=900).strip()


def competitor_analysis(business: dict, competitor_text: str) -> str:
    """VELOR Research: сравнивает бизнес с конкурентом и находит сильные/слабые стороны."""
    system = (
        f"Ты — аналитик-конкурентная разведка VELOR AI при бизнесе {_biz_who(business)}. "
        "Тебе дают информацию о конкуренте (текст сайта, предложения, цены). "
        "Сравни с нашим бизнесом и дай разбор в три части:\n"
        "1) В чём конкурент сильнее нас (конкретно).\n"
        "2) В чём наше преимущество (или где мы можем выделиться).\n"
        "3) 2–3 конкретных шага, что сделать, чтобы выиграть.\n"
        "Опирайся на факты из текста конкурента и на то, что знаешь о нашем бизнесе. "
        "Пиши по-русски, коротко и по делу, без воды."
    )
    system += _knowledge_block(business)
    return _ask(system, [{"role": "user", "content": competitor_text[:3000]}], max_tokens=800).strip()


def finance_insights(business: dict, summary_text: str) -> str:
    """AI-финансист: по цифрам доходов/расходов даёт 2–3 конкретных вывода и рекомендации."""
    name = business.get("name") or "компания"
    system = (
        f"Ты — финансовый аналитик VELOR AI при бизнесе «{name}». "
        "Тебе дают сводку доходов и расходов. "
        "Дай 2–3 конкретных наблюдения и практичных рекомендации по этим цифрам: "
        "где теряются деньги, что убыточно, где точка роста. "
        "Пиши по-русски, коротко и по делу, без воды и без общих фраз. "
        "Опирайся только на приведённые числа, не выдумывай данных."
    )
    return _ask(system, [{"role": "user", "content": summary_text[:1200]}],
                max_tokens=500).strip()


def _system_site(business: dict) -> str:
    name = business.get("name")
    who = f" бизнеса «{name}»" if name and name != "VELOR AI" else ""
    p = (
        f"Ты — VELOR AI, живое ядро{who}: ИИ-ассистент для малого бизнеса. "
        "Тебе задают вопрос. "
        "Отвечай по-русски, коротко (1–3 предложения), дружелюбно и уверенно. "
        "Ты умеешь: принимать заказы и заявки 24/7 в мессенджерах, помнить клиентов, "
        "вести панель заказов — и подключаешься к любому бизнесу. "
        "Не выдумывай функций, которых нет."
    )
    p += _knowledge_block(business)
    return p


# ============================================================
#  Провайдер 1: Claude
# ============================================================

def _claude_chat(system: str, messages: list[dict], max_tokens=1024) -> str:
    r = _claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )
    return "".join(b.text for b in r.content if b.type == "text")


# ============================================================
#  Провайдер 2: GigaChat (Сбер)
# ============================================================

_giga_token = {"value": None, "exp": 0}

def _giga_access_token() -> str:
    """Получить/обновить токен доступа (живёт 30 минут)."""
    if _giga_token["value"] and time.time() < _giga_token["exp"] - 60:
        return _giga_token["value"]
    r = requests.post(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        headers={
            "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"scope": "GIGACHAT_API_PERS"},
        timeout=20,
        verify=False,  # у Сбера российский корневой сертификат
    )
    r.raise_for_status()
    d = r.json()
    _giga_token["value"] = d["access_token"]
    _giga_token["exp"] = d.get("expires_at", (time.time() + 1700) * 1000) / 1000
    return _giga_token["value"]


def _giga_chat(system: str, messages: list[dict], max_tokens=1024) -> str:
    token = _giga_access_token()
    r = requests.post(
        "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "model": "GigaChat",
            "messages": [{"role": "system", "content": system}] + messages,
            "max_tokens": max_tokens,
        },
        timeout=60,
        verify=False,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# отключаем предупреждение про verify=False, чтобы не пугало в логах
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


# ============================================================
#  Общий вход: выбор провайдера + запасной вариант
# ============================================================

def _providers():
    order = []
    if _claude:
        order.append(("claude", _claude_chat))
    if GIGACHAT_AUTH_KEY:
        order.append(("gigachat", _giga_chat))
    # рабочий провайдер — вперёд
    if _active:
        order.sort(key=lambda p: p[0] != _active)
    return order


def ai_available() -> bool:
    return bool(_claude or GIGACHAT_AUTH_KEY)


def ping() -> str | None:
    """Проверить при старте, какой ИИ отвечает. Возвращает имя или None."""
    global _active
    for name, fn in _providers():
        try:
            fn("Отвечай одним словом.", [{"role": "user", "content": "привет"}], max_tokens=10)
            _active = name
            return name
        except Exception:
            continue
    return None


def _ask(system: str, messages: list[dict], max_tokens=1024) -> str:
    """Спросить первый работающий ИИ; при ошибке — попробовать следующий."""
    global _active
    last = None
    for name, fn in _providers():
        try:
            out = fn(system, messages, max_tokens=max_tokens)
            _active = name
            return out
        except Exception as e:
            last = e
            if _active == name:
                _active = None
    raise last or RuntimeError("Нет настроенных ИИ")


# ============================================================
#  Публичные функции
# ============================================================

_ORDER_RE = re.compile(r"ORDER_JSON:\s*(\{.*\})", re.S)

def chat_reply(business: dict, history: list[dict], client_info: dict | None = None,
               docs: list[str] | None = None):
    """
    Один шаг диалога. history — [{"role": "user"/"assistant", "content": str}, ...],
    последним идёт сообщение клиента. docs — найденные фрагменты документов (RAG).
    Возвращает (текст_ответа, данные_заказа_или_None).
    """
    raw = _ask(_system_chat(business, client_info, docs), history)

    order = None
    m = _ORDER_RE.search(raw)
    if m:
        try:
            order = json.loads(m.group(1))
            for k in ("phone", "address", "date_wanted"):
                if isinstance(order.get(k), str) and order[k].lower() in ("null", "none", ""):
                    order[k] = None
        except json.JSONDecodeError:
            order = None
        raw = _ORDER_RE.sub("", raw)   # служебную строку клиенту не показываем

    return raw.strip(), order


def site_answer(business: dict, question: str) -> str:
    """Короткий ответ ядра на сайте."""
    return _ask(_system_site(business),
                [{"role": "user", "content": question[:500]}], max_tokens=300).strip()


def goal_actions(business: dict, goals_text: str) -> dict[int, str]:
    """
    Что сделать сегодня, чтобы прийти к каждой цели в срок.
    На входе — цели с посчитанным прогрессом и темпом, на выходе {id: совет}.
    """
    system = (
        f"Ты — AI-директор бизнеса {_biz_who(business)}. Тебе дают цели владельца: "
        "сколько уже набрано, сколько осталось и успевает ли он по темпу.\n"
        "Для КАЖДОЙ цели дай один конкретный шаг на сегодня — такой, чтобы владелец "
        "сделал его сам за день, без бюджета и найма.\n"
        "Правила: опирайся на цифры из данных и называй их; если цель отстаёт — скажи "
        "прямо, сколько нужно в день, чтобы догнать; никакой воды и общих слов "
        "вроде «улучшайте сервис»; одно предложение, максимум два.\n"
        "Ответь ТОЛЬКО массивом JSON:\n"
        '[{"id":1,"action":"что сделать сегодня"}]'
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": goals_text[:1500]}], max_tokens=900).strip()

    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}

    out = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            gid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        action = str(it.get("action") or "").strip()[:400]
        if action:
            out[gid] = action
    return out


def morning_briefing(business: dict, facts_text: str) -> dict:
    """
    Утренний отчёт руководителю: живые формулировки поверх готовых цифр.
    Возвращает {greeting, today, attention, advice} — пустые ключи допустимы.
    """
    system = (
        f"Ты — AI-директор бизнеса {_biz_who(business)}. Владелец только зашёл в кабинет. "
        "Тебе дают посчитанные цифры за вчера и текущее состояние дел. Собери короткий "
        "утренний брифинг — как личный помощник руководителю, который читает его за минуту.\n"
        "Правила: опирайся только на эти цифры и называй их; не выдумывай события и статусы, "
        "которых нет в данных; прибыль — это доход минус расход, не путай их; "
        "если день был пустой, так и скажи, не придумывай активность; "
        "каждое поле — одно предложение, максимум два; без канцелярита и общих слов; "
        "пиши ТОЛЬКО по-русски, ни одного иностранного слова.\n"
        "Поля:\n"
        "greeting — тёплое приветствие в одну строку, можно с оценкой вчерашнего дня;\n"
        "today — на чём сегодня сосредоточиться, самое главное дело дня;\n"
        "attention — что требует вмешательства прямо сейчас (или пусто, если всё ровно);\n"
        "advice — одна конкретная рекомендация, выполнимая владельцем сегодня.\n"
        "Ответь ТОЛЬКО объектом JSON:\n"
        '{"greeting":"...","today":"...","attention":"...","advice":"..."}'
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": facts_text[:2000]}], max_tokens=700).strip()

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(data.get(k) or "").strip()[:400]
            for k in ("greeting", "today", "attention", "advice")}


def weekly_review(business: dict, facts_text: str) -> dict:
    """
    Еженедельный разбор недели поверх готовых цифр.
    Возвращает {achievements, mistakes, finance, content, clients, goals, next_week}
    — короткие живые формулировки; пустые ключи допустимы.
    """
    system = (
        f"Ты — AI-директор бизнеса {_biz_who(business)}. Прошла неделя. Тебе дают "
        "посчитанные итоги недели: деньги, клиенты, заказы, контент-активность, прогресс "
        "целей и сравнение с прошлой неделей. Собери еженедельный разбор для владельца — "
        "трезво и по делу, как разбор недели с директором.\n"
        "Правила: опирайся только на эти цифры и называй их; не выдумывай события, которых "
        "нет в данных; прибыль — это доход минус расход; если неделя была пустой, так и "
        "скажи, не придумывай активность; каждое поле — одно-два предложения; без канцелярита "
        "и общих слов вроде «улучшайте сервис»; пиши ТОЛЬКО по-русски, ни одного "
        "иностранного слова.\n"
        "Поля:\n"
        "achievements — главные достижения недели (что реально получилось хорошо, с цифрами);\n"
        "mistakes — главные ошибки и потери недели (что пошло не так; если всё ровно — так и скажи);\n"
        "finance — коротко о деньгах: заработали, потратили, прибыль, куда ушло больше всего;\n"
        "content — про контент и продвижение за неделю (сколько сделано, чего не хватило);\n"
        "clients — про клиентов: сколько пришло, как шло общение;\n"
        "goals — как двигаются цели, что успевает, что отстаёт;\n"
        "next_week — план на следующую неделю: 2–3 конкретных шага, выполнимых владельцем.\n"
        "Ответь ТОЛЬКО объектом JSON:\n"
        '{"achievements":"...","mistakes":"...","finance":"...","content":"...",'
        '"clients":"...","goals":"...","next_week":"..."}'
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": facts_text[:2500]}], max_tokens=1100).strip()

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    keys = ("achievements", "mistakes", "finance", "content", "clients", "goals", "next_week")
    return {k: str(data.get(k) or "").strip()[:600] for k in keys}


def client_summary(business: dict, client_facts_text: str) -> dict:
    """
    Краткое резюме клиента + один шаг для повышения лояльности.
    На входе — факты о клиенте (контакты, сумма покупок, заказы, реплики).
    Возвращает {"summary": str, "advice": str}; пустые ключи допустимы.
    """
    system = (
        f"Ты — AI-директор бизнеса {_biz_who(business)}. Тебе дают факты об одном клиенте: "
        "контакты, сумму покупок, список заказов, предпочтения и последние реплики переписки.\n"
        "Сделай портрет клиента для владельца — коротко и по делу.\n"
        "Правила: опирайся только на эти факты и называй их (что покупает, как часто, на "
        "какую сумму, чем интересуется); ничего не выдумывай; если данных мало — так и скажи, "
        "не фантазируй; без канцелярита и общих слов; пиши ТОЛЬКО по-русски.\n"
        "Поля:\n"
        "summary — кто этот клиент, в 1–2 предложениях;\n"
        "advice — один конкретный шаг, который владелец сам сделает, чтобы повысить "
        "лояльность именно этого клиента (без наугад придуманных скидок и бюджета).\n"
        "Ответь ТОЛЬКО объектом JSON:\n"
        '{"summary":"...","advice":"..."}'
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": client_facts_text[:2000]}],
               max_tokens=600).strip()

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(data.get(k) or "").strip()[:500] for k in ("summary", "advice")}


def categorize_operations(business: dict, items: list[dict], categories: list[str]) -> dict[int, str]:
    """
    Разложить по категориям только то, что не разобралось правилами.
    items — [{"i": номер, "text": назначение платежа}], ответ — {номер: категория}.
    Одним запросом на всю пачку: дёргать модель на каждую строку выписки бессмысленно.
    """
    if not items:
        return {}
    listing = "\n".join(f'{it["i"]}. {it["text"][:160]}' for it in items[:60])
    system = (
        f"Ты — бухгалтер бизнеса {_biz_who(business)}. Тебе дают строки банковской выписки — "
        "это РАСХОДЫ. Определи категорию каждой.\n"
        f"Категории строго из списка: {', '.join(categories)}.\n"
        "Правила: если из текста непонятно — ставь «прочее», не угадывай; "
        "номера бери ровно те, что даны; ничего не пропускай.\n"
        "Ответь ТОЛЬКО массивом JSON:\n"
        '[{"i":1,"category":"аренда"}]'
        + _knowledge_block(business)
    )
    raw = _ask(system, [{"role": "user", "content": listing}], max_tokens=900).strip()

    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}

    out = {}
    for it in parsed if isinstance(parsed, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            i = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        category = str(it.get("category") or "").strip().lower()
        if category in categories:
            out[i] = category
    return out


def parse_search_query(question: str, today: str) -> dict:
    """
    Понять вопрос владельца: какие слова искать, за какой период, где именно.
    Сам поиск делает база — модель только раскладывает вопрос на условия.
    Возвращает {terms:[...], since, until, sources:[...]}.
    """
    system = (
        "Ты разбираешь поисковый запрос владельца бизнеса на условия для поиска по базе.\n"
        f"Сегодня {today}. Год бери текущий, если в вопросе не указан другой.\n"
        "terms — ключевые слова для поиска, по-русски, 1–4 штуки. Бери только значимые "
        "слова (что искать), выбрасывай «покажи», «найди», «какие», «все», «мне». "
        "Слова давай КОРНЕМ без окончания, чтобы находились любые формы: "
        "«розы» → «роз», «доставке» → «доставк», «рекламу» → «реклам».\n"
        "since и until — границы периода в виде ГГГГ-ММ-ДД, если период назван "
        "(«в июне», «за неделю», «вчера»). Если периода нет — пустые строки.\n"
        "sources — где искать, из списка: clients, orders, documents, finance, messages, memory. "
        "Если из вопроса ясно (документы, клиенты, расходы) — оставь только нужные, "
        "иначе перечисли все.\n"
        "Ответь ТОЛЬКО объектом JSON:\n"
        '{"terms":["роз"],"since":"","until":"","sources":["clients","orders"]}'
    )
    raw = _ask(system, [{"role": "user", "content": question[:400]}], max_tokens=300).strip()

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    terms = [str(t).strip() for t in (data.get("terms") or []) if str(t).strip()]
    sources = [s for s in (data.get("sources") or []) if isinstance(s, str)]
    iso = lambda v: v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v or "")) else None
    return {"terms": terms[:4], "since": iso(data.get("since")),
            "until": iso(data.get("until")), "sources": sources}


def search_answer(business: dict, question: str, found_text: str) -> str:
    """Пересказать найденное человеческим языком. Цифры уже посчитаны базой."""
    system = (
        f"Ты — AI-сотрудник бизнеса {_biz_who(business)}. Владелец задал вопрос, "
        "а система нашла в базе вот эти записи. Ответь на вопрос по ним.\n"
        "Правила: только по найденному, ничего не додумывай; если записей нет — "
        "прямо скажи, что не нашлось, и предположи почему (например, ещё не вносили); "
        "числа бери готовыми, ничего не пересчитывай и не складывай сам; "
        "коротко, 1–3 предложения, по-русски, без списков и без воды."
    )
    return _ask(system, [{"role": "user",
                          "content": f"Вопрос: {question[:300]}\n\nНайдено:\n{found_text[:2500]}"}],
                max_tokens=400).strip()
