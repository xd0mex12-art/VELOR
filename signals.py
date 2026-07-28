# ===== VELOR — РЕАКТИВНОЕ ЯДРО =====
# «Единый интеллект»: любое изменение данных в одном модуле автоматически
# отражается на остальных. Владелец не ищет взаимосвязи — их находит VELOR.
#
# Два механизма, вместе:
#   1) ДЕТЕРМИНИРОВАННЫЕ ИНСАЙТЫ (мгновенно, без ИИ). На каждое изменение считаем
#      следствия по уже готовым цифрам и пишем их в ОБЩУЮ ленту/уведомления
#      (database.log_event). Эту ленту читают и Dashboard, и Директор, и брифинг —
#      поэтому следствие тут же видно везде, где оно уместно.
#   2) ФЛАГИ «ТРЕБУЕТ ПЕРЕСБОРКИ» (module_state). Изменение помечает зависимые
#      ИИ-модули (Директор, брифинг) устаревшими; при следующем открытии они
#      пересобираются с учётом новых данных, а не раз в сутки.
#
# Всё защищено try/except: реактивный слой никогда не должен ломать основное
# действие (создание заказа, импорт выписки и т.п.).

import calendar
import datetime

import database

# Какой домен данных на какие модули влияет. Меняются данные слева —
# помечаем «устаревшими» модули справа.
DEPENDENCIES = {
    "finance":  ("forecast", "board", "briefing", "risks"),
    "order":    ("board", "briefing", "risks", "opportunities"),
    "client":   ("board", "briefing", "opportunities"),
    "document": ("board", "briefing", "risks"),
    "goal":     ("board", "briefing"),
    "plan":     ("board", "briefing"),
}


# ---------- ФЛАГИ СВЕЖЕСТИ МОДУЛЕЙ ----------

def _ensure():
    try:
        with database._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS module_state (
                       business_id INTEGER NOT NULL,
                       module      TEXT NOT NULL,
                       dirty       INTEGER DEFAULT 0,
                       reason      TEXT,
                       updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                       PRIMARY KEY (business_id, module)
                   )"""
            )
    except Exception:
        pass


def touch(bid, modules, reason=None):
    """Пометить модули как требующие пересборки."""
    _ensure()
    try:
        with database._connect() as conn:
            for m in modules:
                conn.execute(
                    """INSERT OR REPLACE INTO module_state
                       (business_id, module, dirty, reason, updated_at)
                       VALUES (?, ?, 1, ?, CURRENT_TIMESTAMP)""",
                    (bid, m, (reason or "")[:200]),
                )
    except Exception:
        pass


def is_dirty(bid, module):
    """Нужно ли пересобрать модуль (данные менялись после последней сборки)."""
    _ensure()
    try:
        with database._connect() as conn:
            row = conn.execute(
                "SELECT dirty FROM module_state WHERE business_id = ? AND module = ?",
                (bid, module),
            ).fetchone()
            return bool(row and row[0])
    except Exception:
        return False


def settle(bid, module):
    """Модуль пересобран — снимаем флаг."""
    try:
        with database._connect() as conn:
            conn.execute(
                "UPDATE module_state SET dirty = 0, updated_at = CURRENT_TIMESTAMP "
                "WHERE business_id = ? AND module = ?",
                (bid, module),
            )
    except Exception:
        pass


# ---------- ЕДИНАЯ ТОЧКА РЕАКЦИИ ----------

def react(bid, domain, meta=None):
    """
    Данные изменились в `domain`. Помечаем зависимые модули устаревшими и
    сразу считаем детерминированные следствия. Вызывается из эндпоинтов
    после успешной записи в базу.
    """
    if not bid:
        return
    meta = meta or {}
    touch(bid, DEPENDENCIES.get(domain, ()), reason=domain)
    try:
        if domain in ("finance", "order"):
            _finance_insights(bid)
        if domain in ("order", "client"):
            _client_insights(bid)
        if domain == "document":
            _document_insights(bid, meta)
    except Exception:
        pass


def _insight(bid, kind, title, detail, level="important"):
    """Записать следствие в общую ленту (once_key = заголовок: не дублируем за сутки)."""
    database.log_event(bid, kind, title, detail, level=level, once_key=title)


def _finance_insights(bid):
    """Следствия из денег: работа в минус, скачок расходов, падение прибыли, убыточные категории."""
    sig = database.risk_signals(bid)
    g = database.growth_signals(bid)
    cur, ch = sig["current"], sig["change"]

    if cur["profit"] < 0:
        _insight(bid, "risk", "Бизнес работает в минус",
                 f"За 30 дней расход {cur['expense']} ₽ больше дохода {cur['income']} ₽ "
                 f"(прибыль {cur['profit']} ₽). Директор учтёт это в рекомендациях.")
    if ch.get("expense") is not None and ch["expense"] >= 25:
        _insight(bid, "risk", "Расходы резко выросли",
                 f"Рост расходов на {ch['expense']}% к прошлому месяцу — стоит разобраться.")
    if ch.get("profit") is not None and ch["profit"] <= -25:
        _insight(bid, "risk", "Прибыль падает",
                 f"Прибыль снизилась на {abs(ch['profit'])}% к прошлому месяцу.")
    for l in (g.get("losing") or [])[:1]:
        _insight(bid, "risk", f"Категория «{l['category']}» уходит в минус",
                 f"По ней доход {l['income']} ₽ против расхода {l['expense']} ₽.")


def _client_insights(bid):
    """Следствия из клиентской базы: сильная зависимость от одного клиента."""
    share = database.risk_signals(bid).get("top_client_share")
    if share is not None and share >= 40:
        _insight(bid, "client", "Один клиент — крупная доля заказов",
                 f"На него приходится {share}% всех заказов. "
                 f"Директор учтёт это в рекомендациях, а брифинг — в утренней сводке.")


# слова, по которым в документе можно заподозрить юридический/финансовый риск
_RISK_WORDS = ("штраф", "просроч", "задолжен", "претензи", "суд", "неустойк",
               "расторж", "долг", "пени", "нарушени", "риск", "убыт", "взыска")


def _document_insights(bid, meta):
    """Следствия из документов: тревожные слова в тексте → риск для Директора и брифинга."""
    text = (meta.get("text") or "").lower()
    fn = meta.get("filename") or "документ"
    if not text:
        return
    hits = sorted({w for w in _RISK_WORDS if w in text})
    if hits:
        database.log_event(
            bid, "risk", f"В документе «{fn}» возможен риск",
            "Встречаются: " + ", ".join(hits[:5]) + ". Директор учтёт это в брифинге.",
            level="important", once_key=f"docrisk:{fn}")


# ---------- ПРОГНОЗ (детерминированный, живой) ----------

def forecast(bid):
    """
    Прогноз на конец текущего месяца по темпу последних 30 дней. Считается на
    лету из финансов — поэтому «изменились расходы → обновился прогноз»
    происходит само, без пересборки и без ИИ.
    """
    sig = database.risk_signals(bid)
    cur, ch = sig["current"], sig["change"]
    today = datetime.date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    scale = days_in_month / 30.0
    proj_profit = round(cur["profit"] * scale)
    return {
        "month": today.strftime("%Y-%m"),
        "income": round(cur["income"] * scale),
        "expense": round(cur["expense"] * scale),
        "profit": proj_profit,
        "basis": {"income": cur["income"], "expense": cur["expense"], "profit": cur["profit"]},
        "profit_change": ch.get("profit"),
        "expense_change": ch.get("expense"),
        "direction": ("up" if (ch.get("profit") or 0) > 0
                      else "down" if (ch.get("profit") or 0) < 0 else "flat"),
    }


def top_insight(bid):
    """
    Самое важное денежное следствие для фокуса Dashboard — считается вживую,
    чтобы «проблема в финансах» появлялась на главной сразу, без ИИ.
    Возвращает {text, note, href, kind} или None.
    """
    try:
        sig = database.risk_signals(bid)
        g = database.growth_signals(bid)
        cur, ch = sig["current"], sig["change"]
    except Exception:
        return None
    if cur["profit"] < 0:
        return {"text": "Бизнес работает в минус",
                "note": f"Расход {cur['expense']} ₽ превысил доход {cur['income']} ₽ за 30 дней.",
                "href": "finance.html", "kind": "финансы"}
    if g.get("losing"):
        l = g["losing"][0]
        return {"text": f"Категория «{l['category']}» съедает прибыль",
                "note": f"Доход {l['income']} ₽ против расхода {l['expense']} ₽.",
                "href": "finance.html", "kind": "финансы"}
    if ch.get("profit") is not None and ch["profit"] <= -25:
        return {"text": "Прибыль заметно падает",
                "note": f"−{abs(ch['profit'])}% к прошлому месяцу — стоит вмешаться.",
                "href": "finance.html", "kind": "финансы"}
    return None
