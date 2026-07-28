"""
Работа с базой данных. Здесь — все функции чтения/записи.
Важно: почти каждая функция принимает business_id — это и есть
"универсальность": один и тот же код обслуживает любой бизнес.
"""
import datetime
import hashlib
import hmac
import os
import sqlite3
from config import DB_PATH


# ---------- ПАРОЛИ (хранятся только в виде соли+хеша) ----------

def _hash_password(raw):
    """Соль + PBKDF2-HMAC-SHA256. Формат: pbkdf2$<итераций>$<соль>$<хеш>."""
    iters = 200_000
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (raw or "").encode(), salt, iters)
    return f"pbkdf2${iters}${salt.hex()}${dk.hex()}"


def _verify_password(raw, stored):
    """Проверить пароль. Поддерживает старые записи в открытом виде (для миграции)."""
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", (raw or "").encode(),
                                     bytes.fromhex(salt_hex), int(iters))
            return hmac.compare_digest(dk.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    # старый формат — пароль хранился как есть; сравниваем и потом обновим на хеш
    return hmac.compare_digest(str(stored), str(raw or ""))


# ---------- ТАРИФЫ (SaaS-лимиты) ----------
# Лимит — число обработанных сообщений клиентов в календарный месяц.
PLANS = {
    "free":     {"name": "Free trial", "price": 0,     "limit": 100,    "note": "7 дней, до 100 сообщений"},
    "starter":  {"name": "Starter",    "price": 2990,  "limit": 2000,   "note": "1 AI-сотрудник, документы, память"},
    "business": {"name": "Business",   "price": 9990,  "limit": 10000,  "note": "5 AI-сотрудников, аналитика, финансы, контент"},
    "pro":      {"name": "Pro",        "price": 24990, "limit": 100000, "note": "Расширенные лимиты, автоматизации, интеграции"},
}
# Легаси-значения plan из старой базы («Старт» и пр.) не блокируем — считаем Business.
_LEGACY_PLAN = "business"


def plan_status(business):
    """Тариф бизнеса + расход сообщений за месяц. business — dict из get_business."""
    key = (business.get("plan") or "").strip().lower()
    if key not in PLANS:
        key = _LEGACY_PLAN
    p = PLANS[key]
    used = messages_this_month(business["id"])
    limit = p["limit"]
    return {
        "plan": key, "name": p["name"], "price": p["price"], "note": p["note"],
        "limit": limit, "used": used,
        "remaining": max(0, limit - used),
        "over": used >= limit,
    }


def _connect():
    """Открыть соединение с базой. row_factory — чтобы читать поля по имени."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Встроенный LOWER() в SQLite не понимает кириллицу: «Доставка» остаётся с
    # заглавной Д и не находится по «доставк». Подменяем на питоновский .lower(),
    # который правильно приводит регистр в юникоде — от него зависит весь поиск.
    conn.create_function("LOWER", 1, lambda s: s.lower() if s else s, deterministic=True)
    return conn


def init_db():
    """Создать таблицы из schema.sql, если их ещё нет."""
    with open("schema.sql", "r", encoding="utf-8") as f:
        sql = f.read()
    with _connect() as conn:
        conn.executescript(sql)
        # Дополняем старую базу новыми колонками CRM (если их ещё нет).
        for col, typ in [("birthday", "TEXT"), ("notes", "TEXT"), ("favorite", "TEXT"),
                         ("ai_summary", "TEXT"), ("ai_advice", "TEXT"), ("summary_day", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE clients ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # колонка уже есть
        # Описание бизнеса + поля для админки владельца.
        for tbl, col, typ in [
            ("businesses", "about", "TEXT"),
            ("businesses", "fee", "INTEGER DEFAULT 0"),     # абонплата бизнеса VELOR AI'у (твой доход)
            ("businesses", "plan", "TEXT DEFAULT 'Старт'"),  # тариф
            ("businesses", "login", "TEXT"),                 # вход бизнеса в свою панель
            ("businesses", "password", "TEXT"),
            ("businesses", "knowledge", "TEXT"),             # база знаний бизнеса (прайс, услуги, условия)
            ("businesses", "tone", "TEXT"),                   # стиль общения AI-сотрудника
            ("businesses", "ai_name", "TEXT"),                # имя AI-сотрудника (личность)
            ("businesses", "ai_avatar", "TEXT"),              # символ/эмодзи аватара
            ("businesses", "ai_traits", "TEXT"),              # черты характера через запятую
            ("businesses", "ai_desc", "TEXT"),                # описание характера своими словами
            ("businesses", "board_day", "TEXT"),              # день последнего заседания «Совета директоров»
            ("orders", "amount", "INTEGER DEFAULT 0"),       # сумма заказа (оборот бизнеса)
            # центр уведомлений живёт на тех же событиях, что и история бизнеса:
            # вторую копию не заводим, добавляем прочитанность и важность
            ("timeline", "read_at", "TEXT"),
            ("timeline", "level", "TEXT DEFAULT 'info'"),     # info | important
            # операции из выписок: откуда пришли и насколько уверены в категории
            ("finance_entries", "op_date", "TEXT"),           # дата операции по выписке
            ("finance_entries", "counterparty", "TEXT"),
            ("finance_entries", "external_id", "TEXT"),       # чтобы не задвоить при повторной загрузке
            ("finance_entries", "source", "TEXT"),            # ручной ввод | csv | xlsx | pdf | банк
            ("finance_entries", "confidence", "REAL DEFAULT 1"),
            ("finance_entries", "import_id", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        # Возможности роста: что предлагает AI-директор улучшить в бизнесе.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS opportunities (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   category    TEXT,
                   title       TEXT NOT NULL,
                   why         TEXT,
                   action      TEXT,
                   priority    INTEGER DEFAULT 2,
                   status      TEXT DEFAULT 'new',
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Refresh-токены сессий: храним только SHA-256 хеш, чтобы можно было
        # отозвать при выходе и не держать сам токен в открытом виде.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS refresh_tokens (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   token_hash  TEXT NOT NULL UNIQUE,
                   subject     TEXT NOT NULL,          -- owner | business
                   business_id INTEGER,                -- для бизнес-сессий
                   expires_at  TEXT NOT NULL,
                   revoked     INTEGER DEFAULT 0,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Идеи развития: AI постоянно накидывает, что можно попробовать.
        # benefit — ожидаемая польза, effort — сложность внедрения (1 легко .. 3 сложно).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ideas (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   category    TEXT,
                   title       TEXT NOT NULL,
                   benefit     TEXT,
                   how         TEXT,
                   effort      INTEGER DEFAULT 2,
                   status      TEXT DEFAULT 'new',
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Совет директоров: раз в день AI выдаёт до 5 главных рекомендаций
        # по всему бизнесу и запоминает решения владельца (принять/отложить/игнор).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS board_recs (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   day         TEXT,                   -- день заседания
                   fingerprint TEXT,                   -- нормализованная суть — для защиты от повторов
                   problem     TEXT NOT NULL,          -- краткое описание проблемы/возможности
                   why         TEXT,                   -- почему AI пришёл к выводу
                   effect      TEXT,                   -- ожидаемый эффект
                   priority    INTEGER DEFAULT 2,      -- 1 высокий, 2 средний, 3 низкий
                   status      TEXT DEFAULT 'new',     -- new | accepted | deferred | ignored
                   decided_at  TEXT,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Риски: о чём AI-директор предупреждает владельца.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS risks (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   category    TEXT,
                   title       TEXT NOT NULL,
                   why         TEXT,
                   action      TEXT,
                   level       INTEGER DEFAULT 2,
                   status      TEXT DEFAULT 'new',
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Ежедневный AI Journal: по одной записи на день на бизнес.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS journal (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   day         TEXT NOT NULL,
                   happened    TEXT,
                   clients_new INTEGER DEFAULT 0,
                   docs_new    INTEGER DEFAULT 0,
                   income      INTEGER DEFAULT 0,
                   expense     INTEGER DEFAULT 0,
                   advice      TEXT,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(business_id, day)
               )"""
        )
        # История бизнеса (timeline): важные события компании.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS timeline (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   kind        TEXT NOT NULL,
                   title       TEXT NOT NULL,
                   detail      TEXT,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Свои AI-сотрудники бизнеса (кастомные роли поверх одного движка).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agents (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   name        TEXT NOT NULL,
                   avatar      TEXT,
                   persona     TEXT NOT NULL,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Память AI: услуги, товары, правила и цели — то, что владелец
        # заносит списком, а не одним текстом базы знаний.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_facts (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   kind        TEXT NOT NULL,          -- service | product | rule | goal
                   title       TEXT NOT NULL,
                   body        TEXT,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Загрузки выписок: одна строка на файл, чтобы показать итог и уметь откатить.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS finance_imports (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   filename    TEXT,
                   source      TEXT,                  -- csv | xlsx | pdf | название банка
                   total       INTEGER DEFAULT 0,
                   added       INTEGER DEFAULT 0,
                   skipped     INTEGER DEFAULT 0,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Выученные категории: владелец поправил одну операцию — похожие
        # разбираются сами, без ИИ и без повторных вопросов.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS category_rules (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   pattern     TEXT NOT NULL,         -- кусок текста операции в нижнем регистре
                   category    TEXT NOT NULL,
                   hits        INTEGER DEFAULT 0,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(business_id, pattern)
               )"""
        )
        # Утренний брифинг: готовый отчёт руководителю за день, одним JSON.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS briefings (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   day         TEXT NOT NULL,
                   payload     TEXT,
                   shown_on    TEXT,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(business_id, day)
               )"""
        )
        # Еженедельный обзор бизнеса: собирается раз в неделю, копится в истории.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS weekly_reviews (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   week_start  TEXT NOT NULL,       -- понедельник недели, YYYY-MM-DD
                   payload     TEXT,                -- готовый JSON обзора
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(business_id, week_start)
               )"""
        )
        # Цели бизнеса: измеримое число к сроку. Прогресс считается из
        # собственных данных, кроме ручных метрик вроде подписчиков.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS goals (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   metric       TEXT NOT NULL,        -- income|profit|clients|orders|subscribers
                   title        TEXT NOT NULL,
                   target       INTEGER NOT NULL,
                   started_on   TEXT NOT NULL,        -- с какой даты считаем
                   deadline     TEXT,                 -- YYYY-MM-DD или пусто
                   manual_value INTEGER DEFAULT 0,    -- для метрик, которые считает сам владелец
                   status       TEXT DEFAULT 'active',-- active | done | dropped
                   advice       TEXT,                 -- совет ИИ на сегодня
                   advice_day   TEXT,
                   created_at   TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Финансы бизнеса (доходы/расходы) — модуль «AI-директор».
        conn.execute(
            """CREATE TABLE IF NOT EXISTS finance_entries (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   kind        TEXT NOT NULL,          -- 'income' | 'expense'
                   category    TEXT,
                   amount      INTEGER DEFAULT 0,
                   note        TEXT,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Документы бизнеса (RAG): загруженные файлы разбиваются на чанки.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   filename    TEXT,
                   chunks      INTEGER DEFAULT 0,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS doc_chunks (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   doc_id      INTEGER NOT NULL,
                   content     TEXT
               )"""
        )


# ---------- БИЗНЕСЫ (тенанты) ----------

def create_business(name, about=None, greeting=None):
    """Добавить новый бизнес. Возвращает его id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO businesses (name, about, greeting) VALUES (?, ?, ?)",
            (name, about, greeting),
        )
        return cur.lastrowid


def get_business(business_id):
    """Получить бизнес по id (или None)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM businesses WHERE id = ?", (business_id,)
        ).fetchone()
        if not row:
            return None
        b = dict(row)
        # Услуги, товары, правила и цели кладём сюда же: так их видит каждый
        # промпт, который и так получает бизнес, без правок в десяти местах.
        b["facts"] = _facts_text(conn, business_id)
        return b


# ---------- ЦЕЛИ БИЗНЕСА ----------

GOAL_METRICS = {
    "income":      {"name": "Доход",             "unit": "₽"},
    "profit":      {"name": "Прибыль",           "unit": "₽"},
    "clients":     {"name": "Новые клиенты",     "unit": "чел."},
    "orders":      {"name": "Выполненные заявки", "unit": "шт."},
    "subscribers": {"name": "Подписчики",        "unit": "чел."},
}


def _metric_value(conn, business_id, metric, since, manual):
    """Сколько уже набрано по метрике с даты since."""
    if metric == "subscribers":
        return int(manual or 0)          # соцсети мы не читаем — число ставит владелец
    if metric in ("income", "profit"):
        row = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN kind='income'  THEN amount END),0) AS inc,
                      COALESCE(SUM(CASE WHEN kind='expense' THEN amount END),0) AS exp
                 FROM finance_entries WHERE business_id = ? AND date(created_at) >= date(?)""",
            (business_id, since),
        ).fetchone()
        return row["inc"] if metric == "income" else row["inc"] - row["exp"]
    if metric == "clients":
        return conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE business_id = ? AND date(created_at) >= date(?)",
            (business_id, since),
        ).fetchone()["n"]
    if metric == "orders":
        return conn.execute(
            """SELECT COUNT(*) AS n FROM orders WHERE business_id = ?
                 AND status = 'done' AND date(created_at) >= date(?)""",
            (business_id, since),
        ).fetchone()["n"]
    return 0


def list_goals(business_id, only_active=False):
    """Цели с посчитанным прогрессом и темпом: успеваем или отстаём."""
    today = datetime.date.today()
    with _connect() as conn:
        sql = "SELECT * FROM goals WHERE business_id = ?"
        if only_active:
            sql += " AND status = 'active'"
        rows = conn.execute(sql + " ORDER BY status = 'active' DESC, id DESC", (business_id,)).fetchall()

        goals = []
        for r in rows:
            g = dict(r)
            meta = GOAL_METRICS.get(g["metric"], {"name": g["metric"], "unit": ""})
            g["metric_name"], g["unit"] = meta["name"], meta["unit"]
            g["current"] = _metric_value(conn, business_id, g["metric"], g["started_on"], g["manual_value"])
            target = g["target"] or 1
            g["percent"] = max(0, min(100, round(g["current"] * 100 / target)))
            g["left"] = max(0, target - g["current"])

            days_left = None
            if g["deadline"]:
                try:
                    days_left = (datetime.date.fromisoformat(g["deadline"]) - today).days
                except ValueError:
                    days_left = None
            g["days_left"] = days_left

            # темп: сравниваем набранное с тем, сколько надо было набрать к сегодня
            g["pace"] = "unknown"
            try:
                start = datetime.date.fromisoformat(g["started_on"])
                if days_left is not None:
                    total = (datetime.date.fromisoformat(g["deadline"]) - start).days
                    gone = (today - start).days
                    if total > 0 and gone >= 0:
                        g["per_day"] = round(g["left"] / days_left) if days_left > 0 else g["left"]
                        # в первый день судить об отставании ещё не по чему
                        if gone >= 1:
                            should = target * min(1.0, gone / total)
                            g["pace"] = "ahead" if g["current"] >= should else "behind"
            except ValueError:
                pass
            goals.append(g)
        return goals


def add_goal(business_id, metric, title, target, deadline=None, started_on=None):
    started_on = started_on or datetime.date.today().isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO goals (business_id, metric, title, target, started_on, deadline)
               VALUES (?,?,?,?,?,?)""",
            (business_id, metric, title, int(target), started_on, deadline or None),
        )
        gid = cur.lastrowid
    log_event(business_id, "goal", f"Поставлена цель: {title}")
    return gid


def update_goal(goal_id, business_id, **fields):
    allowed = {"title", "target", "deadline", "manual_value", "status"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    with _connect() as conn:
        conn.execute(
            "UPDATE goals SET " + ", ".join(f"{k} = ?" for k in sets)
            + " WHERE id = ? AND business_id = ?",
            (*sets.values(), goal_id, business_id),
        )
    if sets.get("status") == "done":
        log_event(business_id, "goal", "Цель достигнута")


def delete_goal(goal_id, business_id):
    with _connect() as conn:
        conn.execute("DELETE FROM goals WHERE id = ? AND business_id = ?", (goal_id, business_id))


def save_goal_advice(goal_id, business_id, advice, day):
    with _connect() as conn:
        conn.execute(
            "UPDATE goals SET advice = ?, advice_day = ? WHERE id = ? AND business_id = ?",
            (advice, day, goal_id, business_id),
        )


# ---------- ПАМЯТЬ AI: услуги, товары, правила, цели ----------

FACT_KINDS = {
    "service": "УСЛУГИ",
    "product": "ТОВАРЫ",
    "rule":    "ПРАВИЛА РАБОТЫ",
    "goal":    "ЦЕЛИ БИЗНЕСА",
}


def _facts_text(conn, business_id):
    """Услуги/товары/правила/цели одним текстом — так их читает ядро."""
    rows = conn.execute(
        "SELECT kind, title, body FROM memory_facts WHERE business_id = ? ORDER BY kind, id",
        (business_id,),
    ).fetchall()
    if not rows:
        return ""
    out = []
    for kind, caption in FACT_KINDS.items():
        items = [r for r in rows if r["kind"] == kind]
        if not items:
            continue
        out.append(caption + ":")
        for r in items:
            line = "— " + (r["title"] or "")
            if (r["body"] or "").strip():
                line += ": " + r["body"].strip()
            out.append(line)
    return "\n".join(out)


def list_facts(business_id, kind=None):
    with _connect() as conn:
        if kind:
            rows = conn.execute(
                "SELECT * FROM memory_facts WHERE business_id = ? AND kind = ? ORDER BY id DESC",
                (business_id, kind),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_facts WHERE business_id = ? ORDER BY kind, id DESC",
                (business_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def add_fact(business_id, kind, title, body=None):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO memory_facts (business_id, kind, title, body) VALUES (?,?,?,?)",
            (business_id, kind, title, body),
        )
        fid = cur.lastrowid
    log_event(business_id, "memory", f"В память добавлено: {title}")
    return fid


def update_fact(fact_id, business_id, title, body=None):
    with _connect() as conn:
        conn.execute(
            "UPDATE memory_facts SET title = ?, body = ? WHERE id = ? AND business_id = ?",
            (title, body, fact_id, business_id),
        )
    log_event(business_id, "memory", f"В памяти изменено: {title}")


def delete_fact(fact_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT title FROM memory_facts WHERE id = ? AND business_id = ?",
            (fact_id, business_id),
        ).fetchone()
        conn.execute(
            "DELETE FROM memory_facts WHERE id = ? AND business_id = ?", (fact_id, business_id)
        )
    if row:
        log_event(business_id, "memory", f"Из памяти удалено: {row['title']}")


# ---------- REFRESH-ТОКЕНЫ (JWT-сессии) ----------

def _token_hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def save_refresh_token(token, subject, business_id, expires_at):
    """Сохранить refresh-токен (только его хеш) для последующей проверки/отзыва."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO refresh_tokens (token_hash, subject, business_id, expires_at)
               VALUES (?, ?, ?, ?)""",
            (_token_hash(token), subject, business_id, expires_at),
        )


def get_valid_refresh(token):
    """Вернуть данные refresh-токена, если он не отозван и не истёк, иначе None."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM refresh_tokens
               WHERE token_hash = ? AND revoked = 0 AND expires_at > ?""",
            (_token_hash(token), datetime.datetime.utcnow().isoformat()),
        ).fetchone()
        return dict(row) if row else None


def revoke_refresh_token(token):
    """Отозвать refresh-токен (выход из системы)."""
    with _connect() as conn:
        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?",
                     (_token_hash(token),))


def purge_expired_refresh():
    """Подчистить истёкшие/отозванные токены — вызывается изредка при выпуске новых."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM refresh_tokens WHERE revoked = 1 OR expires_at <= ?",
            (datetime.datetime.utcnow().isoformat(),))


def find_business_by_login(login, password):
    """Найти бизнес по логину и паролю (для входа в его панель). None если нет.
    Старые пароли в открытом виде при первом успешном входе тихо переводятся в хеш."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM businesses WHERE login = ?", (login,)).fetchone()
        if not row or not _verify_password(password, row["password"]):
            return None
        if not str(row["password"] or "").startswith("pbkdf2$"):
            conn.execute("UPDATE businesses SET password = ? WHERE id = ?",
                         (_hash_password(password), row["id"]))
        return dict(row)


def login_taken(login):
    """Проверить, занят ли логин (для саморегистрации бизнеса)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM businesses WHERE login = ?", (login,)
        ).fetchone()
        return row is not None


def find_business_by_token(tg_bot_token):
    """Найти бизнес по токену его Telegram-бота (для мультибота). None если нет."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM businesses WHERE tg_bot_token = ?", (tg_bot_token,)
        ).fetchone()
        return dict(row) if row else None


def list_bot_businesses():
    """Бизнесы, у которых задан токен бота — каждому поднимаем свой бот."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM businesses WHERE tg_bot_token IS NOT NULL AND tg_bot_token != ''"
        ).fetchall()
        return [dict(r) for r in rows]


def update_business(business_id, **fields):
    """Обновить настройки бизнеса (название, описание, приветствие, тариф, абонплата, токен, вход)."""
    allowed = {"name", "about", "greeting", "plan", "fee", "tg_bot_token", "login", "password", "knowledge", "tone",
               "ai_name", "ai_avatar", "ai_traits", "ai_desc"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    # пароль в базе держим только как соль+хеш, никогда в открытом виде
    if sets.get("password"):
        sets["password"] = _hash_password(sets["password"])
    before = get_business(business_id) or {}
    q = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        conn.execute(
            f"UPDATE businesses SET {q} WHERE id = ?",
            (*sets.values(), business_id),
        )
    _log_business_changes(before, sets, business_id)


def _log_business_changes(before, sets, business_id):
    """Записать в историю только то, что реально изменилось и важно владельцу."""
    if "knowledge" in sets and (sets["knowledge"] or "") != (before.get("knowledge") or ""):
        old, new = len(before.get("knowledge") or ""), len(sets["knowledge"] or "")
        what = "дополнены" if new > old else "изменены"
        log_event(business_id, "knowledge", f"Знания компании {what}",
                  "услуги, цены и условия — сотрудник отвечает уже по ним")
    if "plan" in sets and sets["plan"] != before.get("plan"):
        p = PLANS.get(sets["plan"], {})
        log_event(business_id, "plan", f"Тариф: {p.get('name', sets['plan'])}",
                  f"{p.get('price', 0)} ₽/мес" if p else None)
    if "tg_bot_token" in sets and sets["tg_bot_token"] and sets["tg_bot_token"] != before.get("tg_bot_token"):
        log_event(business_id, "profile", "Подключён Telegram-бот", "клиенты пишут сотруднику напрямую")
    profile = [k for k in ("name", "about", "greeting", "tone", "ai_name", "ai_traits", "ai_desc")
               if k in sets and (sets[k] or "") != (before.get(k) or "")]
    if profile:
        titles = {"name": "название", "about": "описание", "greeting": "приветствие", "tone": "стиль общения",
                  "ai_name": "имя AI", "ai_traits": "характер AI", "ai_desc": "описание характера"}
        log_event(business_id, "profile", "Обновлён профиль компании",
                  ", ".join(titles[k] for k in profile))


def delete_business(business_id):
    """Удалить бизнес вместе со всеми его клиентами, заказами и перепиской."""
    with _connect() as conn:
        conn.execute("DELETE FROM timeline WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM agents   WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM memory_facts WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM goals        WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM finance_entries WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM briefings    WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM weekly_reviews WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM opportunities WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM ideas        WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM board_recs   WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM refresh_tokens WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM risks        WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM journal      WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM doc_chunks   WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM documents    WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM category_rules  WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM finance_imports WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM messages WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM orders   WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM clients  WHERE business_id = ?", (business_id,))
        conn.execute("DELETE FROM businesses WHERE id = ?", (business_id,))


# ---------- ВОЗМОЖНОСТИ РОСТА ----------

def growth_signals(business_id):
    """
    Факты, из которых видно, где у бизнеса резерв: спящие клиенты, структура
    расходов, маржа, давность последнего заказа. Считаем сами — ИИ не должен
    угадывать цифры, его дело придумать, что с ними делать.
    """
    fin = finance_summary(business_id)
    with _connect() as conn:
        one = lambda q, p=(): conn.execute(q, p).fetchone()[0] or 0
        clients = one("SELECT COUNT(*) FROM clients WHERE business_id = ?", (business_id,))
        # клиент считается спящим, если больше 30 дней ничего не писал
        sleeping = one(
            """SELECT COUNT(*) FROM clients c WHERE c.business_id = ?
               AND NOT EXISTS (SELECT 1 FROM messages m
                               WHERE m.client_id = c.id AND m.business_id = c.business_id
                               AND m.created_at >= date('now','-30 day'))""",
            (business_id,))
        repeat = one(
            """SELECT COUNT(*) FROM (SELECT client_id FROM orders
               WHERE business_id = ? AND client_id IS NOT NULL
               GROUP BY client_id HAVING COUNT(*) > 1)""", (business_id,))
        orders_total = one("SELECT COUNT(*) FROM orders WHERE business_id = ?", (business_id,))
        orders_open = one(
            "SELECT COUNT(*) FROM orders WHERE business_id = ? AND status = 'новый'", (business_id,))
        last_order = conn.execute(
            "SELECT MAX(date(created_at)) FROM orders WHERE business_id = ?", (business_id,)).fetchone()[0]
        msgs_30 = one(
            """SELECT COUNT(*) FROM messages WHERE business_id = ?
               AND role = 'user' AND created_at >= date('now','-30 day')""", (business_id,))
        top_expense = conn.execute(
            """SELECT category, SUM(amount) AS total FROM finance_entries
               WHERE business_id = ? AND kind = 'expense'
               GROUP BY category ORDER BY total DESC LIMIT 3""", (business_id,)).fetchall()
        top_income = conn.execute(
            """SELECT category, SUM(amount) AS total FROM finance_entries
               WHERE business_id = ? AND kind = 'income'
               GROUP BY category ORDER BY total DESC LIMIT 3""", (business_id,)).fetchall()

    margin = round(fin["profit"] / fin["income"] * 100) if fin["income"] else None

    # направления, где по одной и той же категории тратим больше, чем зарабатываем:
    # самый сильный сигнал, и его не стоит оставлять на догадку модели
    inc_by = {r["category"]: r["total"] for r in top_income}
    losing = [{"category": e["category"], "income": inc_by.get(e["category"], 0), "expense": e["total"]}
              for e in top_expense if e["total"] > inc_by.get(e["category"], 0) and e["category"] in inc_by]

    return {
        "losing": losing,
        "income": fin["income"], "expense": fin["expense"], "profit": fin["profit"], "margin": margin,
        "clients": clients, "sleeping": sleeping, "repeat_clients": repeat,
        "orders_total": orders_total, "orders_open": orders_open, "last_order": last_order,
        "messages_30d": msgs_30,
        "top_expense": [dict(r) for r in top_expense],
        "top_income": [dict(r) for r in top_income],
    }


def save_opportunities(business_id, items):
    """Заменить список возможностей на свежий (скрытые и сделанные сохраняем)."""
    with _connect() as conn:
        conn.execute("DELETE FROM opportunities WHERE business_id = ? AND status = 'new'", (business_id,))
        conn.executemany(
            """INSERT INTO opportunities (business_id, category, title, why, action, priority)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(business_id, i.get("category"), i["title"], i.get("why"), i.get("action"),
              int(i.get("priority") or 2)) for i in items],
        )


def list_opportunities(business_id, include_hidden=False):
    """Возможности: сначала важные, скрытые по умолчанию не показываем."""
    with _connect() as conn:
        q = "SELECT * FROM opportunities WHERE business_id = ?"
        if not include_hidden:
            q += " AND status != 'hidden'"
        q += " ORDER BY (status='done'), priority, id DESC"
        return [dict(r) for r in conn.execute(q, (business_id,)).fetchall()]


def set_opportunity_status(opp_id, business_id, status):
    """Отметить возможность: new / done / hidden."""
    with _connect() as conn:
        conn.execute("UPDATE opportunities SET status = ? WHERE id = ? AND business_id = ?",
                     (status, opp_id, business_id))


# ---------- ИДЕИ РАЗВИТИЯ ----------

def add_ideas(business_id, items):
    """Добавить свежие идеи в общую копилку, не дублируя уже имеющиеся по названию.
    Возвращает, сколько реально добавлено."""
    with _connect() as conn:
        have = {r["title"].strip().lower() for r in conn.execute(
            "SELECT title FROM ideas WHERE business_id = ?", (business_id,)).fetchall()}
        fresh = [i for i in items if i.get("title") and i["title"].strip().lower() not in have]
        conn.executemany(
            """INSERT INTO ideas (business_id, category, title, benefit, how, effort)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(business_id, i.get("category"), i["title"], i.get("benefit"), i.get("how"),
              int(i.get("effort") or 2)) for i in fresh],
        )
    return len(fresh)


def list_ideas(business_id, include_hidden=False):
    """Идеи: свежие сверху, простые в внедрении выше, сделанные — в конце."""
    with _connect() as conn:
        q = "SELECT * FROM ideas WHERE business_id = ?"
        if not include_hidden:
            q += " AND status != 'hidden'"
        q += " ORDER BY (status='done'), effort, id DESC"
        return [dict(r) for r in conn.execute(q, (business_id,)).fetchall()]


def idea_titles(business_id):
    """Названия уже собранных идей — чтобы модель не повторялась."""
    with _connect() as conn:
        return [r["title"] for r in conn.execute(
            "SELECT title FROM ideas WHERE business_id = ? ORDER BY id DESC LIMIT 40",
            (business_id,)).fetchall()]


def set_idea_status(idea_id, business_id, status):
    """Отметить идею: new / done / hidden."""
    with _connect() as conn:
        conn.execute("UPDATE ideas SET status = ? WHERE id = ? AND business_id = ?",
                     (status, idea_id, business_id))


# ---------- СОВЕТ ДИРЕКТОРОВ ----------

def _fingerprint(text):
    """Грубая нормализация сути рекомендации — чтобы ловить повторы."""
    import re
    words = re.findall(r"[a-zа-яё0-9]+", (text or "").lower())
    return " ".join(sorted(set(w for w in words if len(w) > 3)))[:200]


def board_decided_fingerprints(business_id):
    """Отпечатки уже решённых рекомендаций (принятых/отклонённых) — их не повторяем."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT fingerprint FROM board_recs
               WHERE business_id = ? AND status IN ('accepted','ignored') AND fingerprint != ''""",
            (business_id,),
        ).fetchall()
        return {r["fingerprint"] for r in rows}


def board_decided_titles(business_id, limit=40):
    """Тексты уже решённых рекомендаций — подсказка модели, что не предлагать снова."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT problem, status FROM board_recs
               WHERE business_id = ? AND status IN ('accepted','ignored')
               ORDER BY id DESC LIMIT ?""",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def add_board_recs(business_id, day, items):
    """Добавить свежие рекомендации. Пропускаем те, что уже решены (учёт прошлого)
    и те, что уже висят активными. Возвращает число добавленных."""
    decided = board_decided_fingerprints(business_id)
    with _connect() as conn:
        active = {r["fingerprint"] for r in conn.execute(
            "SELECT fingerprint FROM board_recs WHERE business_id = ? AND status IN ('new','deferred')",
            (business_id,)).fetchall()}
        added = 0
        for it in items:
            fp = _fingerprint(it.get("problem"))
            if fp and (fp in decided or fp in active):
                continue
            conn.execute(
                """INSERT INTO board_recs
                   (business_id, day, fingerprint, problem, why, effect, priority)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (business_id, day, fp, it["problem"], it.get("why"), it.get("effect"),
                 int(it.get("priority") or 2)),
            )
            active.add(fp)
            added += 1
    return added


def list_board_recs(business_id, limit=5):
    """Активные рекомендации (новые и отложенные) — самые приоритетные сверху, не более пяти."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM board_recs WHERE business_id = ? AND status IN ('new','deferred')
               ORDER BY (status='deferred'), priority, id DESC LIMIT ?""",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_board_history(business_id, limit=50):
    """Уже решённые рекомендации — что приняли и что отклонили."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM board_recs WHERE business_id = ? AND status IN ('accepted','ignored')
               ORDER BY decided_at DESC, id DESC LIMIT ?""",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_board_day(business_id, day):
    """Запомнить день последнего заседания совета — чтобы собирать раз в сутки."""
    with _connect() as conn:
        conn.execute("UPDATE businesses SET board_day = ? WHERE id = ?", (day, business_id))


def set_board_status(rec_id, business_id, status):
    """Решение владельца по рекомендации: accepted / deferred / ignored / new."""
    with _connect() as conn:
        conn.execute(
            "UPDATE board_recs SET status = ?, decided_at = datetime('now') WHERE id = ? AND business_id = ?",
            (status, rec_id, business_id))


# ---------- РИСКИ ----------

def _pct(now, before):
    """Насколько изменилось в процентах. None — если сравнивать не с чем."""
    if not before:
        return None
    return round((now - before) / before * 100)


def risk_signals(business_id):
    """
    Тревожные тренды: последние 30 дней против предыдущих 30. Риск виден только
    в сравнении периодов, поэтому всё считаем парами, а не одной цифрой.
    Плюс зависимость от одного источника дохода и одного клиента.
    """
    with _connect() as conn:
        def money(kind, frm, to):
            return conn.execute(
                """SELECT COALESCE(SUM(amount),0) FROM finance_entries
                   WHERE business_id = ? AND kind = ?
                   AND date(created_at) >= date('now', ?) AND date(created_at) < date('now', ?)""",
                (business_id, kind, frm, to)).fetchone()[0] or 0

        def count(table, frm, to, extra=""):
            return conn.execute(
                f"""SELECT COUNT(*) FROM {table} WHERE business_id = ? {extra}
                    AND date(created_at) >= date('now', ?) AND date(created_at) < date('now', ?)""",
                (business_id, frm, to)).fetchone()[0] or 0

        cur = {"income": money("income", "-30 day", "+1 day"),
               "expense": money("expense", "-30 day", "+1 day"),
               "clients": count("clients", "-30 day", "+1 day"),
               "orders": count("orders", "-30 day", "+1 day"),
               "messages": count("messages", "-30 day", "+1 day", "AND role='user'")}
        prev = {"income": money("income", "-60 day", "-30 day"),
                "expense": money("expense", "-60 day", "-30 day"),
                "clients": count("clients", "-60 day", "-30 day"),
                "orders": count("orders", "-60 day", "-30 day"),
                "messages": count("messages", "-60 day", "-30 day", "AND role='user'")}

        # зависимость от одного источника дохода
        inc_rows = conn.execute(
            """SELECT category, SUM(amount) AS total FROM finance_entries
               WHERE business_id = ? AND kind = 'income'
               GROUP BY category ORDER BY total DESC""", (business_id,)).fetchall()
        inc_total = sum(r["total"] for r in inc_rows) or 0
        top_source = ({"category": inc_rows[0]["category"],
                       "share": round(inc_rows[0]["total"] / inc_total * 100)} if inc_total else None)

        # зависимость от одного клиента
        cl_rows = conn.execute(
            """SELECT client_id, COUNT(*) AS n FROM orders
               WHERE business_id = ? AND client_id IS NOT NULL
               GROUP BY client_id ORDER BY n DESC""", (business_id,)).fetchall()
        orders_named = sum(r["n"] for r in cl_rows) or 0
        top_client_share = round(cl_rows[0]["n"] / orders_named * 100) if orders_named else None

        stale_orders = conn.execute(
            """SELECT COUNT(*) FROM orders WHERE business_id = ? AND status = 'новый'
               AND date(created_at) < date('now','-3 day')""", (business_id,)).fetchone()[0] or 0

    cur["profit"], prev["profit"] = cur["income"] - cur["expense"], prev["income"] - prev["expense"]
    return {
        "current": cur, "previous": prev,
        "change": {k: _pct(cur[k], prev[k]) for k in ("income", "expense", "profit", "clients",
                                                      "orders", "messages")},
        "top_source": top_source, "top_client_share": top_client_share,
        "stale_orders": stale_orders,
        "sources": len(inc_rows),
    }


def save_risks(business_id, items):
    """Заменить актуальные риски свежими (скрытые владельцем не возвращаем)."""
    with _connect() as conn:
        conn.execute("DELETE FROM risks WHERE business_id = ? AND status = 'new'", (business_id,))
        conn.executemany(
            """INSERT INTO risks (business_id, category, title, why, action, level)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(business_id, i.get("category"), i["title"], i.get("why"), i.get("action"),
              int(i.get("level") or 2)) for i in items],
        )


def list_risks(business_id, include_hidden=False):
    """Риски: сначала самые опасные."""
    with _connect() as conn:
        q = "SELECT * FROM risks WHERE business_id = ?"
        if not include_hidden:
            q += " AND status != 'hidden'"
        q += " ORDER BY level, id DESC"
        return [dict(r) for r in conn.execute(q, (business_id,)).fetchall()]


def set_risk_status(risk_id, business_id, status):
    """Отметить риск: new / hidden."""
    with _connect() as conn:
        conn.execute("UPDATE risks SET status = ? WHERE id = ? AND business_id = ?",
                     (status, risk_id, business_id))


# ---------- AI JOURNAL (ежедневный отчёт) ----------

def day_facts(business_id, day):
    """Сухие цифры за один день: клиенты, документы, деньги, события и заказы."""
    with _connect() as conn:
        one = lambda q: conn.execute(q, (business_id, day)).fetchone()[0] or 0
        clients_new = one("SELECT COUNT(*) FROM clients WHERE business_id = ? AND date(created_at) = ?")
        docs_new = one("SELECT COUNT(*) FROM documents WHERE business_id = ? AND date(created_at) = ?")
        orders_new = one("SELECT COUNT(*) FROM orders WHERE business_id = ? AND date(created_at) = ?")
        messages = one("""SELECT COUNT(*) FROM messages
                          WHERE business_id = ? AND date(created_at) = ? AND role = 'user'""")
        income = one("""SELECT COALESCE(SUM(amount),0) FROM finance_entries
                        WHERE business_id = ? AND date(created_at) = ? AND kind = 'income'""")
        expense = one("""SELECT COALESCE(SUM(amount),0) FROM finance_entries
                         WHERE business_id = ? AND date(created_at) = ? AND kind = 'expense'""")
        events = conn.execute(
            """SELECT title, detail FROM timeline
               WHERE business_id = ? AND date(created_at) = ? ORDER BY id""",
            (business_id, day),
        ).fetchall()
    return {"day": day, "clients_new": clients_new, "docs_new": docs_new, "orders_new": orders_new,
            "messages": messages, "income": income, "expense": expense,
            "events": [dict(e) for e in events]}


def save_journal(business_id, day, happened, facts, advice):
    """Сохранить (или переписать) запись журнала за день."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO journal (business_id, day, happened, clients_new, docs_new, income, expense, advice)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(business_id, day) DO UPDATE SET
                   happened=excluded.happened, clients_new=excluded.clients_new,
                   docs_new=excluded.docs_new, income=excluded.income,
                   expense=excluded.expense, advice=excluded.advice""",
            (business_id, day, happened, facts["clients_new"], facts["docs_new"],
             facts["income"], facts["expense"], advice),
        )


# ---------- ИМПОРТ ФИНАНСОВ ----------

def learned_rules(business_id):
    """Выученные категории владельца: {кусок текста: категория}, длинные первыми."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pattern, category FROM category_rules WHERE business_id = ? "
            "ORDER BY LENGTH(pattern) DESC", (business_id,),
        ).fetchall()
        return {r["pattern"]: r["category"] for r in rows}


def learn_category(business_id, pattern, category):
    """Запомнить выбор владельца, чтобы похожие операции разбирались сами."""
    pattern = (pattern or "").strip().lower()
    if len(pattern) < 3:
        return
    with _connect() as conn:
        conn.execute(
            """INSERT INTO category_rules (business_id, pattern, category, hits)
               VALUES (?,?,?,0)
               ON CONFLICT(business_id, pattern) DO UPDATE SET category = excluded.category""",
            (business_id, pattern, category),
        )


def forget_category(business_id, pattern):
    with _connect() as conn:
        conn.execute("DELETE FROM category_rules WHERE business_id = ? AND pattern = ?",
                     (business_id, pattern))


def list_category_rules(business_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM category_rules WHERE business_id = ? ORDER BY id DESC", (business_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def start_import(business_id, filename, source):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO finance_imports (business_id, filename, source) VALUES (?,?,?)",
            (business_id, filename, source),
        )
        return cur.lastrowid


def finish_import(import_id, business_id, total, added, skipped):
    with _connect() as conn:
        conn.execute(
            """UPDATE finance_imports SET total = ?, added = ?, skipped = ?
               WHERE id = ? AND business_id = ?""",
            (total, added, skipped, import_id, business_id),
        )


def known_external_ids(business_id):
    """Что уже загружали — чтобы повторная загрузка того же файла ничего не задвоила."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT external_id FROM finance_entries WHERE business_id = ? AND external_id IS NOT NULL",
            (business_id,),
        ).fetchall()
        return {r["external_id"] for r in rows}


def add_operations(business_id, operations, import_id, source):
    """Записать распознанные операции пачкой. Дубли пропускаем."""
    known = known_external_ids(business_id)
    added = 0
    with _connect() as conn:
        for op in operations:
            if op["external_id"] in known:
                continue
            known.add(op["external_id"])
            conn.execute(
                """INSERT INTO finance_entries
                     (business_id, kind, category, amount, note, created_at,
                      op_date, counterparty, external_id, source, confidence, import_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (business_id, "income" if op["direction"] == "income" else "expense",
                 op.get("category") or "прочее", op["amount"], op.get("description"),
                 op["date"] + " 12:00:00", op["date"], op.get("counterparty"),
                 op["external_id"], source, op.get("confidence", 0.0), import_id),
            )
            added += 1
    if added:
        log_event(business_id, "finance", f"Загружена выписка: {added} операций")
    return added


def list_operations(business_id, limit=300, unsure_only=False):
    """Операции из выписок. unsure_only — только те, в категории которых не уверены."""
    with _connect() as conn:
        q = ("SELECT * FROM finance_entries WHERE business_id = ? AND external_id IS NOT NULL")
        if unsure_only:
            q += " AND kind = 'expense' AND confidence < 0.8"
        q += " ORDER BY op_date DESC, id DESC LIMIT ?"
        rows = conn.execute(q, (business_id, limit)).fetchall()
        return [dict(r) for r in rows]


def set_operation_category(entry_id, business_id, category):
    """Поменять категорию операции. Возвращает саму операцию — из неё учим правило."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM finance_entries WHERE id = ? AND business_id = ?",
            (entry_id, business_id),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE finance_entries SET category = ?, confidence = 1 WHERE id = ? AND business_id = ?",
            (category, entry_id, business_id),
        )
        return dict(row)


def apply_rule_to_existing(business_id, pattern, category):
    """Применить выученное правило к уже загруженным операциям."""
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE finance_entries SET category = ?, confidence = 1
                 WHERE business_id = ? AND kind = 'expense' AND external_id IS NOT NULL
                   AND (LOWER(note) LIKE ? OR LOWER(COALESCE(counterparty,'')) LIKE ?)""",
            (category, business_id, f"%{pattern}%", f"%{pattern}%"),
        )
        return cur.rowcount


def expenses_by_category(business_id):
    """Расходы по категориям — для диаграммы."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT COALESCE(category,'прочее') AS category, SUM(amount) AS total,
                      COUNT(*) AS n
                 FROM finance_entries WHERE business_id = ? AND kind = 'expense'
                 GROUP BY COALESCE(category,'прочее') ORDER BY total DESC""",
            (business_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def save_briefing(business_id, day, payload):
    """Сохранить утренний брифинг за день (payload — уже готовый JSON-текст)."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO briefings (business_id, day, payload) VALUES (?,?,?)
               ON CONFLICT(business_id, day) DO UPDATE SET payload = excluded.payload""",
            (business_id, day, payload),
        )


def get_briefing(business_id, day):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM briefings WHERE business_id = ? AND day = ?", (business_id, day)
        ).fetchone()
        return dict(row) if row else None


def mark_briefing_shown(business_id, day):
    """Отметить, что владелец брифинг уже видел — больше сегодня не показываем."""
    with _connect() as conn:
        conn.execute(
            "UPDATE briefings SET shown_on = ? WHERE business_id = ? AND day = ?",
            (day, business_id, day),
        )


def list_briefings(business_id, limit=30):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM briefings WHERE business_id = ? ORDER BY day DESC LIMIT ?",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def save_weekly_review(business_id, week_start, payload):
    """Сохранить (или переписать) еженедельный обзор за неделю (payload — готовый JSON)."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO weekly_reviews (business_id, week_start, payload) VALUES (?,?,?)
               ON CONFLICT(business_id, week_start) DO UPDATE SET payload = excluded.payload""",
            (business_id, week_start, payload),
        )


def get_weekly_review(business_id, week_start):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM weekly_reviews WHERE business_id = ? AND week_start = ?",
            (business_id, week_start),
        ).fetchone()
        return dict(row) if row else None


def list_weekly_reviews(business_id, limit=30):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM weekly_reviews WHERE business_id = ? ORDER BY week_start DESC LIMIT ?",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def week_facts(business_id, week_start, week_end):
    """Сухие цифры за неделю [week_start; week_end] включительно: деньги, клиенты,
    заказы, контент-активность. Всё — из базы, без ИИ."""
    with _connect() as conn:
        one = lambda q, *a: conn.execute(q, a).fetchone()[0] or 0
        rng = (business_id, week_start, week_end)
        income = one("""SELECT COALESCE(SUM(amount),0) FROM finance_entries
                        WHERE business_id=? AND kind='income'
                          AND date(created_at) BETWEEN ? AND ?""", *rng)
        expense = one("""SELECT COALESCE(SUM(amount),0) FROM finance_entries
                         WHERE business_id=? AND kind='expense'
                           AND date(created_at) BETWEEN ? AND ?""", *rng)
        clients_new = one("""SELECT COUNT(*) FROM clients
                             WHERE business_id=? AND date(created_at) BETWEEN ? AND ?""", *rng)
        orders_new = one("""SELECT COUNT(*) FROM orders
                            WHERE business_id=? AND date(created_at) BETWEEN ? AND ?""", *rng)
        orders_done = one("""SELECT COUNT(*) FROM orders
                             WHERE business_id=? AND status='выполнен'
                               AND date(created_at) BETWEEN ? AND ?""", *rng)
        messages = one("""SELECT COUNT(*) FROM messages
                          WHERE business_id=? AND role='user'
                            AND date(created_at) BETWEEN ? AND ?""", *rng)
        # контент-активность недели — из ленты событий
        content = one("""SELECT COUNT(*) FROM timeline
                         WHERE business_id=? AND kind IN ('content','document','knowledge')
                           AND date(created_at) BETWEEN ? AND ?""", *rng)
        expense_cats = conn.execute(
            """SELECT COALESCE(category,'без категории') AS category, SUM(amount) AS total
               FROM finance_entries
               WHERE business_id=? AND kind='expense' AND date(created_at) BETWEEN ? AND ?
               GROUP BY category ORDER BY total DESC LIMIT 3""",
            rng,
        ).fetchall()
    return {"week_start": week_start, "week_end": week_end,
            "income": income, "expense": expense, "profit": income - expense,
            "clients_new": clients_new, "orders_new": orders_new,
            "orders_done": orders_done, "messages": messages, "content": content,
            "expense_top": [dict(r) for r in expense_cats]}


def list_journal(business_id, limit=60):
    """Записи журнала, свежие сверху."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM journal WHERE business_id = ? ORDER BY day DESC LIMIT ?",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_journal(business_id, day):
    """Запись за конкретный день, если она уже есть."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM journal WHERE business_id = ? AND day = ?", (business_id, day)
        ).fetchone()
        return dict(row) if row else None


def journal_days(business_id):
    """Какие дни уже записаны — чтобы не собирать их заново."""
    with _connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT day FROM journal WHERE business_id = ?", (business_id,)).fetchall()}


# ---------- ИСТОРИЯ БИЗНЕСА (timeline) ----------

def log_event(business_id, kind, title, detail=None, level="info", once_key=None):
    """
    Записать важное событие компании: клиент, заказ, документ, деньги, тариф, настройка.
    Ошибку записи глотаем — история не должна ломать основное действие.

    once_key — не повторять такое же событие в течение суток. Нужен для того,
    что иначе завалит ленту: ответы сотрудника клиентам и предупреждения о лимите.
    """
    try:
        with _connect() as conn:
            if once_key:
                seen = conn.execute(
                    """SELECT 1 FROM timeline WHERE business_id = ? AND kind = ? AND title = ?
                         AND date(created_at) = date('now') LIMIT 1""",
                    (business_id, kind, str(once_key)[:200]),
                ).fetchone()
                if seen:
                    return
            conn.execute(
                "INSERT INTO timeline (business_id, kind, title, detail, level) VALUES (?,?,?,?,?)",
                (business_id, kind, str(title)[:200], str(detail)[:400] if detail else None, level),
            )
    except sqlite3.Error:
        pass


# ---------- ГЛОБАЛЬНЫЙ ПОИСК ----------
#
# Ищем обычным SQL по шести источникам. ИИ здесь не ищет — он только понимает
# вопрос (какие слова искать, за какой период) и потом пересказывает найденное.
# Все цифры считает база: так ответ невозможно «придумать».

SEARCH_SOURCES = ["clients", "orders", "documents", "finance", "messages", "memory"]


def _like_clause(field, terms):
    """Условие «поле содержит любое из слов» + аргументы к нему."""
    if not terms:
        return "1", []
    return ("(" + " OR ".join(f"LOWER(COALESCE({field},'')) LIKE ?" for _ in terms) + ")",
            [f"%{t.lower()}%" for t in terms])


def _period_clause(field, since, until):
    sql, args = "", []
    if since:
        sql += f" AND date({field}) >= date(?)"
        args.append(since)
    if until:
        sql += f" AND date({field}) <= date(?)"
        args.append(until)
    return sql, args


def global_search(business_id, terms, since=None, until=None, sources=None, limit=12):
    """
    Поиск по клиентам, заказам, документам, финансам, сообщениям и памяти.
    Возвращает {источник: [найденное]} — только то, что реально есть в базе.
    """
    terms = [t for t in (terms or []) if len(t) >= 2]
    sources = sources or SEARCH_SOURCES
    out = {}

    with _connect() as conn:
        def run(sql, args):
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

        if "clients" in sources:
            # клиента ищем и по его имени, и по тому, что он заказывал
            where, args = _like_clause("c.name", terms)
            where2, args2 = _like_clause("o.text", terms)
            period, pargs = _period_clause("c.created_at", since, until)
            out["clients"] = run(
                f"""SELECT DISTINCT c.id, c.name, c.phone, c.created_at,
                           (SELECT COUNT(*) FROM orders WHERE client_id = c.id) AS orders_count
                      FROM clients c LEFT JOIN orders o ON o.client_id = c.id
                     WHERE c.business_id = ? AND ({where} OR {where2}){period}
                     ORDER BY c.id DESC LIMIT ?""",
                [business_id] + args + args2 + pargs + [limit])

        if "orders" in sources:
            where, args = _like_clause("o.text", terms)
            period, pargs = _period_clause("o.created_at", since, until)
            out["orders"] = run(
                f"""SELECT o.id, o.text, o.status, o.amount, o.created_at, c.name AS client
                      FROM orders o LEFT JOIN clients c ON c.id = o.client_id
                     WHERE o.business_id = ? AND {where}{period}
                     ORDER BY o.id DESC LIMIT ?""",
                [business_id] + args + pargs + [limit])

        if "documents" in sources:
            # ищем по содержимому кусков, показываем — файл и фрагмент
            where, args = _like_clause("ch.content", terms)
            wname, nargs = _like_clause("d.filename", terms)
            out["documents"] = run(
                f"""SELECT d.id, d.filename, MIN(ch.content) AS excerpt, d.created_at
                      FROM documents d LEFT JOIN doc_chunks ch ON ch.doc_id = d.id
                     WHERE d.business_id = ? AND ({where} OR {wname})
                     GROUP BY d.id ORDER BY d.id DESC LIMIT ?""",
                [business_id] + args + nargs + [limit])

        if "finance" in sources:
            where, args = _like_clause("note", terms)
            wcat, cargs = _like_clause("category", terms)
            wparty, pargs2 = _like_clause("counterparty", terms)
            period, pargs = _period_clause("COALESCE(op_date, created_at)", since, until)
            cond = f"({where} OR {wcat} OR {wparty})"
            all_args = [business_id] + args + cargs + pargs2 + pargs
            out["finance"] = run(
                f"""SELECT id, kind, category, amount, note, counterparty,
                           COALESCE(op_date, date(created_at)) AS day
                      FROM finance_entries WHERE business_id = ? AND {cond}{period}
                     ORDER BY day DESC, id DESC LIMIT ?""",
                all_args + [limit])
            # итог по найденному считаем в базе — не даём модели складывать самой
            totals = conn.execute(
                f"""SELECT COALESCE(SUM(CASE WHEN kind='income'  THEN amount END),0) AS income,
                           COALESCE(SUM(CASE WHEN kind='expense' THEN amount END),0) AS expense,
                           COUNT(*) AS n
                      FROM finance_entries WHERE business_id = ? AND {cond}{period}""",
                all_args).fetchone()
            out["finance_totals"] = dict(totals)

        if "messages" in sources:
            where, args = _like_clause("m.content", terms)
            period, pargs = _period_clause("m.created_at", since, until)
            out["messages"] = run(
                f"""SELECT m.id, m.role, m.content, m.created_at, c.name AS client
                      FROM messages m LEFT JOIN clients c ON c.id = m.client_id
                     WHERE m.business_id = ? AND {where}{period}
                     ORDER BY m.id DESC LIMIT ?""",
                [business_id] + args + pargs + [limit])

        if "memory" in sources:
            where, args = _like_clause("title", terms)
            wbody, bargs = _like_clause("body", terms)
            facts = run(
                f"""SELECT id, kind, title, body FROM memory_facts
                     WHERE business_id = ? AND ({where} OR {wbody})
                     ORDER BY id DESC LIMIT ?""",
                [business_id] + args + bargs + [limit])
            # плюс общая база знаний: отдаём только абзацы со словами из запроса
            row = conn.execute("SELECT knowledge FROM businesses WHERE id = ?",
                               (business_id,)).fetchone()
            knowledge = (row["knowledge"] if row else "") or ""
            hits = [p.strip() for p in knowledge.split("\n")
                    if p.strip() and any(t.lower() in p.lower() for t in terms)]
            out["memory"] = facts
            out["knowledge"] = hits[:limit]

    return out


# ---------- ЦЕНТР УВЕДОМЛЕНИЙ ----------
#
# Отдельной таблицы нет намеренно: уведомление — это то же событие бизнеса,
# только с отметкой о прочтении. Одна запись, один источник правды.

NOTIFY_KINDS = {
    "client":      "Клиенты",
    "order":       "Заказы",
    "reply":       "Ответы сотрудника",
    "document":    "Документы",
    "finance":     "Финансы",
    "risk":        "Риски",
    "opportunity": "Возможности",
    "idea":        "Идеи",
    "content":     "Контент",
    "board":       "Совет директоров",
    "plan":        "Тариф",
    "goal":        "Цели",
}


def list_notifications(business_id, kinds=None, unread_only=False, query=None, limit=200):
    """Уведомления с фильтром по типу, поиском по тексту и режимом «только новые»."""
    sql = "SELECT * FROM timeline WHERE business_id = ?"
    args = [business_id]
    if kinds:
        sql += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
        args += list(kinds)
    if unread_only:
        sql += " AND read_at IS NULL"
    if query:
        sql += " AND (LOWER(title) LIKE ? OR LOWER(COALESCE(detail,'')) LIKE ?)"
        like = f"%{query.strip().lower()}%"
        args += [like, like]
    # именно по дате: события могут записываться задним числом, и тогда
    # сортировка по id разрывает группировку по дням на странице
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def notify_plan_limit(business_id):
    """
    Предупредить, когда лимит тарифа подходит к концу или уже кончился.
    Не чаще раза в день на каждое состояние — иначе каждое сообщение клиента
    порождало бы новое уведомление.
    """
    business = get_business(business_id)
    if not business:
        return
    status = plan_status(business)
    limit = status.get("limit") or 0
    if limit <= 0:
        return
    used, remaining = status["used"], status["remaining"]

    if status["over"]:
        title = "Лимит тарифа исчерпан"
        detail = f"Использовано {used} из {limit} сообщений. Клиенты могут остаться без ответа."
    elif used >= limit * 0.8:
        title = "Заканчивается лимит тарифа"
        detail = f"Осталось {remaining} сообщений из {limit} на тарифе «{status['name']}»."
    else:
        return
    log_event(business_id, "plan", title, detail, level="important", once_key=title)


def unread_count(business_id):
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM timeline WHERE business_id = ? AND read_at IS NULL",
            (business_id,),
        ).fetchone()["n"]


def unread_by_kind(business_id):
    with _connect() as conn:
        rows = conn.execute(
            """SELECT kind, COUNT(*) AS n FROM timeline
                 WHERE business_id = ? AND read_at IS NULL GROUP BY kind""",
            (business_id,),
        ).fetchall()
        return {r["kind"]: r["n"] for r in rows}


def mark_read(business_id, event_id=None, kinds=None):
    """Отметить прочитанным одно уведомление или всё разом (можно в рамках фильтра)."""
    sql = "UPDATE timeline SET read_at = datetime('now') WHERE business_id = ? AND read_at IS NULL"
    args = [business_id]
    if event_id:
        sql += " AND id = ?"
        args.append(event_id)
    elif kinds:
        sql += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
        args += list(kinds)
    with _connect() as conn:
        return conn.execute(sql, args).rowcount


def list_events(business_id, limit=200, kind=None):
    """События бизнеса, новые сверху. kind — фильтр по типу."""
    with _connect() as conn:
        if kind:
            rows = conn.execute(
                "SELECT * FROM timeline WHERE business_id = ? AND kind = ? ORDER BY id DESC LIMIT ?",
                (business_id, kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM timeline WHERE business_id = ? ORDER BY id DESC LIMIT ?",
                (business_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def delete_event(event_id, business_id):
    """Убрать событие из истории (если попало лишнее)."""
    with _connect() as conn:
        conn.execute("DELETE FROM timeline WHERE id = ? AND business_id = ?", (event_id, business_id))


def timeline_digest(business_id, limit=14):
    """Короткая выжимка последних событий — уходит в память AI-сотрудника."""
    lines = []
    for e in list_events(business_id, limit):
        day = (e.get("created_at") or "")[:10]
        line = f"{day} — {e['title']}"
        if e.get("detail"):
            line += f" ({e['detail']})"
        lines.append(line)
    return "\n".join(lines)


# ---------- СВОИ AI-СОТРУДНИКИ (кастомные роли) ----------

def add_agent(business_id, name, persona, avatar=None):
    """Создать своего AI-сотрудника: имя, характер (persona) и символ аватара."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO agents (business_id, name, avatar, persona) VALUES (?, ?, ?, ?)",
            (business_id, name, avatar, persona),
        )
        agent_id = cur.lastrowid
    log_event(business_id, "agent", f"Создан AI-сотрудник: {name}", persona[:200])
    return agent_id


def list_agents(business_id):
    """Все свои сотрудники бизнеса (новые сверху)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agents WHERE business_id = ? ORDER BY id DESC", (business_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_agent(agent_id, business_id):
    """Один сотрудник — только своего бизнеса."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE id = ? AND business_id = ?", (agent_id, business_id)
        ).fetchone()
        return dict(row) if row else None


def delete_agent(agent_id, business_id):
    """Удалить своего сотрудника."""
    with _connect() as conn:
        conn.execute("DELETE FROM agents WHERE id = ? AND business_id = ?", (agent_id, business_id))


# ---------- АДМИНКА ВЛАДЕЛЬЦА: все бизнесы + деньги ----------

def list_businesses_with_stats():
    """
    Все подключённые бизнесы со сводкой: сколько клиентов, заказов,
    оборот (сумма заказов) и абонплата VELOR AI'у (твой доход с бизнеса).
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT b.*,
                      (SELECT COUNT(*) FROM clients c WHERE c.business_id = b.id)             AS clients_count,
                      (SELECT COUNT(*) FROM orders  o WHERE o.business_id = b.id)             AS orders_count,
                      (SELECT COALESCE(SUM(o.amount),0) FROM orders o WHERE o.business_id = b.id) AS turnover,
                      (SELECT MAX(o.created_at) FROM orders o WHERE o.business_id = b.id)     AS last_order_at
               FROM businesses b
               ORDER BY b.id"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_chats(business_id):
    """
    Список чатов бизнеса: по одному на клиента, с последним сообщением,
    числом сообщений и временем последней активности.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT client_id, name, phone, msg_count, last_msg, last_at FROM (
                   SELECT c.id AS client_id, c.name, c.phone,
                          (SELECT COUNT(*) FROM messages m WHERE m.client_id = c.id) AS msg_count,
                          (SELECT m.content FROM messages m WHERE m.client_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_msg,
                          (SELECT m.created_at FROM messages m WHERE m.client_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_at
                   FROM clients c
                   WHERE c.business_id = ?
               )
               WHERE msg_count > 0
               ORDER BY last_at DESC""",
            (business_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_chat(business_id, client_id, limit=200):
    """Полная переписка с одним клиентом."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT role, content, created_at FROM messages
               WHERE business_id = ? AND client_id = ?
               ORDER BY id ASC LIMIT ?""",
            (business_id, client_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- КЛИЕНТЫ ----------

def get_or_create_client(business_id, tg_user_id, name=None):
    """
    Найти клиента этого бизнеса по его Telegram-id, а если нет — создать.
    Так один и тот же человек не задваивается.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE business_id = ? AND tg_user_id = ?",
            (business_id, tg_user_id),
        ).fetchone()
        if row:
            return dict(row)
        cur = conn.execute(
            "INSERT INTO clients (business_id, tg_user_id, name) VALUES (?, ?, ?)",
            (business_id, tg_user_id, name),
        )
        client_id = cur.lastrowid
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    log_event(business_id, "client", "Добавлен клиент", name or f"клиент #{client_id}")
    return dict(row)


def list_clients(business_id, query=None, limit=50, offset=0):
    """Клиенты бизнеса + число заказов, сумма покупок, дата последнего заказа.
    Поддерживает поиск по имени/телефону и постраничную загрузку."""
    where = "c.business_id = ?"
    params = [business_id]
    if query:
        where += " AND (LOWER(c.name) LIKE ? OR c.phone LIKE ?)"
        like = "%" + query.strip().lower() + "%"
        params += [like, like]
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT c.*,
                      COUNT(o.id)            AS orders_count,
                      COALESCE(SUM(o.amount), 0) AS total_spent,
                      MAX(o.created_at)      AS last_order_at
               FROM clients c
               LEFT JOIN orders o ON o.client_id = c.id
               WHERE {where}
               GROUP BY c.id
               ORDER BY last_order_at DESC NULLS LAST, c.id DESC
               LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def count_clients(business_id, query=None):
    """Сколько всего клиентов подходит под фильтр — для пагинации."""
    where = "business_id = ?"
    params = [business_id]
    if query:
        where += " AND (LOWER(name) LIKE ? OR phone LIKE ?)"
        like = "%" + query.strip().lower() + "%"
        params += [like, like]
    with _connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM clients WHERE {where}", tuple(params)
        ).fetchone()["n"]


def clients_overview(business_id, query=None):
    """Итоги по клиентам под фильтр: всего, с телефоном, суммарно заказов."""
    where = "business_id = ?"
    params = [business_id]
    if query:
        where += " AND (LOWER(name) LIKE ? OR phone LIKE ?)"
        like = "%" + query.strip().lower() + "%"
        params += [like, like]
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM clients WHERE {where}", tuple(params)
        ).fetchone()["n"]
        phones = conn.execute(
            f"SELECT COUNT(*) AS n FROM clients WHERE {where} AND phone IS NOT NULL AND phone != ''",
            tuple(params),
        ).fetchone()["n"]
        orders = conn.execute(
            f"""SELECT COUNT(*) AS n FROM orders
                WHERE client_id IN (SELECT id FROM clients WHERE {where})""",
            tuple(params),
        ).fetchone()["n"]
    return {"total": total, "with_phone": phones, "orders_total": orders}


def get_client(client_id, business_id):
    """Одна карточка клиента + число заказов, сумма покупок, дата последнего заказа."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT c.*,
                      COUNT(o.id)            AS orders_count,
                      COALESCE(SUM(o.amount), 0) AS total_spent,
                      MAX(o.created_at)      AS last_order_at
               FROM clients c
               LEFT JOIN orders o ON o.client_id = c.id
               WHERE c.id = ? AND c.business_id = ?
               GROUP BY c.id""",
            (client_id, business_id),
        ).fetchone()
        return dict(row) if row else None


def get_client_messages(client_id, business_id, limit=50):
    """История переписки клиента (старые -> новые) для показа в карточке."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT role, content, created_at FROM messages
               WHERE business_id = ? AND client_id = ?
               ORDER BY id DESC LIMIT ?""",
            (business_id, client_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def save_client_summary(client_id, business_id, summary, advice, day):
    """Сохранить резюме AI и совет по лояльности + отметку дня (кэш на сутки)."""
    with _connect() as conn:
        conn.execute(
            """UPDATE clients SET ai_summary = ?, ai_advice = ?, summary_day = ?
               WHERE id = ? AND business_id = ?""",
            (summary, advice, day, client_id, business_id),
        )


def update_client(client_id, business_id, **fields):
    """Обновить карточку клиента (имя, телефон, день рождения, заметки, любимое)."""
    allowed = {"name", "phone", "birthday", "notes", "favorite"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    q = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        conn.execute(
            f"UPDATE clients SET {q} WHERE id = ? AND business_id = ?",
            (*sets.values(), client_id, business_id),
        )


def get_client_orders(client_id, business_id, limit=20):
    """Заказы одного клиента — история покупок."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM orders WHERE client_id = ? AND business_id = ?
               ORDER BY id DESC LIMIT ?""",
            (client_id, business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- ИСТОРИЯ СООБЩЕНИЙ (память диалога для ИИ) ----------

def business_stats(business_id):
    """Аналитика пользы: сколько сообщений обработано, заказов, клиентов."""
    with _connect() as conn:
        msgs = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE business_id = ? AND role = 'user'",
            (business_id,),
        ).fetchone()["n"]
        orders_total = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE business_id = ?", (business_id,)
        ).fetchone()["n"]
        orders_done = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE business_id = ? AND status IN ('принят','выполнен')",
            (business_id,),
        ).fetchone()["n"]
        clients = conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE business_id = ?", (business_id,)
        ).fetchone()["n"]
    return {"messages": msgs, "orders_total": orders_total,
            "orders_done": orders_done, "clients": clients}


# ---------- ЗДОРОВЬЕ БИЗНЕСА ----------

def _score(value, target):
    """Доля выполнения цели: 0..1, где target — «здоровый» уровень."""
    if target <= 0:
        return 1.0
    return min(1.0, value / target)


def _plural(n, one, few, many):
    """«1 день / 3 дня / 10 дней» — чтобы подписи звучали по-человечески."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = one
    elif 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def business_health(business_id):
    """
    Оценка здоровья бизнеса 0–100 по шести факторам.
    Каждый фактор — свой вес и понятная владельцу подпись.
    Возвращает {score, level, factors[], strengths[], advice[]}.
    """
    b = get_business(business_id) or {}
    stats = business_stats(business_id)
    fin = finance_summary(business_id)

    with _connect() as conn:
        active_days = conn.execute(
            """SELECT COUNT(DISTINCT date(created_at)) AS n FROM messages
               WHERE business_id = ? AND created_at >= date('now','-30 day')""",
            (business_id,),
        ).fetchone()["n"]
        docs = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE business_id = ?", (business_id,)
        ).fetchone()["n"]

    # заполненность профиля: название, описание, приветствие, стиль, личность AI
    filled = sum(1 for k in ("name", "about", "greeting", "tone", "ai_name") if (b.get(k) or "").strip())
    knowledge_len = len((b.get("knowledge") or "").strip())
    orders_total = stats["orders_total"]
    conversion = (stats["orders_done"] / orders_total) if orders_total else 0.0

    factors = [
        {"key": "activity", "name": "Активность", "weight": 20,
         "value": _score(active_days, 15),
         "fact": _plural(active_days, "активный день", "активных дня", "активных дней") + " за месяц",
         "tip": "Клиенты пишут редко — подключите бота к соцсетям и добавьте ссылку на него в профиль."},
        {"key": "clients", "name": "Клиентская база", "weight": 15,
         "value": _score(stats["clients"], 50),
         "fact": _plural(stats["clients"], "клиент", "клиента", "клиентов") + " в базе",
         "tip": "База растёт медленно — запустите повод вернуться: акцию или напоминание постоянным клиентам."},
        {"key": "finance", "name": "Финансы", "weight": 20,
         "value": 1.0 if fin["profit"] > 0 else (0.4 if fin["income"] else 0.0),
         "fact": (f"прибыль {fin['profit']:,}".replace(",", " ") + " ₽"
                  if fin["income"] or fin["expense"] else "данных о деньгах нет"),
         "tip": "Внесите доходы и расходы в разделе «Финансы» — без цифр не видно, что приносит прибыль."},
        {"key": "profile", "name": "Профиль компании", "weight": 15,
         "value": _score(filled, 5),
         "fact": f"заполнено {filled} из 5 полей",
         "tip": "Допишите профиль в настройках — описание, приветствие и характер AI влияют на каждый ответ клиенту."},
        {"key": "knowledge", "name": "База знаний", "weight": 20,
         "value": max(_score(knowledge_len, 800), _score(docs, 3)),
         "fact": (_plural(knowledge_len, "символ", "символа", "символов") + " знаний"
                  + (" и " + _plural(docs, "документ", "документа", "документов") if docs else "")),
         "tip": "Добавьте прайс, условия и частые вопросы в «Память» — сотрудник перестанет отправлять клиентов уточнять."},
        {"key": "orders", "name": "Работа с заявками", "weight": 10,
         "value": conversion if orders_total else 0.0,
         "fact": (f"{stats['orders_done']} из {orders_total} заявок доведены"
                  if orders_total else "заявок пока нет"),
         "tip": "Заявки зависают в статусе «новый» — разбирайте ленту заказов, иначе клиент уходит к конкуренту."},
    ]

    score = round(sum(f["value"] * f["weight"] for f in factors))
    for f in factors:
        f["points"] = round(f["value"] * f["weight"])
        f["value"] = round(f["value"] * 100)

    if score >= 75:
        level = "Здоровый"
    elif score >= 50:
        level = "Стабильный"
    elif score >= 25:
        level = "Требует внимания"
    else:
        level = "На старте"

    strengths = [f["name"] + ": " + f["fact"] for f in factors if f["value"] >= 70]
    advice = [{"name": f["name"], "tip": f["tip"]}
              for f in sorted(factors, key=lambda f: f["points"] - f["weight"])[:3]
              if f["value"] < 70]

    return {"score": score, "level": level, "factors": factors,
            "strengths": strengths, "advice": advice}


# ---------- ДОКУМЕНТЫ (RAG: знания из файлов) ----------

def _chunk_text(text, size=600):
    """Разбить текст на куски ~size символов по границам абзацев/предложений."""
    text = " ".join(text.split())
    chunks, buf = [], ""
    for part in text.replace("。", ". ").split(". "):
        part = part.strip()
        if not part:
            continue
        piece = (part + ". ")
        if len(buf) + len(piece) > size and buf:
            chunks.append(buf.strip())
            buf = piece
        else:
            buf += piece
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if len(c) > 20]


def add_document(business_id, filename, text):
    """Сохранить документ: создать запись и разбить текст на чанки. Возвращает (doc_id, n_chunks)."""
    chunks = _chunk_text(text)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO documents (business_id, filename, chunks) VALUES (?, ?, ?)",
            (business_id, filename, len(chunks)),
        )
        doc_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO doc_chunks (business_id, doc_id, content) VALUES (?, ?, ?)",
            [(business_id, doc_id, c) for c in chunks],
        )
    log_event(business_id, "document", "Загружен документ",
              f"{filename} · {_plural(len(chunks), 'фрагмент', 'фрагмента', 'фрагментов')}")
    return doc_id, len(chunks)


def count_events(business_id, kinds, since_days=30):
    """Сколько событий заданных типов случилось за последние N дней (для контекста совета)."""
    with _connect() as conn:
        ph = ",".join("?" * len(kinds))
        return conn.execute(
            f"""SELECT COUNT(*) AS n FROM timeline
                WHERE business_id = ? AND kind IN ({ph})
                  AND created_at >= date('now', ?)""",
            (business_id, *kinds, f"-{int(since_days)} day"),
        ).fetchone()["n"]


def all_finance_entries(business_id):
    """Все денежные операции бизнеса (для выгрузки), свежие сверху."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM finance_entries WHERE business_id = ?
               ORDER BY COALESCE(op_date, date(created_at)) DESC, id DESC""",
            (business_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_documents(business_id):
    """Загруженные документы бизнеса (имя, число чанков, дата)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE business_id = ? ORDER BY id DESC",
            (business_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_document(doc_id, business_id):
    """Удалить документ вместе с его чанками — только свой."""
    with _connect() as conn:
        conn.execute("DELETE FROM doc_chunks WHERE doc_id = ? AND business_id = ?", (doc_id, business_id))
        conn.execute("DELETE FROM documents WHERE id = ? AND business_id = ?", (doc_id, business_id))


def search_chunks(business_id, query, k=4):
    """
    Лёгкий RAG-поиск: находим чанки, где встречаются слова из вопроса.
    Без внешних сервисов — скоринг по совпадению слов (для MVP достаточно).
    """
    words = [w for w in "".join(c.lower() if c.isalnum() else " " for c in (query or "")).split() if len(w) >= 4]
    if not words:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT content FROM doc_chunks WHERE business_id = ?", (business_id,)
        ).fetchall()
    scored = []
    for r in rows:
        low = r["content"].lower()
        score = sum(low.count(w) for w in words)
        if score:
            scored.append((score, r["content"]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:k]]


def messages_this_month(business_id):
    """Сколько сообщений клиентов обработано в текущем календарном месяце (для лимита тарифа)."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM messages
               WHERE business_id = ? AND role = 'user'
                 AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')""",
            (business_id,),
        ).fetchone()
        return row["n"]


def save_message(business_id, client_id, role, content):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (business_id, client_id, role, content) VALUES (?, ?, ?, ?)",
            (business_id, client_id, role, content),
        )


def get_history(business_id, client_id, limit=20):
    """Последние сообщения диалога в формате для ИИ (старые -> новые)."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT role, content FROM messages
               WHERE business_id = ? AND client_id = ?
               ORDER BY id DESC LIMIT ?""",
            (business_id, client_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ---------- ЗАКАЗЫ ----------

def add_order(business_id, text, client_id=None, phone=None, address=None, date_wanted=None):
    """Записать новый заказ. Возвращает id заказа."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO orders (business_id, client_id, text, phone, address, date_wanted)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (business_id, client_id, text, phone, address, date_wanted),
        )
        order_id = cur.lastrowid
    log_event(business_id, "order", "Создан заказ", (text or "")[:120])
    return order_id


def get_orders(business_id, limit=20):
    """Получить последние заказы бизнеса (для просмотра/отчётов)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE business_id = ? ORDER BY id DESC LIMIT ?",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def update_order_status(order_id, status, business_id):
    """
    Сменить статус заказа — ТОЛЬКО в своём бизнесе. business_id обязателен:
    без него мы бы могли задеть чужой заказ по id (межарендная утечка), поэтому
    запрос без привязки к компании не выполняем вовсе.
    """
    if not business_id:
        raise ValueError("update_order_status требует business_id (защита арендаторов)")
    with _connect() as conn:
        conn.execute(
            "UPDATE orders SET status = ? WHERE id = ? AND business_id = ?",
            (status, order_id, business_id),
        )
    if status in ("принят", "выполнен", "отменён"):
        log_event(business_id, "order", f"Заказ №{order_id} — {status}")


# ---------- ФИНАНСЫ (модуль AI-директор) ----------

def add_finance_entry(business_id, kind, category, amount, note=None):
    """Записать доход или расход. kind = 'income' | 'expense'. Возвращает id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO finance_entries (business_id, kind, category, amount, note)
               VALUES (?, ?, ?, ?, ?)""",
            (business_id, kind, category, int(amount or 0), note),
        )
        entry_id = cur.lastrowid
    log_event(business_id, "finance",
              ("Доход" if kind == "income" else "Расход") + f": {category}",
              f"{int(amount or 0)} ₽" + (f" · {note}" if note else ""))
    return entry_id


def list_finance_entries(business_id, limit=100):
    """Последние доходы/расходы бизнеса (новые сверху)."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM finance_entries WHERE business_id = ?
               ORDER BY id DESC LIMIT ?""",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_finance_entry(entry_id, business_id):
    """Удалить запись — только свою (защита по business_id)."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM finance_entries WHERE id = ? AND business_id = ?",
            (entry_id, business_id),
        )


def finance_summary(business_id):
    """
    Сводка по деньгам: выручка, расходы, прибыль и разбивка по категориям.
    by_category — для аналитики «на что уходят деньги».
    """
    with _connect() as conn:
        income = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM finance_entries WHERE business_id = ? AND kind = 'income'",
            (business_id,),
        ).fetchone()["s"]
        expense = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM finance_entries WHERE business_id = ? AND kind = 'expense'",
            (business_id,),
        ).fetchone()["s"]
        cats = conn.execute(
            """SELECT kind, COALESCE(category,'Без категории') AS category, SUM(amount) AS total
               FROM finance_entries WHERE business_id = ?
               GROUP BY kind, category
               ORDER BY total DESC""",
            (business_id,),
        ).fetchall()
    return {
        "income": income,
        "expense": expense,
        "profit": income - expense,
        "by_category": [dict(r) for r in cats],
    }
