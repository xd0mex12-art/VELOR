"""
Работа с базой данных. Здесь — все функции чтения/записи.
Важно: почти каждая функция принимает business_id — это и есть
"универсальность": один и тот же код обслуживает любой бизнес.
"""
import datetime
import hashlib
import hmac
import os
import re
import sqlite3
from config import DB_PATH, IS_PRODUCTION

# ============================================================
#  ВЫБОР БАЗЫ: SQLite (локально) или PostgreSQL (прод, Supabase)
# ============================================================
# Если задан DATABASE_URL (postgres://…) — работаем с Postgres через тонкий
# адаптер, который на лету переводит SQLite-диалект в Postgres (плейсхолдеры ?→%s,
# функции дат, DDL). Если нет — всё как раньше, SQLite. Так локальная разработка не
# меняется, а на Render/Supabase данные живут постоянно.
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
_pg_pool = None

# Защита от случайного запуска на эфемерном SQLite в бою: если APP_ENV=production,
# но DATABASE_URL не задан (или не Postgres) — не стартуем вовсе. Иначе данные
# клиентов молча писались бы в SQLite и терялись при каждом передеплое Render.
if IS_PRODUCTION and not _PG:
    raise RuntimeError(
        "ОСТАНОВКА ЗАПУСКА (APP_ENV=production): не задан DATABASE_URL (Postgres). "
        "Без него данные писались бы в эфемерный SQLite и терялись при передеплое. "
        "Укажите строку подключения Postgres в переменной окружения DATABASE_URL "
        "(Supabase → Project Settings → Database → Connection string)."
    )

if _PG:
    from psycopg_pool import ConnectionPool
    # Пул: не открываем новое TLS-соединение на каждый из сотен запросов.
    # prepare_threshold=None — чтобы работать и через транзакционный пулер Supabase.
    _pg_pool = ConnectionPool(
        DATABASE_URL, min_size=1, max_size=5, open=True,
        kwargs={"autocommit": False, "prepare_threshold": None},
    )


class _Row(dict):
    """Строка результата: и по имени row['col'], и по индексу row[0] — как sqlite3.Row."""
    def __init__(self, cols, vals):
        super().__init__(zip(cols, vals))
        self._vals = list(vals)

    def __getitem__(self, k):
        return self._vals[k] if isinstance(k, int) else super().__getitem__(k)


def _pg_row_factory(cursor):
    cols = [c.name for c in cursor.description] if cursor.description else []
    return lambda vals: _Row(cols, vals)


_RE_DATE_NOW_PARAM = re.compile(r"date\(\s*'now'\s*,\s*\?\s*\)")
_RE_DATE_NOW_LIT = re.compile(r"date\(\s*'now'\s*,\s*'([^']+)'\s*\)")
_RE_STRFTIME_COL = re.compile(r"strftime\(\s*'%Y-%m'\s*,\s*([A-Za-z_][\w.]*)\s*\)")
_RE_DATE_COL = re.compile(r"date\(\s*([A-Za-z_][\w.]*)\s*\)")


def _translate(sql: str) -> str:
    """Перевести SQLite-SQL в PostgreSQL: даты, DDL, плейсхолдеры."""
    s = sql
    # ---- функции дат (время храним текстом 'YYYY-MM-DD HH:MM:SS', как в SQLite) ----
    s = _RE_DATE_NOW_PARAM.sub("to_char((now() at time zone 'utc') + (%s)::interval, 'YYYY-MM-DD')", s)
    s = _RE_DATE_NOW_LIT.sub(lambda m: "to_char((now() at time zone 'utc') + interval '%s', 'YYYY-MM-DD')" % m.group(1), s)
    s = s.replace("datetime('now')", "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')")
    s = s.replace("date('now')", "to_char(now() at time zone 'utc', 'YYYY-MM-DD')")
    s = re.sub(r"strftime\(\s*'%Y-%m'\s*,\s*'now'\s*\)", "to_char(now() at time zone 'utc', 'YYYY-MM')", s)
    s = _RE_STRFTIME_COL.sub(r"substr(\1, 1, 7)", s)
    s = _RE_DATE_COL.sub(r"substr(\1, 1, 10)", s)   # date('now') уже заменён выше
    # ---- DDL: целые числа = 64 бита (Telegram id, совпадение типов для FK) ----
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY(\s+AUTOINCREMENT)?", "BIGSERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bAUTOINCREMENT\b", "", s, flags=re.I)
    s = re.sub(r"\bINTEGER\b", "BIGINT", s, flags=re.I)
    # Двоичные данные: в SQLite это BLOB, в Postgres — BYTEA. Нужен для
    # оригиналов файлов Inbox, которые на Render живут в базе, а не на диске.
    s = re.sub(r"\bBLOB\b", "BYTEA", s, flags=re.I)
    s = re.sub(r"DEFAULT\s+CURRENT_TIMESTAMP",
               "DEFAULT to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')", s, flags=re.I)
    s = re.sub(r"CREATE\s+TABLE\s+(?!IF NOT EXISTS)", "CREATE TABLE IF NOT EXISTS ", s, flags=re.I)
    s = re.sub(r"ADD\s+COLUMN\s+(?!IF NOT EXISTS)", "ADD COLUMN IF NOT EXISTS ", s, flags=re.I)
    # ---- плейсшолдеры в самом конце ----
    return s.replace("?", "%s")


def _split_sql(script: str):
    """Разбить многооператорный скрипт на отдельные операторы (у Postgres нет executescript)."""
    return [st for st in script.split(";") if st.strip()]


class _PGCursor:
    """Обёртка курсора psycopg под интерфейс sqlite3 (execute/fetchone/fetchall/lastrowid)."""
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())

    @property
    def rowcount(self):
        """Число затронутых строк — как у sqlite3.Cursor. Без этого свойства
        mark_read() и другие UPDATE/DELETE, читающие .rowcount, падали на Postgres
        с AttributeError → откат транзакции → изменения (напр. «прочитано») терялись."""
        return self._cur.rowcount

    @property
    def lastrowid(self):
        self._cur.execute("SELECT lastval()")
        return self._cur.fetchone()[0]


class _PGConn:
    """Обёртка соединения psycopg под интерфейс sqlite3.Connection, что ждёт код."""
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(_translate(sql), tuple(params))
        return _PGCursor(cur)

    def executemany(self, sql, seq_of_params):
        """Пакетная вставка/обновление — как в sqlite3.Connection. Без этого метода
        add_document и AI-директор (возможности/идеи/риски) падали бы на Postgres."""
        cur = self._raw.cursor()
        cur.executemany(_translate(sql), [tuple(p) for p in seq_of_params])
        return _PGCursor(cur)

    def executescript(self, script):
        for stmt in _split_sql(script):
            self._raw.cursor().execute(_translate(stmt))

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._raw.rollback() if exc_type else self._raw.commit()
        finally:
            _pg_pool.putconn(self._raw)
        return False


# ---------- НАРУШЕНИЕ УНИКАЛЬНОСТИ ----------

class DuplicateError(Exception):
    """Значение уже занято (логин бизнеса, токен бота). Поле — в .field."""
    def __init__(self, field, message=None):
        self.field = field
        super().__init__(message or f"Значение поля {field} уже занято")


def _is_unique_violation(exc) -> bool:
    """Отличить «занято» от прочих ошибок базы. Работает и для SQLite, и для
    Postgres: у первого это IntegrityError с текстом UNIQUE constraint failed,
    у второго — UniqueViolation (класс из psycopg)."""
    name = type(exc).__name__
    if name in ("UniqueViolation", "IntegrityError"):
        text = str(exc).lower()
        return ("unique" in text or "duplicate key" in text
                or name == "UniqueViolation")
    return False


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
    """Открыть соединение с базой. row_factory — чтобы читать поля по имени.

    Postgres (если задан DATABASE_URL) — соединение из пула в обёртке, которая
    переводит SQLite-диалект. Иначе — SQLite, как раньше.
    """
    if _PG:
        raw = _pg_pool.getconn()
        raw.row_factory = _pg_row_factory
        return _PGConn(raw)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Встроенный LOWER() в SQLite не понимает кириллицу: «Доставка» остаётся с
    # заглавной Д и не находится по «доставк». Подменяем на питоновский .lower(),
    # который правильно приводит регистр в юникоде — от него зависит весь поиск.
    # (В Postgres встроенный lower() и так корректен для юникода.)
    conn.create_function("LOWER", 1, lambda s: s.lower() if s else s, deterministic=True)
    return conn


def _migrate_columns(conn):
    """
    Дополнить существующие таблицы новыми колонками (для старых баз) — идемпотентно.
    Вызывать ТОЛЬКО после создания всех таблиц. На SQLite «duplicate column»/«no such
    table» ловим; на Postgres перевод добавляет ADD COLUMN IF NOT EXISTS (без ошибок).
    """
    migrations = [("clients", c, "TEXT") for c in
                  ("birthday", "notes", "favorite", "ai_summary", "ai_advice", "summary_day")]
    migrations += [
        ("businesses", "about", "TEXT"),
        ("businesses", "fee", "INTEGER DEFAULT 0"),      # абонплата бизнеса VELOR AI'у (твой доход)
        ("businesses", "plan", "TEXT DEFAULT 'Старт'"),  # тариф
        ("businesses", "login", "TEXT"),                 # вход бизнеса в свою панель
        ("businesses", "password", "TEXT"),
        ("businesses", "knowledge", "TEXT"),             # база знаний бизнеса (прайс, услуги, условия)
        ("businesses", "tone", "TEXT"),                  # стиль общения AI-сотрудника
        ("businesses", "ai_name", "TEXT"),               # имя AI-сотрудника (личность)
        ("businesses", "ai_avatar", "TEXT"),             # символ/эмодзи аватара
        ("businesses", "ai_traits", "TEXT"),             # черты характера через запятую
        ("businesses", "ai_desc", "TEXT"),               # описание характера своими словами
        ("businesses", "board_day", "TEXT"),             # день последнего заседания «Совета директоров»
        ("orders", "amount", "INTEGER DEFAULT 0"),       # сумма заказа (оборот бизнеса)
        # Откуда пришёл заказ и его id в чужой системе. Нужны, чтобы повторная
        # синхронизация с Ozon/amoCRM не создавала дубли тех же заказов.
        ("orders", "external_id", "TEXT"),
        ("orders", "source", "TEXT"),                    # telegram | ozon | amocrm | …
        ("clients", "external_id", "TEXT"),              # id клиента в чужой CRM
        ("clients", "source", "TEXT"),
        ("timeline", "read_at", "TEXT"),                 # центр уведомлений: прочитанность
        ("timeline", "level", "TEXT DEFAULT 'info'"),    # info | important
        ("finance_entries", "op_date", "TEXT"),          # дата операции по выписке
        ("finance_entries", "counterparty", "TEXT"),
        ("finance_entries", "external_id", "TEXT"),      # чтобы не задвоить при повторной загрузке
        ("finance_entries", "source", "TEXT"),           # ручной ввод | csv | xlsx | pdf | банк
        ("finance_entries", "confidence", "REAL DEFAULT 1"),
        ("finance_entries", "import_id", "INTEGER"),
        # ── Trial / подписка (централизованный TrialService) ──
        ("businesses", "trial_start", "TEXT"),
        ("businesses", "trial_end", "TEXT"),
        ("businesses", "trial_used", "INTEGER DEFAULT 0"),
        ("businesses", "subscription_status", "TEXT DEFAULT 'trial'"),   # trial|active|expired|canceled
        ("businesses", "subscription_plan", "TEXT"),
        ("businesses", "subscription_started", "TEXT"),
        ("businesses", "subscription_expires", "TEXT"),
        ("businesses", "risk_score", "INTEGER DEFAULT 0"),   # сигнал абьюза (не блокировка)
        ("businesses", "tg_verify_code", "TEXT"),            # одноразовый код привязки владельца
        ("businesses", "owner_verified", "INTEGER DEFAULT 0"),  # личность владельца подтверждена
        # Сотруднику и поставщику мало пары «название + текст»: у них есть
        # должность, телефон, условия. Держим это структурой в JSON, а не
        # склеенной строкой, — иначе правка теряет разбиение на поля.
        ("memory_facts", "data", "TEXT"),
    ]
    for tbl, col, typ in migrations:
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # колонка уже есть (SQLite); на Postgres ADD COLUMN IF NOT EXISTS


def init_db():
    """Создать таблицы из schema.sql, если их ещё нет."""
    with open("schema.sql", "r", encoding="utf-8") as f:
        sql = f.read()
    with _connect() as conn:
        conn.executescript(sql)
        # ВАЖНО: миграции колонок (_migrate_columns) вызываются НИЖЕ — уже ПОСЛЕ
        # создания всех таблиц. Иначе ALTER для timeline/finance_entries шёл бы до
        # их CREATE: на SQLite молча терялись колонки, на Postgres рушилась вся
        # транзакция init_db.
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
        # ---------- UNIVERSAL INBOX ----------
        # Одно место, куда владелец сбрасывает ЛЮБОЙ материал о бизнесе, не
        # выбирая заранее категорию: заметку, скриншот, счёт, выписку, прайс.
        # Разбор (что это и куда положить) появится позже отдельным слоем —
        # поэтому здесь есть статус и поле ошибки, но нет ни одной догадки о
        # содержимом. Оригинал не теряется: storage.py кладёт его на диск или
        # в inbox_blobs, а здесь остаётся только ключ.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS inbox_items (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   kind         TEXT NOT NULL DEFAULT 'file',   -- text | file
                   title        TEXT,            -- как показать в списке
                   body         TEXT,            -- текст заметки или комментарий к файлу
                   filename     TEXT,            -- имя, как его прислали
                   mime         TEXT,
                   size         INTEGER DEFAULT 0,
                   storage_key  TEXT,            -- имя оригинала в хранилище
                   source       TEXT DEFAULT 'web',      -- откуда пришло
                   status       TEXT DEFAULT 'RECEIVED',
                   error        TEXT,            -- почему FAILED
                   archived_at  TEXT,
                   created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                   updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Что VELOR понял про материал. Отдельной таблицей, а не колонками в
        # inbox_items: разборов у одного материала бывает несколько (модель
        # ответила плохо → переразобрали), и прошлые ответы стирать нельзя —
        # по ним видно, ошибается ли ИИ и в какую сторону.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS inbox_results (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   item_id      INTEGER NOT NULL,
                   type         TEXT NOT NULL DEFAULT 'UNKNOWN',
                   confidence   REAL DEFAULT 0,
                   level        TEXT DEFAULT 'LOW',      -- HIGH | MEDIUM | LOW
                   summary      TEXT,
                   extracted    TEXT,      -- JSON: что удалось вытащить
                   actions      TEXT,      -- JSON: предложенные действия
                   engine       TEXT,      -- llm | rules
                   model        TEXT,      -- какой провайдер ответил
                   error        TEXT,
                   applied      TEXT,      -- JSON: что уже применено
                   created_at   TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Что человек решил по разбору. Отдельная таблица, потому что это
        # НЕ состояние материала, а история: кто, когда, что подтвердил, что
        # исправил и во что это превратилось. По ней видно, где ИИ ошибается
        # систематически, — а значит, чему его учить.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS inbox_decisions (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   item_id      INTEGER NOT NULL,
                   result_id    INTEGER,
                   decision     TEXT NOT NULL,   -- confirmed|edited|dismissed|auto|classified
                   action       TEXT,            -- какое действие подтверждали
                   entity_type  TEXT,            -- что создали (expense, client…)
                   entity_id    INTEGER,
                   original     TEXT,            -- JSON: что предложил ИИ
                   corrected    TEXT,            -- JSON: что стало после правки
                   changes      TEXT,            -- JSON: только изменённые поля
                   actor        TEXT,            -- business | owner | velor
                   actor_id     INTEGER,
                   note         TEXT,
                   created_at   TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # Оригиналы для backend'а "db" (Render: диск эфемерный, база — нет).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS inbox_blobs (
                   storage_key TEXT PRIMARY KEY,
                   business_id INTEGER NOT NULL,
                   data        BLOB NOT NULL,
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # ---------- ПАМЯТЬ БИЗНЕСА: откуда что известно ----------
        # Знания живут в своих таблицах (memory_facts, clients, orders,
        # finance_entries, goals, documents) — дублировать их здесь было бы
        # ошибкой: появились бы две правды об одном и том же. Эта таблица
        # хранит только ЦЕПОЧКУ: источник (материал во входящих) → что VELOR
        # из него понял (разбор) → чем это подтвердили (решение) → какая
        # запись из этого выросла. Плюс события правок: кто и что поменял.
        #
        # Подробности разбора и правок не копируем — на них стоят ссылки
        # (result_id, decision_id). Копия рано или поздно разошлась бы с
        # оригиналом, и было бы непонятно, какой из них верить.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_links (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   entity_type  TEXT NOT NULL,    -- service|client|expense|rule|…
                   entity_id    INTEGER NOT NULL,
                   event        TEXT NOT NULL DEFAULT 'created',  -- created|edited|removed
                   source_kind  TEXT DEFAULT 'manual',  -- inbox|manual|import|telegram|…
                   item_id      INTEGER,          -- inbox_items.id: сам источник
                   result_id    INTEGER,          -- inbox_results.id: что понял VELOR
                   decision_id  INTEGER,          -- inbox_decisions.id: чем подтвердили
                   confidence   REAL,
                   changes      TEXT,             -- JSON, только для event='edited'
                   actor        TEXT DEFAULT 'business',
                   actor_id     INTEGER,
                   note         TEXT,
                   created_at   TEXT DEFAULT CURRENT_TIMESTAMP
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

        # Вечный реестр использования триала — НЕ привязан к business и НЕ удаляется
        # вместе с компанией (защита «триал один раз»). Заполняется на регистрации.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS trial_registry (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER,
                   fingerprint TEXT,
                   email       TEXT,
                   telegram    TEXT,
                   ip          TEXT,
                   created_at  TEXT DEFAULT (datetime('now'))
               )"""
        )

        # Личность владельца бизнеса (Owner Identity) — сущность, к которой
        # ПРИВЯЗЫВАЕТСЯ триал (а НЕ к Telegram-боту: бота легко пересоздать).
        # Универсальна под несколько способов идентификации (telegram/phone/email/
        # google/apple). Как и trial_registry — НЕ удаляется вместе с компанией:
        # факт «этот владелец уже брал триал» должен пережить удаление аккаунта.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS owner_identity (
                   id                INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id       INTEGER,
                   method            TEXT,          -- telegram|phone|email|google|apple
                   telegram_user_id  TEXT,          -- ЛИЧНЫЙ id владельца (не бота!)
                   telegram_username TEXT,
                   first_name        TEXT,
                   last_name         TEXT,
                   phone             TEXT,
                   email             TEXT,
                   fingerprint       TEXT,
                   risk_score        INTEGER DEFAULT 0,
                   trial_used_at     TEXT,          -- заполнен → владелец уже брал триал
                   created_at        TEXT DEFAULT (datetime('now'))
               )"""
        )

        # Подключённые внешние сервисы (источники знаний): Ozon, ЮKassa, amoCRM…
        # credentials — ЗАШИФРОВАННЫЙ JSON с ключами доступа (см. secretbox.py):
        # их нужно читать обратно на каждую синхронизацию, поэтому хешировать,
        # как пароли, нельзя. meta — открытые несекретные детали (домен магазина,
        # выбранная воронка), cursor — докуда дочитали в прошлый раз.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS connections (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   provider     TEXT NOT NULL,
                   status       TEXT DEFAULT 'connected',   -- connected | error | paused
                   credentials  TEXT,
                   meta         TEXT,
                   cursor       TEXT,
                   last_sync_at TEXT,
                   last_error   TEXT,
                   items_total  INTEGER DEFAULT 0,
                   created_at   TEXT DEFAULT (datetime('now')),
                   UNIQUE(business_id, provider)
               )"""
        )

        # Обработанные апдейты Telegram — защита от повторной доставки (webhook).
        # Telegram при таймауте/ошибке повторяет апдейт; по (business_id, update_id)
        # отсекаем дубли, чтобы не создавать повторные заявки и не слать повторный ответ.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tg_updates (
                   business_id INTEGER NOT NULL,
                   update_id   INTEGER NOT NULL,
                   created_at  TEXT DEFAULT (datetime('now')),
                   PRIMARY KEY (business_id, update_id)
               )"""
        )

        # Теперь все таблицы существуют — можно безопасно домигрировать колонки.
        _migrate_columns(conn)

        # ---- Индексы под горячие пути (мультитенантные выборки идут по business_id) ----
        # Без них каждый запрос — полное сканирование таблицы; на росте данных это
        # заметно тормозит. Работает и в SQLite, и в Postgres (IF NOT EXISTS).
        # Все колонки заведомо существуют (таблицы созданы выше), поэтому безопасно.
        for idx, table, cols in [
            ("idx_messages_biz_client",  "messages",        "business_id, client_id"),
            ("idx_messages_biz_created", "messages",        "business_id, created_at"),
            ("idx_orders_biz",           "orders",          "business_id"),
            ("idx_orders_biz_client",    "orders",          "business_id, client_id"),
            ("idx_clients_biz",          "clients",         "business_id"),
            ("idx_timeline_biz_created", "timeline",        "business_id, created_at"),
            ("idx_finance_biz_created",  "finance_entries", "business_id, created_at"),
            ("idx_documents_biz",        "documents",       "business_id"),
            ("idx_doc_chunks_biz",       "doc_chunks",      "business_id"),
            ("idx_risks_biz",            "risks",           "business_id"),
            ("idx_opps_biz",             "opportunities",   "business_id"),
            ("idx_ideas_biz",            "ideas",           "business_id"),
            ("idx_board_biz",            "board_recs",      "business_id"),
            ("idx_owner_tg",             "owner_identity",  "telegram_user_id"),
            ("idx_owner_email",          "owner_identity",  "email"),
            ("idx_owner_phone",          "owner_identity",  "phone"),
            ("idx_connections_biz",      "connections",     "business_id"),
            ("idx_orders_external",      "orders",          "business_id, external_id"),
            ("idx_clients_external",     "clients",         "business_id, external_id"),
            ("idx_finance_external",     "finance_entries", "business_id, external_id"),
            ("idx_inbox_biz",            "inbox_items",     "business_id, id"),
            ("idx_inbox_biz_status",     "inbox_items",     "business_id, status"),
            ("idx_inbox_blobs_biz",      "inbox_blobs",     "business_id"),
            ("idx_inbox_results_item",   "inbox_results",   "business_id, item_id"),
            ("idx_inbox_decisions_item", "inbox_decisions", "business_id, item_id"),
            ("idx_mem_links_entity",     "memory_links",    "business_id, entity_type, entity_id"),
            ("idx_mem_links_item",       "memory_links",    "business_id, item_id"),
        ]:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table} ({cols})")

    # ---- Уникальность логина и токена бота (защита арендаторов) ----
    # Проверки «сначала SELECT, потом INSERT» недостаточно: два одновременных
    # запроса успевают пройти её оба. Уникальность должна гарантировать БАЗА.
    # Токен бота критичнее логина: два бизнеса с одним токеном — это чужие
    # клиенты в чужой панели, потому что бота ищут именно по токену.
    # Индексы частичные (WHERE ... IS NOT NULL) — пустые значения не мешают
    # друг другу; синтаксис поддерживают и SQLite, и Postgres.
    # Отдельным соединением и по одному: если в старой базе уже есть дубли,
    # создание индекса упадёт — это НЕ должно рушить запуск сервера.
    for idx, cols in [("uq_businesses_login", "login"),
                      ("uq_businesses_bot_token", "tg_bot_token")]:
        try:
            with _connect() as conn:
                conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON businesses ({cols}) "
                    f"WHERE {cols} IS NOT NULL AND {cols} != ''"
                )
        except Exception:
            # Дубликаты в существующей базе. Сервер поднимаем, но говорим об этом
            # владельцу в лог — их нужно развести вручную.
            import logging as _lg
            _lg.getLogger("velor").warning(
                "Не удалось включить уникальность %s: в таблице businesses есть "
                "повторяющиеся значения. Разведите их вручную.", cols)


# ---------- БИЗНЕСЫ (тенанты) ----------

def create_business(name, about=None, greeting=None, login=None, password=None):
    """Добавить новый бизнес. Возвращает его id.

    Логин и пароль принимаем ЗДЕСЬ, а не отдельным update после создания: иначе
    при занятом логине в базе оставался бы «осиротевший» бизнес без входа.
    Одна вставка — либо аккаунт создан целиком, либо не создан вовсе.
    """
    try:
        with _connect() as conn:
            cur = conn.execute(
                """INSERT INTO businesses (name, about, greeting, login, password)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, about, greeting, (login or None),
                 _hash_password(password) if password else None),
            )
            return cur.lastrowid
    except Exception as e:
        if _is_unique_violation(e):
            raise DuplicateError("login") from e
        raise


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
    # metric правится тоже: если цель завели из разбора и показатель угадан
    # неверно, чинить это должно быть можно там же, где смотрят цель.
    allowed = {"title", "target", "deadline", "manual_value", "status", "metric"}
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


def get_goal(goal_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM goals WHERE id = ? AND business_id = ?", (goal_id, business_id)
        ).fetchone()
    return dict(row) if row else None


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
    "rule":     "ПРАВИЛА РАБОТЫ",
    "goal":     "ЦЕЛИ БИЗНЕСА",
    "employee": "СОТРУДНИКИ",
    "supplier": "ПОСТАВЩИКИ",
    "company":  "О КОМПАНИИ",
}


def _facts_text(conn, business_id):
    """Услуги/товары/правила/цели одним текстом — так их читает ядро."""
    rows = conn.execute(
        "SELECT kind, title, body, data FROM memory_facts WHERE business_id = ? ORDER BY kind, id",
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
            # Структурные поля (должность, телефон, условия) — тоже знание, и
            # ядру они нужны наравне с текстом. Иначе на вопрос «кто у вас
            # мастер?» сотрудник знает имя, но не знает должности.
            extra = _fact_extra(r)
            if extra:
                line += " (" + extra + ")"
            if (r["body"] or "").strip():
                line += ": " + r["body"].strip()
            out.append(line)
    return "\n".join(out)


def _fact_extra(row):
    """Структурные поля факта одной строкой: «должность: мастер, тел.: …»."""
    try:
        data = _json.loads(row["data"] or "{}")
    except (ValueError, TypeError, KeyError, IndexError):
        return ""
    if not isinstance(data, dict):
        return ""
    return ", ".join(f"{k}: {v}" for k, v in data.items() if str(v or "").strip())


def _fact_row(row):
    """Строка факта наружу: JSON разложен в словарь, а не отдан текстом."""
    d = dict(row)
    try:
        d["data"] = _json.loads(d.get("data") or "{}")
    except (ValueError, TypeError):
        d["data"] = {}
    if not isinstance(d["data"], dict):
        d["data"] = {}
    return d


def get_fact(fact_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM memory_facts WHERE id = ? AND business_id = ?",
            (fact_id, business_id),
        ).fetchone()
    return _fact_row(row) if row else None


def count_facts(business_id, kind=None):
    with _connect() as conn:
        if kind:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_facts WHERE business_id = ? AND kind = ?",
                (business_id, kind),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_facts WHERE business_id = ?",
                (business_id,),
            ).fetchone()
    return int(row["n"] or 0)


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
        return [_fact_row(r) for r in rows]


def add_fact(business_id, kind, title, body=None, data=None):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO memory_facts (business_id, kind, title, body, data) VALUES (?,?,?,?,?)",
            (business_id, kind, title, body,
             _json.dumps(data or {}, ensure_ascii=False)),
        )
        fid = cur.lastrowid
    log_event(business_id, "memory", f"В память добавлено: {title}")
    return fid


def update_fact(fact_id, business_id, title, body=None, data=None):
    # data=None означает «не трогать»: страница базы знаний правит только
    # название и текст и не должна затирать поля, которых она не показывает.
    with _connect() as conn:
        if data is None:
            conn.execute(
                "UPDATE memory_facts SET title = ?, body = ? WHERE id = ? AND business_id = ?",
                (title, body, fact_id, business_id),
            )
        else:
            conn.execute(
                "UPDATE memory_facts SET title = ?, body = ?, data = ? "
                "WHERE id = ? AND business_id = ?",
                (title, body, _json.dumps(data or {}, ensure_ascii=False),
                 fact_id, business_id),
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


def tg_update_seen(business_id, update_id):
    """Уже обрабатывали этот апдейт Telegram? Защита от повторной доставки webhook.

    Возвращает True, если апдейт уже был (обработку нужно пропустить), иначе False —
    и одновременно ФИКСИРУЕТ его как обработанный. Помечаем ДО обработки, чтобы
    повтор при таймауте не создал дубль заявки и не отправил повторный ответ.
    """
    if update_id is None:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM tg_updates WHERE business_id = ? AND update_id = ?",
            (business_id, update_id),
        ).fetchone()
        if row:
            return True
        conn.execute(
            "INSERT INTO tg_updates (business_id, update_id) VALUES (?, ?)",
            (business_id, update_id),
        )
        return False


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
               "ai_name", "ai_avatar", "ai_traits", "ai_desc",
               "trial_start", "trial_end", "trial_used", "subscription_status",
               "subscription_plan", "subscription_started", "subscription_expires", "board_day", "risk_score",
               # Подтверждение личности владельца. Без этих двух полей в списке
               # код привязки Telegram молча терялся: /api/trial/start выдавал код,
               # он не сохранялся, webhook его не находил — и кнопка «Запустить
               # VELOR» не могла сработать никогда.
               "tg_verify_code", "owner_verified"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    # пароль в базе держим только как соль+хеш, никогда в открытом виде
    if sets.get("password"):
        sets["password"] = _hash_password(sets["password"])
    before = get_business(business_id) or {}
    q = ", ".join(f"{k} = ?" for k in sets)
    try:
        with _connect() as conn:
            conn.execute(
                f"UPDATE businesses SET {q} WHERE id = ?",
                (*sets.values(), business_id),
            )
    except Exception as e:
        if _is_unique_violation(e):
            # Занят либо логин, либо токен бота. Определяем по тому, что меняли:
            # токен важнее — с ним чужие клиенты попали бы в чужую панель.
            field = "tg_bot_token" if "tg_bot_token" in sets else "login"
            raise DuplicateError(field) from e
        raise
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
        # ВНИМАНИЕ: trial_registry НЕ трогаем — признак использования триала должен
        # пережить удаление компании (иначе абьюз через «удалить и создать заново»).


# ---------- TRIAL / ПОДПИСКА (данные для TrialService) ----------

def trial_used_before(fingerprint=None, email=None, telegram=None):
    """Выдавался ли уже триал на любой из признаков (вечный реестр)."""
    conds, params = [], []
    if fingerprint:
        conds.append("fingerprint = ?"); params.append(fingerprint)
    if email:
        conds.append("LOWER(email) = ?"); params.append(email.lower())
    if telegram:
        conds.append("telegram = ?"); params.append(str(telegram))
    if not conds:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM trial_registry WHERE " + " OR ".join(conds) + " LIMIT 1",
            tuple(params)).fetchone()
    return bool(row)


def record_trial_usage(business_id, fingerprint=None, email=None, telegram=None, ip=None):
    """Записать факт выдачи триала — навсегда."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO trial_registry (business_id, fingerprint, email, telegram, ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (business_id, fingerprint or None, (email or None),
             (str(telegram) if telegram else None), ip))


# ---------- OWNER IDENTITY (личность владельца — к ней привязан триал) ----------

_OWNER_FIELDS = ("method", "telegram_user_id", "telegram_username", "first_name",
                 "last_name", "phone", "email", "fingerprint", "risk_score",
                 "trial_used_at")


def owner_identity_get(business_id):
    """Текущая (самая свежая) личность владельца для бизнеса, либо None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM owner_identity WHERE business_id = ? ORDER BY id DESC LIMIT 1",
            (business_id,)).fetchone()
    return dict(row) if row else None


def owner_identity_upsert(business_id, **fields):
    """Создать личность владельца для бизнеса или дополнить её новыми признаками
    (объединение способов входа). Пустые значения не затирают уже известные."""
    clean = {k: v for k, v in fields.items() if k in _OWNER_FIELDS and v not in (None, "")}
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM owner_identity WHERE business_id = ? ORDER BY id DESC LIMIT 1",
            (business_id,)).fetchone()
        if row:
            if clean:
                sets = ", ".join(f"{k} = ?" for k in clean)
                conn.execute(f"UPDATE owner_identity SET {sets} WHERE id = ?",
                             tuple(clean.values()) + (row["id"],))
            return row["id"]
        cols = ["business_id"] + list(clean.keys())
        ph = ", ".join(["?"] * len(cols))
        cur = conn.execute(
            f"INSERT INTO owner_identity ({', '.join(cols)}) VALUES ({ph})",
            (business_id, *clean.values()))
        return cur.lastrowid


def owner_trial_used(telegram_user_id=None, phone=None, email=None, exclude_business=None):
    """Брал ли ЭТОТ владелец триал ранее — по СИЛЬНЫМ признакам личности
    (личный Telegram id ИЛИ телефон ИЛИ email). Fingerprint здесь НЕ учитывается
    (он только повышает risk_score). Ищем среди записей, где триал уже выдан."""
    conds, params = [], []
    if telegram_user_id:
        conds.append("telegram_user_id = ?"); params.append(str(telegram_user_id))
    if phone:
        conds.append("phone = ?"); params.append(str(phone))
    if email:
        conds.append("LOWER(email) = ?"); params.append(email.lower())
    if not conds:
        return False
    where = "trial_used_at IS NOT NULL AND (" + " OR ".join(conds) + ")"
    if exclude_business is not None:
        where += " AND business_id <> ?"; params.append(exclude_business)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM owner_identity WHERE {where} LIMIT 1", tuple(params)).fetchone()
    return bool(row)


def owner_weak_match(email=None, fingerprint=None, exclude_business=None):
    """Слабый комбинированный признак: email + fingerprint совпали одновременно
    с уже выданным триалом (когда сильного email-совпадения ещё недостаточно)."""
    if not (email and fingerprint):
        return False
    params = [email.lower(), str(fingerprint)]
    where = "trial_used_at IS NOT NULL AND LOWER(email) = ? AND fingerprint = ?"
    if exclude_business is not None:
        where += " AND business_id <> ?"; params.append(exclude_business)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM owner_identity WHERE {where} LIMIT 1", tuple(params)).fetchone()
    return bool(row)


def owner_fingerprint_seen(fingerprint, exclude_business=None):
    """Встречался ли уже такой fingerprint у владельца, взявшего триал — СИГНАЛ риска."""
    if not fingerprint:
        return False
    params = [str(fingerprint)]
    where = "trial_used_at IS NOT NULL AND fingerprint = ?"
    if exclude_business is not None:
        where += " AND business_id <> ?"; params.append(exclude_business)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM owner_identity WHERE {where} LIMIT 1", tuple(params)).fetchone()
    return bool(row)


def trial_stats(business_id):
    """Итоги для экрана окончания триала: что VELOR успел сделать."""
    with _connect() as conn:
        msgs = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE business_id = ? AND role = 'user'",
                            (business_id,)).fetchone()["n"]
        orders = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE business_id = ?", (business_id,)).fetchone()["n"]
        clients = conn.execute("SELECT COUNT(*) AS n FROM clients WHERE business_id = ?", (business_id,)).fetchone()["n"]
        recs = conn.execute("SELECT COUNT(*) AS n FROM board_recs WHERE business_id = ?", (business_id,)).fetchone()["n"]
    return {"messages": msgs, "orders": orders, "clients": clients,
            "recommendations": recs, "hours_saved": round(msgs * 2 / 60, 1)}


def trial_funnel():
    """Воронка конверсии: регистрация → запуск → Telegram → документы → 1-я заявка → подписка."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*) AS registered,
                 SUM(CASE WHEN trial_start IS NOT NULL THEN 1 ELSE 0 END) AS launched,
                 SUM(CASE WHEN COALESCE(tg_bot_token, '') <> '' THEN 1 ELSE 0 END) AS telegram,
                 SUM(CASE WHEN (SELECT COUNT(*) FROM documents d WHERE d.business_id = b.id) > 0 THEN 1 ELSE 0 END) AS documents,
                 SUM(CASE WHEN (SELECT COUNT(*) FROM orders o WHERE o.business_id = b.id) > 0 THEN 1 ELSE 0 END) AS first_order,
                 SUM(CASE WHEN subscription_status = 'active' THEN 1 ELSE 0 END) AS subscribed
               FROM businesses b"""
        ).fetchone()
    keys = ("registered", "launched", "telegram", "documents", "first_order", "subscribed")
    return {k: (row[k] or 0) for k in keys}


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


# Срезы базы клиентов. Список из тысячи имён не отвечает ни на один вопрос
# владельца; вопросы у него другие: «кто пришёл недавно», «кто уже покупал» и
# «кто перестал возвращаться». Считаем эти срезы по тем же данным, что уже есть
# (дата появления клиента и дата его последнего заказа) — без новых таблиц.
SLEEPING_DAYS = 60          # столько без заказа — и клиент считается «уснувшим»
SEGMENTS = ("all", "new", "buyers", "sleeping")


def _segment_having(segment):
    """Условие сегмента: (кусок HAVING, параметры). Пустая строка — без фильтра."""
    if segment == "new":
        return "c.created_at >= date('now', '-30 day')", []
    if segment == "buyers":
        return "COUNT(o.id) > 0", []
    if segment == "sleeping":
        # Именно «перестал», а не «никогда не покупал»: заказы были, но давно.
        return (f"COUNT(o.id) > 0 AND MAX(o.created_at) < date('now', '-{SLEEPING_DAYS} day')", [])
    return "", []


def list_clients(business_id, query=None, limit=50, offset=0, segment="all"):
    """Клиенты бизнеса + число заказов, сумма покупок, дата последнего заказа.
    Поддерживает поиск по имени/телефону, срез базы и постраничную загрузку."""
    where = "c.business_id = ?"
    params = [business_id]
    if query:
        where += " AND (LOWER(c.name) LIKE ? OR c.phone LIKE ?)"
        like = "%" + query.strip().lower() + "%"
        params += [like, like]
    having, hparams = _segment_having(segment)
    having_sql = f"HAVING {having}" if having else ""
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
               {having_sql}
               ORDER BY last_order_at DESC NULLS LAST, c.id DESC
               LIMIT ? OFFSET ?""",
            (*params, *hparams, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def count_segment(business_id, query=None, segment="all"):
    """Сколько клиентов в срезе — для счётчика на чипе и постраничной загрузки."""
    where = "c.business_id = ?"
    params = [business_id]
    if query:
        where += " AND (LOWER(c.name) LIKE ? OR c.phone LIKE ?)"
        like = "%" + query.strip().lower() + "%"
        params += [like, like]
    having, hparams = _segment_having(segment)
    if not having:
        with _connect() as conn:
            return conn.execute(
                f"SELECT COUNT(*) AS n FROM clients c WHERE {where}", tuple(params)
            ).fetchone()["n"]
    with _connect() as conn:
        return conn.execute(
            f"""SELECT COUNT(*) AS n FROM (
                    SELECT c.id FROM clients c
                    LEFT JOIN orders o ON o.client_id = c.id
                    WHERE {where}
                    GROUP BY c.id
                    HAVING {having}
                ) AS seg""",
            (*params, *hparams),
        ).fetchone()["n"]


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


def active_clients(business_id, days=30):
    """
    Сколько клиентов живы: написали или заказали за последние N дней.

    «Всего клиентов» растёт вечно и ничего не говорит владельцу — база в
    тысячу человек, из которых пишут трое, выглядит успехом только на бумаге.
    Активные считаются по обеим сторонам жизни клиента (заказ и обращение),
    поэтому цифра не врёт ни у тех, кто работает заявками, ни у тех, кто
    живёт перепиской.
    """
    window = f"-{int(days)} day"
    with _connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) AS n FROM (
                   SELECT client_id FROM orders
                    WHERE business_id = ? AND client_id IS NOT NULL
                      AND date(created_at) >= date('now', ?)
                   UNION
                   SELECT client_id FROM messages
                    WHERE business_id = ? AND client_id IS NOT NULL
                      AND date(created_at) >= date('now', ?)
               ) AS live""",
            (business_id, window, business_id, window)).fetchone()["n"]


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


def get_document(doc_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND business_id = ?", (doc_id, business_id)
        ).fetchone()
    return dict(row) if row else None


def rename_document(doc_id, business_id, filename):
    with _connect() as conn:
        conn.execute(
            "UPDATE documents SET filename = ? WHERE id = ? AND business_id = ?",
            (filename, doc_id, business_id),
        )


def count_documents(business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE business_id = ?", (business_id,)
        ).fetchone()
    return int(row["n"] or 0)


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

def add_order(business_id, text, client_id=None, phone=None, address=None,
              date_wanted=None, amount=0):
    """Записать новый заказ. Возвращает id заказа.

    amount — сумма заказа в рублях. Именно из неё складывается оборот бизнеса,
    сумма покупок клиента и вся денежная аналитика, поэтому её нужно писать
    сразу при создании (раньше колонка существовала, но не заполнялась никогда,
    и все обороты в системе были нулями).
    """
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO orders (business_id, client_id, text, phone, address, date_wanted, amount)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (business_id, client_id, text, phone, address, date_wanted, _money(amount)),
        )
        order_id = cur.lastrowid
    detail = (text or "")[:120]
    if _money(amount):
        detail += f" · {_money(amount)} ₽"
    log_event(business_id, "order", "Создан заказ", detail)
    return order_id


def _money(value):
    """Привести сумму к целым рублям: принимаем 1500, '1500', '1 500,50', None."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(round(value)))
    s = str(value).replace(" ", " ").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return max(0, int(round(float(s))))
    except (ValueError, TypeError):
        return 0


def set_order_amount(order_id, business_id, amount):
    """Проставить/исправить сумму заказа — только в своём бизнесе."""
    if not business_id:
        raise ValueError("set_order_amount требует business_id (защита арендаторов)")
    value = _money(amount)
    with _connect() as conn:
        conn.execute(
            "UPDATE orders SET amount = ? WHERE id = ? AND business_id = ?",
            (value, order_id, business_id),
        )
    return value


def get_orders(business_id, limit=20, offset=0):
    """Получить последние заказы бизнеса (для просмотра/отчётов)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE business_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (business_id, limit, max(0, int(offset or 0))),
        ).fetchall()
        return [dict(r) for r in rows]


def get_order(order_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND business_id = ?",
            (order_id, business_id),
        ).fetchone()
    return dict(row) if row else None


def update_order(order_id, business_id, **fields):
    allowed = {"text", "phone", "address", "date_wanted", "status", "amount"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        conn.execute(
            f"UPDATE orders SET {cols} WHERE id = ? AND business_id = ?",
            (*sets.values(), order_id, business_id),
        )


def count_orders(business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE business_id = ?", (business_id,)
        ).fetchone()
    return int(row["n"] or 0)


def orders_overview(business_id):
    """Настоящие итоги по заказам: сколько всего, сколько новых, сколько сегодня
    и на какую сумму. Считается в базе через COUNT/SUM, а не длиной выборки —
    раньше ИИ и главная получали «заказов всего 20», потому что мерили len()
    списка, ограниченного LIMIT."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN status = 'новый' THEN 1 ELSE 0 END), 0) AS new,
                      COALESCE(SUM(CASE WHEN status = 'выполнен' THEN 1 ELSE 0 END), 0) AS done,
                      COALESCE(SUM(CASE WHEN date(created_at) = date('now') THEN 1 ELSE 0 END), 0) AS today,
                      COALESCE(SUM(CASE WHEN COALESCE(amount,0) > 0 THEN 1 ELSE 0 END), 0) AS with_amount,
                      COALESCE(SUM(amount), 0) AS turnover
                 FROM orders WHERE business_id = ?""",
            (business_id,),
        ).fetchone()
    return {"total": int(row["total"] or 0), "new": int(row["new"] or 0),
            "done": int(row["done"] or 0), "today": int(row["today"] or 0),
            # Сколько заявок с проставленной суммой: по разнице с total видно,
            # насколько можно верить обороту и среднему чеку.
            "with_amount": int(row["with_amount"] or 0),
            "turnover": int(row["turnover"] or 0)}


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


def get_finance_entry(entry_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM finance_entries WHERE id = ? AND business_id = ?",
            (entry_id, business_id),
        ).fetchone()
    return dict(row) if row else None


def update_finance_entry(entry_id, business_id, **fields):
    """Правка операции. Меняем только разрешённые поля — сумму, категорию, заметку."""
    allowed = {"kind", "category", "amount", "note", "op_date", "counterparty"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        conn.execute(
            f"UPDATE finance_entries SET {cols} WHERE id = ? AND business_id = ?",
            (*sets.values(), entry_id, business_id),
        )


def count_finance_entries(business_id, kind=None):
    with _connect() as conn:
        if kind:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM finance_entries WHERE business_id = ? AND kind = ?",
                (business_id, kind),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM finance_entries WHERE business_id = ?",
                (business_id,),
            ).fetchone()
    return int(row["n"] or 0)


def list_finance_entries(business_id, limit=100, kind=None, offset=0):
    """Последние доходы/расходы бизнеса (новые сверху)."""
    sql = "SELECT * FROM finance_entries WHERE business_id = ?"
    args = [business_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    with _connect() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
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


# ============================================================
#  UNIVERSAL INBOX
# ============================================================
# Правило то же, что у остального кабинета: любой запрос ограничен
# business_id. Чужую запись нельзя ни прочитать, ни удалить — не потому что
# UI её не показывает, а потому что она не проходит через WHERE.

# Единый словарь статусов. Разбор материала ещё не написан, но состояния он
# будет менять именно эти — держим их в одном месте, чтобы завтра не появилось
# второго набора строк где-нибудь в сервере.
INBOX_STATUSES = ("RECEIVED", "PROCESSING", "PROCESSED", "NEEDS_REVIEW", "FAILED")
INBOX_SOURCES = ("web", "telegram", "email", "api", "import")


def add_inbox_item(business_id, kind="file", title=None, body=None, filename=None,
                   mime=None, size=0, storage_key=None, source="web",
                   status="RECEIVED"):
    """Записать входящий материал. Возвращает id."""
    if status not in INBOX_STATUSES:
        status = "RECEIVED"
    if source not in INBOX_SOURCES:
        source = "web"
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO inbox_items
               (business_id, kind, title, body, filename, mime, size,
                storage_key, source, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, kind, title, body, filename, mime, int(size or 0),
             storage_key, source, status),
        )
        item_id = cur.lastrowid
    log_event(business_id, "inbox", "Новый материал во входящих",
              (title or filename or "заметка")[:160])
    return item_id


def list_inbox(business_id, status=None, archived=False, limit=50, offset=0):
    """Входящие бизнеса, новые сверху. archived=True — только архив."""
    where = "business_id = ?"
    params = [business_id]
    where += " AND archived_at IS NOT NULL" if archived else " AND archived_at IS NULL"
    if status in INBOX_STATUSES:
        where += " AND status = ?"
        params.append(status)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT id, business_id, kind, title, body, filename, mime, size,
                       storage_key, source, status, error, archived_at,
                       created_at, updated_at
                  FROM inbox_items WHERE {where}
                 ORDER BY id DESC LIMIT ? OFFSET ?""",
            (*params, max(1, min(int(limit), 200)), max(0, int(offset))),
        ).fetchall()
        return [dict(r) for r in rows]


def count_inbox(business_id, status=None, archived=False):
    """Сколько записей подходит под фильтр — для «показать ещё» и счётчиков."""
    where = "business_id = ?"
    params = [business_id]
    where += " AND archived_at IS NOT NULL" if archived else " AND archived_at IS NULL"
    if status in INBOX_STATUSES:
        where += " AND status = ?"
        params.append(status)
    with _connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM inbox_items WHERE {where}", tuple(params)
        ).fetchone()["n"]


def inbox_overview(business_id):
    """Разбивка активных входящих по статусам + размер архива."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT status, COUNT(*) AS n FROM inbox_items
                WHERE business_id = ? AND archived_at IS NULL
                GROUP BY status""",
            (business_id,),
        ).fetchall()
        archived = conn.execute(
            """SELECT COUNT(*) AS n FROM inbox_items
                WHERE business_id = ? AND archived_at IS NOT NULL""",
            (business_id,),
        ).fetchone()["n"]
        total_size = conn.execute(
            """SELECT COALESCE(SUM(size), 0) AS s FROM inbox_items
                WHERE business_id = ? AND archived_at IS NULL""",
            (business_id,),
        ).fetchone()["s"]
    by = {st: 0 for st in INBOX_STATUSES}
    for r in rows:
        if r["status"] in by:
            by[r["status"]] = r["n"]
    return {"by_status": by, "active": sum(by.values()),
            "archived": archived, "bytes": int(total_size or 0)}


def get_inbox_item(item_id, business_id):
    """Одна запись — только своя."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM inbox_items WHERE id = ? AND business_id = ?",
            (item_id, business_id),
        ).fetchone()
        return dict(row) if row else None


def set_inbox_status(item_id, business_id, status, error=None):
    """
    Сменить статус записи. Разбора материала ещё нет — эту дверь открываем
    заранее и ровно одну, чтобы будущий слой не начал писать в таблицу мимо
    словаря статусов. Возвращает True, если запись нашлась.
    """
    if status not in INBOX_STATUSES:
        raise ValueError("Неизвестный статус входящего: %s" % status)
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE inbox_items
                  SET status = ?, error = ?, updated_at = datetime('now')
                WHERE id = ? AND business_id = ?""",
            (status, error, item_id, business_id),
        )
        return cur.rowcount > 0


def archive_inbox_item(item_id, business_id, archived=True):
    """Убрать из ленты, не теряя материал (или вернуть обратно)."""
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE inbox_items
                  SET archived_at = %s, updated_at = datetime('now')
                WHERE id = ? AND business_id = ?"""
            % ("datetime('now')" if archived else "NULL"),
            (item_id, business_id),
        )
        return cur.rowcount > 0


def delete_inbox_item(item_id, business_id):
    """Удалить запись. Оригинал стирает вызывающий (storage.delete) — база о
    хранилище ничего не знает и знать не должна."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM inbox_items WHERE id = ? AND business_id = ?",
            (item_id, business_id),
        )
        return cur.rowcount > 0


# ---------- что VELOR понял про материал ----------

def save_inbox_result(business_id, item_id, result):
    """
    Сохранить разбор. Прошлые НЕ трогаем — история ответов модели это тоже
    данные: по ней видно, где ИИ систематически ошибается.
    """
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO inbox_results
               (business_id, item_id, type, confidence, level, summary,
                extracted, actions, engine, model, error, applied)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, item_id,
             result.get("type") or "UNKNOWN",
             float(result.get("confidence") or 0),
             result.get("level") or "LOW",
             result.get("summary"),
             _json.dumps(result.get("extracted_data") or {}, ensure_ascii=False),
             _json.dumps(result.get("suggested_actions") or [], ensure_ascii=False),
             result.get("engine"), result.get("model"), result.get("error"),
             _json.dumps(result.get("applied") or [], ensure_ascii=False)),
        )
        return cur.lastrowid


def _result_row(row):
    """Строка таблицы → тот же вид, в каком разбор живёт в коде и в API."""
    if not row:
        return None
    d = dict(row)
    for src, dst in (("extracted", "extracted_data"), ("actions", "suggested_actions"),
                     ("applied", "applied")):
        try:
            d[dst] = _json.loads(d.get(src) or ("[]" if src != "extracted" else "{}"))
        except (ValueError, TypeError):
            d[dst] = [] if src != "extracted" else {}
    d.pop("extracted", None)
    d.pop("actions", None)
    return d


def get_inbox_result(business_id, item_id):
    """Последний разбор материала — его и показываем."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM inbox_results
                WHERE business_id = ? AND item_id = ?
                ORDER BY id DESC LIMIT 1""",
            (business_id, item_id),
        ).fetchone()
    return _result_row(row)


def get_inbox_results(business_id, item_ids):
    """Разборы сразу для пачки материалов — чтобы лента не делала N запросов."""
    ids = [int(i) for i in item_ids]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM inbox_results
                 WHERE business_id = ? AND item_id IN ({ph})
                 ORDER BY id ASC""",
            (business_id, *ids),
        ).fetchall()
    out = {}
    for r in rows:                      # последний по каждому материалу победит
        d = _result_row(r)
        out[d["item_id"]] = d
    return out


def mark_result_applied(result_id, business_id, applied):
    """Отметить, какие предложенные действия уже выполнены."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE inbox_results SET applied = ? WHERE id = ? AND business_id = ?",
            (_json.dumps(applied, ensure_ascii=False), result_id, business_id),
        )
        return cur.rowcount > 0


# ---------- журнал решений по разборам ----------

def add_inbox_decision(business_id, item_id, decision, result_id=None, action=None,
                       entity_type=None, entity_id=None, original=None, corrected=None,
                       changes=None, actor="business", actor_id=None, note=None):
    """
    Записать решение. Ничего не перезаписывает: каждое нажатие — новая строка.
    История нужна целиком, а не в последней редакции.
    """
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO inbox_decisions
               (business_id, item_id, result_id, decision, action, entity_type,
                entity_id, original, corrected, changes, actor, actor_id, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, item_id, result_id, decision, action, entity_type, entity_id,
             _json.dumps(original or {}, ensure_ascii=False),
             _json.dumps(corrected or {}, ensure_ascii=False),
             _json.dumps(changes or {}, ensure_ascii=False),
             actor, actor_id, note),
        )
        return cur.lastrowid


def list_inbox_decisions(business_id, item_id):
    """История решений по материалу — от старых к новым, как она и случалась."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM inbox_decisions
                WHERE business_id = ? AND item_id = ?
                ORDER BY id ASC""",
            (business_id, item_id),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for key in ("original", "corrected", "changes"):
            try:
                d[key] = _json.loads(d.get(key) or "{}")
            except (ValueError, TypeError):
                d[key] = {}
        out.append(d)
    return out


def count_inbox_decisions(business_id, item_ids):
    """Сколько решений по каждому материалу — чтобы лента знала без N запросов."""
    ids = [int(i) for i in item_ids]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT item_id, COUNT(*) AS n FROM inbox_decisions
                 WHERE business_id = ? AND item_id IN ({ph})
                 GROUP BY item_id""",
            (business_id, *ids),
        ).fetchall()
    return {r["item_id"]: r["n"] for r in rows}


def inbox_correction_stats(business_id):
    """
    Насколько часто человек правит ИИ. Не украшение: если доля правок высокая,
    автоматике доверять рано, и это должно быть видно владельцу.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT decision, COUNT(*) AS n FROM inbox_decisions
                WHERE business_id = ? GROUP BY decision""",
            (business_id,),
        ).fetchall()
    by = {r["decision"]: r["n"] for r in rows}
    human = sum(by.get(k, 0) for k in ("confirmed", "edited", "dismissed", "classified"))
    return {"by_decision": by, "human_total": human,
            "edited": by.get("edited", 0), "dismissed": by.get("dismissed", 0)}


# ---------- ПАМЯТЬ БИЗНЕСА: цепочка «источник → знание» ----------

def add_memory_link(business_id, entity_type, entity_id, event="created",
                    source_kind="manual", item_id=None, result_id=None,
                    decision_id=None, confidence=None, changes=None,
                    actor="business", actor_id=None, note=None):
    """
    Записать событие о знании. Ничего не перезаписывает: правка — новая строка.
    По этим строкам собирается и происхождение записи, и её история.
    """
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO memory_links
               (business_id, entity_type, entity_id, event, source_kind, item_id,
                result_id, decision_id, confidence, changes, actor, actor_id, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, entity_type, int(entity_id), event, source_kind, item_id,
             result_id, decision_id, confidence,
             _json.dumps(changes or {}, ensure_ascii=False), actor, actor_id, note),
        )
        return cur.lastrowid


def _link_row(row):
    d = dict(row)
    try:
        d["changes"] = _json.loads(d.get("changes") or "{}")
    except (ValueError, TypeError):
        d["changes"] = {}
    return d


def memory_links(business_id, entity_type, entity_id):
    """
    Вся история одной записи: как появилась, из какого материала, что правили.

    Подтягиваем имя исходного файла и разбор — но не копией, а join'ом, чтобы
    в памяти и во входящих не завелось двух разных правд об одном материале.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT l.*, i.title AS item_title, i.filename AS item_filename,
                      i.kind AS item_kind, i.mime AS item_mime, i.archived_at AS item_archived,
                      r.type AS result_type, r.confidence AS result_confidence,
                      r.summary AS result_summary, r.extracted AS result_extracted,
                      d.decision AS decision, d.original AS decision_original,
                      d.corrected AS decision_corrected, d.changes AS decision_changes,
                      d.action AS decision_action
                 FROM memory_links l
                 LEFT JOIN inbox_items     i ON i.id = l.item_id
                 LEFT JOIN inbox_results   r ON r.id = l.result_id
                 LEFT JOIN inbox_decisions d ON d.id = l.decision_id
                WHERE l.business_id = ? AND l.entity_type = ? AND l.entity_id = ?
                ORDER BY l.id ASC""",
            (business_id, entity_type, int(entity_id)),
        ).fetchall()
    out = []
    for r in rows:
        d = _link_row(r)
        for key in ("result_extracted", "decision_original",
                    "decision_corrected", "decision_changes"):
            try:
                d[key] = _json.loads(d.get(key) or "{}")
            except (ValueError, TypeError):
                d[key] = {}
        out.append(d)
    return out


def memory_origins(business_id, entity_type, entity_ids):
    """
    Происхождение сразу нескольких записей — чтобы список не делал N запросов.
    Берём первое событие 'created' у каждой: именно оно отвечает «откуда это».
    """
    ids = [int(i) for i in entity_ids if i is not None]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT l.*, i.title AS item_title, i.filename AS item_filename,
                       i.kind AS item_kind, i.archived_at AS item_archived
                  FROM memory_links l
                  LEFT JOIN inbox_items i ON i.id = l.item_id
                 WHERE l.business_id = ? AND l.entity_type = ?
                   AND l.entity_id IN ({ph}) AND l.event = 'created'
                 ORDER BY l.id ASC""",
            (business_id, entity_type, *ids),
        ).fetchall()
    out = {}
    for r in rows:
        d = _link_row(r)
        out.setdefault(d["entity_id"], d)      # первое создание, а не последнее
    return out


def memory_edit_counts(business_id, entity_type, entity_ids):
    """Сколько раз каждую запись правили — список показывает это без N запросов."""
    ids = [int(i) for i in entity_ids if i is not None]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT entity_id, COUNT(*) AS n FROM memory_links
                  WHERE business_id = ? AND entity_type = ? AND entity_id IN ({ph})
                    AND event = 'edited'
                  GROUP BY entity_id""",
            (business_id, entity_type, *ids),
        ).fetchall()
    return {r["entity_id"]: r["n"] for r in rows}


def memory_from_item(business_id, item_id):
    """Что выросло из одного материала: обратный взгляд на ту же цепочку."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM memory_links
                WHERE business_id = ? AND item_id = ? AND event = 'created'
                ORDER BY id ASC""",
            (business_id, item_id),
        ).fetchall()
    return [_link_row(r) for r in rows]


def memory_sourced_counts(business_id):
    """
    Сколько записей каждого вида откуда взялись.

    Два числа, а не одно: «есть запись о происхождении» и «подтверждено
    документом». Смешивать их нельзя — знание, внесённое руками, проверить
    нечем, и выдавать его за подкреплённое документом было бы неправдой.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT entity_type,
                      COUNT(DISTINCT entity_id) AS linked,
                      COUNT(DISTINCT CASE WHEN item_id IS NOT NULL THEN entity_id END) AS documented
                 FROM memory_links
                WHERE business_id = ? AND event = 'created'
                GROUP BY entity_type""",
            (business_id,),
        ).fetchall()
    return {r["entity_type"]: {"linked": int(r["linked"] or 0),
                               "documented": int(r["documented"] or 0)} for r in rows}


def memory_recent(business_id, limit=12):
    """Последние события памяти — что VELOR узнал и что поправили."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT l.*, i.title AS item_title, i.filename AS item_filename
                 FROM memory_links l
                 LEFT JOIN inbox_items i ON i.id = l.item_id
                WHERE l.business_id = ?
                ORDER BY l.id DESC LIMIT ?""",
            (business_id, int(limit)),
        ).fetchall()
    return [_link_row(r) for r in rows]


# ---------- оригиналы файлов в базе (backend "db" из storage.py) ----------

def put_blob(business_id, storage_key, data):
    """Положить оригинал. Повторная запись тем же ключом заменяет содержимое."""
    with _connect() as conn:
        conn.execute("DELETE FROM inbox_blobs WHERE storage_key = ? AND business_id = ?",
                     (storage_key, business_id))
        conn.execute(
            "INSERT INTO inbox_blobs (storage_key, business_id, data) VALUES (?, ?, ?)",
            (storage_key, business_id, sqlite3.Binary(data) if not _PG else data),
        )


def get_blob(business_id, storage_key):
    """Прочитать оригинал своего бизнеса. None — если такого ключа нет."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM inbox_blobs WHERE storage_key = ? AND business_id = ?",
            (storage_key, business_id),
        ).fetchone()
    if not row:
        return None
    data = row["data"]
    # psycopg отдаёт bytea как memoryview — приводим к bytes, иначе FastAPI
    # не сможет отдать содержимое ответом.
    return bytes(data)


def delete_blob(business_id, storage_key):
    with _connect() as conn:
        conn.execute("DELETE FROM inbox_blobs WHERE storage_key = ? AND business_id = ?",
                     (storage_key, business_id))


# ============================================================
#  ПОДКЛЮЧЁННЫЕ СЕРВИСЫ (Ozon, ЮKassa, amoCRM…) И ИХ ДАННЫЕ
# ============================================================
# Правило: данные из внешних сервисов ложатся в УЖЕ СУЩЕСТВУЮЩИЕ сущности —
# заказы в orders, платежи в finance_entries, покупатели в clients. Отдельных
# таблиц «заказы Ozon» и «платежи ЮKassa» нет и не нужно: тогда весь продукт
# (CRM, финансы, AI-директор, экспорт, цели) видит эти данные без единой правки.
# Дедупликацию обеспечивает пара (business_id, external_id) в каждой таблице.

import json as _json


def list_connections(business_id):
    """Все подключения бизнеса. Секреты НЕ отдаём — только статус и метаданные."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM connections WHERE business_id = ? ORDER BY provider",
            (business_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("credentials", None)
        try:
            d["meta"] = _json.loads(d.get("meta") or "{}")
        except (ValueError, TypeError):
            d["meta"] = {}
        out.append(d)
    return out


def get_connection(business_id, provider, with_secrets=False):
    """Одно подключение. with_secrets=True — только для самой синхронизации."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE business_id = ? AND provider = ?",
            (business_id, provider),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["meta"] = _json.loads(d.get("meta") or "{}")
    except (ValueError, TypeError):
        d["meta"] = {}
    if not with_secrets:
        d.pop("credentials", None)
    return d


def save_connection(business_id, provider, credentials_blob=None, meta=None, status="connected"):
    """Создать или обновить подключение. credentials_blob уже зашифрован (secretbox)."""
    meta_json = _json.dumps(meta or {}, ensure_ascii=False)
    exists = get_connection(business_id, provider)
    with _connect() as conn:
        if exists:
            if credentials_blob is None:      # секреты не меняли — не затираем
                conn.execute(
                    "UPDATE connections SET meta = ?, status = ?, last_error = NULL "
                    "WHERE business_id = ? AND provider = ?",
                    (meta_json, status, business_id, provider))
            else:
                conn.execute(
                    "UPDATE connections SET credentials = ?, meta = ?, status = ?, last_error = NULL "
                    "WHERE business_id = ? AND provider = ?",
                    (credentials_blob, meta_json, status, business_id, provider))
        else:
            conn.execute(
                "INSERT INTO connections (business_id, provider, credentials, meta, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (business_id, provider, credentials_blob, meta_json, status))
    if not exists:
        log_event(business_id, "integration", "Подключён источник: " + provider,
                  "VELOR начнёт забирать оттуда данные автоматически")
    return get_connection(business_id, provider)


def mark_connection_synced(business_id, provider, added=0, cursor=None, error=None):
    """Записать итог синхронизации: когда, сколько нового, была ли ошибка."""
    with _connect() as conn:
        if error:
            conn.execute(
                "UPDATE connections SET status = 'error', last_error = ?, "
                "last_sync_at = datetime('now') WHERE business_id = ? AND provider = ?",
                (str(error)[:400], business_id, provider))
        else:
            conn.execute(
                "UPDATE connections SET status = 'connected', last_error = NULL, "
                "last_sync_at = datetime('now'), items_total = COALESCE(items_total,0) + ?, "
                "cursor = COALESCE(?, cursor) WHERE business_id = ? AND provider = ?",
                (int(added or 0), cursor, business_id, provider))


def delete_connection(business_id, provider):
    """Отключить сервис. Уже загруженные данные остаются — они принадлежат бизнесу."""
    with _connect() as conn:
        conn.execute("DELETE FROM connections WHERE business_id = ? AND provider = ?",
                     (business_id, provider))
    log_event(business_id, "integration", "Отключён источник: " + provider,
              "Ранее загруженные данные сохранены")


def connected_providers(business_id):
    """Названия подключённых сервисов — для контекста ИИ и шкалы знаний."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT provider FROM connections WHERE business_id = ? AND status != 'paused'",
            (business_id,),
        ).fetchall()
    return [r["provider"] for r in rows]


# ---------- ИДЕМПОТЕНТНАЯ ЗАПИСЬ ТОГО, ЧТО ПРИШЛО ИЗВНЕ ----------

def upsert_external_client(business_id, external_id, source, name=None, phone=None, notes=None):
    """Покупатель из внешней системы. Повторная синхронизация не создаёт дубль."""
    key = source + ":" + str(external_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE business_id = ? AND external_id = ?",
            (business_id, key),
        ).fetchone()
        if row:
            # дополняем то, чего не хватало (телефон часто приходит позже)
            fields, params = [], []
            if phone and not row["phone"]:
                fields.append("phone = ?")
                params.append(phone)
            if name and not row["name"]:
                fields.append("name = ?")
                params.append(name)
            if fields:
                conn.execute("UPDATE clients SET " + ", ".join(fields) + " WHERE id = ?",
                             (*params, row["id"]))
            return row["id"], False
        cur = conn.execute(
            """INSERT INTO clients (business_id, name, phone, notes, external_id, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (business_id, name, phone, notes, key, source),
        )
        return cur.lastrowid, True


def upsert_external_order(business_id, external_id, source, text, amount=0,
                          status=None, client_id=None, phone=None, created_at=None):
    """
    Заказ из внешней системы. Возвращает (order_id, создан_ли_новый).
    Статус и сумма обновляются при каждой синхронизации: заказ в маркетплейсе
    живёт своей жизнью (оплачен, отменён, возвращён), и мы должны это видеть.
    """
    key = source + ":" + str(external_id)
    value = _money(amount)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, status, amount FROM orders WHERE business_id = ? AND external_id = ?",
            (business_id, key),
        ).fetchone()
        if row:
            if (status and status != row["status"]) or value != (row["amount"] or 0):
                conn.execute(
                    "UPDATE orders SET status = COALESCE(?, status), amount = ? WHERE id = ?",
                    (status, value, row["id"]))
            return row["id"], False
        cur = conn.execute(
            """INSERT INTO orders (business_id, client_id, text, phone, status, amount,
                                   external_id, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
            (business_id, client_id, text, phone, status or "новый", value,
             key, source, created_at),
        )
        return cur.lastrowid, True


def upsert_external_finance(business_id, external_id, source, kind, amount,
                            category=None, note=None, counterparty=None, op_date=None):
    """Платёж/операция из внешней системы в общие финансы. Дубли отсекаются."""
    key = source + ":" + str(external_id)
    value = _money(amount)
    if not value:
        return None, False
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM finance_entries WHERE business_id = ? AND external_id = ?",
            (business_id, key),
        ).fetchone()
        if row:
            return row["id"], False
        cur = conn.execute(
            """INSERT INTO finance_entries (business_id, kind, category, amount, note,
                                            counterparty, external_id, source, op_date, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (business_id, kind, category, value, note, counterparty, key, source, op_date),
        )
        return cur.lastrowid, True
