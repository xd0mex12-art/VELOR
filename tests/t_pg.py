# -*- coding: utf-8 -*-
"""
БОЕВАЯ БАЗА ГОВОРИТ НА ДРУГОМ ДИАЛЕКТЕ.

Локально VELOR работает на SQLite, на сервере — на Postgres. Диалекты похожи,
и разницу закрывает переводчик `database._translate`. Пока его никто не
проверял, продукт месяц был сломан в бою и цел дома: главная страница
отвечала «Сервер не отвечает», потому что запрос уходил в Postgres с вызовом
`date(COALESCE(...))`, который прежняя регулярка не узнавала.

Поэтому проверка не про функции, а про продукт: **ни один запрос во всём
проекте не должен уехать на боевой сервер на языке, которого тот не понимает.**
Тест собирает все SQL-строки из исходников, прогоняет через переводчик и
смотрит, не осталось ли в них слов, которых в Postgres нет.

Тест не требует ни базы, ни сети: переводчик — чистая функция над строкой.
"""
import ast
import io
import os
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = ""      # переводчик берём как функцию, база не нужна

import database                      # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("  FAIL:", name, ("| " + str(extra)[:220]) if extra else "")


# ---------- что Postgres НЕ поймёт ----------
# Каждое правило — то, на чём продукт уже падал или упал бы следующим.
FORBIDDEN = [
    ("julianday", re.compile(r"\bjulianday\s*\(", re.I)),
    ("strftime", re.compile(r"\bstrftime\s*\(", re.I)),
    ("date(...)", re.compile(r"(?<![A-Za-z_.])date\s*\(", re.I)),
    ("datetime(...)", re.compile(r"(?<![A-Za-z_.])datetime\s*\(", re.I)),
    ("CURRENT_TIMESTAMP", re.compile(r"\bCURRENT_TIMESTAMP\b", re.I)),
    ("INSERT OR REPLACE", re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I)),
    ("INSERT OR IGNORE", re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.I)),
    ("AUTOINCREMENT", re.compile(r"\bAUTOINCREMENT\b", re.I)),
    ("PRAGMA", re.compile(r"\bPRAGMA\b", re.I)),
]

# Строка считается запросом, если в ней есть слова, которые вне SQL почти не
# встречаются. Список широкий намеренно: пропущенный запрос — это дыра,
# лишняя строка — просто ещё одна успешная проверка.
SQL_WORDS = re.compile(
    r"\b(SELECT|INSERT\s+INTO|INSERT\s+OR|UPDATE\s+\w+\s+SET|DELETE\s+FROM|"
    r"CREATE\s+TABLE|CREATE\s+INDEX|ALTER\s+TABLE|FROM\s+\w+|WHERE\s+|"
    r"GROUP\s+BY|ORDER\s+BY|VALUES\s*\(|PRAGMA)\b", re.I)


def sql_literals(path):
    """Все строковые литералы файла, похожие на SQL (включая куски f-строк)."""
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            # f-строка: подставленные значения нам не важны, важен каркас.
            parts = [v.value for v in node.values
                     if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            if parts:
                found.append((node.lineno, " ".join(parts)))
    return [(n, t) for n, t in found if SQL_WORDS.search(t)]


# Тесты и папка agents работают со своими локальными файлами SQLite и на
# боевой сервер не ходят — их диалект никому не мешает.
FILES = sorted(ROOT.glob("*.py")) + sorted((ROOT / "connectors").glob("*.py"))

# Единственное исключение. PRAGMA стоит внутри ветки _connect(), которая
# работает только с SQLite: до Postgres этот оператор не доходит физически —
# выше в той же функции стоит `if _PG: return`. Исключение названо поимённо,
# чтобы новое такое место пришлось добавлять сюда осознанно, а не случайно.
ALLOWED = {("database.py", "PRAGMA foreign_keys")}


def allowed(fname, sql):
    return any(f == fname and frag in sql for f, frag in ALLOWED)


print("== ЗАПРОСЫ ПРОЕКТА НА ЯЗЫКЕ БОЕВОЙ БАЗЫ ==")
total = 0
offenders = []
for path in FILES:
    for lineno, sql in sql_literals(path):
        total += 1
        out = database._translate(sql)
        if allowed(path.name, sql):
            continue
        for label, rx in FORBIDDEN:
            if rx.search(out):
                offenders.append("%s:%d — %s | %s" % (path.name, lineno, label,
                                                      " ".join(sql.split())[:120]))

check("во всём проекте нет запросов, непонятных Postgres",
      not offenders, "\n       " + "\n       ".join(offenders[:8]))
check("запросы вообще нашлись (иначе проверка ничего не проверяет)", total > 200, total)
print("   проверено запросов:", total)

# ---------- разбор со счётчиком скобок ----------
print("\n== ВЛОЖЕННЫЕ СКОБКИ ==")
# Именно на этом продукт и падал: регулярка видела только простой столбец.
t = database._translate
check("date(COALESCE(a, b)) переводится",
      "substr((COALESCE(a, b))::text, 1, 10)" in t("SELECT date(COALESCE(a, b)) FROM x"))
check("date(?) переводится",
      "substr((?)::text, 1, 10)".replace("?", "%s") in t("SELECT 1 WHERE date(created_at) >= date(?)"))
check("вложенность любой глубины",
      "date(" not in t("SELECT date(COALESCE(a, MAX(b, date(c)))) FROM x"))
check("datetime('now') не пострадал от правила для date(...)",
      t("UPDATE t SET a = datetime('now')").count("to_char") == 1)
check("op_date — столбец, а не вызов: не трогаем",
      "op_date" in t("SELECT op_date FROM finance_entries WHERE business_id = ?"))
check("julianday даёт разницу в днях",
      "::date" in t("SELECT CAST(julianday('now') - julianday(?) AS INTEGER)"))
check("strftime по месяцу — срез в 7 символов",
      "1, 7)" in t("SELECT strftime('%Y-%m', created_at) FROM x"))
check("незнакомый формат strftime не выдумываем",
      "strftime" in t("SELECT strftime('%W', created_at) FROM x"))

# ---------- то, ради чего всё затевалось ----------
print("\n== ЗАПРОСЫ, СЛОМАВШИЕ ГЛАВНУЮ СТРАНИЦУ ==")
# Дословно из database.sales_today — с них начиналась ошибка
# «operator does not exist: date = text» в боевых логах.
for sql in (
    "SELECT COUNT(*) FROM leads WHERE business_id = ? AND status = 'won' "
    "AND date(COALESCE(converted_at, created_at)) = date('now')",
    "SELECT COALESCE(SUM(value), 0) FROM leads WHERE business_id = ? "
    "AND date(COALESCE(lost_at, created_at)) >= date('now', ?)",
):
    out = t(sql)
    check("запрос главной понятен Postgres", not re.search(r"(?<![A-Za-z_.])date\s*\(", out), out[:120])

# ---------- поведение SQLite не изменилось ----------
print("\n== ДОМАШНЯЯ БАЗА РАБОТАЕТ КАК РАБОТАЛА ==")
# Переводчик включается только на Postgres. Проверяем, что обычный путь цел:
# запрос с date() и strftime() отвечает на живой SQLite.
import sqlite3                                    # noqa: E402
conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE t (a TEXT, b TEXT)")
conn.execute("INSERT INTO t VALUES (datetime('now'), NULL)")
row = conn.execute("SELECT date(COALESCE(b, a)) AS d, strftime('%Y-%m', a) AS m FROM t").fetchone()
check("SQLite по-прежнему понимает date(COALESCE(...))", bool(row and row[0]))
check("SQLite по-прежнему понимает strftime", bool(row and row[1]))
conn.close()

print("\nИТОГО: успешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
