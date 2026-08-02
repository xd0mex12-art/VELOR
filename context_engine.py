"""
VELOR CONTEXT ENGINE — слой сборки контекста ПОВЕРХ существующей архитектуры.

Задача: перед каждым запросом к LLM автоматически собрать максимально полный и
РЕЛЕВАНТНЫЙ контекст бизнеса, чтобы пользователь писал одно предложение
(«ответь клиенту», «сделай рекламу», «подумай»), а VELOR отвечал как настоящий
сотрудник, знающий компанию, клиента, документы, историю и цифры.

Что это НЕ делает (осознанно, ради обратной совместимости):
  • не меняет Prompt Engine — переиспользует его (build_persona/compose);
  • не меняет ai.py — переиспользует его блоки (_knowledge_block, _docs_block…)
    и единую точку вызова модели ai._ask (провайдер-агностик: Claude/GigaChat/…);
  • не меняет БД/API/бизнес-логику — только ЧИТАЕТ существующими функциями;
  • не зависит от конкретной LLM — на вход модели уходит только готовый текст.

Pipeline (respond):
  запрос → роль (Prompt Engine) → сбор контекста → память → документы (RAG) →
  динамический системный промпт (Prompt Engine.compose) → LLM (ai._ask) →
  проверка качества → ответ.

Интеграция обратно совместима: server вызывает respond() и при ЛЮБОЙ ошибке
откатывается на прежний ai.assistant_answer — поведение не ломается никогда.
"""
from __future__ import annotations

import json
import re
import time

import ai
import database
import prompt_engine


# ============================================================
#  Лёгкий кэш (производительность: не дёргать БД повторно в пределах окна)
# ============================================================
_CACHE_TTL = 20          # секунд — короткое окно: дедуп в рамках серии запросов,
_cache: dict = {}        # но данные не успевают «протухнуть» для пользователя.


def _cached(key, producer):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    val = producer()
    _cache[key] = (now, val)
    return val


def _business(bid):
    return _cached(("biz", bid), lambda: database.get_business(bid) or {})


def _finance(bid):
    return _cached(("fin", bid), lambda: _safe(lambda: database.finance_summary(bid), {}))


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


# ============================================================
#  Определение сути запроса (термины для Memory Builder / RAG)
# ============================================================
# Убираем командные глаголы и служебные слова — оставляем значимые существительные
# (клиент, договор, реклама, доставка…), по которым ищем похожее в базе.
_STOP = {
    "напиши", "сделай", "ответь", "подумай", "покажи", "дай", "составь", "придумай",
    "нужно", "хочу", "надо", "это", "весь", "вся", "все", "вот", "как", "что", "чем",
    "мне", "нам", "для", "под", "про", "или", "меня", "мой", "моя", "мои", "дальше",
    "сейчас", "пожалуйста", "можешь", "давай", "будет", "есть",
}


def _terms(question: str) -> list[str]:
    words = re.findall(r"[\wа-яёА-ЯЁ]{4,}", (question or "").lower())
    out = []
    for w in words:
        if w in _STOP or w in out:
            continue
        out.append(w)
    return out[:5]


# ============================================================
#  БЛОКИ КОНТЕКСТА
#  Базовые блоки (тон, знания, документы, история) берём ГОТОВЫМИ из ai.py —
#  чтобы сохранить проверенные guardrail'ы (запрет выдумывать ассортимент и т.п.).
#  Новые блоки (компания, CRM, финансы, клиент, память) добавляем поверх.
# ============================================================

def _company_block(b: dict) -> str:
    """Расширенная справка о компании. Работает с полями, если они есть в БД
    (миссия/ценности/ниша/страна/город/часовой пояс) — иначе тихо пропускает.
    Название/описание/тон уже уходят в identity и _tone_line, здесь не дублируем."""
    pairs = [("mission", "Миссия"), ("values", "Ценности"), ("niche", "Ниша"),
             ("country", "Страна"), ("city", "Город"), ("timezone", "Часовой пояс")]
    bits = []
    for key, label in pairs:
        v = b.get(key)
        v = v.strip() if isinstance(v, str) else v
        if v:
            bits.append(f"{label}: {v}")
    return ("\n\nО КОМПАНИИ:\n" + "\n".join(bits)) if bits else ""


def _snapshot_block(bid: int, snapshot: str | None) -> str:
    if snapshot:
        return "\n\nТекущие данные бизнеса: " + snapshot + "."
    orders = _safe(lambda: database.get_orders(bid, limit=20), []) or []
    clients = _safe(lambda: database.list_clients(bid, limit=20), []) or []
    if not orders and not clients:
        return ""
    new = sum(1 for o in orders if (o.get("status") if isinstance(o, dict) else None) == "новый")
    return f"\n\nТекущие данные бизнеса: заказов {len(orders)}, новых {new}, клиентов {len(clients)}."


def _crm_block(bid: int) -> str:
    """Свежий срез CRM: последние заявки и последние клиенты — чтобы сотрудник был
    «в курсе дел» без отдельного вопроса."""
    orders = _safe(lambda: database.get_orders(bid, limit=5), []) or []
    lines = []
    if orders:
        lines.append("Последние заявки:")
        for o in orders:
            txt = (o.get("text") or "").strip()[:70]
            st = o.get("status") or "новый"
            lines.append(f"  · {txt} [{st}]")
    clients = _safe(lambda: database.list_clients(bid, limit=5), []) or []
    if clients:
        names = ", ".join((c.get("name") or "без имени") for c in clients[:5])
        lines.append("Недавние клиенты: " + names)
    return ("\n\nCRM (свежее):\n" + "\n".join(lines)) if lines else ""


def _finance_block(bid: int) -> str:
    fs = _finance(bid) or {}
    income = int(fs.get("income") or 0)
    expense = int(fs.get("expense") or 0)
    if income == 0 and expense == 0:
        return ""
    profit = int(fs.get("profit") or (income - expense))
    lines = [f"Доход {income} ₽, расход {expense} ₽, прибыль {profit} ₽."]
    cats = fs.get("by_category") or []
    top = [c for c in cats if c.get("kind") == "expense"][:3]
    if top:
        lines.append("Крупные расходы: "
                     + "; ".join(f"{c.get('category')}: {int(c.get('total') or 0)} ₽" for c in top))
    if expense > income and income > 0:
        lines.append("Внимание: расходы превышают доход — возможна аномалия.")
    return "\n\nФИНАНСЫ (только по данным, ничего не досчитывать):\n" + "\n".join(lines)


def _client_block(bid: int, client_id, with_messages: bool = True) -> str:
    """Досье клиента — если запрос идёт в контексте конкретного клиента.
    with_messages=False — не подкладывать переписку (в клиентском диалоге история
    уже уходит в messages, дублировать не нужно)."""
    if not client_id:
        return ""
    c = _safe(lambda: database.get_client(client_id, bid), None)
    if not c:
        return ""
    lines = [f"Имя: {c.get('name') or 'без имени'}"]
    for key, label in [("phone", "Телефон"), ("favorite", "Предпочтения"), ("notes", "Заметки")]:
        if (c.get(key) or "").strip():
            lines.append(f"{label}: {c[key]}")
    orders = _safe(lambda: database.get_client_orders(client_id, bid, limit=6), []) or []
    if orders:
        lines.append("Заказы: " + "; ".join((o.get("text") or "").strip()[:50] for o in orders[:6]))
    if with_messages:
        msgs = _safe(lambda: database.get_client_messages(client_id, bid, limit=8), []) or []
        if msgs:
            lines.append("Последние реплики:")
            for m in msgs[-6:]:
                who = "клиент" if m.get("role") == "user" else "сотрудник"
                lines.append(f"  {who}: {(m.get('content') or '').strip()[:120]}")
    return "\n\nКЛИЕНТ (история — опирайся на неё, узнавай постоянного):\n" + "\n".join(lines)


def _memory_block(bid: int, question: str) -> str:
    """Memory Builder: похожие ситуации в базе по теме запроса (заявки, документы,
    переписка, память, клиенты). Только релевантное — по ключевым словам вопроса."""
    terms = _terms(question)
    if not terms:
        return ""
    res = _safe(lambda: database.global_search(
        bid, terms, sources=["orders", "documents", "messages", "memory"], limit=6), {}) or {}
    frag = []
    for src, label in [("orders", "заявки"), ("documents", "документы"),
                       ("messages", "переписка"), ("memory", "заметки")]:
        items = res.get(src) or []
        if not items:
            continue
        sample = []
        for it in items[:3]:
            if isinstance(it, dict):
                txt = it.get("text") or it.get("content") or it.get("title") or it.get("body") or ""
            else:
                txt = str(it)
            txt = str(txt).strip()[:90]
            if txt:
                sample.append(txt)
        if sample:
            frag.append(f"{label}: " + " | ".join(sample))
    if not frag:
        return ""
    return ("\n\nПОХОЖЕЕ В БАЗЕ (по теме запроса — используй, если уместно):\n"
            + "\n".join(frag))


def _docs_block(bid: int, question: str) -> str:
    """RAG: не отправляем все документы — ищем только релевантные фрагменты под
    вопрос и отдаём выжимку. Рендер и guardrail берём готовыми из ai._docs_block."""
    docs = _safe(lambda: database.search_chunks(bid, question, k=4), []) or []
    return ai._docs_block(docs)


# ============================================================
#  ДИНАМИЧЕСКИЙ СИСТЕМНЫЙ ПРОМПТ (Identity+Business+Knowledge+Memory+Client+CRM+
#  Finance+Role+Task+Rules) — собираем через существующий Prompt Engine.
# ============================================================

def _base_identity(business: dict) -> str:
    name = business.get("name") or "компания"
    about = business.get("about")
    who = f"«{name}»" + (f" ({about})" if about else "")
    return (
        f"Ты — VELOR AI, цифровой сотрудник бизнеса {who}. "
        + ai._identity(business) +
        "Ты не языковая модель и не чат-бот, а опытный член команды: пользователь "
        "должен чувствовать, что говорит с живым профессионалом. Ты партнёр, "
        "ориентированный на результат: видишь проблему или возможность — коротко "
        "предлагаешь, но не навязываешься."
    )


def build_system(business: dict, question: str, *, role: str | None = None,
                 client_id=None, snapshot: str | None = None,
                 persona: str | None = None) -> str:
    """Собрать полный динамический системный промпт. Роль определяет Prompt Engine
    сам по тексту запроса (пользователь её не выбирает)."""
    bid = business.get("id")
    role_key, mandate = prompt_engine.build_persona(
        business, question, ui_role=role, forced_persona=persona)

    ctx = ai._tone_line(business)
    ctx += _company_block(business)
    ctx += _snapshot_block(bid, snapshot)
    ctx += ai._knowledge_block(business)                       # товары/услуги/цены + guardrail
    ctx += ai._timeline_block(_safe(lambda: database.timeline_digest(bid), ""))
    ctx += _finance_block(bid)
    ctx += _crm_block(bid)
    ctx += _client_block(bid, client_id)
    ctx += _memory_block(bid, question)
    ctx += _docs_block(bid, question)

    return prompt_engine.compose(business, _base_identity(business), mandate, role_key, ctx)


# ============================================================
#  ПРОВЕРКА КАЧЕСТВА ОТВЕТА (Output Check)
# ============================================================
_FILLER = (
    "конечно!", "конечно,", "разумеется", "вот несколько", "надеюсь, это поможет",
    "надеюсь, помог", "как ии", "как языковая модель", "стоит отметить, что",
    "в целом можно сказать", "важно понимать, что", "давайте разберём",
)


def _quality_issues(text: str) -> list[str]:
    low = (text or "").lower()
    issues = []
    if any(f in low for f in _FILLER):
        issues.append("шаблон/вода")
    sents = [s.strip() for s in re.split(r"[.!?\n]+", low) if len(s.strip()) > 12]
    if len(sents) != len(set(sents)):
        issues.append("повтор")
    return issues


def check_and_improve(text: str, business: dict) -> str:
    """Если в ответе вода/штампы/повторы — один проход улучшения (провайдер-агностик).
    Если модель недоступна или улучшение пустое — возвращаем исходный текст."""
    if not text or not _quality_issues(text):
        return text
    system = (
        "Ты — строгий редактор VELOR. Перепиши ответ короче и сильнее: убери воду, "
        "штампы, повторы и фразы вроде «конечно», «как ИИ», «надеюсь, поможет». "
        "Сохрани ВСЕ факты, цифры и смысл, держи живой деловой тон, пиши по делу. "
        "Верни ТОЛЬКО улучшенный текст, без пояснений." + ai._tone_line(business)
    )
    try:
        better = ai._ask(system, [{"role": "user", "content": text[:1600]}],
                         max_tokens=700).strip()
        return better or text
    except Exception:
        return text


# ============================================================
#  ГЛАВНЫЙ ВХОД
# ============================================================

def respond(business_id: int, question: str, *, role: str | None = None,
            client_id=None, snapshot: str | None = None, max_tokens: int = 600) -> str:
    """Полный цикл: контекст → системный промпт → LLM → проверка качества → ответ.
    LLM-агностик: сама модель выбирается в ai._ask (Claude/GigaChat/…)."""
    business = _business(business_id)
    # Свой AI-сотрудник владельца (роль agent:N) — характер берём из БД, как в кабинете.
    persona = None
    if role and role.startswith("agent:") and role[6:].isdigit():
        ag = _safe(lambda: database.get_agent(int(role[6:]), business_id), None)
        if ag:
            persona = f"Сейчас ты — {ag['name']}. {ag['persona']}"

    system = build_system(business, question, role=role, client_id=client_id,
                          snapshot=snapshot, persona=persona)
    answer = ai._ask(system, [{"role": "user", "content": (question or "")[:800]}],
                     max_tokens=max_tokens).strip()
    return check_and_improve(answer, business)


def respond_chat(business_id: int, history: list[dict], *, client_info: dict | None = None,
                 client_id=None):
    """Клиентский диалог (Telegram) через Context Engine. Возвращает (reply, order|None) —
    совместимо с ai.chat_reply, чтобы botcore не менял свою логику.

    Клиентский путь держим лёгким (горячий вебхук): базовый системный промпт
    диалога с клиентом (ai._system_chat — там тон, база знаний с guardrail, docs-RAG
    и инструкция ORDER_JSON) ОБОГАЩАЕМ досье клиента, чтобы узнавать постоянного.
    Внутренние CRM/финансы владельца в ответ клиенту НЕ подкладываем. Второго
    LLM-прохода (Output Check) здесь нет — ответ клиенту должен быть быстрым, а
    служебная строка ORDER_JSON не должна пострадать от переписывания."""
    business = _business(business_id)
    last_user = ""
    for m in reversed(history or []):
        if m.get("role") == "user":
            last_user = m.get("content") or ""
            break
    docs = _safe(lambda: database.search_chunks(business_id, last_user, k=4), []) or []

    system = ai._system_chat(business, client_info, docs)
    system += _client_block(business_id, client_id, with_messages=False)

    raw = ai._ask(system, history)

    # Извлечение заказа — та же логика, что в ai.chat_reply (бизнес-логику не меняем).
    order = None
    m = ai._ORDER_RE.search(raw)
    if m:
        try:
            order = json.loads(m.group(1))
            for k in ("phone", "address", "date_wanted"):
                if isinstance(order.get(k), str) and order[k].lower() in ("null", "none", ""):
                    order[k] = None
        except json.JSONDecodeError:
            order = None
        raw = ai._ORDER_RE.sub("", raw)

    return raw.strip(), order
