# -*- coding: utf-8 -*-
"""
Прогон всего набора тестов VELOR одной командой:

    python tests/run_all.py            — всё
    python tests/run_all.py inbox one  — только файлы, чьё имя содержит эти слова

Почему отдельным процессом на файл, а не импортом. Каждый тест поднимает свою
базу во временной папке и свой экземпляр приложения; в одном процессе они
поделили бы уже импортированные модули и настройки, и падение одного
превращалось бы в загадочное падение следующего.

Итог считается по строке «ИТОГО: успешно N, провалено M», а не только по коду
возврата: два файла исторически завершаются без sys.exit, и молчаливый ноль от
них выглядел бы как успех даже при провале проверки.
"""
import io
import os
import pathlib
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = pathlib.Path(__file__).resolve().parent
# Две формулировки итога сложились исторически, и переписывать их в пятнадцати
# файлах ради единообразия — риск ради косметики. Понимаем обе.
TOTAL = re.compile(r"ИТОГО:\s*(?:успешно\s*(\d+),\s*провалено\s*(\d+)"
                   r"|(\d+)\s*зелёных,\s*(\d+)\s*упавших)")


def files(argv):
    names = sorted(p for p in HERE.glob("t_*.py"))
    if argv:
        names = [p for p in names if any(a.lower() in p.name.lower() for a in argv)]
    return names


def main():
    picked = files(sys.argv[1:])
    if not picked:
        print("Ни одного файла не подошло."); return 1

    ok = fail = 0
    broken, quiet = [], []
    for path in picked:
        r = subprocess.run([sys.executable, str(path)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        m = TOTAL.search(out)
        if m:
            good = int(m.group(1) or m.group(3))
            bad = int(m.group(2) or m.group(4))
            ok += good; fail += bad
            mark = "OK  " if not bad and not r.returncode else "FAIL"
            print(f"  {mark} {path.name:22} успешно {good:4}, провалено {bad}")
            if bad:
                for line in out.splitlines():
                    if line.lstrip().startswith("FAIL"):
                        print("        " + line.strip())
        elif r.returncode == 0:
            # Файл без счётчика (например, проверка разметки) — считаем за одну.
            ok += 1; quiet.append(path.name)
            print(f"  OK   {path.name:22} без счётчика, код возврата 0")
        else:
            broken.append(path.name)
            print(f"  СБОЙ {path.name:22} код возврата {r.returncode}")
            print("        " + "\n        ".join(out.strip().splitlines()[-6:]))

    print(f"\n=== ВСЕГО: успешно {ok}, провалено {fail}"
          + (f", не запустилось {len(broken)}" if broken else "") + " ===")
    return 1 if (fail or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
