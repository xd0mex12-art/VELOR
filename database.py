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
        # Каким каналом пришло сообщение. Пусто — Telegram или веб: так было до
        # появления второго канала, и переписывать прошлое задним числом нельзя.
        ("messages", "channel", "TEXT"),
        # Почему разговор ушёл человеку и что VELOR хотел сказать. Черновик
        # нужен владельцу больше, чем причина: по нему видно, какой цены или
        # какого условия не хватает в памяти бизнеса.
        ("ig_threads", "paused_why", "TEXT"),
        ("ig_threads", "ai_draft", "TEXT"),
        ("timeline", "read_at", "TEXT"),                 # центр уведомлений: прочитанность
        ("timeline", "level", "TEXT DEFAULT 'info'"),    # info | important
        ("finance_entries", "op_date", "TEXT"),          # дата операции по выписке
        ("finance_entries", "counterparty", "TEXT"),
        ("finance_entries", "external_id", "TEXT"),      # чтобы не задвоить при повторной загрузке
        ("finance_entries", "source", "TEXT"),           # ручной ввод | csv | xlsx | pdf | банк
        ("finance_entries", "confidence", "REAL DEFAULT 1"),
        ("finance_entries", "import_id", "INTEGER"),
        # С кем была операция. Ссылки, а не текст: имя сотрудника меняется, а
        # связь остаётся, и «сколько мы платим Петровой» считается запросом, а
        # не сравнением строк.
        ("finance_entries", "doc_type", "TEXT"),       # чек | банк | счёт | накладная | зарплата | возврат | вручную
        ("finance_entries", "employee_id", "INTEGER"), # memory_facts, kind='employee'
        ("finance_entries", "supplier_id", "INTEGER"), # memory_facts, kind='supplier'
        ("finance_entries", "client_id", "INTEGER"),   # clients
        ("finance_entries", "order_id", "INTEGER"),    # orders
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
        # Архив. Уволенный сотрудник, снятая с продажи услуга, устаревший
        # документ — это не ошибка, которую надо стереть, а знание, которое
        # перестало действовать. Удаление уносит с собой историю; архив нет.
        ("memory_facts", "archived_at", "TEXT"),
        # Почему VELOR решил, что операция относится к этой заявке. Догадка без
        # объяснения непроверяема, а значит, ей нельзя доверять.
        ("inbox_results", "relations", "TEXT"),
        # Паспорт подключения: когда подключили, какие права запрошены и с
        # какой настройкой работает. Секреты сюда не попадают никогда — они
        # лежат отдельно и зашифрованными.
        ("connections", "connected_at", "TEXT"),
        ("connections", "permissions", "TEXT"),   # JSON: что именно разрешено
        ("connections", "config", "TEXT"),        # JSON: настройка без секретов
        ("clients", "archived_at", "TEXT"),
        ("documents", "archived_at", "TEXT"),
        # ── единое окно входящих ────────────────────────────────────────────
        # Отпечаток содержимого. Один и тот же чек, присланный дважды, — это
        # не два расхода. Считается по байтам файла или по нормализованному
        # тексту заметки, поэтому «тот же файл под другим именем» тоже ловится.
        ("inbox_items", "content_hash", "TEXT"),
        # Кто прислал. Раньше было известно только «какой бизнес»; когда дверь
        # одна на кабинет, бота и будущие каналы, без отправителя журнал
        # обработки не отвечает на первый же вопрос при разборе ошибки.
        ("inbox_items", "actor", "TEXT"),
        ("inbox_items", "actor_id", "INTEGER"),
        # Что известно об обстоятельствах приёма (канал, id сообщения, ссылка
        # на исходник). JSON, потому что у каждого канала свои подробности, и
        # заводить под них колонки значило бы менять схему на каждый канал.
        ("inbox_items", "meta", "TEXT"),
        # Подтверждён ли факт человеком. Вывод модели и знание, за которое
        # владелец поручился, — разные вещи, и складывать их в одну колонку
        # значит потерять единственную разницу, которая тут важна.
        ("memory_facts", "verified", "INTEGER DEFAULT 1"),
        # Оговорки разбора: модель промахнулась по цифрам, VELOR не стал
        # переписывать подтверждённое. Раньше они считались и терялись при
        # сохранении — а это ровно то, что человеку нужно прочитать, когда он
        # видит «ничего не изменилось» и не понимает почему.
        ("inbox_results", "notes", "TEXT"),
        # ── оценка возможности ──────────────────────────────────────────────
        # Три разных вопроса, три разные колонки. Один общий балл ответил бы
        # сразу на все и ни на один: «73» не объясняет, дорогая это сделка или
        # горячая, и что с ней делать.
        ("leads", "intent", "TEXT"),            # low|medium|high — хочет ли купить
        ("leads", "fit", "TEXT"),               # unknown|low|medium|high — наше ли это
        # Приоритет считается на лету и в колонке НЕ хранится: он зависит от
        # времени («бизнес молчит третий час»), а записанное время устаревает
        # молча. В колонке лежит только то, что владелец поставил рукой, —
        # понять, что это его решение, можно по owner_fields.
        ("leads", "priority", "TEXT"),
        # Оценка суммы по собственному прайсу. Отдельно от value: value — то,
        # что назвали вслух, estimated_value — то, что мы посчитали сами, и
        # смешивать их значило бы выдать расчёт за слова клиента.
        ("leads", "estimated_value", "INTEGER"),
        ("leads", "wanted_at", "TEXT"),         # к какой дате нужно, если её назвали
        # Наблюдения с происхождением: что услышали, в каком сообщении и когда.
        ("leads", "signals", "TEXT"),
        ("leads", "qualified_at", "TEXT"),
        # ── автономность по действиям ───────────────────────────────────────
        # Что VELOR вправе делать сам, что — только спросив, а что нельзя вовсе.
        # Живёт в той же строке, что уровень и поимённые права, потому что это
        # тот же вопрос: «насколько ты самостоятелен». Заводить рядом вторую
        # таблицу ради трёх слов на действие значило бы завести вторую правду —
        # и однажды они разошлись бы.
        ("ai_policy", "modes", "TEXT"),
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
        # ---------- РЕЗУЛЬТАТЫ ----------
        # То, что владелец уносит из VELOR наружу: отчёт, коммерческое
        # предложение, письмо клиенту. Отдельная таблица, а не «документы»:
        # documents — это то, что человек ЗАГРУЗИЛ и из чего VELOR учится,
        # а здесь — то, что VELOR СОБРАЛ из данных бизнеса. Смешать их значило
        # бы получить список, в котором непонятно, чему верить как источнику.
        #
        # doc хранит документ целиком (блоки), а не только ссылку на файл:
        # цифры в отчёте — это снимок на момент сборки. Пересчитать их завтра
        # заново значило бы показать человеку другой отчёт под тем же именем.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS outputs (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   kind         TEXT NOT NULL,
                   title        TEXT,
                   subtitle     TEXT,
                   request      TEXT,      -- JSON: по какому запросу собрано
                   doc          TEXT,      -- JSON: сам документ (блоки)
                   sources      TEXT,      -- JSON: на каких данных построено
                   body         TEXT,      -- текст письма/КП — то, что копируют
                   file_key     TEXT,      -- ключ файла в хранилище
                   file_name    TEXT,
                   file_size    INTEGER DEFAULT 0,
                   engine       TEXT,      -- llm | rules
                   status       TEXT DEFAULT 'READY',
                   error        TEXT,
                   created_at   TEXT DEFAULT CURRENT_TIMESTAMP
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
        # Рёбра «многие ко многим». Всё, что выражается колонкой (заказ →
        # клиент, операция → сотрудник), колонкой и остаётся: дублировать это
        # рёбрами значило бы завести вторую правду о той же связи.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS entity_links (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   src_type     TEXT NOT NULL,     -- order|client|document|…
                   src_id       INTEGER NOT NULL,
                   dst_type     TEXT NOT NULL,     -- service|product|order|…
                   dst_id       INTEGER NOT NULL,
                   kind         TEXT DEFAULT 'related',  -- includes|about|…
                   confidence   REAL DEFAULT 1,
                   source       TEXT DEFAULT 'manual',   -- manual|auto|inbox
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

        # ---------- АВТОНОМИЯ AI-ПРОДАВЦА ----------
        # Насколько самостоятельно VELOR разговаривает с клиентами в канале.
        # Отдельно по каналам: директ и телеграм — разные аудитории, и владелец
        # вправе доверять им по-разному.
        #
        # Уровень и поимённые разрешения хранятся врозь не для красоты: уровень
        # это готовый набор для обычных действий, а опасные (скидка, возврат,
        # цена мимо прайса) не входят ни в один уровень и включаются только
        # поштучно. Слив их в одно поле, мы однажды выдали бы право на скидку
        # вместе с повышением уровня.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ai_policy (
                   business_id INTEGER NOT NULL,
                   channel     TEXT NOT NULL,
                   level       INTEGER,
                   grants      TEXT,               -- JSON: разрешения поимённо
                   updated_at  TEXT DEFAULT (datetime('now')),
                   updated_by  TEXT,
                   PRIMARY KEY (business_id, channel)
               )"""
        )

        # ---------- INSTAGRAM КАК КАНАЛ ----------
        # Переписка в директе. Отдельная таблица, потому что здесь живут факты
        # самого канала, которым не место в карточке клиента: его id в Instagram,
        # @-логин, время последнего входящего (от него Meta отсчитывает окно
        # ответа) и признак того, что разговор взял на себя человек.
        #
        # Сами сообщения сюда НЕ копируются: они лежат в общей таблице messages,
        # как и переписка в Telegram. Иначе у одного разговора появилось бы две
        # правды, и память клиента разошлась бы с тем, что видит ИИ.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ig_threads (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id  INTEGER NOT NULL,
                   igsid        TEXT NOT NULL,      -- id собеседника в Instagram
                   client_id    INTEGER NOT NULL,   -- он же в базе клиентов VELOR
                   username     TEXT,
                   name         TEXT,
                   avatar       TEXT,
                   last_in_at   TEXT,               -- от него считается окно ответа
                   last_out_at  TEXT,
                   ai_paused    INTEGER DEFAULT 0,  -- разговор ведёт человек
                   paused_by    TEXT,
                   paused_at    TEXT,
                   paused_why   TEXT,               -- чего не хватило VELOR
                   ai_draft     TEXT,               -- что он хотел ответить
                   created_at   TEXT DEFAULT (datetime('now')),
                   UNIQUE(business_id, igsid)
               )"""
        )
        # Уже виденные сообщения Instagram. Meta повторяет доставку при любой
        # заминке, а ещё возвращает эхом наш собственный ответ. Без этой отметки
        # клиент получил бы второй такой же ответ, а собственное эхо VELOR принял
        # бы за вмешательство человека и замолчал бы ни с того ни с сего.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ig_seen (
                   business_id INTEGER NOT NULL,
                   mid         TEXT NOT NULL,
                   created_at  TEXT DEFAULT (datetime('now')),
                   PRIMARY KEY (business_id, mid)
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

        # ЛИДЫ — коммерческие возможности. Не клиент и не заявка, а то, что
        # между ними: человек проявил интерес, но ещё ничего не купил.
        #
        # Почему отдельной таблицей. Раньше «лид» был строчкой «Интерес: …» в
        # заметках клиента. У такой записи нет ни статуса, ни истории, ни
        # причины проигрыша, а главное — её нельзя посчитать: количество лидов
        # приходилось выводить из переписки, заметок и заявок, и три способа
        # давали три разных числа. Одна возможность — одна строка здесь.
        #
        # client_id пустой допустим: человек может написать раньше, чем назовёт
        # себя. Один клиент имеет НЕСКОЛЬКО лидов: майский букет и августовская
        # свадьба — две разные возможности, а не два клиента.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS leads (
                   id            INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id   INTEGER NOT NULL,
                   client_id     INTEGER,
                   title         TEXT NOT NULL,      -- коротко: о чём возможность
                   interest      TEXT,               -- чего человек хочет, его словами
                   status        TEXT DEFAULT 'new', -- new|qualified|in_progress|won|lost
                   source        TEXT,               -- telegram|instagram|website|manual|…
                   channel       TEXT,               -- канал переписки, если он был
                   value         INTEGER,            -- сумма, ТОЛЬКО если её назвали
                   currency      TEXT,
                   order_id      INTEGER,            -- чем закончилась: существующая заявка
                   lost_reason   TEXT,               -- price|competitor|no_response|…
                   -- Какие поля правил человек. Автоматика их не трогает: цена,
                   -- исправленная владельцем, не должна возвращаться к догадке
                   -- модели при следующем сообщении.
                   owner_fields  TEXT,
                   -- Догадки и обстоятельства. Сюда же уходит всё, что VELOR
                   -- предположил: в колонки попадает только сказанное вслух.
                   meta          TEXT,
                   -- История разговора не копируется внутрь лида: она уже есть
                   -- в messages. Здесь только границы — от какого сообщения до
                   -- какого шёл этот разговор.
                   first_message_id INTEGER,
                   last_message_id  INTEGER,
                   created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
                   updated_at       TEXT,
                   last_activity_at TEXT,
                   converted_at     TEXT,
                   lost_at          TEXT
               )"""
        )


        # Касания, которые VELOR предлагает сделать по возможности. Отдельная
        # таблица, а не поле в лиде: у одной возможности касаний несколько, и
        # каждое обязано помнить своё — почему предложено, на чём основано,
        # когда уместно, что написано и чем кончилось.
        #
        # Черновик и отправка — РАЗНЫЕ состояния. Пока сообщение не ушло по
        # сети и не подтвердилось, оно не отправлено, как бы уверенно ни
        # выглядела очередь: «запланировано» и «отправлено» путать нельзя.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS followups (
                   id            INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id   INTEGER NOT NULL,
                   lead_id       INTEGER NOT NULL,
                   client_id     INTEGER,
                   channel       TEXT,               -- канал ИСХОДНОГО разговора
                   reason        TEXT NOT NULL,      -- business_gap|customer_no_response|…
                   trigger       TEXT,               -- что именно случилось, словами
                   status        TEXT DEFAULT 'draft',
                   attempt       INTEGER DEFAULT 1,  -- какое это касание по счёту
                   recommended_at TEXT,              -- когда уместно
                   message       TEXT,               -- черновик
                   based_on      TEXT,               -- провенанс: id сообщений и даты
                   stop_reason   TEXT,
                   error         TEXT,               -- почему не ушло
                   tries         INTEGER DEFAULT 0,  -- сколько раз пробовали отправить
                   outcome       TEXT,               -- replied|converted|rejected|ignored|failed
                   outcome_at    TEXT,
                   reply_message_id INTEGER,         -- ответ клиента после касания
                   order_id      INTEGER,            -- заявка после касания
                   created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                   updated_at    TEXT,
                   sent_at       TEXT
               )"""
        )

        # Журнал действий VELOR. Одна запись = одно намерение что-то сделать:
        # предложенное, выполненное, запрещённое или сорвавшееся.
        #
        # Заводится отдельно от timeline не для порядка ради порядка. timeline —
        # лента событий компании, написанная для чтения: «Новая возможность»,
        # «Сотрудник ответил». На вопрос «кто это сделал, по чьему разрешению,
        # на основании чего и что было до» она не отвечает и отвечать не должна:
        # это разные жанры. Здесь — второй жанр, и без него нельзя ни разобрать
        # спор, ни показать очередь на подтверждение.
        #
        # Запрещённое хранится наравне с выполненным. Владелец должен видеть не
        # только то, что VELOR сделал, но и то, чего ему не дали сделать: иначе
        # сработавшая защита выглядит как поломка.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS actions (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   action      TEXT NOT NULL,      -- id из реестра actions.REGISTRY
                   actor       TEXT DEFAULT 'velor',  -- velor | owner | system
                   actor_id    INTEGER,
                   mode        TEXT,               -- automatic | approved | manual
                   status      TEXT DEFAULT 'proposed',
                   channel     TEXT,               -- где происходит: telegram|instagram|…
                   target_type TEXT,               -- lead | client | order | followup | …
                   target_id   INTEGER,
                   reason      TEXT,               -- человеческое «почему»
                   based_on    TEXT,               -- JSON: id сообщений, лидов, записей
                   payload     TEXT,               -- JSON: чем выполнять, если разрешат
                   before      TEXT,               -- JSON: что было (только для изменений)
                   after       TEXT,               -- JSON: что стало
                   result      TEXT,               -- чем кончилось, словами
                   error       TEXT,
                   dedupe_key  TEXT,               -- один повод — одно предложение
                   expires_at  TEXT,               -- после этого выполнять поздно
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                   updated_at  TEXT,
                   decided_at  TEXT,               -- когда владелец сказал да/нет
                   done_at     TEXT                -- когда выполнилось или сорвалось
               )"""
        )

        # Находки VELOR. Одна запись = один замеченный сигнал: проблема, риск
        # или возможность, которую владелец сам бы заметил не сразу.
        #
        # Почему не в `opportunities`. Та таблица уже занята другим смыслом:
        # там лежат идеи роста, которые придумал AI-директор по общей картине
        # бизнеса. Здесь — обнаруженный факт с доказательствами и сроком
        # годности. Сложить их в одну значило бы получить список, в котором
        # «попробуйте продавать подписку» стоит рядом с «шесть клиентов ждут
        # ответа пятый час», и владелец перестал бы читать оба.
        #
        # Почему не в `actions`. Обнаружение — не действие. VELOR ничего не
        # сделал, когда заметил; он сделает, только если владелец согласится.
        # Смешать одно с другим — значит однажды показать в журнале действий
        # «VELOR обнаружил» рядом с «VELOR отправил» и потерять разницу между
        # наблюдением и поступком.
        #
        # fingerprint — отпечаток повода, а не набора доказательств. Он нарочно
        # грубый: одним лидом больше — та же проблема, и заводить вторую запись
        # значило бы каждый час сообщать владельцу одно и то же.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS initiatives (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   business_id INTEGER NOT NULL,
                   type        TEXT NOT NULL,      -- id из initiatives.TYPES
                   level       TEXT DEFAULT 'act', -- watch | act
                   priority    TEXT DEFAULT 'medium',  -- шкала qualify: low..urgent
                   confidence  TEXT,               -- solid | likely
                   title       TEXT NOT NULL,      -- что произошло, одной строкой
                   summary     TEXT,               -- подробнее, теми же цифрами
                   why         TEXT,               -- почему это важно бизнесу
                   hypothesis  TEXT,               -- догадка ИИ; НЕ факт
                   evidence    TEXT,               -- JSON: id записей, окно, числа
                   impact      INTEGER,            -- оценка суммы; NULL — неизвестна
                   impact_note TEXT,               -- как её читать
                   action      TEXT,               -- что предлагаем сделать (actions.REGISTRY)
                   action_note TEXT,               -- то же словами владельца
                   href        TEXT,               -- где посмотреть своими глазами
                   status      TEXT DEFAULT 'new',
                   outcome     TEXT,               -- чем кончилось, когда закрылась
                   fingerprint TEXT,               -- один повод — одна запись
                   actions_ids TEXT,               -- JSON: какие действия из неё выросли
                   created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                   updated_at  TEXT,
                   last_seen_at TEXT,              -- когда сигнал подтверждался в последний раз
                   decided_at  TEXT,               -- когда владелец ответил
                   closed_at   TEXT,
                   expires_at  TEXT
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
            ("idx_ig_threads_biz",       "ig_threads",      "business_id"),
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
            # Отпечаток спрашивается на КАЖДОМ приёме — до записи, а не после.
            # Без индекса единое окно превращало бы каждую отправку в полный
            # перебор входящих этого бизнеса.
            ("idx_inbox_hash",           "inbox_items",     "business_id, content_hash"),
            ("idx_inbox_blobs_biz",      "inbox_blobs",     "business_id"),
            ("idx_outputs_biz",          "outputs",         "business_id, id"),
            ("idx_inbox_results_item",   "inbox_results",   "business_id, item_id"),
            ("idx_inbox_decisions_item", "inbox_decisions", "business_id, item_id"),
            ("idx_mem_links_entity",     "memory_links",    "business_id, entity_type, entity_id"),
            ("idx_mem_links_item",       "memory_links",    "business_id, item_id"),
            ("idx_finance_biz_kind",     "finance_entries", "business_id, kind"),
            ("idx_links_src",            "entity_links",    "business_id, src_type, src_id"),
            ("idx_links_dst",            "entity_links",    "business_id, dst_type, dst_id"),
            # Лиды спрашивают тремя вопросами: покажи все, покажи по статусу
            # («сколько открытых») и покажи по клиенту («есть ли у него живая
            # возможность»). Последний — на КАЖДОЕ входящее сообщение, поэтому
            # без индекса воронка тормозила бы сам разговор.
            ("idx_leads_biz_created",    "leads",           "business_id, created_at"),
            ("idx_leads_biz_status",     "leads",           "business_id, status"),
            ("idx_leads_biz_client",     "leads",           "business_id, client_id"),
            ("idx_leads_biz_source",     "leads",           "business_id, source"),
            ("idx_followups_biz_status", "followups",       "business_id, status"),
            ("idx_followups_lead",       "followups",       "business_id, lead_id"),
            # Журнал спрашивают двумя вопросами: «покажи ленту» и «что ждёт
            # решения». Оба — по бизнесу, поэтому индексы такие же парные.
            ("idx_actions_biz",          "actions",         "business_id, id"),
            # Находки спрашиваются тремя вопросами: покажи все, покажи живые и
            # «есть ли уже такая». Третий идёт на КАЖДОМ обходе, до записи.
            ("idx_init_biz",             "initiatives",     "business_id"),
            ("idx_init_biz_status",      "initiatives",     "business_id, status"),
            ("idx_init_fp",              "initiatives",     "business_id, fingerprint"),
            ("idx_actions_biz_status",   "actions",         "business_id, status"),
            ("idx_actions_dedupe",       "actions",         "business_id, dedupe_key"),
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


def list_goals(business_id, only_active=False, archived=False):
    """Цели с посчитанным прогрессом и темпом: успеваем или отстаём."""
    today = datetime.date.today()
    with _connect() as conn:
        sql = "SELECT * FROM goals WHERE business_id = ?"
        if only_active:
            sql += " AND status = 'active'"
        # У цели уже есть жизненный цикл, поэтому архив — это статус, а не
        # вторая отдельная пометка о том же самом.
        sql += " AND status = 'archived'" if archived else " AND status != 'archived'"
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
        # Архивное знание в ядро не попадает: уволенный мастер не должен
        # всплывать в ответе клиенту, который спрашивает, кто его подстрижёт.
        "SELECT kind, title, body, data, verified FROM memory_facts "
        "WHERE business_id = ? AND archived_at IS NULL ORDER BY kind, id",
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
            # Знание, которое VELOR понял сам из присланного материала и никто
            # не подтверждал. Ядру оно нужно — иначе оно отвечало бы хуже, чем
            # может, — но выдавать его за проверенный факт нельзя.
            if not _verified_of(r):
                line += "  [не подтверждено владельцем]"
            out.append(line)
    if any(not _verified_of(r) for r in rows):
        out.append("")
        out.append("Строки с пометкой «не подтверждено владельцем» VELOR понял "
                   "сам из присланных материалов. Ссылаться на них можно, "
                   "выдавать за точные — нельзя: назови и предложи уточнить.")
    return "\n".join(out)


def _verified_of(row):
    """Подтверждено ли знание. У старых строк колонки может не быть — считаем да."""
    try:
        value = row["verified"]
    except (KeyError, IndexError):
        return True
    return True if value is None else bool(value)


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


def count_facts(business_id, kind=None, archived=False):
    """Сколько знаний этого вида. По умолчанию — только действующие."""
    where = "business_id = ?" + (" AND archived_at IS NOT NULL" if archived
                                 else " AND archived_at IS NULL")
    params = [business_id]
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM memory_facts WHERE {where}", tuple(params)
        ).fetchone()
    return int(row["n"] or 0)


def list_facts(business_id, kind=None, archived=False):
    """
    Знания вида. archived=True — наоборот, только архив: владелец должен
    иметь возможность посмотреть, что он убрал, и вернуть это обратно.
    """
    where = "business_id = ?" + (" AND archived_at IS NOT NULL" if archived
                                 else " AND archived_at IS NULL")
    params = [business_id]
    order = "kind, id DESC"
    if kind:
        where += " AND kind = ?"
        params.append(kind)
        order = "id DESC"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM memory_facts WHERE {where} ORDER BY {order}", tuple(params)
        ).fetchall()
        return [_fact_row(r) for r in rows]


def add_fact(business_id, kind, title, body=None, data=None, verified=True):
    """
    Записать знание о бизнесе.

    verified=False означает «так понял VELOR, человек не подтверждал». Такое
    знание живёт в памяти наравне с остальными — иначе его пришлось бы
    выбросить, — но помечено, и ядро говорит о нём осторожнее. По умолчанию
    True: всё, что заводится руками владельца или через подтверждение, —
    подтверждённый факт.
    """
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO memory_facts (business_id, kind, title, body, data, verified) "
            "VALUES (?,?,?,?,?,?)",
            (business_id, kind, title, body,
             _json.dumps(data or {}, ensure_ascii=False), 1 if verified else 0),
        )
        fid = cur.lastrowid
    log_event(business_id, "memory", f"В память добавлено: {title}")
    return fid


def set_fact_verified(fact_id, business_id, verified=True):
    """Подтвердить знание (или снять подтверждение). Возвращает, нашлось ли."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE memory_facts SET verified = ? WHERE id = ? AND business_id = ?",
            (1 if verified else 0, int(fact_id), business_id))
        return cur.rowcount > 0


def update_fact(fact_id, business_id, title, body=None, data=None, verified=None):
    # data=None означает «не трогать»: страница базы знаний правит только
    # название и текст и не должна затирать поля, которых она не показывает.
    #
    # verified=None — тоже «не трогать». Правка руками означает, что человек
    # это прочитал и согласился, поэтому вызывающая сторона обычно передаёт
    # True: непроверенное знание после первой же правки становится фактом.
    with _connect() as conn:
        if verified is not None:
            conn.execute(
                "UPDATE memory_facts SET verified = ? WHERE id = ? AND business_id = ?",
                (1 if verified else 0, fact_id, business_id))
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


# ---------- ЦИФРЫ ДЛЯ ДИРЕКТОРА ----------
# Директор не считает ничего «в уме»: каждая цифра приходит запросом, и у
# каждой известно, из чего она сложилась — сколько операций, за какой период.
# Без этого вывод «расходы выросли на 31%» невозможно проверить, а значит, и
# доверять ему нельзя.
#
# Дата операции важнее даты записи: чек могли внести через неделю, но потратили
# деньги тогда, когда потратили. Поэтому везде COALESCE(op_date, дата записи).

_OP_DAY = "COALESCE(f.op_date, date(f.created_at))"


def _window(days, offset=0):
    """Границы окна в днях назад: (начало, конец). Конец не включается."""
    end = -int(offset)
    start = -(int(offset) + int(days))
    return (f"{start} day", f"{end} day" if end else "+1 day")


def money_period(business_id, days=30, offset=0):
    """
    Деньги за окно: доход, расход, прибыль и сколько операций их дало.

    Количество операций возвращаем всегда: процент, посчитанный по одной
    записи, — это не тренд, и решать это должен тот, кто читает.
    """
    frm, to = _window(days, offset)
    with _connect() as conn:
        row = conn.execute(
            f"""SELECT
                   COALESCE(SUM(CASE WHEN f.kind='income'  THEN f.amount END),0) AS income,
                   COALESCE(SUM(CASE WHEN f.kind='expense' THEN f.amount END),0) AS expense,
                   SUM(CASE WHEN f.kind='income'  THEN 1 ELSE 0 END) AS income_n,
                   SUM(CASE WHEN f.kind='expense' THEN 1 ELSE 0 END) AS expense_n
                 FROM finance_entries f
                WHERE f.business_id = ?
                  AND {_OP_DAY} >= date('now', ?) AND {_OP_DAY} < date('now', ?)""",
            (business_id, frm, to)).fetchone()
    income, expense = int(row["income"] or 0), int(row["expense"] or 0)
    return {"income": income, "expense": expense, "profit": income - expense,
            "income_n": int(row["income_n"] or 0), "expense_n": int(row["expense_n"] or 0),
            "entries": int(row["income_n"] or 0) + int(row["expense_n"] or 0)}


def category_period(business_id, kind="expense", days=14, offset=0):
    """Разбивка по категориям за окно: {категория: (сумма, число операций)}."""
    frm, to = _window(days, offset)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(f.category,'без категории') AS category,
                       SUM(f.amount) AS total, COUNT(*) AS n
                  FROM finance_entries f
                 WHERE f.business_id = ? AND f.kind = ?
                   AND {_OP_DAY} >= date('now', ?) AND {_OP_DAY} < date('now', ?)
                 GROUP BY COALESCE(f.category,'без категории')""",
            (business_id, kind, frm, to)).fetchall()
    return {r["category"]: {"total": int(r["total"] or 0), "n": int(r["n"] or 0)} for r in rows}


def counts_period(business_id, table, days=30, offset=0, extra=""):
    """Сколько записей появилось за окно (клиенты, заявки, обращения)."""
    if table not in ("clients", "orders", "messages", "documents"):
        return 0
    frm, to = _window(days, offset)
    with _connect() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS n FROM {table}
                 WHERE business_id = ? {extra}
                   AND date(created_at) >= date('now', ?) AND date(created_at) < date('now', ?)""",
            (business_id, frm, to)).fetchone()
    return int(row["n"] or 0)


ORDER_CANCELLED = "отменён"


def orders_period(business_id, days=30, offset=0):
    """
    Заявки за окно: сколько, на какую сумму и у скольких сумма проставлена.

    Отменённые заявки считаются в count (они были, владелец их видел), но НЕ
    участвуют в деньгах: отменённый заказ не принёс ни рубля, а в среднем чеке
    он завышал цифру. Возвращаем cancelled отдельно, чтобы это было видно, а не
    спрятано в разнице между count и with_amount.
    """
    frm, to = _window(days, offset)
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS cancelled,
                      COALESCE(SUM(CASE WHEN status <> ? THEN amount END),0) AS total,
                      SUM(CASE WHEN status <> ? AND amount > 0 THEN 1 ELSE 0 END) AS with_amount
                 FROM orders
                WHERE business_id = ?
                  AND date(created_at) >= date('now', ?) AND date(created_at) < date('now', ?)""",
            (ORDER_CANCELLED, ORDER_CANCELLED, ORDER_CANCELLED,
             business_id, frm, to)).fetchone()
    n = int(row["n"] or 0)
    with_amount = int(row["with_amount"] or 0)
    total = int(row["total"] or 0)
    return {"count": n, "amount": total, "with_amount": with_amount,
            "cancelled": int(row["cancelled"] or 0),
            "avg": round(total / with_amount) if with_amount else None}


def clients_split(business_id, days=30, offset=0):
    """
    Новые и вернувшиеся клиенты за окно — расчёт, а не догадка модели.

    Вернувшийся — тот, у кого есть заказ РАНЬШЕ начала окна. Новый — тот, у
    кого такого заказа нет. Определение детерминированное: один и тот же клиент
    на одних и тех же данных всегда попадёт в ту же группу.

    Считаем по клиентам, оформившим заказ в окне, а не по всем заведённым: у
    клиента без заказов нет поведения, которое можно назвать возвратом.
    Отменённые заказы не в счёт — отмена это не покупка.
    """
    frm, to = _window(days, offset)
    with _connect() as conn:
        row = conn.execute(
            """SELECT
                   COUNT(*) AS active,
                   SUM(CASE WHEN prior > 0 THEN 1 ELSE 0 END) AS came_back
                 FROM (
                   SELECT o.client_id,
                          (SELECT COUNT(*) FROM orders p
                            WHERE p.business_id = o.business_id
                              AND p.client_id = o.client_id
                              AND p.status <> ?
                              AND date(p.created_at) < date('now', ?)) AS prior
                     FROM orders o
                    WHERE o.business_id = ? AND o.client_id IS NOT NULL
                      AND o.status <> ?
                      AND date(o.created_at) >= date('now', ?)
                      AND date(o.created_at) <  date('now', ?)
                    GROUP BY o.client_id
                 ) AS in_window""",
            (ORDER_CANCELLED, frm, business_id, ORDER_CANCELLED, frm, to)).fetchone()
    active = int(row["active"] or 0)
    returning = int(row["came_back"] or 0)
    return {"active": active, "returning": returning, "new": active - returning}


def daily_series(business_id, days=30):
    """
    Дневной ряд за окно: по строке на каждый день, включая дни без движения.

    Ряд обязан совпадать с числами сводки, иначе график будет спорить с цифрой
    над ним. Поэтому день денег считается тем же выражением, что и в
    money_period (COALESCE(op_date, дата записи)), а заявки и клиенты — по
    дате появления записи, как в orders_period и counts_period.

    Пустые дни заполняются нулями здесь, а не на фронте: пропуск в ряду
    превращает линию в ложь о том, что между двумя точками ничего не было.
    """
    days = max(1, min(int(days or 30), 180))
    frm, to = _window(days, 0)
    money, orders, clients = {}, {}, {}
    with _connect() as conn:
        for r in conn.execute(
            f"""SELECT {_OP_DAY} AS day,
                       COALESCE(SUM(CASE WHEN f.kind='income'  THEN f.amount END),0) AS income,
                       COALESCE(SUM(CASE WHEN f.kind='expense' THEN f.amount END),0) AS expense
                  FROM finance_entries f
                 WHERE f.business_id = ?
                   AND {_OP_DAY} >= date('now', ?) AND {_OP_DAY} < date('now', ?)
                 GROUP BY day""", (business_id, frm, to)):
            money[r["day"]] = (int(r["income"] or 0), int(r["expense"] or 0))
        for r in conn.execute(
            """SELECT date(created_at) AS day, COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN status <> ? THEN amount END),0) AS amount
                 FROM orders
                WHERE business_id = ?
                  AND date(created_at) >= date('now', ?) AND date(created_at) < date('now', ?)
                GROUP BY day""", (ORDER_CANCELLED, business_id, frm, to)):
            orders[r["day"]] = (int(r["n"] or 0), int(r["amount"] or 0))
        for r in conn.execute(
            """SELECT date(created_at) AS day, COUNT(*) AS n FROM clients
                WHERE business_id = ?
                  AND date(created_at) >= date('now', ?) AND date(created_at) < date('now', ?)
                GROUP BY day""", (business_id, frm, to)):
            clients[r["day"]] = int(r["n"] or 0)

    today = datetime.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        income, expense = money.get(d, (0, 0))
        o_n, o_sum = orders.get(d, (0, 0))
        out.append({"day": d, "income": income, "expense": expense,
                    "profit": income - expense, "orders": o_n,
                    "order_amount": o_sum, "clients": clients.get(d, 0)})
    return out


def service_revenue(business_id, days=30):
    """
    Выручка по услугам и товарам за окно — через связи «заявка включает услугу».

    Считаем по сумме заявок: это единственная цифра, которая у позиции есть.
    Заявка с двумя услугами даст обеим одну и ту же сумму, поэтому доли по
    услугам не складываются в 100% — и Директор об этом не врёт.
    """
    frm, to = _window(days, 0)
    with _connect() as conn:
        rows = conn.execute(
            """SELECT l.dst_type AS type, l.dst_id AS id, mf.title AS title,
                      COUNT(DISTINCT o.id) AS orders, COALESCE(SUM(o.amount),0) AS amount
                 FROM entity_links l
                 JOIN orders o ON o.id = l.src_id AND o.business_id = l.business_id
                 LEFT JOIN memory_facts mf ON mf.id = l.dst_id AND mf.business_id = l.business_id
                WHERE l.business_id = ? AND l.src_type = 'order'
                  AND l.dst_type IN ('service','product')
                  AND date(o.created_at) >= date('now', ?) AND date(o.created_at) < date('now', ?)
                GROUP BY l.dst_type, l.dst_id, mf.title
                ORDER BY amount DESC, orders DESC""",
            (business_id, frm, to)).fetchall()
    return [{"type": r["type"], "id": r["id"], "title": r["title"] or "без названия",
             "orders": int(r["orders"] or 0), "amount": int(r["amount"] or 0)} for r in rows]


def payroll_period(business_id, days=30, offset=0):
    """Сколько ушло людям за окно: выплаты сотрудникам и всё с пометкой «зарплата»."""
    frm, to = _window(days, offset)
    with _connect() as conn:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(f.amount),0) AS total, COUNT(*) AS n,
                       COUNT(DISTINCT f.employee_id) AS people
                  FROM finance_entries f
                 WHERE f.business_id = ? AND f.kind = 'expense'
                   AND (f.employee_id IS NOT NULL OR f.doc_type = 'salary')
                   AND {_OP_DAY} >= date('now', ?) AND {_OP_DAY} < date('now', ?)""",
            (business_id, frm, to)).fetchone()
    return {"total": int(row["total"] or 0), "n": int(row["n"] or 0),
            "people": int(row["people"] or 0)}


def sleeping_clients(business_id, days=30, limit=5):
    """Кто покупал, но давно не возвращался, — с суммой, которую они приносили."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, COUNT(o.id) AS orders,
                      COALESCE(SUM(o.amount),0) AS spent, MAX(o.created_at) AS last_at
                 FROM clients c JOIN orders o ON o.client_id = c.id
                WHERE c.business_id = ? AND c.archived_at IS NULL
                GROUP BY c.id
                HAVING COUNT(o.id) > 0 AND MAX(o.created_at) < date('now', ?)
                ORDER BY spent DESC LIMIT ?""",
            (business_id, f"-{int(days)} day", int(limit))).fetchall()
    return [dict(r) for r in rows]


def data_span(business_id):
    """
    С какого дня у бизнеса вообще есть данные и сколько их.

    Нужно, чтобы честно сказать «недостаточно данных»: сравнивать два периода
    у бизнеса, который живёт в системе четыре дня, — это выдумывать тренды.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT MIN(d) AS first_day, SUM(n) AS rows FROM (
                   SELECT MIN(COALESCE(op_date, date(created_at))) AS d, COUNT(*) AS n
                     FROM finance_entries WHERE business_id = ?
                   UNION ALL
                   SELECT MIN(date(created_at)), COUNT(*) FROM orders  WHERE business_id = ?
                   UNION ALL
                   SELECT MIN(date(created_at)), COUNT(*) FROM clients WHERE business_id = ?
               ) AS all_data""",
            (business_id, business_id, business_id)).fetchone()
        days = conn.execute(
            "SELECT CAST(julianday('now') - julianday(?) AS INTEGER) AS d",
            (row["first_day"],)).fetchone()["d"] if row and row["first_day"] else None
    return {"first_day": row["first_day"] if row else None,
            "rows": int((row["rows"] if row else 0) or 0),
            "days": int(days) if days is not None else 0}


# ---------- СВЯЗИ МЕЖДУ ЗАПИСЯМИ ----------
# Ребро всегда двунаправленное по смыслу: если заявка включает услугу, то
# услуга участвует в заявке. Поэтому храним его один раз, а читаем с обеих
# сторон — иначе пришлось бы следить за симметрией двух строк.

def add_entity_link(business_id, src_type, src_id, dst_type, dst_id,
                    kind="related", confidence=1.0, source="manual", note=None):
    """Связать две записи. Повтор той же связи не создаёт второго ребра."""
    src_id, dst_id = int(src_id), int(dst_id)
    if src_type == dst_type and src_id == dst_id:
        return None                      # запись, связанная сама с собой, — мусор
    with _connect() as conn:
        row = conn.execute(
            """SELECT id FROM entity_links
                WHERE business_id = ? AND src_type = ? AND src_id = ?
                  AND dst_type = ? AND dst_id = ? AND kind = ?""",
            (business_id, src_type, src_id, dst_type, dst_id, kind),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            """INSERT INTO entity_links
                   (business_id, src_type, src_id, dst_type, dst_id, kind,
                    confidence, source, note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (business_id, src_type, src_id, dst_type, dst_id, kind,
             confidence, source, note),
        )
        return cur.lastrowid


def delete_entity_link(link_id, business_id):
    with _connect() as conn:
        conn.execute("DELETE FROM entity_links WHERE id = ? AND business_id = ?",
                     (int(link_id), business_id))


def drop_entity_links(business_id, entity_type, entity_id, kind=None, source=None):
    """Убрать рёбра записи с обеих сторон — при удалении или пересборке связей."""
    where = "business_id = ? AND ((src_type = ? AND src_id = ?) OR (dst_type = ? AND dst_id = ?))"
    params = [business_id, entity_type, int(entity_id), entity_type, int(entity_id)]
    if kind:
        where += " AND kind = ?"; params.append(kind)
    if source:
        where += " AND source = ?"; params.append(source)
    with _connect() as conn:
        conn.execute(f"DELETE FROM entity_links WHERE {where}", tuple(params))


def entity_links_of(business_id, entity_type, entity_id):
    """
    Все рёбра записи, приведённые к виду «сосед». Направление сохраняем в
    поле dir: «включает» и «входит в» — это одна связь, прочитанная с разных
    сторон, и человеку важно, с какой стороны он смотрит.
    """
    eid = int(entity_id)
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM entity_links
                WHERE business_id = ? AND ((src_type = ? AND src_id = ?)
                                        OR (dst_type = ? AND dst_id = ?))
                ORDER BY id DESC""",
            (business_id, entity_type, eid, entity_type, eid),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out_going = d["src_type"] == entity_type and d["src_id"] == eid
        out.append({"link_id": d["id"], "kind": d["kind"], "source": d["source"],
                    "confidence": d["confidence"], "note": d["note"],
                    "created_at": d["created_at"], "dir": "out" if out_going else "in",
                    "type": d["dst_type"] if out_going else d["src_type"],
                    "id": d["dst_id"] if out_going else d["src_id"]})
    return out


def entity_link_counts(business_id, entity_type, entity_ids):
    """Сколько связей у каждой записи — чтобы список не делал N запросов."""
    ids = [int(i) for i in entity_ids if i is not None]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT ref, COUNT(*) AS n FROM (
                    SELECT src_id AS ref FROM entity_links
                     WHERE business_id = ? AND src_type = ? AND src_id IN ({ph})
                    UNION ALL
                    SELECT dst_id AS ref FROM entity_links
                     WHERE business_id = ? AND dst_type = ? AND dst_id IN ({ph})
                ) AS both GROUP BY ref""",
            (business_id, entity_type, *ids, business_id, entity_type, *ids),
        ).fetchall()
    return {int(r["ref"]): int(r["n"]) for r in rows}


def finance_by_ref(business_id, field, entity_id, limit=50):
    """Денежные операции, привязанные к записи (сотруднику, заявке, клиенту…)."""
    if field not in FINANCE_REFS:
        return []
    with _connect() as conn:
        rows = conn.execute(
            _finance_join(f" AND f.{field} = ? ORDER BY f.id DESC LIMIT ?"),
            (business_id, int(entity_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def finance_totals_by_ref(business_id, field, entity_id):
    """Итог по записи: сколько получено и сколько заплачено."""
    if field not in FINANCE_REFS:
        return {"income": 0, "expense": 0, "count": 0}
    with _connect() as conn:
        row = conn.execute(
            f"""SELECT
                   COALESCE(SUM(CASE WHEN kind = 'income'  THEN amount ELSE 0 END),0) AS income,
                   COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END),0) AS expense,
                   COUNT(*) AS n
                 FROM finance_entries WHERE business_id = ? AND {field} = ?""",
            (business_id, int(entity_id)),
        ).fetchone()
    return {"income": int(row["income"] or 0), "expense": int(row["expense"] or 0),
            "count": int(row["n"] or 0)}


def count_client_messages(business_id, client_id):
    """Сколько раз клиент писал — обращение это тоже связь, а не только заказ."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM messages
                WHERE business_id = ? AND client_id = ? AND role = 'user'""",
            (business_id, int(client_id)),
        ).fetchone()
    return int(row["n"] or 0)


def orders_by_ids(business_id, order_ids):
    ids = [int(i) for i in order_ids if i is not None]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM orders WHERE business_id = ? AND id IN ({ph}) ORDER BY id DESC",
            (business_id, *ids),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- АРХИВ И УДАЛЕНИЕ ----------
# Две разные вещи, и путать их нельзя. Архив: запись была правдой и перестала
# действовать — она уходит из работы, но остаётся в базе вместе со всей своей
# историей. Удаление: записи не должно было быть — она уходит совсем.
#
# Удалять запись, на которую ссылаются деньги или заказы, нельзя: в выплате
# останется id несуществующего сотрудника, и «сколько мы платим Петровой»
# начнёт врать. Такие записи архивируются — связь при этом продолжает
# работать, потому что join по архиву не фильтрует.

ARCHIVABLE = {"memory_facts", "clients", "documents"}


def set_archived(table, entity_id, business_id, on=True):
    """Убрать запись из работы или вернуть обратно. Данные не трогаем."""
    if table not in ARCHIVABLE:
        return False
    when = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") if on else None
    with _connect() as conn:
        conn.execute(
            f"UPDATE {table} SET archived_at = ? WHERE id = ? AND business_id = ?",
            (when, int(entity_id), business_id),
        )
    return True


def is_archived(table, entity_id, business_id):
    if table not in ARCHIVABLE:
        return False
    with _connect() as conn:
        row = conn.execute(
            f"SELECT archived_at FROM {table} WHERE id = ? AND business_id = ?",
            (int(entity_id), business_id),
        ).fetchone()
    return bool(row and row["archived_at"])


# На что ссылаются деньги: поле операции -> что в нём лежит.
FINANCE_REFS = {"employee_id": "memory_facts", "supplier_id": "memory_facts",
                "client_id": "clients", "order_id": "orders"}


def finance_ref_count(business_id, field, entity_id):
    """Сколько денежных операций ссылается на эту запись."""
    if field not in FINANCE_REFS:
        return 0
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM finance_entries "
            f"WHERE business_id = ? AND {field} = ?",
            (business_id, int(entity_id)),
        ).fetchone()
    return int(row["n"] or 0)


def orders_of_client_count(business_id, client_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE business_id = ? AND client_id = ?",
            (business_id, int(client_id)),
        ).fetchone()
    return int(row["n"] or 0)


def delete_client(client_id, business_id):
    """Удалить клиента — только своего и только без заказов (иначе архив)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT name FROM clients WHERE id = ? AND business_id = ?",
            (client_id, business_id),
        ).fetchone()
        conn.execute("DELETE FROM clients WHERE id = ? AND business_id = ?",
                     (client_id, business_id))
    if row:
        log_event(business_id, "client", f"Клиент удалён: {row['name'] or 'без имени'}")


def delete_order(order_id, business_id):
    """Удалить заявку — только свою."""
    with _connect() as conn:
        conn.execute("DELETE FROM orders WHERE id = ? AND business_id = ?",
                     (order_id, business_id))


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
        # Входящие материалы и цепочка «источник → запись» — тоже данные бизнеса.
        # Без этого от удалённой компании оставался бы её журнал происхождения.
        for tbl in ("memory_links", "entity_links", "inbox_decisions", "inbox_results",
                    "inbox_blobs", "inbox_items", "module_state", "connections",
                    "ig_threads", "ig_seen", "ai_policy", "leads", "followups",
                    "actions", "initiatives", "outputs"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE business_id = ?", (business_id,))
            except Exception:
                pass          # таблицы может не быть в старой базе
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


def list_clients(business_id, query=None, limit=50, offset=0, segment="all",
                 archived=False):
    """Клиенты бизнеса + число заказов, сумма покупок, дата последнего заказа.
    Поддерживает поиск по имени/телефону, срез базы и постраничную загрузку."""
    where = "c.business_id = ?" + (" AND c.archived_at IS NOT NULL" if archived
                                   else " AND c.archived_at IS NULL")
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
    where = "c.business_id = ? AND c.archived_at IS NULL"
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


def count_clients(business_id, query=None, archived=False):
    """Сколько всего клиентов подходит под фильтр — для пагинации."""
    where = "business_id = ?" + (" AND archived_at IS NOT NULL" if archived
                                 else " AND archived_at IS NULL")
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
    where = "business_id = ? AND archived_at IS NULL"
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
            # Канал — часть истории: у клиента, который писал и в бот, и в
            # директ, это единственный способ понять, где именно шёл разговор.
            """SELECT role, content, channel, created_at FROM messages
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


def list_documents(business_id, archived=False):
    """Загруженные документы бизнеса (имя, число чанков, дата)."""
    where = "business_id = ?" + (" AND archived_at IS NOT NULL" if archived
                                 else " AND archived_at IS NULL")
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM documents WHERE {where} ORDER BY id DESC",
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


def count_documents(business_id, archived=False):
    where = "business_id = ?" + (" AND archived_at IS NOT NULL" if archived
                                 else " AND archived_at IS NULL")
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM documents WHERE {where}", (business_id,)
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
        # Архивный документ не отвечает на вопросы: если владелец убрал
        # старый прайс, ядро не должно цитировать его цены.
        rows = conn.execute(
            """SELECT ch.content FROM doc_chunks ch
                 JOIN documents d ON d.id = ch.doc_id AND d.business_id = ch.business_id
                WHERE ch.business_id = ? AND d.archived_at IS NULL""",
            (business_id,),
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


def save_message(business_id, client_id, role, content, channel=None, created_at=None):
    """
    Записать реплику разговора.

    channel — откуда она: instagram, telegram, веб. Пусто у всего, что записано
    до появления второго канала; проставлять его задним числом было бы догадкой.
    created_at нужен, когда сообщение подтянуто из чужой истории: время у него
    своё, и подменять его моментом загрузки — значит переписать прошлое.

    Возвращает id реплики. Он нужен лиду: возможность помнит, с какого
    сообщения разговор начался, — и это дешевле и честнее, чем копировать
    текст переписки внутрь карточки.
    """
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO messages (business_id, client_id, role, content, channel, created_at)
               VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
            (business_id, client_id, role, content, channel, created_at),
        )
        return cur.lastrowid


# ---------- АВТОНОМИЯ AI-ПРОДАВЦА ----------

def ai_policy(business_id, channel):
    """Настройка автономии канала. None — владелец её ещё не трогал."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_policy WHERE business_id = ? AND channel = ?",
            (business_id, channel)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["grants"] = _json.loads(d.get("grants") or "[]")
    except (ValueError, TypeError):
        d["grants"] = []
    try:
        modes = _json.loads(d.get("modes") or "{}")
    except (ValueError, TypeError):
        modes = {}
    # Испорченная настройка читается как пустая, а пустая означает «как по
    # умолчанию», то есть осторожно. Развалить JSON и получить больше свободы,
    # чем владелец давал, здесь невозможно.
    d["modes"] = modes if isinstance(modes, dict) else {}
    return d


def save_ai_policy(business_id, channel, level=None, grants=None, actor=None,
                   modes=None):
    """
    Записать уровень и/или разрешения.

    None означает «не трогали»: включить право, не сбив уровень, и поднять
    уровень, не тронув права, — два разных действия, и путать их нельзя.
    """
    cur = ai_policy(business_id, channel) or {}
    new_level = cur.get("level") if level is None else int(level)
    new_grants = cur.get("grants") or [] if grants is None else list(grants)
    new_modes = cur.get("modes") or {} if modes is None else dict(modes)
    blob = _json.dumps(new_grants, ensure_ascii=False)
    mblob = _json.dumps(new_modes, ensure_ascii=False)
    with _connect() as conn:
        if cur:
            conn.execute(
                """UPDATE ai_policy SET level = ?, grants = ?, modes = ?, updated_by = ?,
                       updated_at = datetime('now')
                   WHERE business_id = ? AND channel = ?""",
                (new_level, blob, mblob, actor, business_id, channel))
        else:
            conn.execute(
                """INSERT INTO ai_policy (business_id, channel, level, grants, modes, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (business_id, channel, new_level, blob, mblob, actor))


def ig_thread_reason(business_id, igsid, why=None, draft=None):
    """Запомнить, чего VELOR не хватило и что он хотел ответить."""
    with _connect() as conn:
        conn.execute(
            """UPDATE ig_threads SET paused_why = ?, ai_draft = ?
               WHERE business_id = ? AND igsid = ?""",
            (why, draft, business_id, str(igsid)))


# ---------- INSTAGRAM: переписки и защита от повторов ----------

def find_business_by_ig_id(ig_id):
    """
    Чей это аккаунт Instagram.

    Вебхук у приложения Meta один на всех, а компаний у нас много: в звонке
    приходит только id аккаунта, и по нему нужно попасть ровно в тот бизнес,
    которому он принадлежит. Ошибиться здесь — значит показать переписку чужой
    компании, поэтому ищем по точному совпадению записанного при подключении id.
    """
    ig_id = str(ig_id or "").strip()
    if not ig_id:
        return None
    with _connect() as conn:
        rows = conn.execute(
            "SELECT business_id, meta FROM connections WHERE provider = 'instagram'"
        ).fetchall()
    for r in rows:
        try:
            meta = _json.loads(r["meta"] or "{}")
        except (ValueError, TypeError):
            continue
        if str(meta.get("ig_id") or "") == ig_id:
            return get_business(r["business_id"])
    return None


def ig_seen_mid(business_id, mid):
    """
    Видели это сообщение раньше? Заодно помечаем как виденное.

    Помечаем ДО обработки — как и с апдейтами Telegram: повтор при таймауте не
    должен родить второй ответ клиенту.
    """
    mid = str(mid or "").strip()
    if not mid:
        return False
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM ig_seen WHERE business_id = ? AND mid = ?",
                           (business_id, mid)).fetchone()
        if row:
            return True
        try:
            conn.execute("INSERT INTO ig_seen (business_id, mid) VALUES (?, ?)",
                         (business_id, mid))
        except Exception:
            return True          # кто-то вставил её параллельно — значит, уже видели
        return False


def ig_thread(business_id, igsid):
    """Одна переписка директа. None — такой ещё не было."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ig_threads WHERE business_id = ? AND igsid = ?",
            (business_id, str(igsid))).fetchone()
    return dict(row) if row else None


def ig_thread_of_client(business_id, client_id):
    """
    Переписка директа этого клиента. Нужна, когда идти надо в обратную сторону:
    у нас есть человек в базе, а найти надо его id в Instagram.
    """
    if not client_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM ig_threads WHERE business_id = ? AND client_id = ?
                ORDER BY COALESCE(last_in_at, created_at) DESC LIMIT 1""",
            (business_id, int(client_id))).fetchone()
    return dict(row) if row else None


def ig_thread_upsert(business_id, igsid, client_id, username=None, name=None,
                     avatar=None, last_in_at=None, last_out_at=None):
    """
    Завести или дополнить переписку.

    Дополняем только пустое: имя, полученное при первом сообщении, не должно
    затираться пустым ответом профиля в следующий раз.
    """
    igsid = str(igsid)
    existing = ig_thread(business_id, igsid)
    with _connect() as conn:
        if not existing:
            conn.execute(
                """INSERT INTO ig_threads (business_id, igsid, client_id, username, name,
                                           avatar, last_in_at, last_out_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (business_id, igsid, client_id, username, name, avatar,
                 last_in_at, last_out_at))
            return
        sets, params = [], []
        for col, val in (("username", username), ("name", name), ("avatar", avatar)):
            if val and not existing.get(col):
                sets.append(f"{col} = ?")
                params.append(val)
        for col, val in (("last_in_at", last_in_at), ("last_out_at", last_out_at)):
            if val:
                sets.append(f"{col} = ?")
                params.append(val)
        if sets:
            conn.execute("UPDATE ig_threads SET " + ", ".join(sets)
                         + " WHERE business_id = ? AND igsid = ?",
                         (*params, business_id, igsid))


def ig_thread_mark(business_id, igsid, last_in_at=None, last_out_at=None):
    """Отметить время последнего входящего или исходящего."""
    sets, params = [], []
    if last_in_at:
        sets.append("last_in_at = ?")
        params.append(last_in_at)
    if last_out_at:
        sets.append("last_out_at = ?")
        params.append(last_out_at)
    if not sets:
        return
    with _connect() as conn:
        conn.execute("UPDATE ig_threads SET " + ", ".join(sets)
                     + " WHERE business_id = ? AND igsid = ?",
                     (*params, business_id, str(igsid)))


def ig_thread_pause(business_id, igsid, paused=True, by="owner"):
    """
    Передать разговор человеку — или вернуть его VELOR.

    Кто именно взял разговор, записываем: «владелец нажал кнопку» и «владелец
    ответил из приложения Instagram» — разные события, и по ним видно, где
    людям приходится вмешиваться чаще всего.
    """
    on = 1 if paused else 0
    with _connect() as conn:
        # Возвращая разговор VELOR, стираем и причину, и черновик: они
        # относились к прошлой остановке, а оставленные висеть объясняли бы
        # владельцу то, чего уже нет.
        conn.execute(
            """UPDATE ig_threads SET ai_paused = ?, paused_by = ?,
                   paused_at = CASE WHEN ? = 1 THEN datetime('now') ELSE NULL END,
                   paused_why = CASE WHEN ? = 1 THEN paused_why ELSE NULL END,
                   ai_draft   = CASE WHEN ? = 1 THEN ai_draft   ELSE NULL END
               WHERE business_id = ? AND igsid = ?""",
            (on, by if paused else None, on, on, on, business_id, str(igsid)))


def ig_thread_paused(business_id, igsid):
    t = ig_thread(business_id, igsid)
    return bool(t and t.get("ai_paused"))


def ig_threads_list(business_id, limit=100):
    """Все переписки директа — свежие сверху, с последней репликой для списка."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT t.*, c.name AS client_name, c.phone AS client_phone,
                      (SELECT content FROM messages m WHERE m.business_id = t.business_id
                        AND m.client_id = t.client_id ORDER BY m.id DESC LIMIT 1) AS last_text,
                      (SELECT role FROM messages m WHERE m.business_id = t.business_id
                        AND m.client_id = t.client_id ORDER BY m.id DESC LIMIT 1) AS last_role,
                      (SELECT COUNT(*) FROM messages m WHERE m.business_id = t.business_id
                        AND m.client_id = t.client_id) AS msgs
               FROM ig_threads t
               LEFT JOIN clients c ON c.id = t.client_id
               WHERE t.business_id = ?
               ORDER BY COALESCE(t.last_in_at, t.last_out_at, t.created_at) DESC
               LIMIT ?""",
            (business_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


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
              date_wanted=None, amount=0, source=None):
    """Записать новый заказ. Возвращает id заказа.

    amount — сумма заказа в рублях. Именно из неё складывается оборот бизнеса,
    сумма покупок клиента и вся денежная аналитика, поэтому её нужно писать
    сразу при создании (раньше колонка существовала, но не заполнялась никогда,
    и все обороты в системе были нулями).

    source — каким каналом пришла заявка. Без него нельзя ответить на вопрос
    «сколько денег приносит директ», а он и есть причина подключать канал.
    """
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO orders (business_id, client_id, text, phone, address,
                                   date_wanted, amount, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, client_id, text, phone, address, date_wanted,
             _money(amount), source),
        )
        order_id = cur.lastrowid
    detail = (text or "")[:120]
    if _money(amount):
        detail += f" · {_money(amount)} ₽"
    log_event(business_id, "order", "Создан заказ", detail)
    # Заявку создают из пяти мест (панель, бот, разбор входящих, ручной ввод,
    # импорт). Связь «из каких услуг она состоит» нужна во всех пяти, поэтому
    # ставится здесь — одна дверь вместо пяти одинаковых вызовов, которые
    # однажды разойдутся. Импорт локальный: graph знает про базу, база про
    # graph знать не обязана.
    try:
        import graph
        graph.link_order_items(business_id, order_id, text)
    except Exception:
        pass
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


# ---------- ЛИДЫ (коммерческие возможности) ----------
#
# Здесь только хранение. Что считать лидом, когда его заводить и как он живёт —
# в leads.py: база не должна знать про коммерческий смысл, а смысл не должен
# знать про SQL.

LEAD_STATUSES = ("new", "qualified", "in_progress", "won", "lost")
# Открытый лид — тот, по которому ещё можно что-то сделать. Именно он
# продолжается следующим сообщением, а не заводится заново.
LEAD_OPEN = ("new", "qualified", "in_progress")

LEAD_FIELDS = ("client_id", "title", "interest", "status", "source", "channel",
               "value", "currency", "order_id", "lost_reason", "owner_fields",
               "meta", "first_message_id", "last_message_id",
               "last_activity_at", "converted_at", "lost_at",
               # оценка возможности: чем она сильна, насколько наша и почему
               "intent", "fit", "priority", "estimated_value", "wanted_at",
               "signals", "qualified_at")

# Поля, которые хранятся строкой JSON. Пустое значение — не «null», а пустой
# список или словарь: иначе каждый вызывающий писал бы свою проверку на None.
LEAD_JSON = {"meta": dict, "owner_fields": list, "signals": list}


def now():
    """Сейчас — в том же виде, в каком время пишет сама база (CURRENT_TIMESTAMP).

    Иначе «последняя активность» и «создан» оказались бы в разных форматах, и
    сравнение дат в SQL молча перестало бы работать.
    """
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _lead_row(row):
    """Строка лида наружу: JSON-поля разобраны, число — числом."""
    d = dict(row)
    for key, empty in LEAD_JSON.items():
        if key not in d:
            continue
        raw = d.get(key)
        try:
            d[key] = _json.loads(raw) if raw else empty()
        except (ValueError, TypeError):
            d[key] = empty()
    for key in ("value", "estimated_value"):
        d[key] = int(d[key]) if d.get(key) not in (None, "") else None
    return d


def _lead_value(value):
    """
    Сумма лида. Пусто — значит НЕ ЗНАЕМ, и это записывается как NULL, а не 0.

    Разница принципиальная: 0 читается как «сделка на ноль рублей» и портит
    средний чек, а «бюджет не назвали» — обычное состояние живого лида.
    """
    if value in (None, "", "null"):
        return None
    v = _money(value)
    return v or None


def add_lead(business_id, title, *, client_id=None, interest=None, status="new",
             source=None, channel=None, value=None, currency=None, meta=None,
             owner_fields=None, first_message_id=None, last_message_id=None):
    """Завести лид. Возвращает id."""
    if not business_id:
        raise ValueError("add_lead требует business_id (защита арендаторов)")
    status = status if status in LEAD_STATUSES else "new"
    stamp = now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO leads (business_id, client_id, title, interest, status,
                                  source, channel, value, currency, meta, owner_fields,
                                  first_message_id, last_message_id, last_activity_at,
                                  updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (business_id, int(client_id) if client_id else None,
             str(title or "").strip()[:200], (interest or None), status,
             source or None, channel or None, _lead_value(value),
             (currency or None) if _lead_value(value) else None,
             _json.dumps(meta or {}, ensure_ascii=False),
             _json.dumps(sorted(set(owner_fields or [])), ensure_ascii=False),
             first_message_id, last_message_id, stamp, stamp),
        )
        return cur.lastrowid


def get_lead(lead_id, business_id):
    """Лид вместе с именем и телефоном клиента — в списке они нужны всегда."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT l.*, c.name AS client_name, c.phone AS client_phone
                 FROM leads l LEFT JOIN clients c
                   ON c.id = l.client_id AND c.business_id = l.business_id
                WHERE l.id = ? AND l.business_id = ?""",
            (int(lead_id), business_id),
        ).fetchone()
    return _lead_row(row) if row else None


def list_leads(business_id, status=None, client_id=None, source=None,
               limit=50, offset=0):
    """
    Лиды бизнеса. status='open' — все незакрытые: так о них и спрашивают.
    """
    where = ["l.business_id = ?"]
    args = [business_id]
    if status == "open":
        where.append("l.status IN (%s)" % ",".join("?" * len(LEAD_OPEN)))
        args += list(LEAD_OPEN)
    elif status in LEAD_STATUSES:
        where.append("l.status = ?")
        args.append(status)
    if client_id:
        where.append("l.client_id = ?")
        args.append(int(client_id))
    if source:
        where.append("l.source = ?")
        args.append(source)
    args += [int(limit), max(0, int(offset or 0))]
    with _connect() as conn:
        rows = conn.execute(
            """SELECT l.*, c.name AS client_name, c.phone AS client_phone
                 FROM leads l LEFT JOIN clients c
                   ON c.id = l.client_id AND c.business_id = l.business_id
                WHERE """ + " AND ".join(where) + """
                ORDER BY l.id DESC LIMIT ? OFFSET ?""",
            tuple(args),
        ).fetchall()
    return [_lead_row(r) for r in rows]


def count_leads(business_id, status=None):
    where, args = ["business_id = ?"], [business_id]
    if status == "open":
        where.append("status IN (%s)" % ",".join("?" * len(LEAD_OPEN)))
        args += list(LEAD_OPEN)
    elif status in LEAD_STATUSES:
        where.append("status = ?")
        args.append(status)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE " + " AND ".join(where), tuple(args)
        ).fetchone()
    return int(row["n"] or 0)


def open_lead_of_client(business_id, client_id):
    """
    Живая возможность этого клиента, если она есть. Самая свежая из открытых.

    Ради неё и существует индекс по (business_id, client_id): вопрос задаётся
    на каждое входящее сообщение.
    """
    if not client_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM leads
                WHERE business_id = ? AND client_id = ?
                  AND status IN (%s)
                ORDER BY COALESCE(last_activity_at, created_at) DESC, id DESC
                LIMIT 1""" % ",".join("?" * len(LEAD_OPEN)),
            (business_id, int(client_id), *LEAD_OPEN),
        ).fetchone()
    return _lead_row(row) if row else None


def update_lead(lead_id, business_id, **fields):
    """
    Изменить лид — только в своём бизнесе.

    Списки и словари (owner_fields, meta) складываем в JSON здесь: вызывающему
    коду не нужно помнить, как они хранятся.
    """
    if not business_id:
        raise ValueError("update_lead требует business_id (защита арендаторов)")
    sets = {}
    for key, val in fields.items():
        if key not in LEAD_FIELDS:
            continue
        if key in ("value", "estimated_value"):
            sets[key] = _lead_value(val)
        elif key == "status":
            if val not in LEAD_STATUSES:
                raise ValueError("Неизвестный статус лида: %s" % val)
            sets[key] = val
        elif key in LEAD_JSON:
            if isinstance(val, (dict, list, tuple, set)):
                # owner_fields — множество имён, его сортируем; signals —
                # последовательность наблюдений, и её порядок сам по себе
                # смысл: по нему видно, как намерение росло.
                if key == "owner_fields":
                    val = sorted(set(val))
                elif isinstance(val, (tuple, set)):
                    val = list(val)
                sets[key] = _json.dumps(val, ensure_ascii=False)
            else:
                sets[key] = val
        else:
            sets[key] = val
    if not sets:
        return False
    sets["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE leads SET {cols} WHERE id = ? AND business_id = ?",
            (*sets.values(), int(lead_id), business_id),
        )
        return bool(cur.rowcount)


def delete_lead(lead_id, business_id):
    if not business_id:
        raise ValueError("delete_lead требует business_id (защита арендаторов)")
    with _connect() as conn:
        cur = conn.execute("DELETE FROM leads WHERE id = ? AND business_id = ?",
                           (int(lead_id), business_id))
        return bool(cur.rowcount)


def leads_overview(business_id):
    """
    Воронка числами. Всё считается ЗДЕСЬ, из таблицы лидов, — второго способа
    узнать «сколько у нас лидов» в системе больше нет.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END),0) AS new,
                      COALESCE(SUM(CASE WHEN status = 'qualified' THEN 1 ELSE 0 END),0) AS qualified,
                      COALESCE(SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END),0) AS in_progress,
                      COALESCE(SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END),0) AS won,
                      COALESCE(SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END),0) AS lost,
                      COALESCE(SUM(CASE WHEN date(created_at) >= date('now','-30 day')
                                        THEN 1 ELSE 0 END),0) AS last30
                 FROM leads WHERE business_id = ?""",
            (business_id,),
        ).fetchone()
    out = {k: int(row[k] or 0) for k in
           ("total", "new", "qualified", "in_progress", "won", "lost", "last30")}
    out["open"] = out["new"] + out["qualified"] + out["in_progress"]
    # Конверсия считается от ЗАКРЫТЫХ: делить выигранные на все — значит
    # занижать её ровно на те лиды, по которым ещё идёт разговор.
    closed = out["won"] + out["lost"]
    out["closed"] = closed
    out["conversion"] = round(out["won"] * 100 / closed) if closed else 0
    return out


def leads_period(business_id, days=14, offset=0):
    """
    Воронка за окно: сколько появилось, сколько выиграно, сколько потеряно.

    Считаем по датам СОБЫТИЙ, а не создания: возможность, заведённая месяц
    назад и проигранная вчера, — это потеря вчерашнего периода, а не
    прошлого. Иначе сравнение двух недель показывало бы движение там, где
    его нет.

    Возвращает и id потерянных: инициативе нужны не только числа, но и
    возможность показать владельцу, о каких именно возможностях речь.
    """
    frm, to = _window(days, offset)
    with _connect() as conn:
        created = conn.execute(
            """SELECT COUNT(*) AS n FROM leads WHERE business_id = ?
                 AND date(created_at) >= date('now', ?) AND date(created_at) < date('now', ?)""",
            (business_id, frm, to)).fetchone()["n"] or 0
        won = conn.execute(
            """SELECT COUNT(*) AS n FROM leads WHERE business_id = ? AND status = 'won'
                 AND date(COALESCE(converted_at, created_at)) >= date('now', ?)
                 AND date(COALESCE(converted_at, created_at)) < date('now', ?)""",
            (business_id, frm, to)).fetchone()["n"] or 0
        lost_rows = conn.execute(
            """SELECT id, lost_reason FROM leads WHERE business_id = ? AND status = 'lost'
                 AND date(COALESCE(lost_at, created_at)) >= date('now', ?)
                 AND date(COALESCE(lost_at, created_at)) < date('now', ?)
                ORDER BY id DESC""",
            (business_id, frm, to)).fetchall()
    reasons = {}
    for r in lost_rows:
        key = r["lost_reason"] or "other"
        reasons[key] = reasons.get(key, 0) + 1
    lost = len(lost_rows)
    closed = won + lost
    return {"created": int(created), "won": int(won), "lost": lost,
            "closed": closed,
            # Конверсия — от ЗАКРЫТЫХ. Делить на все значило бы занижать её
            # ровно на те возможности, по которым разговор ещё идёт.
            "conversion": round(won * 100 / closed) if closed else None,
            "lost_reasons": reasons,
            "lost_ids": [int(r["id"]) for r in lost_rows]}


def sales_today(business_id):
    """
    Что произошло с продажами сегодня — одним запросом на каждую цифру.

    Ни одной новой сущности: считаем по тем же таблицам, по которым живёт
    воронка. Второй «отчёт о продажах» рядом с ними однажды разошёлся бы с
    ними в цифрах, и владелец не знал бы, какой из двух верить.
    """
    with _connect() as conn:
        one = lambda q, p=(): int(conn.execute(q, p).fetchone()[0] or 0)
        new_leads = one(
            "SELECT COUNT(*) FROM leads WHERE business_id = ? "
            "AND date(created_at) = date('now')", (business_id,))
        open_leads = one(
            "SELECT COUNT(*) FROM leads WHERE business_id = ? AND status IN (%s)"
            % ",".join("?" * len(LEAD_OPEN)), (business_id, *LEAD_OPEN))
        won = one(
            "SELECT COUNT(*) FROM leads WHERE business_id = ? AND status = 'won' "
            "AND date(COALESCE(converted_at, created_at)) = date('now')",
            (business_id,))
        lost = one(
            "SELECT COUNT(*) FROM leads WHERE business_id = ? AND status = 'lost' "
            "AND date(COALESCE(lost_at, created_at)) = date('now')", (business_id,))
        drafts = one(
            "SELECT COUNT(*) FROM followups WHERE business_id = ? AND status = ?",
            (business_id, FU_DRAFT))
        sent = one(
            "SELECT COUNT(*) FROM followups WHERE business_id = ? AND status = ? "
            "AND date(sent_at) = date('now')", (business_id, FU_SENT))
        replies = one(
            "SELECT COUNT(*) FROM followups WHERE business_id = ? "
            "AND outcome IN ('replied','converted') AND date(outcome_at) = date('now')",
            (business_id,))
        orders = one(
            "SELECT COUNT(*) FROM orders WHERE business_id = ? "
            "AND date(created_at) = date('now')", (business_id,))
        # Выручка — только из заявок с проставленной суммой. Оценка стоимости
        # возможности сюда не попадает и попасть не может: это разные вещи, и
        # смешать их значит однажды показать владельцу деньги, которых нет.
        turnover = one(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE business_id = ? "
            "AND date(created_at) = date('now')", (business_id,))
    return {"new_leads": new_leads, "open_leads": open_leads, "won": won,
            "lost": lost, "drafts": drafts, "sent": sent, "replies": replies,
            "orders": orders, "turnover": turnover}


def lead_messages(business_id, lead_id, limit=100):
    """
    Переписка, из которой вырос лид. Не копия — выборка из messages по границам
    разговора. Поэтому «почему лид потерян» смотрится в тех же словах, которые
    клиент писал на самом деле, и правка карточки историю не переписывает.
    """
    lead = get_lead(lead_id, business_id)
    if not lead or not lead.get("client_id"):
        return []
    first = lead.get("first_message_id") or 0
    last = lead.get("last_message_id") or 0
    args = [business_id, lead["client_id"]]
    where = "business_id = ? AND client_id = ?"
    if first:
        where += " AND id >= ?"
        args.append(int(first))
    if last:
        where += " AND id <= ?"
        args.append(int(last))
    args.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY id ASC LIMIT ?", tuple(args)
        ).fetchall()
    return [dict(r) for r in rows]


def leads_of_client(business_id, client_id, limit=20):
    """Все возможности одного человека — и живые, и закрытые."""
    return list_leads(business_id, client_id=client_id, limit=limit)


def last_exchanges(business_id, client_ids):
    """
    То же, что last_exchange, но сразу по списку людей — одним запросом.

    Список возможностей показывается страницами по полсотни, и спрашивать про
    каждую отдельно значило бы делать полсотни запросов ради двух дат.
    """
    ids = [int(c) for c in (client_ids or []) if c]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT client_id,
                       MAX(CASE WHEN role = 'user' THEN created_at END) AS last_in,
                       MAX(CASE WHEN role = 'user' THEN id END)         AS last_in_id,
                       MAX(CASE WHEN role != 'user' THEN created_at END) AS last_out,
                       MAX(CASE WHEN role != 'user' THEN id END)        AS last_out_id
                  FROM messages
                 WHERE business_id = ? AND client_id IN ({marks})
                 GROUP BY client_id""",
            (business_id, *ids),
        ).fetchall()
    out = {}
    for r in rows:
        out[int(r["client_id"])] = {
            "last_in_at": r["last_in"], "last_out_at": r["last_out"],
            "last_in_id": r["last_in_id"],
            "unanswered": bool(r["last_in_id"]) and (
                not r["last_out_id"] or r["last_out_id"] < r["last_in_id"]),
        }
    return out


def last_exchange(business_id, client_id):
    """
    Когда клиент написал в последний раз и когда ему в последний раз ответили.

    Нужно ради одного вопроса, который стоит денег: «клиент спросил, а бизнес
    молчит?». Считаем в базе одним запросом — спрашивается это по каждой живой
    возможности, и вытаскивать ради двух дат всю переписку было бы расточительно.
    """
    if not client_id:
        return {"last_in_at": None, "last_out_at": None, "last_in_id": None,
                "unanswered": False}
    with _connect() as conn:
        row = conn.execute(
            """SELECT MAX(CASE WHEN role = 'user' THEN created_at END) AS last_in,
                      MAX(CASE WHEN role = 'user' THEN id END)         AS last_in_id,
                      MAX(CASE WHEN role != 'user' THEN created_at END) AS last_out,
                      MAX(CASE WHEN role != 'user' THEN id END)        AS last_out_id
                 FROM messages WHERE business_id = ? AND client_id = ?""",
            (business_id, int(client_id)),
        ).fetchone()
    last_in_id = row["last_in_id"]
    last_out_id = row["last_out_id"]
    return {
        "last_in_at": row["last_in"], "last_out_at": row["last_out"],
        "last_in_id": last_in_id,
        # Сравниваем по id, а не по времени: две реплики в одну секунду
        # различаются порядком записи, и «ответили раньше, чем спросили» —
        # это ошибка сравнения строк, а не факт.
        "unanswered": bool(last_in_id) and (not last_out_id or last_out_id < last_in_id),
    }


# ---------- КАСАНИЯ (follow-up по возможности) ----------
#
# Здесь только хранение. Когда касание уместно, что в нём написать и можно ли
# его отправить — в followup.py.

# Состояния. Их восемь, и разница между ними — это разница между «мы решили»,
# «мы собираемся» и «оно ушло». Слить их в одно поле «отправлено?» значит
# однажды посчитать отправленным то, что не ушло.
FU_DRAFT = "draft"           # VELOR подготовил, владелец ещё не смотрел
FU_APPROVED = "approved"     # владелец разрешил — отправить в подходящее время
FU_SCHEDULED = "scheduled"   # автоматика разрешена, время ещё не пришло
FU_SENDING = "sending"       # захвачено отправителем; чужой процесс мимо не пройдёт
FU_SENT = "sent"             # ушло по сети и подтвердилось
FU_CANCELLED = "cancelled"   # больше не нужно (клиент ответил, владелец отменил)
FU_BLOCKED = "blocked"       # нельзя отправлять, и написано почему
FU_FAILED = "failed"         # пробовали отправить, не вышло

FOLLOWUP_STATUSES = (FU_DRAFT, FU_APPROVED, FU_SCHEDULED, FU_SENDING, FU_SENT,
                     FU_CANCELLED, FU_BLOCKED, FU_FAILED)
# Живое касание — то, которое ещё может уйти.
FOLLOWUP_LIVE = (FU_DRAFT, FU_APPROVED, FU_SCHEDULED, FU_SENDING)
# Что считается потраченной попыткой. Заблокированное и отменённое не считаем:
# клиент их не видел, и наказывать за них следующую возможность не за что.
# А вот сорвавшуюся отправку считаем: сообщение могло уйти и не подтвердиться.
FOLLOWUP_SPENT = (FU_SENT, FU_FAILED, FU_SENDING)

FOLLOWUP_FIELDS = ("channel", "reason", "trigger", "status", "attempt",
                   "recommended_at", "message", "based_on", "stop_reason",
                   "error", "tries", "outcome", "outcome_at",
                   "reply_message_id", "order_id", "sent_at", "client_id")

FOLLOWUP_JSON = {"based_on": dict}


def _followup_row(row):
    d = dict(row)
    raw = d.get("based_on")
    try:
        d["based_on"] = _json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        d["based_on"] = {}
    for key in ("attempt", "tries"):
        d[key] = int(d.get(key) or 0)
    return d


def add_followup(business_id, lead_id, *, reason, channel=None, client_id=None,
                 trigger=None, status=FU_DRAFT, attempt=1, recommended_at=None,
                 message=None, based_on=None, stop_reason=None):
    """Записать предложенное касание. Возвращает id."""
    if not business_id:
        raise ValueError("add_followup требует business_id (защита арендаторов)")
    if status not in FOLLOWUP_STATUSES:
        status = FU_DRAFT
    stamp = now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO followups (business_id, lead_id, client_id, channel,
                                      reason, trigger, status, attempt,
                                      recommended_at, message, based_on,
                                      stop_reason, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (business_id, int(lead_id), int(client_id) if client_id else None,
             channel or None, reason, trigger or None, status, int(attempt or 1),
             recommended_at or None, message or None,
             _json.dumps(based_on or {}, ensure_ascii=False),
             stop_reason or None, stamp),
        )
        return cur.lastrowid


def get_followup(followup_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM followups WHERE id = ? AND business_id = ?",
            (int(followup_id), business_id)).fetchone()
    return _followup_row(row) if row else None


def list_followups(business_id, *, lead_id=None, status=None, live=False,
                   due_before=None, limit=100, offset=0):
    """
    Касания бизнеса. status может быть строкой или набором строк.

    due_before — только те, чьё время уже пришло: это и есть очередь отправки.
    """
    where, args = ["business_id = ?"], [business_id]
    if lead_id:
        where.append("lead_id = ?")
        args.append(int(lead_id))
    if live:
        where.append("status IN (%s)" % ",".join("?" * len(FOLLOWUP_LIVE)))
        args += list(FOLLOWUP_LIVE)
    elif isinstance(status, (list, tuple, set)):
        vals = [v for v in status if v in FOLLOWUP_STATUSES]
        if vals:
            where.append("status IN (%s)" % ",".join("?" * len(vals)))
            args += vals
    elif status in FOLLOWUP_STATUSES:
        where.append("status = ?")
        args.append(status)
    if due_before:
        where.append("COALESCE(recommended_at, created_at) <= ?")
        args.append(due_before)
    args += [int(limit), max(0, int(offset or 0))]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM followups WHERE " + " AND ".join(where) +
            " ORDER BY COALESCE(recommended_at, created_at) ASC, id ASC"
            " LIMIT ? OFFSET ?", tuple(args)).fetchall()
    return [_followup_row(r) for r in rows]


def update_followup(followup_id, business_id, **fields):
    """Изменить касание — только в своём бизнесе."""
    if not business_id:
        raise ValueError("update_followup требует business_id (защита арендаторов)")
    sets = {}
    for key, val in fields.items():
        if key not in FOLLOWUP_FIELDS:
            continue
        if key == "status" and val not in FOLLOWUP_STATUSES:
            raise ValueError("Неизвестное состояние касания: %s" % val)
        if key in FOLLOWUP_JSON and isinstance(val, (dict, list)):
            sets[key] = _json.dumps(val, ensure_ascii=False)
        else:
            sets[key] = val
    if not sets:
        return False
    sets["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE followups SET {cols} WHERE id = ? AND business_id = ?",
            (*sets.values(), int(followup_id), business_id))
        return bool(cur.rowcount)


def claim_followup(followup_id, business_id):
    """
    Захватить касание для отправки. True — оно наше и больше ничьё.

    Это и есть защита от повторной отправки. Проверить статус, а потом
    отправить — значит оставить щель между проверкой и отправкой, в которую
    пролезет второй запуск планировщика. Здесь проверка и захват — один
    UPDATE, и выиграть его может ровно один процесс.
    """
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE followups
                  SET status = ?, tries = tries + 1, updated_at = ?
                WHERE id = ? AND business_id = ? AND sent_at IS NULL
                  AND status IN (?, ?)""",
            (FU_SENDING, now(), int(followup_id), business_id,
             FU_APPROVED, FU_SCHEDULED))
        return bool(cur.rowcount)


def mark_followup_sent(followup_id, business_id):
    """Отметить отправленным. Только из состояния «отправляем» и только раз."""
    stamp = now()
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE followups SET status = ?, sent_at = ?, updated_at = ?
                WHERE id = ? AND business_id = ? AND status = ? AND sent_at IS NULL""",
            (FU_SENT, stamp, stamp, int(followup_id), business_id, FU_SENDING))
        return bool(cur.rowcount)


def followup_attempts(business_id, lead_id):
    """Сколько касаний по этой возможности уже потрачено."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM followups WHERE business_id = ? AND lead_id = ?"
            " AND status IN (%s)" % ",".join("?" * len(FOLLOWUP_SPENT)),
            (business_id, int(lead_id), *FOLLOWUP_SPENT)).fetchone()
    return int(row["n"] or 0)


def followups_sent_since(business_id, since):
    """Сколько касаний ушло с этого момента — для общего лимита на бизнес."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM followups WHERE business_id = ?"
            " AND status = ? AND sent_at >= ?",
            (business_id, FU_SENT, since)).fetchone()
    return int(row["n"] or 0)


def followups_of_leads(business_id, lead_ids):
    """
    Касание, о котором стоит сказать в строке списка, — одним запросом на всю
    страницу.

    Живое побеждает: пока что-то может уйти, важно именно оно. Если живого нет,
    показываем последнее случившееся — «отправлено, клиент ответил» это тоже
    ответ на вопрос «что тут происходит».
    """
    ids = [int(i) for i in (lead_ids or []) if i]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM followups
                 WHERE business_id = ? AND lead_id IN ({marks})
                 ORDER BY id ASC""",
            (business_id, *ids)).fetchall()
    out = {}
    for r in rows:
        lead_id = int(r["lead_id"])
        old = out.get(lead_id)
        if old and old["status"] in FOLLOWUP_LIVE and r["status"] not in FOLLOWUP_LIVE:
            continue
        out[lead_id] = _followup_row(r)
    return out


def followups_overview(business_id):
    """Картина по касаниям — для сводки владельцу."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM followups WHERE business_id = ?"
            " GROUP BY status", (business_id,)).fetchall()
    out = {k: 0 for k in FOLLOWUP_STATUSES}
    for r in rows:
        out[r["status"]] = int(r["n"] or 0)
    out["live"] = sum(out[k] for k in FOLLOWUP_LIVE)
    return out


def delete_followup(followup_id, business_id):
    if not business_id:
        raise ValueError("delete_followup требует business_id (защита арендаторов)")
    with _connect() as conn:
        cur = conn.execute("DELETE FROM followups WHERE id = ? AND business_id = ?",
                           (int(followup_id), business_id))
        return bool(cur.rowcount)


def messages_after(business_id, client_id, when, *, role=None, limit=20):
    """
    Что было сказано после этой отметки времени.

    Нужно ровно для сверки: касание помечено отправленным — значит, в переписке
    должно быть наше сообщение, и оно должно быть НЕ раньше отправки. Без
    привязки ко времени проверка «сообщение есть» проходила бы у любого
    разговора, где мы когда-либо отвечали.
    """
    if not client_id or not when:
        return []
    where = "business_id = ? AND client_id = ? AND created_at >= ?"
    args = [business_id, int(client_id), str(when)[:19]]
    if role == "user":
        where += " AND role = 'user'"
    elif role:
        where += " AND role != 'user'"
    args.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY id ASC LIMIT ?", args
        ).fetchall()
    return [dict(r) for r in rows]


def last_outbound(business_id, client_id):
    """
    Последнее сообщение, которое бизнес отправил этому человеку.

    Нужно ради одного вопроса: мы уже назвали условия и ждём решения — или
    просто поговорили? Границы лида для этого не годятся: они кончаются на
    последней реплике КЛИЕНТА, и наш собственный ответ в них не попадает.
    """
    if not client_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM messages
                WHERE business_id = ? AND client_id = ? AND role != 'user'
                ORDER BY id DESC LIMIT 1""",
            (business_id, int(client_id))).fetchone()
    return dict(row) if row else None


def outbound_hours(business_id, limit=500):
    """
    В какие часы этот бизнес на самом деле отвечает клиентам.

    Часового пояса у нас нет, и выдумывать его нельзя. Зато есть факт: время
    исходящих сообщений самого бизнеса. По нему видно рабочее окно, и оно
    настоящее, а не предположенное за владельца.

    Возвращает список часов (UTC) последних исходящих сообщений.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT created_at FROM messages
                WHERE business_id = ? AND role != 'user' AND created_at IS NOT NULL
                ORDER BY id DESC LIMIT ?""",
            (business_id, int(limit))).fetchall()
    hours = []
    for r in rows:
        raw = str(r["created_at"] or "")
        if len(raw) >= 13 and raw[10] == " " and raw[11:13].isdigit():
            hours.append(int(raw[11:13]))
    return hours


# ---------- ФИНАНСЫ (модуль AI-директор) ----------

# Из какого документа выросла операция. Не украшение: по нему видно, чем
# подтверждена цифра, и в разборе «покажи все зарплаты» это первый фильтр.
DOC_TYPES = {
    "receipt":  "Чек",
    "bank":     "Банковская операция",
    "invoice":  "Счёт",
    "waybill":  "Накладная",
    "salary":   "Зарплата",
    "refund":   "Возврат",
    "income":   "Документ о доходе",
    "manual":   "Внесено вручную",
    "import":   "Из выписки",
}

FINANCE_EXTRA = ("op_date", "counterparty", "source", "doc_type", "confidence",
                 "employee_id", "supplier_id", "client_id", "order_id", "external_id")


def add_finance_entry(business_id, kind, category, amount, note=None, **extra):
    """
    Записать доход или расход. kind = 'income' | 'expense'. Возвращает id.

    Дополнительные поля (дата операции, контрагент, источник, сотрудник,
    поставщик, клиент, заявка) приходят именованными — так одна и та же дверь
    годится и для ручного ввода, и для подтверждённого разбора, и для выписки.
    """
    cols = ["business_id", "kind", "category", "amount", "note"]
    vals = [business_id, kind, category, int(amount or 0), note]
    for key in FINANCE_EXTRA:
        if extra.get(key) not in (None, ""):
            cols.append(key)
            vals.append(extra[key])
    ph = ", ".join("?" * len(cols))
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO finance_entries ({', '.join(cols)}) VALUES ({ph})", tuple(vals)
        )
        entry_id = cur.lastrowid
    log_event(business_id, "finance",
              ("Доход" if kind == "income" else "Расход") + f": {category}",
              f"{int(amount or 0)} ₽" + (f" · {note}" if note else ""))
    return entry_id


def _finance_join(where_extra="", args=()):
    """
    Операции вместе с именами тех, с кем они были.

    Имена берём join'ом, а не копией в самой операции: сотрудник переименован —
    и во всех прошлых выплатах он тоже переименован, потому что это один и тот
    же человек, а не строка, записанная когда-то.
    """
    return (
        """SELECT f.*,
                  emp.title  AS employee_name,
                  sup.title  AS supplier_name,
                  cl.name    AS client_name,
                  o.text     AS order_text
             FROM finance_entries f
             LEFT JOIN memory_facts emp ON emp.id = f.employee_id AND emp.business_id = f.business_id
             LEFT JOIN memory_facts sup ON sup.id = f.supplier_id AND sup.business_id = f.business_id
             LEFT JOIN clients      cl  ON cl.id  = f.client_id   AND cl.business_id  = f.business_id
             LEFT JOIN orders       o   ON o.id   = f.order_id    AND o.business_id   = f.business_id
            WHERE f.business_id = ? """ + where_extra)


def finance_rows(business_id, limit=100, offset=0, kind=None, doc_type=None):
    """Лента операций для экрана финансов — с людьми, а не с одними цифрами."""
    sql = _finance_join()
    args = [business_id]
    if kind in ("income", "expense"):
        sql += " AND f.kind = ?"
        args.append(kind)
    if doc_type:
        sql += " AND f.doc_type = ?"
        args.append(doc_type)
    sql += " ORDER BY f.id DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def finance_row(entry_id, business_id):
    with _connect() as conn:
        row = conn.execute(_finance_join(" AND f.id = ?"),
                           (business_id, entry_id)).fetchone()
    return dict(row) if row else None


def get_finance_entry(entry_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM finance_entries WHERE id = ? AND business_id = ?",
            (entry_id, business_id),
        ).fetchone()
    return dict(row) if row else None


def update_finance_entry(entry_id, business_id, **fields):
    """Правка операции. Меняем только разрешённые поля."""
    allowed = {"kind", "category", "amount", "note", "op_date", "counterparty",
               "source", "doc_type", "employee_id", "supplier_id", "client_id",
               "order_id"}
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
    Сводка по деньгам: выручка, расходы, прибыль, маржа и разбивка.

    Два правила, которые нельзя нарушать ни при каких данных:
        прибыль = выручка − расходы
        маржа   = прибыль / выручка
    Маржа при нулевой выручке НЕ равна нулю — её просто нет, и говорить
    «0%» там, где делить не на что, значит соврать. В таком случае None.
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
        refunds = conn.execute(
            """SELECT COALESCE(SUM(amount),0) AS s FROM finance_entries
                WHERE business_id = ? AND doc_type = 'refund'""",
            (business_id,),
        ).fetchone()["s"]
        salary = conn.execute(
            """SELECT COALESCE(SUM(amount),0) AS s FROM finance_entries
                WHERE business_id = ? AND doc_type = 'salary'""",
            (business_id,),
        ).fetchone()["s"]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM finance_entries WHERE business_id = ?",
            (business_id,),
        ).fetchone()["n"]
    income, expense = int(income or 0), int(expense or 0)
    profit = income - expense
    return {
        "income": income,
        "expense": expense,
        "profit": profit,
        # Проценты считаем ОДИН раз здесь, а не на каждой странице: две разные
        # формулы маржи в двух местах — это два разных ответа владельцу.
        "margin": round(profit * 100 / income, 1) if income else None,
        "refunds": int(refunds or 0),
        "salary": int(salary or 0),
        "entries": int(n or 0),
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
                   status="RECEIVED", content_hash=None, actor=None, actor_id=None,
                   meta=None):
    """Записать входящий материал. Возвращает id."""
    if status not in INBOX_STATUSES:
        status = "RECEIVED"
    if source not in INBOX_SOURCES:
        source = "web"
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO inbox_items
               (business_id, kind, title, body, filename, mime, size,
                storage_key, source, status, content_hash, actor, actor_id, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, kind, title, body, filename, mime, int(size or 0),
             storage_key, source, status, content_hash, actor,
             int(actor_id) if actor_id else None,
             _json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        item_id = cur.lastrowid
    log_event(business_id, "inbox", "Новый материал во входящих",
              (title or filename or "заметка")[:160])
    return item_id


def find_inbox_by_hash(business_id, content_hash):
    """
    Уже присылали такое? Возвращает самую раннюю запись с тем же отпечатком.

    Ищем именно первую, а не последнюю: человеку надо показать тот материал,
    из которого уже сделаны выводы, а не свежую копию. Архивированные тоже
    считаются — «я это убрал» не значит «этого не было».
    """
    if not content_hash:
        return None
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM inbox_items
                WHERE business_id = ? AND content_hash = ?
                ORDER BY id ASC LIMIT 1""",
            (business_id, content_hash)).fetchone()
        return dict(row) if row else None


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
                       created_at, updated_at, actor, actor_id, content_hash
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
                extracted, actions, engine, model, error, applied, relations, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, item_id,
             result.get("type") or "UNKNOWN",
             float(result.get("confidence") or 0),
             result.get("level") or "LOW",
             result.get("summary"),
             _json.dumps(result.get("extracted_data") or {}, ensure_ascii=False),
             _json.dumps(result.get("suggested_actions") or [], ensure_ascii=False),
             result.get("engine"), result.get("model"), result.get("error"),
             _json.dumps(result.get("applied") or [], ensure_ascii=False),
             _json.dumps(result.get("relations") or [], ensure_ascii=False),
             _json.dumps(result.get("notes") or [], ensure_ascii=False)),
        )
        return cur.lastrowid


def _result_row(row):
    """Строка таблицы → тот же вид, в каком разбор живёт в коде и в API."""
    if not row:
        return None
    d = dict(row)
    for src, dst in (("extracted", "extracted_data"), ("actions", "suggested_actions"),
                     ("applied", "applied"), ("relations", "relations"),
                     ("notes", "notes")):
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


def last_import(business_id):
    """
    Последняя загрузка выписки или таблицы.

    Для источников, которые приходят файлом, это и есть «последняя
    синхронизация»: другого способа получить оттуда данные у нас нет, и
    показывать пустое поле было бы неправдой — загрузка ведь была.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM finance_imports WHERE business_id = ?
                ORDER BY id DESC LIMIT 1""", (business_id,)).fetchone()
    return dict(row) if row else None


def last_message_at(business_id):
    """Когда бот в последний раз получал сообщение клиента."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT MAX(created_at) AS t FROM messages
                WHERE business_id = ? AND role = 'user'""", (business_id,)).fetchone()
    return row["t"] if row else None


def count_client_messages_all(business_id):
    """Сколько всего обращений пришло — счётчик работы канала."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE business_id = ? AND role = 'user'",
            (business_id,)).fetchone()
    return int((row["n"] if row else 0) or 0)


def list_connections(business_id):
    """Все подключения бизнеса. Секреты НЕ отдаём — только статус и метаданные."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM connections WHERE business_id = ? ORDER BY provider",
            (business_id,),
        ).fetchall()
    return [_connection_row(r) for r in rows]


def get_connection(business_id, provider, with_secrets=False):
    """Одно подключение. with_secrets=True — только для самой синхронизации."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE business_id = ? AND provider = ?",
            (business_id, provider),
        ).fetchone()
    if not row:
        return None
    return _connection_row(row, with_secrets)


def _connection_row(row, with_secrets=False):
    """Строка подключения наружу: JSON разложен, секреты убраны."""
    d = dict(row)
    for key, empty in (("meta", {}), ("permissions", []), ("config", {})):
        try:
            d[key] = _json.loads(d.get(key) or ("[]" if empty == [] else "{}"))
        except (ValueError, TypeError):
            d[key] = empty
    if not with_secrets:
        d.pop("credentials", None)
    return d


def save_connection(business_id, provider, credentials_blob=None, meta=None,
                    status="connected", permissions=None, config=None):
    """
    Создать или обновить подключение. credentials_blob уже зашифрован (secretbox).

    permissions и config — открытая часть паспорта: что разрешено и как
    настроено. Их видно владельцу целиком, поэтому секретам здесь не место.
    """
    meta_json = _json.dumps(meta or {}, ensure_ascii=False)
    perm_json = _json.dumps(permissions or [], ensure_ascii=False)
    conf_json = _json.dumps(config or {}, ensure_ascii=False)
    exists = get_connection(business_id, provider)
    with _connect() as conn:
        if exists:
            if credentials_blob is None:      # секреты не меняли — не затираем
                conn.execute(
                    "UPDATE connections SET meta = ?, status = ?, last_error = NULL, "
                    "permissions = ?, config = ? WHERE business_id = ? AND provider = ?",
                    (meta_json, status, perm_json, conf_json, business_id, provider))
            else:
                conn.execute(
                    "UPDATE connections SET credentials = ?, meta = ?, status = ?, "
                    "last_error = NULL, permissions = ?, config = ? "
                    "WHERE business_id = ? AND provider = ?",
                    (credentials_blob, meta_json, status, perm_json, conf_json,
                     business_id, provider))
        else:
            # Дата подключения ставится один раз: это факт из прошлого, и
            # переподключение его не отменяет.
            conn.execute(
                "INSERT INTO connections (business_id, provider, credentials, meta, status, "
                "connected_at, permissions, config) VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?)",
                (business_id, provider, credentials_blob, meta_json, status,
                 perm_json, conf_json))
    if not exists:
        log_event(business_id, "integration", "Подключён источник: " + provider,
                  "VELOR начнёт забирать оттуда данные автоматически")
    return get_connection(business_id, provider)


def mark_connection_synced(business_id, provider, added=0, cursor=None, error=None,
                           needs_auth=False):
    """
    Записать итог синхронизации: когда, сколько нового, была ли ошибка.

    needs_auth — сервис отверг ключ. Это не поломка на нашей стороне, а
    просьба переподключиться, и статус для неё отдельный: владелец должен
    видеть разницу между «сервис лежит» и «нужен новый доступ».
    """
    with _connect() as conn:
        if error:
            conn.execute(
                "UPDATE connections SET status = ?, last_error = ?, "
                "last_sync_at = datetime('now') WHERE business_id = ? AND provider = ?",
                ("requires_auth" if needs_auth else "error",
                 str(error)[:400], business_id, provider))
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


# ---------- ЖУРНАЛ ДЕЙСТВИЙ ----------
#
# Здесь только хранение. Что VELOR вправе делать сам, что — спросив, и что
# значит каждое действие, живёт в actions.py.

AC_PROPOSED = "proposed"     # VELOR предложил, ждёт решения владельца
AC_APPROVED = "approved"     # владелец разрешил именно это
AC_RUNNING = "running"       # захвачено исполнителем; чужой процесс мимо не пройдёт
AC_SUCCEEDED = "succeeded"   # выполнено и подтверждено
AC_FAILED = "failed"         # пробовали, не вышло
AC_BLOCKED = "blocked"       # не дали выполнить, и написано почему
AC_CANCELLED = "cancelled"   # владелец отклонил или повод исчез
AC_STALE = "stale"           # пока ждало решения, обстоятельства изменились

ACTION_STATUSES = (AC_PROPOSED, AC_APPROVED, AC_RUNNING, AC_SUCCEEDED,
                   AC_FAILED, AC_BLOCKED, AC_CANCELLED, AC_STALE)
# Живое — то, что ещё может выполниться.
ACTION_LIVE = (AC_PROPOSED, AC_APPROVED, AC_RUNNING)
# Ждёт человека. Ровно это и есть очередь подтверждений — отдельного списка,
# который мог бы с ней разойтись, не существует.
ACTION_WAITING = (AC_PROPOSED,)

ACTION_FIELDS = ("actor", "actor_id", "mode", "status", "channel", "target_type",
                 "target_id", "reason", "based_on", "payload", "before", "after",
                 "result", "error", "dedupe_key", "expires_at", "decided_at",
                 "done_at")

ACTION_JSON = ("based_on", "payload", "before", "after")


def _action_row(row):
    d = dict(row)
    for key in ACTION_JSON:
        raw = d.get(key)
        try:
            d[key] = _json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            d[key] = {}
    d["target_id"] = int(d["target_id"]) if d.get("target_id") else None
    return d


def add_action(business_id, action, *, actor="velor", actor_id=None, mode=None,
               status=AC_PROPOSED, channel=None, target_type=None, target_id=None,
               reason=None, based_on=None, payload=None, before=None, after=None,
               result=None, error=None, dedupe_key=None, expires_at=None):
    """Записать действие. Возвращает id."""
    if not business_id:
        raise ValueError("add_action требует business_id (защита арендаторов)")
    if status not in ACTION_STATUSES:
        status = AC_PROPOSED
    stamp = now()
    done = stamp if status in (AC_SUCCEEDED, AC_FAILED, AC_BLOCKED) else None
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO actions (business_id, action, actor, actor_id, mode,
                                    status, channel, target_type, target_id,
                                    reason, based_on, payload, before, after,
                                    result, error, dedupe_key, expires_at,
                                    updated_at, done_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (business_id, str(action), actor or "velor",
             int(actor_id) if actor_id else None, mode or None, status,
             channel or None, target_type or None,
             int(target_id) if target_id else None,
             (reason or "")[:400] or None,
             _json.dumps(based_on or {}, ensure_ascii=False),
             _json.dumps(payload or {}, ensure_ascii=False),
             _json.dumps(before or {}, ensure_ascii=False),
             _json.dumps(after or {}, ensure_ascii=False),
             (result or "")[:400] or None, (error or "")[:400] or None,
             (dedupe_key or "")[:200] or None, expires_at or None, stamp, done),
        )
        return cur.lastrowid


def get_action(action_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM actions WHERE id = ? AND business_id = ?",
            (int(action_id), business_id)).fetchone()
    return _action_row(row) if row else None


def list_actions(business_id, *, action=None, status=None, live=False,
                 waiting=False, target_type=None, target_id=None,
                 since=None, limit=100, offset=0):
    """Действия бизнеса, новые сверху. status — строка или набор строк."""
    where, args = ["business_id = ?"], [business_id]
    if action:
        if isinstance(action, (list, tuple, set)):
            where.append("action IN (%s)" % ",".join("?" * len(action)))
            args += list(action)
        else:
            where.append("action = ?")
            args.append(action)
    if waiting:
        where.append("status IN (%s)" % ",".join("?" * len(ACTION_WAITING)))
        args += list(ACTION_WAITING)
    elif live:
        where.append("status IN (%s)" % ",".join("?" * len(ACTION_LIVE)))
        args += list(ACTION_LIVE)
    elif isinstance(status, (list, tuple, set)):
        vals = [v for v in status if v in ACTION_STATUSES]
        if vals:
            where.append("status IN (%s)" % ",".join("?" * len(vals)))
            args += vals
    elif status in ACTION_STATUSES:
        where.append("status = ?")
        args.append(status)
    if target_type:
        where.append("target_type = ?")
        args.append(target_type)
    if target_id:
        where.append("target_id = ?")
        args.append(int(target_id))
    if since:
        where.append("created_at >= ?")
        args.append(since)
    args += [int(limit), max(0, int(offset or 0))]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions WHERE " + " AND ".join(where)
            + " ORDER BY id DESC LIMIT ? OFFSET ?", args).fetchall()
    return [_action_row(r) for r in rows]


def update_action(action_id, business_id, **fields):
    """Изменить запись действия — только в своём бизнесе."""
    if not business_id:
        raise ValueError("update_action требует business_id (защита арендаторов)")
    sets = {}
    for key, val in fields.items():
        if key not in ACTION_FIELDS:
            raise ValueError("Нельзя менять поле действия: %s" % key)
        if key == "status" and val not in ACTION_STATUSES:
            raise ValueError("Неизвестное состояние действия: %s" % val)
        if key in ACTION_JSON and isinstance(val, (dict, list)):
            sets[key] = _json.dumps(val, ensure_ascii=False)
        else:
            sets[key] = val
    if not sets:
        return
    sets["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        conn.execute(f"UPDATE actions SET {cols} WHERE id = ? AND business_id = ?",
                     list(sets.values()) + [int(action_id), business_id])


def claim_action(action_id, business_id):
    """
    Захватить действие для выполнения. True — захват наш.

    Проверка и захват — одна операция. Разделить их значит оставить щель между
    «оно ещё не выполнялось» и «теперь выполняю», а в эту щель однажды войдёт
    второй планировщик, и одно и то же случится дважды.
    """
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE actions SET status = ?, updated_at = ?
               WHERE id = ? AND business_id = ? AND status IN (?, ?)
                 AND done_at IS NULL""",
            (AC_RUNNING, now(), int(action_id), business_id,
             AC_PROPOSED, AC_APPROVED))
        return bool(cur.rowcount)


def finish_action(action_id, business_id, status, *, result=None, error=None,
                  after=None, target_id=None):
    """Закрыть выполнение: получилось или нет. Один раз и только из running."""
    if status not in ACTION_STATUSES:
        raise ValueError("Неизвестное состояние действия: %s" % status)
    sets = ["status = ?", "result = ?", "error = ?", "updated_at = ?", "done_at = ?"]
    args = [status, (result or "")[:400] or None, (error or "")[:400] or None,
            now(), now()]
    if after is not None:
        sets.append("after = ?")
        args.append(_json.dumps(after, ensure_ascii=False))
    if target_id:
        sets.append("target_id = ?")
        args.append(int(target_id))
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE actions SET " + ", ".join(sets)
            + " WHERE id = ? AND business_id = ? AND status = ?",
            args + [int(action_id), business_id, AC_RUNNING])
        return bool(cur.rowcount)


def find_live_action(business_id, dedupe_key):
    """
    Живое действие с тем же поводом. Одна причина — одно предложение.

    Отдаём САМОЕ РАННЕЕ, а не последнее. Разница видна только в споре двух
    одновременных обходов, но именно там она и решает: спор должен разрешаться
    одинаково у обоих, а «кто записался первым» — единственный признак, который
    оба видят одинаково.
    """
    key = (dedupe_key or "").strip()
    if not key:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM actions WHERE business_id = ? AND dedupe_key = ?"
            " AND status IN (%s) ORDER BY id ASC LIMIT 1" % ",".join("?" * len(ACTION_LIVE)),
            [business_id, key[:200]] + list(ACTION_LIVE)).fetchone()
    return _action_row(row) if row else None


def count_actions(business_id, *, status=None, waiting=False, since=None):
    """Сколько действий в таком состоянии. Для сводок и цифры на кнопке."""
    where, args = ["business_id = ?"], [business_id]
    if waiting:
        where.append("status IN (%s)" % ",".join("?" * len(ACTION_WAITING)))
        args += list(ACTION_WAITING)
    elif isinstance(status, (list, tuple, set)):
        where.append("status IN (%s)" % ",".join("?" * len(status)))
        args += list(status)
    elif status:
        where.append("status = ?")
        args.append(status)
    if since:
        where.append("created_at >= ?")
        args.append(since)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM actions WHERE " + " AND ".join(where),
            args).fetchone()
    return int(row["n"] if row else 0)


# ---------- НАХОДКИ VELOR ----------
#
# Здесь только хранение. Что считается находкой, как она обнаруживается и что
# по ней предлагается сделать, живёт в initiatives.py.

IN_NEW = "new"                   # обнаружено, владелец ещё не видел
IN_SEEN = "seen"                 # показано на экране
IN_ACKNOWLEDGED = "acknowledged" # владелец сказал «понял»
IN_ACTED = "acted"               # из находки выросло действие
IN_DISMISSED = "dismissed"       # «не интересно»
IN_RESOLVED = "resolved"         # проблема исчезла сама или после действия
IN_EXPIRED = "expired"           # повод устарел, никто не ответил

INITIATIVE_STATUSES = (IN_NEW, IN_SEEN, IN_ACKNOWLEDGED, IN_ACTED,
                       IN_DISMISSED, IN_RESOLVED, IN_EXPIRED)

# Живое — то, что ещё висит перед владельцем или ждёт результата. Прочитанное
# и подтверждённое остаются живыми намеренно: «я это видел» не означает «этого
# больше нет», и повторно сообщать о той же проблеме всё равно нельзя.
INITIATIVE_LIVE = (IN_NEW, IN_SEEN, IN_ACKNOWLEDGED, IN_ACTED)
# Ждёт глаз владельца — ровно это и есть счётчик на главной.
INITIATIVE_UNSEEN = (IN_NEW,)
# Закрытые: повод больше не действует.
INITIATIVE_CLOSED = (IN_DISMISSED, IN_RESOLVED, IN_EXPIRED)

INITIATIVE_FIELDS = ("level", "priority", "confidence", "title", "summary",
                     "why", "hypothesis", "evidence", "impact", "impact_note",
                     "action", "action_note", "href", "status", "outcome",
                     "fingerprint", "actions_ids", "last_seen_at", "decided_at",
                     "closed_at", "expires_at")

INITIATIVE_JSON = {"evidence": dict, "actions_ids": list}


def _initiative_row(row):
    d = dict(row)
    for key, empty in INITIATIVE_JSON.items():
        raw = d.get(key)
        try:
            d[key] = _json.loads(raw) if raw else empty()
        except (ValueError, TypeError):
            d[key] = empty()
    d["impact"] = int(d["impact"]) if d.get("impact") is not None else None
    return d


def add_initiative(business_id, kind, *, title, level="act", priority="medium",
                   confidence=None, summary=None, why=None, hypothesis=None,
                   evidence=None, impact=None, impact_note=None, action=None,
                   action_note=None, href=None, status=IN_NEW, fingerprint=None,
                   expires_at=None):
    """Записать находку. Возвращает id."""
    if not business_id:
        raise ValueError("add_initiative требует business_id (защита арендаторов)")
    if status not in INITIATIVE_STATUSES:
        status = IN_NEW
    stamp = now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO initiatives (business_id, type, level, priority,
                                        confidence, title, summary, why,
                                        hypothesis, evidence, impact, impact_note,
                                        action, action_note, href, status,
                                        fingerprint, actions_ids, updated_at,
                                        last_seen_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (business_id, str(kind), level, priority, confidence or None,
             str(title)[:200], (summary or "")[:600] or None,
             (why or "")[:600] or None, (hypothesis or "")[:600] or None,
             _json.dumps(evidence or {}, ensure_ascii=False),
             int(impact) if impact is not None else None,
             (impact_note or "")[:200] or None, action or None,
             (action_note or "")[:200] or None, href or None, status,
             (fingerprint or "")[:200] or None, _json.dumps([]),
             stamp, stamp, expires_at or None),
        )
        return cur.lastrowid


def get_initiative(initiative_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM initiatives WHERE id = ? AND business_id = ?",
            (int(initiative_id), business_id)).fetchone()
    return _initiative_row(row) if row else None


def list_initiatives(business_id, *, kind=None, status=None, live=False,
                     level=None, limit=50, offset=0):
    """Находки бизнеса, новые сверху. status — строка или набор строк."""
    where, args = ["business_id = ?"], [business_id]
    if kind:
        where.append("type = ?")
        args.append(kind)
    if live:
        where.append("status IN (%s)" % ",".join("?" * len(INITIATIVE_LIVE)))
        args += list(INITIATIVE_LIVE)
    elif isinstance(status, (list, tuple, set)):
        vals = [v for v in status if v in INITIATIVE_STATUSES]
        if vals:
            where.append("status IN (%s)" % ",".join("?" * len(vals)))
            args += vals
    elif status in INITIATIVE_STATUSES:
        where.append("status = ?")
        args.append(status)
    if level:
        where.append("level = ?")
        args.append(level)
    args += [int(limit), max(0, int(offset or 0))]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM initiatives WHERE " + " AND ".join(where)
            + " ORDER BY id DESC LIMIT ? OFFSET ?", args).fetchall()
    return [_initiative_row(r) for r in rows]


def update_initiative(initiative_id, business_id, **fields):
    """Изменить находку — только в своём бизнесе."""
    if not business_id:
        raise ValueError("update_initiative требует business_id (защита арендаторов)")
    sets = {}
    for key, val in fields.items():
        if key not in INITIATIVE_FIELDS:
            raise ValueError("Нельзя менять поле находки: %s" % key)
        if key == "status" and val not in INITIATIVE_STATUSES:
            raise ValueError("Неизвестное состояние находки: %s" % val)
        if key in INITIATIVE_JSON and isinstance(val, (dict, list)):
            sets[key] = _json.dumps(val, ensure_ascii=False)
        else:
            sets[key] = val
    if not sets:
        return None
    sets["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        conn.execute(
            f"UPDATE initiatives SET {cols} WHERE id = ? AND business_id = ?",
            list(sets.values()) + [int(initiative_id), business_id])
    return get_initiative(initiative_id, business_id)


def find_initiative(business_id, fingerprint, *, statuses=None):
    """
    Есть ли уже находка с таким отпечатком. Самая свежая — ответ на два разных
    вопроса сразу: «не показываем ли мы это прямо сейчас» и «не отмахнулся ли
    владелец от этого недавно».
    """
    key = (fingerprint or "").strip()
    if not key:
        return None
    vals = [s for s in (statuses or INITIATIVE_STATUSES)
            if s in INITIATIVE_STATUSES]
    if not vals:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM initiatives WHERE business_id = ? AND fingerprint = ?"
            " AND status IN (%s) ORDER BY id DESC LIMIT 1" % ",".join("?" * len(vals)),
            [business_id, key[:200]] + list(vals)).fetchone()
    return _initiative_row(row) if row else None


def count_initiatives(business_id, *, status=None, live=False, level=None):
    """Сколько находок в таком состоянии. Для счётчика на главной."""
    where, args = ["business_id = ?"], [business_id]
    if live:
        where.append("status IN (%s)" % ",".join("?" * len(INITIATIVE_LIVE)))
        args += list(INITIATIVE_LIVE)
    elif isinstance(status, (list, tuple, set)):
        where.append("status IN (%s)" % ",".join("?" * len(status)))
        args += list(status)
    elif status:
        where.append("status = ?")
        args.append(status)
    if level:
        where.append("level = ?")
        args.append(level)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM initiatives WHERE " + " AND ".join(where),
            args).fetchone()
    return int(row["n"] if row else 0)


# ============================================================
#  РЕЗУЛЬТАТЫ (отчёты, документы, письма)
# ============================================================
# Правило то же, что у всего кабинета: любой запрос ограничен business_id.
# Чужой результат нельзя ни прочитать, ни скачать, ни удалить — не потому что
# его не показывает интерфейс, а потому что он не проходит через WHERE.

OUTPUT_STATUSES = ("READY", "FAILED")


def add_output(business_id, kind, *, title=None, subtitle=None, request=None,
               doc=None, sources=None, body=None, file_key=None, file_name=None,
               file_size=0, engine="rules", status="READY", error=None):
    """Записать готовый результат. Возвращает id."""
    if status not in OUTPUT_STATUSES:
        status = "READY"
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO outputs
               (business_id, kind, title, subtitle, request, doc, sources, body,
                file_key, file_name, file_size, engine, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, kind, title, subtitle,
             _json.dumps(request or {}, ensure_ascii=False),
             _json.dumps(doc or {}, ensure_ascii=False),
             _json.dumps(sources or [], ensure_ascii=False),
             body, file_key, file_name, int(file_size or 0), engine, status, error),
        )
        out_id = cur.lastrowid
    log_event(business_id, "output", "VELOR собрал результат", (title or kind)[:160])
    return out_id


def _output_row(row):
    if not row:
        return None
    d = dict(row)
    for field in ("request", "doc", "sources"):
        try:
            d[field] = _json.loads(d.get(field) or ("[]" if field == "sources" else "{}"))
        except (ValueError, TypeError):
            d[field] = [] if field == "sources" else {}
    return d


def list_outputs(business_id, kind=None, limit=30, offset=0):
    """Результаты бизнеса, новые сверху. Без doc — список не должен тащить
    целиком каждый отчёт: на экране истории видно название, дату и файл."""
    where, params = "business_id = ?", [business_id]
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT id, business_id, kind, title, subtitle, request, sources,
                       file_key, file_name, file_size, engine, status, error, created_at
                  FROM outputs WHERE {where}
                 ORDER BY id DESC LIMIT ? OFFSET ?""",
            (*params, max(1, min(int(limit), 200)), max(0, int(offset))),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for field in ("request", "sources"):
            try:
                d[field] = _json.loads(d.get(field) or ("[]" if field == "sources" else "{}"))
            except (ValueError, TypeError):
                d[field] = [] if field == "sources" else {}
        out.append(d)
    return out


def count_outputs(business_id, kind=None):
    where, params = "business_id = ?", [business_id]
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    with _connect() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) AS n FROM outputs WHERE {where}", tuple(params)
        ).fetchone()["n"] or 0)


def get_output(output_id, business_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM outputs WHERE id = ? AND business_id = ?",
            (int(output_id), business_id)).fetchone()
    return _output_row(row)


def delete_output(output_id, business_id):
    """Удалить запись о результате. Возвращает ключ файла, чтобы вызвавший
    убрал и сам файл: хранилище про базу не знает и осиротевший файл иначе
    остался бы занимать место навсегда."""
    row = get_output(output_id, business_id)
    if not row:
        return None
    with _connect() as conn:
        conn.execute("DELETE FROM outputs WHERE id = ? AND business_id = ?",
                     (int(output_id), business_id))
    return row.get("file_key")
