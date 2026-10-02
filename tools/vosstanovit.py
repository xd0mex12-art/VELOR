# -*- coding: utf-8 -*-
"""
Восстановление базы VELOR из копии, снятой tools/kopiya.py.

Порядок, если боевая база потеряна:
  1. Создать чистый проект Supabase (или новую базу) и вписать её адрес в
     DATABASE_URL — в .env локально либо в настройках Render.
  2. Один раз запустить сервер: init_db() создаст все таблицы нынешней схемы.
  3. Запустить этот скрипт:  python tools/vosstanovit.py <путь к velor-….zip>

Почему именно так, а не «залить дамп целиком». Копия хранит ДАННЫЕ, а не
структуру: структура у нас живая, её создаёт код и меняют миграции. Восстановив
старую структуру, мы получили бы базу, которую нынешний код не понимает. А
данные в чистую свежую схему ложатся ровно и предсказуемо.

Скрипт не трогает таблицы, в которых уже что-то есть: ошибиться базой и затереть
живые данные — беда страшнее той, от которой мы спасаемся. Если нужно именно
перезаписать, сначала очистите таблицу руками, осознанно.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()


def строка_подключения() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit("Не задан DATABASE_URL (Postgres) в .env — некуда восстанавливать.")
    return url


def из_json(значение):
    if isinstance(значение, dict) and "__байты__" in значение:
        return bytes.fromhex(значение["__байты__"])
    return значение


def main() -> int:
    p = argparse.ArgumentParser(description="Восстановить базу VELOR из копии")
    p.add_argument("arhiv", help="путь к файлу velor-….zip")
    p.add_argument("--da", action="store_true",
                   help="действительно писать в базу (без этого — только показать план)")
    а = p.parse_args()

    import psycopg

    with zipfile.ZipFile(а.arhiv) as zf:
        опись = json.loads(zf.read("opis.json").decode("utf-8"))
        print("Копия снята: %s, таблиц %d, строк %d\n"
              % (опись["снято"], опись["таблиц"], опись["строк_всего"]))

        with psycopg.connect(строка_подключения()) as conn:
            for сведения in опись["таблицы"]:
                имя, строк = сведения["таблица"], сведения["строк"]
                if not строк:
                    continue
                with conn.cursor() as cur:
                    cur.execute('SELECT count(*) FROM "%s"' % имя)
                    уже = cur.fetchone()[0]
                if уже:
                    print("  %-28s пропускаю: в базе уже %d строк" % (имя, уже))
                    continue
                if not а.da:
                    print("  %-28s влил бы %d строк" % (имя, строк))
                    continue

                данные = zf.read("tablitsy/%s.jsonl" % имя).decode("utf-8")
                строки = [json.loads(s) for s in данные.split("\n") if s]
                колонки = сведения["колонки"]
                места = ", ".join(["%s"] * len(колонки))
                поля = ", ".join('"%s"' % k for k in колонки)
                sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (имя, поля, места)
                with conn.cursor() as cur:
                    cur.executemany(sql, [[из_json(r.get(k)) for k in колонки] for r in строки])
                conn.commit()
                print("  %-28s влито %d строк" % (имя, len(строки)))

    if not а.da:
        print("\nЭто была примерка. Чтобы действительно записать, добавьте --da")
    else:
        print("\nГотово. Проверьте кабинет.")
        print("ВАЖНО: счётчики id в Postgres после прямой вставки надо подтянуть,")
        print("иначе новые записи столкнутся со старыми. Выполните в SQL-редакторе")
        print("Supabase по одному запросу на таблицу, например:")
        print("  SELECT setval(pg_get_serial_sequence('businesses','id'),")
        print("                (SELECT max(id) FROM businesses));")
    return 0


if __name__ == "__main__":
    sys.exit(main())
