# -*- coding: utf-8 -*-
"""
Резервная копия боевой базы — с вашего компьютера, без GitHub и без pg_dump.

Зачем ещё один скрипт. docker/backup.py копирует SQLite и написан под
Docker-развёртывание, которого у нас нет. Боевая база — Postgres в Supabase,
и копий её не делает никто: бесплатный тариф точку восстановления не хранит.
Задание в GitHub Actions мы завели, но для него нужен вход в тот аккаунт, под
которым лежит репозиторий. Этот скрипт — путь, не требующий ни того, ни другого:
запускается здесь, читает строку подключения из .env и складывает копию в папку.

Что внутри копии. Все таблицы построчно, в JSON Lines, упакованные в один .zip.
Схему не сохраняем намеренно: её создаёт init_db() при первом запуске, и ровно
такую, какую ждёт нынешний код. Значит, восстановление — это чистая база плюс
данные, а не попытка оживить устаревшую структуру.

Как запустить:
    python tools/kopiya.py                 # копия боевой базы из DATABASE_URL
    python tools/kopiya.py --kuda D:\\Копии  # в другую папку

Что нужно в .env (вводите сами, в переписку не присылайте):
    DATABASE_URL=postgresql://postgres.xxxx:ПАРОЛЬ@...   # Supabase → Database → URI

Восстановление: tools/vosstanovit.py — он же объясняет порядок действий.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

# Сколько копий держим. Старые чистим сами: папка, которая растёт без предела,
# однажды кончится местом — и именно в тот день копия не снимется.
ХРАНИМ_ШТУК = 30

# Таблицы, которые копировать незачем: это кеш и журналы, они восстановятся
# сами. Данные бизнеса здесь не перечисляем никогда.
ПРОПУСКАЕМ = {"rate_limits", "sessions"}


def строка_подключения() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit(
            "Не задан DATABASE_URL в файле .env.\n"
            "Возьмите строку в Supabase: Project Settings, затем Database,\n"
            "блок Connection string, вкладка URI. Впишите её в .env строкой\n"
            "DATABASE_URL=postgresql://...\n"
            "В переписку строку не присылайте."
        )
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit("DATABASE_URL не похож на адрес Postgres: " + url[:30] + "…")
    return url


def таблицы(cur) -> list[str]:
    cur.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
         ORDER BY table_name
    """)
    return [r[0] for r in cur.fetchall()]


def в_json(значение):
    """Даты, время и деньги — в текст: JSON таких типов не знает."""
    if isinstance(значение, (datetime.date, datetime.datetime)):
        return значение.isoformat()
    if isinstance(значение, (bytes, bytearray)):
        return {"__байты__": значение.hex()}
    return str(значение)


def драйвер():
    """Драйвер Postgres. Локально его часто нет: здесь работают на SQLite."""
    try:
        import psycopg
    except ModuleNotFoundError:
        raise SystemExit(
            "Не установлен драйвер Postgres. Поставьте его один раз командой:\n"
            '    pip install "psycopg[binary]"'
        )
    return psycopg


def снять(куда: str) -> str:
    psycopg = драйвер()

    os.makedirs(куда, exist_ok=True)
    метка = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    путь = os.path.join(куда, "velor-%s.zip" % метка)

    всего_строк = 0
    опись = []
    with psycopg.connect(строка_подключения()) as conn:
        with conn.cursor() as cur, zipfile.ZipFile(путь, "w", zipfile.ZIP_DEFLATED) as zf:
            список = [t for t in таблицы(cur) if t not in ПРОПУСКАЕМ]
            if not список:
                raise SystemExit("В базе нет ни одной таблицы — проверьте строку подключения.")
            for имя in список:
                cur.execute('SELECT * FROM "%s"' % имя)
                колонки = [d.name for d in cur.description]
                куски = []
                строк = 0
                for строка in cur:
                    запись = dict(zip(колонки, строка))
                    куски.append(json.dumps(запись, ensure_ascii=False, default=в_json))
                    строк += 1
                zf.writestr("tablitsy/%s.jsonl" % имя, "\n".join(куски))
                опись.append({"таблица": имя, "строк": строк, "колонки": колонки})
                всего_строк += строк
                print("  %-28s %6d" % (имя, строк))

            zf.writestr("opis.json", json.dumps({
                "снято": datetime.datetime.now().isoformat(timespec="seconds"),
                "таблиц": len(список),
                "строк_всего": всего_строк,
                "таблицы": опись,
                "как_восстановить": "python tools/vosstanovit.py <путь к этому zip>",
            }, ensure_ascii=False, indent=2))

    размер = os.path.getsize(путь)
    print("\nКопия: %s  (%.1f КБ, строк %d)" % (путь, размер / 1024.0, всего_строк))

    # Пустая копия — худший вид копии: о ней узнают только в день беды.
    if всего_строк == 0:
        raise SystemExit("ОШИБКА: в копии ноль строк. Это не копия, удалите её и разберитесь.")
    return путь


def почистить_старые(куда: str) -> None:
    файлы = sorted(
        (f for f in os.listdir(куда) if f.startswith("velor-") and f.endswith(".zip")),
        reverse=True,
    )
    for лишний in файлы[ХРАНИМ_ШТУК:]:
        os.remove(os.path.join(куда, лишний))
        print("убрал старую копию:", лишний)


def main() -> int:
    p = argparse.ArgumentParser(description="Резервная копия базы VELOR")
    p.add_argument("--kuda", default=os.path.join(os.path.expanduser("~"), "VELOR-копии"),
                   help="папка для копий (по умолчанию ~/VELOR-копии)")
    а = p.parse_args()

    # Сначала то, что проверяется мгновенно: адрес базы и драйвер. Иначе
    # человек узнаёт о нехватке драйвера после минуты ожидания.
    строка_подключения()
    драйвер()

    print("Снимаю копию боевой базы VELOR…\n")
    снять(а.kuda)
    почистить_старые(а.kuda)
    print("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
