# -*- coding: utf-8 -*-
"""
VELOR пишет владельцу первым — и не превращается в спам.

Проактивность легко испортить в обе стороны. Замолчать — и продукт снова
разговаривает только с теми, кто сам зашёл. Или начать писать каждый день «всё
спокойно» — и через неделю эти сообщения перестанут открывать; тогда
проактивность хуже её отсутствия, потому что канал сгорел.

Что проверяем:
  1) по умолчанию рассылка ВЫКЛЮЧЕНА — это сообщения живым людям, и решение
     принимает владелец, а не код;
  2) без подтверждённого личного Telegram письмо не уходит никуда;
  3) раньше назначенного часа не пишем, дважды в день — тоже;
  4) когда сообщать нечего, письма нет, и это не ошибка;
  5) вчерашняя новость не приходит второй раз, даже если число в ней сменилось;
  6) выход из рассылки назван в самом письме;
  7) отказ всегда объяснён: «почему мне не пришло» должен иметь ответ.
"""
import os, sys, tempfile, pathlib

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "testowner"
os.environ["OWNER_PASSWORD"] = "s3cret-owner"
os.environ["JWT_SECRET"] = "test-secret-out"
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

import datetime, json                                                # noqa: E402
import database, outreach, botcore                                   # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


database.init_db()
bid = database.create_business(name="Клиника Проба", about="", greeting="",
                               login="out-one", password="pass123")
database.update_business(bid, tg_bot_token="123:FAKE")

TODAY = datetime.date.today().isoformat()
NOON = datetime.datetime.combine(datetime.date.today(), datetime.time(12, 0))
DAWN = datetime.datetime.combine(datetime.date.today(), datetime.time(5, 0))

PAYLOAD = {
    "attention": ["3 заявки висят без движения больше трёх дней",
                  "цель «Первичные пациенты» отстаёт: 40%"],
    "risk": {"title": "Расходы растут быстрее выручки", "action": "Сравните статьи"},
    "opportunity": {"title": "5 клиентов не возвращались", "action": "Позвоните"},
    "income_yday": 48000, "expense_yday": 12000,
    "today": "Главное сегодня — разобрать зависшие заявки.",
}


def put_briefing(payload=PAYLOAD, day=TODAY):
    database.save_briefing(bid, day, json.dumps(payload, ensure_ascii=False))


# Телеграм подменяем: проверяем решения вокруг отправки, а не чужой API.
SENT = []
_real_send = botcore.send_text


def fake_send(b, chat, text):
    SENT.append({"bid": b, "chat": chat, "text": text})
    return True, ""


botcore.send_text = fake_send

print("== ПО УМОЛЧАНИЮ МОЛЧИМ ==")
put_briefing()
okay, why = outreach.send_morning(bid, now=NOON)
check("выключено, пока владелец не включил", okay is False, (okay, why))
check("и сказано почему", "выключ" in why, why)
check("ничего не отправлено", not SENT, SENT)

print("\n== БЕЗ ПОДТВЕРЖДЁННОГО TELEGRAM НЕ ПИШЕМ ==")
database.update_business(bid, morning_push="9")
okay, why = outreach.send_morning(bid, now=NOON)
check("не пишем в никуда", okay is False, (okay, why))
check("причина названа", "telegram" in why.lower(), why)

database.owner_identity_upsert(bid, method="telegram", telegram_user_id="777000")
check("личный чат владельца найден", outreach.owner_chat(bid) == "777000",
      outreach.owner_chat(bid))

print("\n== ЧАС И ЧАСТОТА ==")
okay, why = outreach.send_morning(bid, now=DAWN)
check("до назначенного часа не пишем", okay is False and "рано" in why, (okay, why))

okay, why = outreach.send_morning(bid, now=NOON)
check("в свой час письмо уходит", okay is True, (okay, why))
check("и ушло оно в личный чат владельца", SENT and SENT[-1]["chat"] == "777000", SENT[-1:])

okay, why = outreach.send_morning(bid, now=NOON)
check("второй раз за день не пишем", okay is False and "уже" in why, (okay, why))
check("и второго сообщения нет", len(SENT) == 1, len(SENT))

print("\n== ЧТО В ПИСЬМЕ ==")
text = SENT[0]["text"]
print("      " + text.replace("\n", "\n      "))
check("названа компания", "Клиника Проба" in text)
check("зависшие заявки на месте", "3 заявки висят" in text)
check("риск на месте", "Расходы растут быстрее выручки" in text)
check("деньги за вчера названы", "48 000 ₽" in text)
check("выход из рассылки назван прямо", "Настройки" in text and "письмо" in text.lower())
check("письмо короткое", len(text) < 900, len(text))

print("\n== ВЧЕРАШНЕЕ НЕ ПРИХОДИТ ДВАЖДЫ ==")
# Отпечаток берётся без чисел: «3 заявки висят» и «4 заявки висят» — это одна
# и та же новость, и приходить она должна один раз, а не всю неделю.
YDAY = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
database.save_push_state(bid, sent_on=YDAY)
again = dict(PAYLOAD, attention=["4 заявки висят без движения больше трёх дней"],
             risk=PAYLOAD["risk"], opportunity=None)
put_briefing(again)
okay, why = outreach.send_morning(bid, now=NOON)
check("повторная новость письма не рождает", okay is False and "нечего" in why, (okay, why))
check("и правда ничего не ушло", len(SENT) == 1, len(SENT))

print("\n== НОВОЕ — ПРИХОДИТ ==")
fresh = dict(PAYLOAD, attention=["вчера потратили больше, чем заработали"],
             risk=None, opportunity=None)
put_briefing(fresh)
okay, why = outreach.send_morning(bid, now=NOON)
check("новая новость доходит", okay is True, (okay, why))
check("и в письме именно она", "потратили больше" in SENT[-1]["text"], SENT[-1]["text"][:120])

print("\n== СПОКОЙНЫЙ ДЕНЬ — МОЛЧАНИЕ ==")
database.save_push_state(bid, sent_on=YDAY, lines="[]")
put_briefing({"income_yday": 5000, "expense_yday": 1000, "attention": []})
okay, why = outreach.send_morning(bid, now=NOON)
check("«всё спокойно» каждый день не рассылаем", okay is False and "нечего" in why,
      (okay, why))

print("\n== БЕЗ БРИФИНГА НЕ ВЫДУМЫВАЕМ ==")
b2 = database.create_business(name="Без данных", about="", greeting="",
                              login="out-two", password="pass123")
database.update_business(b2, morning_push="9", tg_bot_token="456:FAKE")
database.owner_identity_upsert(b2, method="telegram", telegram_user_id="777111")
okay, why = outreach.send_morning(b2, now=NOON)
check("нет брифинга — нет письма", okay is False and "брифинг" in why, (okay, why))

print("\n== НАСТРОЙКА ЧИТАЕТСЯ ПО-ЧЕЛОВЕЧЕСКИ ==")
for raw, want in ((None, (False, 9)), ("", (False, 9)), ("off", (False, 9)),
                  ("on", (True, 9)), ("7", (True, 7)), ("21", (True, 21)),
                  ("3", (True, 6)), ("99", (True, 22)), ("мусор", (False, 9))):
    got = outreach.setting({"morning_push": raw})
    check("«%s» → %s" % (raw, want), got == want, got)

print("\n== ОБХОД НЕ ПАДАЕТ ИЗ-ЗА ОДНОЙ КОМПАНИИ ==")
database.save_push_state(bid, sent_on=None, lines="[]")
put_briefing()
sent = outreach.round_all(now=NOON)
check("обход прошёл и что-то отправил", sent >= 1, sent)

botcore.send_text = _real_send
print("\nИТОГО: успешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
