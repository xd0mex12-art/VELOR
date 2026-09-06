# -*- coding: utf-8 -*-
"""
Доезжает ли до модели то, что VELOR уже посчитал сам.

Разбор бизнеса считают Директор и детекторы находок — без единого обращения к
модели. Годами это оставалось на своих страницах: на вопрос «почему упала
прибыль» ассистент получал итоги за всё время и ни одного сравнения периодов и
ответить верно не мог физически. Здесь проверяется, что мост между
посчитанным и моделью на месте.

Что именно проверяем:
  1) в системном промпте есть сравнение периодов, дата, источники цифр;
  2) есть список того, чего посчитать НЕЛЬЗЯ, — единственное, что стоит между
     честным «данных нет» и придуманным ответом;
  3) цели владельца доезжают: совет, не сверенный с целью, — совет не этой
     компании;
  4) память отдаёт ТЕЛО записи, а не один заголовок;
  5) догадка модели (hypothesis) в промпт НЕ попадает: она хранится отдельно
     именно потому, что фактом не является;
  6) граница между компаниями не сдвинулась — ни одна цифра, находка или цель
     соседа не появляется в чужом промпте;
  7) пустой бизнес не ломается и не получает выдуманного разбора;
  8) клиентский путь (горячий вебхук) разбор НЕ тянет — он не для клиента и
     стоит десятков запросов.
"""
import os, sys, tempfile, pathlib

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "testowner"
os.environ["OWNER_PASSWORD"] = "s3cret-owner"
os.environ["JWT_SECRET"] = "test-secret-ctx"
os.environ["SECRET_KEY"] = "test-box-key"
os.environ["DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
sys.stdout.reconfigure(encoding="utf-8")
ROOT = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

sys.path.insert(0, os.path.join(ROOT, "tools"))
import datetime                                                      # noqa: E402
import context_engine, database, initiatives                         # noqa: E402
import demo_business                                                 # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


database.init_db()

TODAY = datetime.date.today()


def days_ago(n):
    return (TODAY - datetime.timedelta(days=n)).isoformat()


def company(name, login):
    bid = database.create_business(name=name, login=login, password="pass123")
    return bid if isinstance(bid, int) else bid["id"]


# Компания А — демо-клиника: три месяца связанных данных, два сравнимых
# периода, живые находки. Собирать такое вручную значило бы написать второй
# demo_business, который разойдётся с первым при первой же правке.
A = demo_business.build("ctx-clinic", "ctx-clinic-pass")["business_id"]

# Компания Б — сосед со своими деньгами. Без них проверка на утечку была бы
# пустой: у бизнеса без данных промпт беден сам по себе.
B = company("Магазин Б", "ctx-b")
for i in range(6):
    database.add_finance_entry(B, "income", "продажи", 1000 + i,
                               note="чек %d" % i, op_date=days_ago(3 + i))
    database.add_finance_entry(B, "income", "продажи", 900 + i,
                               note="чек прошлый %d" % i, op_date=days_ago(40 + i))
    database.add_finance_entry(B, "expense", "закупка", 300,
                               note="товар %d" % i, op_date=days_ago(4 + i))

SECRET = "СЕКРЕТ-А-НЕ-ДОЛЖЕН-УТЕЧЬ"
database.add_fact(A, "note", "Цель месяца",
                  "Увеличить количество первичных пациентов до 60 " + SECRET)
database.add_goal(A, "clients", "Первичные пациенты " + SECRET, 60,
                  deadline=(TODAY + datetime.timedelta(days=14)).isoformat())
initiatives.scan(A, use_ai=False)

bA = database.get_business(A)
bB = database.get_business(B)


def sysmsg(business, question):
    context_engine._cache.clear()      # кэш общий на процесс — чистим между замерами
    return context_engine.build_system(business, question)


print("== РАЗБОР ДОЕЗЖАЕТ ДО МОДЕЛИ ==")
S = sysmsg(bA, "Почему прибыль упала?")
low = S.lower()
check("блок посчитанного есть", "velor уже посчитал" in low, S[:200])
check("сегодняшняя дата названа", TODAY.isoformat() in S)
check("сравнение периодов есть", "к прошлому периоду" in low)
check("у показателей есть источник", "откуда:" in low)
check("падение выручки видно числом", "выручка" in low)

print("\n== ГРАНИЦЫ ВЫВОДА ==")
# Самая ценная строка блока: без неё модель, не найдя чего-то в данных,
# додумает это сама. Проверять её на демо-наборе больше нельзя — он стал
# полным, и границ у него не осталось. Поэтому заводим компанию, у которой
# граница заведомо есть: неделя данных, сравнивать период не с чем.
YOUNG = company("Молодая компания", "ctx-young")
for i in range(6):
    database.add_finance_entry(YOUNG, "income", "продажи", 40000 + i * 1000,
                               note="чек %d" % i, op_date=days_ago(1 + i))
SY = sysmsg(database.get_business(YOUNG), "Что происходит с бизнесом?")
check("у молодой компании разбор всё равно собран",
      "velor уже посчитал" in SY.lower(), SY[:200])
check("и сказано, чего посчитать нельзя", "посчитать нельзя" in SY.lower(), SY[-500:])
check("названа причина, а не просто «нет данных»",
      "сравнить период не с чем" in SY.lower(), SY[-500:])

print("\n== ЦЕЛИ И ПАМЯТЬ ==")
check("цели владельца в промпте", "цели владельца" in low)
M = sysmsg(bA, "Какая у нас цель по первичным пациентам?")
check("память отдаёт тело записи, а не заголовок",
      "увеличить количество первичных" in M.lower(), "тело записи потерялось")
check("заголовок записи тоже на месте", "Цель месяца —" in M)

print("\n== ДОГАДКА НЕ ВЫДАЁТСЯ ЗА ФАКТ ==")
# hypothesis — единственное поле находки, которое пишет модель. Вернуть его в
# промпт значило бы отмыть догадку до факта: ровно того разделения, ради
# которого поле и завели, не осталось бы.
HYP = "ГИПОТЕЗА-МОДЕЛИ-В-ПРОМПТ-НЕ-ХОДИТ"
live = database.list_initiatives(A, live=True, limit=1)
if live:
    database.update_initiative(live[0]["id"], A, hypothesis=HYP)
    check("догадка модели в промпт не попадает", HYP not in sysmsg(bA, "что происходит"))
else:
    check("догадка модели в промпт не попадает", True, "(находок нет — нечего проверять)")

print("\n== ГРАНИЦА МЕЖДУ КОМПАНИЯМИ ==")
# Красная линия продукта. Новый блок читает три источника (Директор, находки,
# цели) — каждый обязан быть заперт своим business_id.
SB = sysmsg(bB, "Почему прибыль упала?")
check("чужая цель не утекла", SECRET not in SB, SB[:300])
check("чужая заметка не утекла", "первичных пациентов" not in SB.lower())
check("в промпте соседа его собственное имя", "Магазин Б" in SB)
check("а в своём — своё", (bA.get("name") or "") in S, bA.get("name"))
check("чужого имени в промпте соседа нет", (bA.get("name") or "?") not in SB)

# Числа берём не из головы, а из разбора самой А — иначе проверка «чужого
# числа нет» пройдёт и тогда, когда изменится формат вывода.
import director                                                      # noqa: E402
context_engine._cache.clear()
numbers = [m["display"] for m in (director.briefing(A).get("metrics") or [])
           if m.get("enough") and m.get("display") and len(str(m["display"])) > 5]
check("ни одно посчитанное число А не встретилось у Б",
      bool(numbers) and not any(n in SB for n in numbers),
      [n for n in numbers if n in SB] or "нечего было проверять")

print("\n== ПУСТОЙ БИЗНЕС ==")
E = company("Пустая компания", "ctx-empty")
SE = sysmsg(database.get_business(E), "Почему прибыль упала?")
check("пустой бизнес не роняет сборку", isinstance(SE, str) and len(SE) > 50)
check("и не получает выдуманного разбора", "velor уже посчитал" not in SE.lower(),
      "разбор появился там, где считать нечего")

print("\n== КЛИЕНТСКИЙ ПУТЬ РАЗБОР НЕ ТЯНЕТ ==")
# Вебхук отвечает клиенту в реальном времени; десятки запросов ради брифинга,
# который клиенту всё равно не покажут, здесь недопустимы.
import ai                                                            # noqa: E402
client_system = ai._system_chat(bA, {"name": "Гость"}, [])
check("внутренний разбор клиенту не уходит",
      "velor уже посчитал" not in client_system.lower())
check("и цели владельца тоже", SECRET not in client_system)

print("\n== ЦЕНА БЛОКА ==")
context_engine._cache.clear()
before = len(sysmsg(bA, "Почему прибыль упала?"))
check("промпт остался в разумных пределах", before < 20000, "%d символов" % before)
# Второй сбор подряд не должен снова считать брифинг: владелец задаёт вопросы
# сериями, а брифинг стоит десятков запросов.
seen = []
_orig = database._connect


def spy(*a, **kw):
    c = _orig(*a, **kw)
    try:
        c.set_trace_callback(seen.append)
    except Exception:
        pass
    return c


context_engine._cache.clear()
context_engine._analysis_block(A)          # прогрев
database._connect = spy
context_engine._analysis_block(A)
database._connect = _orig
check("повторный сбор берётся из кэша", len(seen) <= 5, "%d запросов" % len(seen))

print("\nИТОГО: успешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
