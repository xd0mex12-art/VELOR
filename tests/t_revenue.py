# -*- coding: utf-8 -*-
"""
СКВОЗНОЙ КОММЕРЧЕСКИЙ ЦИКЛ — от сообщения до денег и обратно.

Здесь не проверяется ни один модуль по отдельности: каждый из них уже покрыт
своим файлом. Здесь проверяются СТЫКИ — то место, где цепочка рвётся:

  • сообщение находит клиента, клиент — возможность, возможность — оценку;
  • четыре сообщения подряд остаются одним разговором, а не четырьмя;
  • отправленное касание становится настоящим сообщением в переписке;
  • ответ клиента отменяет запланированное и засчитывается касанию;
  • заявка закрывает возможность, из какого бы входа она ни пришла;
  • «купил» означает, что заявка существует, а не что так решили;
  • выручка берётся из заявок, а не из оценки возможности;
  • неудача, запрет и устаревание видны, а не выглядят как успех;
  • повтор события не удваивает ничего;
  • владелец видит всю цепочку в одном месте.
"""
import os, sys, tempfile, pathlib, datetime

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "testowner"
os.environ["OWNER_PASSWORD"] = "s3cret-owner"
os.environ["JWT_SECRET"] = "test-secret-xyz"
os.environ["SECRET_KEY"] = "test-box-key"
os.environ["DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
os.environ["REGISTER_MAX"] = "500"
sys.stdout.reconfigure(encoding="utf-8")
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, actions, initiatives, leads, followup, qualify, botcore
import entities

c = TestClient(server.app)
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


def reg(login):
    d = c.post("/api/register", json={"name": login, "login": login,
                                      "password": "pass123", "consent": True}).json()
    bid = d["business_id"]
    database.update_business(bid, tg_bot_token="1234:TESTTOKEN-" + login)
    database.add_fact(bid, "service", "Букет на заказ", "от 3500 ₽, сборка 40 минут")
    return bid, {"X-Auth": d["token"]}


def ago(hours=0, days=0):
    return (datetime.datetime.utcnow()
            - datetime.timedelta(hours=hours, days=days)).strftime("%Y-%m-%d %H:%M:%S")


def who(bid, tg, name):
    return database.get_or_create_client(bid, tg_user_id=tg, name=name)


def said(bid, cl, text, *, at=None):
    """Клиент написал. Ровно тот же путь, что у настоящего сообщения."""
    mid = database.save_message(bid, cl["id"], "user", text, channel="telegram",
                                created_at=at or database.now())
    lid = leads.from_message(bid, cl, text, source="telegram", channel="telegram",
                             message_id=mid)
    return mid, lid


def answered(bid, cl, text="Здравствуйте! Считаю и возвращаюсь.", *, at=None):
    return database.save_message(bid, cl["id"], "assistant", text,
                                 channel="telegram", created_at=at or database.now())


def work_history(bid, client_id, n=12):
    """Известные рабочие часы: иначе автоматическая отправка не пойдёт вовсе."""
    for i in range(n):
        hour = int(i * 23 / max(1, n - 1))
        database.save_message(bid, client_id, "assistant", "старый ответ %d" % i,
                              channel="telegram",
                              created_at="2020-01-0%d %02d:00:00" % (1 + i % 9, hour))


# ── подставной канал ───────────────────────────────────────────────────────
# Подменяем ТОЛЬКО транспорт. Всё остальное — настоящее: записи в базе,
# сообщения в переписке, состояния, журнал.
SENT = []
OUTAGE = {"on": False, "why": "Telegram: Unauthorized"}
_real_send = botcore.send_text


def fake_send(bid, tg_user_id, text):
    if OUTAGE["on"]:
        return False, OUTAGE["why"]
    SENT.append({"bid": bid, "to": str(tg_user_id), "text": text})
    return True, ""


botcore.send_text = fake_send

ASK = "Здравствуйте! Хочу заказать букет на завтра, сколько будет стоить?"


# ═══ 1. ПОЛНАЯ ПРОДАЖА ═════════════════════════════════════════════════════
print("\n== 1. ПОЛНАЯ ПРОДАЖА ==")
bidA, HA = reg("rev_a")
cl = who(bidA, 100, "Марина")
mid, lid = said(bidA, cl, ASK)

check("сообщение сохранено", bool(database.get_messages(bidA, cl["id"], limit=5))
      if hasattr(database, "get_messages") else True)
check("клиент один", database.count_clients(bidA) == 1)
check("возможность заведена", bool(lid), lid)
lead = database.get_lead(lid, bidA)
check("возможность знает своего клиента", lead["client_id"] == cl["id"])
check("возможность знает, с какого сообщения началась",
      lead["first_message_id"] == mid, (lead["first_message_id"], mid))
check("сообщение видно в границах разговора",
      any(m["id"] == mid for m in database.lead_messages(bidA, lid)))
check("оценка посчитана", lead.get("intent") == qualify.HIGH, lead.get("intent"))
check("оценка помечена временем", bool(lead.get("qualified_at")))

view = leads.public(lead)["q"]
check("приоритет посчитан живьём",
      view["priority"] in (qualify.HIGH, qualify.URGENT), view["priority"])
check("и объяснён словами", bool(view.get("priority_why")), view.get("priority_why"))
check("в базе приоритет не хранится — он зависит от времени",
      lead.get("priority") is None, lead.get("priority"))
check("виден следующий шаг", bool(view.get("action")), view.get("action"))

# Бизнес ответил — разрыв закрыт, писать первым больше не нужно.
answered(bidA, cl)
gap = database.last_exchange(bidA, cl["id"])
check("после ответа бизнеса разрыва нет", not gap["unanswered"], gap)
plan = followup.plan(bidA, database.get_lead(lid, bidA))
# «Бизнес молчит» как повод исчез. Другой повод остаться может — клиент назвал
# завтрашнюю дату, — и это не то же самое: в тексте касания будет про дату, а
# не про наш долг.
check("повод «бизнес молчит» исчез",
      plan is None or plan["reason"] != followup.BUSINESS_GAP,
      plan and (plan["status"], plan.get("reason")))
check("а если касание всё же готовится, то по другому поводу",
      plan is None or plan["reason"] == followup.TIMING,
      plan and plan.get("reason"))

# Клиент ответил сам.
mid2, _ = said(bidA, cl, "Да, давайте оформим")
check("это тот же разговор, а не второй",
      len(database.list_leads(bidA, client_id=cl["id"], limit=10)) == 1)

# Появилась заявка.
oid = database.add_order(bidA, "Букет 15 роз", client_id=cl["id"], amount=5200)
leads.link_order(bidA, oid, client_id=cl["id"], amount=5200, channel="telegram")
lead = database.get_lead(lid, bidA)
check("возможность закрыта сделкой", lead["status"] == leads.WON, lead["status"])
check("и связана с настоящей заявкой", lead["order_id"] == oid)
check("дата конверсии записана", bool(lead["converted_at"]))
check("сумма взята из заявки", lead["value"] == 5200, lead["value"])

s = database.sales_today(bidA)
check("выручка дня — из заявки", s["turnover"] == 5200, s)
check("выигранная возможность посчитана", s["won"] == 1, s)
check("оценка возможности в выручку не попала",
      s["turnover"] == database.get_order(oid, bidA)["amount"], s["turnover"])


# ═══ 2. БИЗНЕС МОЛЧИТ ══════════════════════════════════════════════════════
print("\n== 2. БИЗНЕС МОЛЧИТ — И ЦЕПОЧКА ДОХОДИТ ДО ЗАЯВКИ ==")
bidB, HB = reg("rev_b")
clB = who(bidB, 200, "Игорь")
work_history(bidB, clB["id"])
midB, lidB = said(bidB, clB, ASK, at=ago(hours=6))

initiatives.scan(bidB)
found = [i for i in initiatives.feed(bidB)
         if i["type"] == initiatives.HOT_LEADS_UNANSWERED]
check("VELOR заметил, что бизнес молчит", bool(found),
      [i["type"] for i in initiatives.feed(bidB)])
check("и назвал этот разговор",
      found and lidB in found[0]["evidence"]["ids"], found and found[0]["evidence"])

# Из находки вырастает действие — через полномочия.
res = initiatives.act(bidB, found[0]["id"])
check("действие создано", bool(res["actions"]), res)
made = database.get_action(res["actions"][0], bidB)
check("действие ссылается на находку",
      made["based_on"].get("initiative_id") == found[0]["id"], made["based_on"])
check("черновик готов",
      bool(database.list_followups(bidB, lead_id=lidB, live=True, limit=5)))

# Отправка спрашивается отдельно.
actions.set_mode(bidB, "send_followup", actions.APPROVAL)
fu = database.list_followups(bidB, lead_id=lidB, live=True, limit=5)[0]
followup._propose_action(bidB, fu)
waiting = [a for a in actions.pending(bidB) if a["action"] == "send_followup"]
check("отправка ждёт разрешения владельца", bool(waiting), actions.pending(bidB))
check("пока ничего не ушло", not SENT)

before_msgs = len(database.lead_messages(bidB, lidB))
row = actions.approve(bidB, waiting[0]["id"])
check("после разрешения сообщение ушло", len(SENT) == 1, SENT)
check("действие отмечено выполненным", row["status"] == database.AC_SUCCEEDED,
      (row["status"], row["error"]))
fu = database.get_followup(fu["id"], bidB)
check("касание отмечено отправленным", fu["status"] == database.FU_SENT, fu["status"])

# Самое важное: отправленное стало настоящим сообщением.
msgs = database.lead_messages(bidB, lidB)
check("касание стало сообщением в переписке", len(msgs) > before_msgs,
      (before_msgs, len(msgs)))
check("и это исходящее сообщение", msgs[-1]["role"] != "user", msgs[-1]["role"])
check("текст в переписке тот же, что ушёл",
      msgs[-1]["content"] == SENT[0]["text"])
lead = database.get_lead(lidB, bidB)
check("граница разговора сдвинулась на него",
      lead["last_message_id"] == msgs[-1]["id"],
      (lead["last_message_id"], msgs[-1]["id"]))
check("«клиент молчит» больше не считается правдой",
      not database.last_exchange(bidB, clB["id"])["unanswered"])

# Клиент откликнулся.
midB2, _ = said(bidB, clB, "Да, интересно, давайте оформим")
fu = database.get_followup(fu["id"], bidB)
check("касанию засчитан ответ", fu["outcome"] == followup.REPLIED, fu["outcome"])
check("и записано, каким именно сообщением",
      fu["reply_message_id"] == midB2, (fu["reply_message_id"], midB2))

oidB = database.add_order(bidB, "Букет 25 роз", client_id=clB["id"], amount=9000)
leads.link_order(bidB, oidB, client_id=clB["id"], amount=9000, channel="telegram")
fu = database.get_followup(fu["id"], bidB)
check("касанию засчитана заявка", fu["outcome"] == followup.CONVERTED)
check("и это та самая заявка", fu["order_id"] == oidB)
check("возможность выиграна", database.get_lead(lidB, bidB)["status"] == leads.WON)

# Атрибуция: цепочка есть, а вывода «VELOR принёс деньги» — нет.
check("нигде не написано, что деньги принёс VELOR",
      not any("принёс" in (a.get("result") or "")
              for a in actions.feed(bidB, limit=30)))
check("но последовательность событий восстановима",
      fu["sent_at"] and fu["outcome_at"] and fu["order_id"],
      (fu["sent_at"], fu["outcome_at"], fu["order_id"]))


# ═══ 3. КЛИЕНТ НЕ ОТВЕЧАЕТ ═════════════════════════════════════════════════
print("\n== 3. КЛИЕНТ ПРОПАЛ — НО ЭТО НЕ ПРОИГРЫШ ==")
bidC, HC = reg("rev_c")
clC = who(bidC, 300, "Ольга")
work_history(bidC, clC["id"])
midC, lidC = said(bidC, clC, ASK, at=ago(days=3))
answered(bidC, clC, "Здравствуйте! Букет из 15 роз — 5 200 ₽.", at=ago(days=3))

row = followup.plan(bidC, database.get_lead(lidC, bidC))
check("касание подготовлено", row and row["status"] == database.FU_DRAFT,
      row and row["status"])
check("и повод назван", row and row["reason"] in followup.REASONS, row and row["reason"])

n_before = len(SENT)
followup.send(bidC, row["id"], auto=False)
check("касание ушло", len(SENT) == n_before + 1)
check("клиент по-прежнему молчит",
      not database.last_exchange(bidC, clC["id"])["unanswered"])

# Прошла неделя, ответа нет.
database.update_followup(row["id"], bidC, sent_at=ago(days=8))
followup.settle(bidC)
row = database.get_followup(row["id"], bidC)
check("записано, что ответа не было", row["outcome"] == followup.IGNORED,
      row["outcome"])
lead = database.get_lead(lidC, bidC)
check("возможность осталась открытой", lead["status"] in database.LEAD_OPEN,
      lead["status"])
check("молчание не превратилось в проигрыш", lead["status"] != leads.LOST)
check("и причины проигрыша никто не выдумал", not lead.get("lost_reason"))


# ═══ 4. КЛИЕНТ ОТВЕТИЛ РАНЬШЕ, ЧЕМ МЫ НАПИСАЛИ ═════════════════════════════
print("\n== 4. КЛИЕНТ ОПЕРЕДИЛ КАСАНИЕ ==")
bidD, HD = reg("rev_d")
clD = who(bidD, 400, "Пётр")
work_history(bidD, clD["id"])
midD, lidD = said(bidD, clD, ASK, at=ago(days=2))
answered(bidD, clD, "Здравствуйте! 5 200 ₽.", at=ago(days=2))
row = followup.plan(bidD, database.get_lead(lidD, bidD))
check("касание запланировано", bool(row) and row["status"] in database.FOLLOWUP_LIVE)

n_before = len(SENT)
said(bidD, clD, "Извините, задержался — давайте оформим")
row = database.get_followup(row["id"], bidD)
check("запланированное отменено", row["status"] == database.FU_CANCELLED,
      row["status"])
check("и объяснено человеческими словами",
      followup.STOP_RU.get(row["stop_reason"]) == "клиент снова написал сам",
      row["stop_reason"])
check("устаревшее сообщение никуда не ушло", len(SENT) == n_before)
check("повторная отправка его не воскрешает",
      followup.send(bidD, row["id"], auto=False)["status"] == database.FU_CANCELLED
      and len(SENT) == n_before)


# ═══ 5. ОТПРАВКА СОРВАЛАСЬ ═════════════════════════════════════════════════
print("\n== 5. КАНАЛ НЕ ОТВЕТИЛ ==")
bidE, HE = reg("rev_e")
clE = who(bidE, 500, "Анна")
work_history(bidE, clE["id"])
midE, lidE = said(bidE, clE, ASK, at=ago(days=2))
answered(bidE, clE, "Здравствуйте! 5 200 ₽.", at=ago(days=2))
row = followup.plan(bidE, database.get_lead(lidE, bidE))
followup._propose_action(bidE, row)
prop = [a for a in actions.pending(bidE) if a["action"] == "send_followup"]

OUTAGE["on"] = True
n_before, msgs_before = len(SENT), len(database.lead_messages(bidE, lidE))
after = actions.approve(bidE, prop[0]["id"]) if prop else None
OUTAGE["on"] = False

row = database.get_followup(row["id"], bidE)
check("сообщение не ушло", len(SENT) == n_before)
check("и в переписке его нет",
      len(database.lead_messages(bidE, lidE)) == msgs_before)
check("касание не считается отправленным", row["status"] != database.FU_SENT,
      row["status"])
check("ошибка записана словами", "Unauthorized" in (row.get("error") or ""),
      row.get("error"))
check("исхода «ответил» у него нет", not row.get("outcome"), row.get("outcome"))
check("владелец видит ошибку в журнале",
      any("Unauthorized" in (a.get("error") or "")
          for a in actions.feed(bidE, limit=20)) or
      any("Unauthorized" in (a.get("error") or "")
          for a in actions.pending(bidE)),
      [(a["status_ru"], a["error"]) for a in actions.feed(bidE, limit=5)])


# ═══ 6. ДЕЙСТВИЕ ЗАПРЕЩЕНО ═════════════════════════════════════════════════
print("\n== 6. ВЛАДЕЛЕЦ ЗАПРЕТИЛ ==")
bidF, HF = reg("rev_f")
actions.set_mode(bidF, "prepare_followup", actions.DENY)
for i in range(2):
    clF = who(bidF, 600 + i, "Клиент %d" % i)
    said(bidF, clF, ASK, at=ago(hours=6))
initiatives.scan(bidF)
fnd = [i for i in initiatives.feed(bidF)
       if i["type"] == initiatives.HOT_LEADS_UNANSWERED]
check("находка есть и при запрете", bool(fnd))
n_before = len(SENT)
res = initiatives.act(bidF, fnd[0]["id"])
rows = [database.get_action(i, bidF) for i in res["actions"]]
check("действия заблокированы",
      all(r["status"] == database.AC_BLOCKED for r in rows),
      [r["status"] for r in rows])
check("наружу ничего не ушло", len(SENT) == n_before)
check("черновиков не появилось",
      not database.list_followups(bidF, live=True, limit=10))
check("причина запрета записана",
      all("запретили" in (r["error"] or "") for r in rows), [r["error"] for r in rows])
check("находка осталась видна владельцу",
      database.get_initiative(fnd[0]["id"], bidF)["status"]
      in database.INITIATIVE_LIVE)


# ═══ 7. ДЕЙСТВИЕ РАЗРЕШЕНО ЗАРАНЕЕ ═════════════════════════════════════════
print("\n== 7. РАЗРЕШЕНО — ДЕЛАЕТ САМ ==")
bidG, HG = reg("rev_g")
clG = who(bidG, 700, "Кирилл")
work_history(bidG, clG["id"])
midG, lidG = said(bidG, clG, ASK, at=ago(days=2))
answered(bidG, clG, "Здравствуйте! 5 200 ₽.", at=ago(days=2))
actions.set_mode(bidG, "send_followup", actions.AUTO)
n_before, msgs_before = len(SENT), len(database.lead_messages(bidG, lidG))
got = followup.run(bidG)
check("обход подготовил и отправил", got.get("sent", 0) >= 1, got)
check("сообщение действительно ушло", len(SENT) == n_before + 1)
msgs = database.lead_messages(bidG, lidG)
check("и появилось в переписке", len(msgs) > msgs_before, (msgs_before, len(msgs)))
check("в переписке лежит ровно тот текст, что ушёл",
      any(m["content"] == SENT[-1]["text"] for m in msgs))
sent_rows = database.list_followups(bidG, status=database.FU_SENT, limit=5)
check("касание отмечено отправленным", bool(sent_rows))
check("действие записано автоматическим",
      any(a["action"] == "send_followup" and a["mode"] == actions.AUTOMATIC
          and a["status"] == database.AC_SUCCEEDED
          for a in database.list_actions(bidG, limit=20)),
      [(a["action"], a["mode"], a["status"])
       for a in database.list_actions(bidG, limit=5)])
check("в журнале виден результат",
      any("доставлено" in (a.get("result") or "")
          for a in database.list_actions(bidG, action="send_followup", limit=5)))


# ═══ 8. СТАРЫЙ КЛИЕНТ — НОВАЯ ВОЗМОЖНОСТЬ ══════════════════════════════════
print("\n== 8. ТОТ ЖЕ ЧЕЛОВЕК, ДРУГАЯ ПОКУПКА ==")
bidH, HH = reg("rev_h")
clH = who(bidH, 800, "Дарья")
mid1, lid1 = said(bidH, clH, ASK)
oidH = database.add_order(bidH, "Букет 15 роз", client_id=clH["id"], amount=5200)
leads.link_order(bidH, oidH, client_id=clH["id"], amount=5200, channel="telegram")
check("первая возможность выиграна",
      database.get_lead(lid1, bidH)["status"] == leads.WON)

mid2, lid2 = said(bidH, clH,
                  "Здравствуйте! Снова хочу заказать букет, теперь на пятницу")
check("заведена новая возможность", lid2 and lid2 != lid1, (lid1, lid2))
check("клиент остался прежним", database.count_clients(bidH) == 1)
check("у обеих один и тот же человек",
      database.get_lead(lid2, bidH)["client_id"] == clH["id"])
check("старая осталась выигранной",
      database.get_lead(lid1, bidH)["status"] == leads.WON)
check("истории разные",
      database.get_lead(lid1, bidH)["first_message_id"] !=
      database.get_lead(lid2, bidH)["first_message_id"])
check("в воронке два разных исхода",
      database.leads_overview(bidH)["won"] == 1
      and database.leads_overview(bidH)["open"] == 1,
      database.leads_overview(bidH))


# ═══ 9. НЕ СЛОЖИЛОСЬ ═══════════════════════════════════════════════════════
print("\n== 9. КЛИЕНТ ОТКАЗАЛСЯ ==")
bidI, HI = reg("rev_i")
clI = who(bidI, 900, "Сергей")
work_history(bidI, clI["id"])
midI, lidI = said(bidI, clI, ASK, at=ago(days=2))
answered(bidI, clI, "Здравствуйте! 5 200 ₽.", at=ago(days=2))
row = followup.plan(bidI, database.get_lead(lidI, bidI))
check("касание было запланировано", bool(row))

r = c.post("/api/leads/%d/status" % lidI,
           json={"status": "lost", "lost_reason": "price"}, headers=HI)
check("проигрыш записан", r.status_code == 200
      and r.json()["lead"]["status"] == leads.LOST, r.status_code)
lead = database.get_lead(lidI, bidI)
check("причина сохранена", lead["lost_reason"] == "price", lead["lost_reason"])
check("дата закрытия записана", bool(lead["lost_at"]))
check("касания остановлены",
      not database.list_followups(bidI, lead_id=lidI, live=True, limit=5))
check("проигрыш виден в воронке", database.leads_overview(bidI)["lost"] == 1)

# Без причины проигрыш не остаётся безымянным.
clI2 = who(bidI, 901, "Без причины")
_, lidI2 = said(bidI, clI2, ASK)
leads.set_status(bidI, lidI2, leads.LOST)
check("причину не выдумывают — ставят «другое»",
      database.get_lead(lidI2, bidI)["lost_reason"] == leads.DEFAULT_LOST_REASON,
      database.get_lead(lidI2, bidI)["lost_reason"])


# ═══ 10. НАХОДКА ЗАКРЫВАЕТСЯ САМА ══════════════════════════════════════════
print("\n== 10. ПРОБЛЕМА ИСЧЕЗЛА — НАХОДКА ЗАКРЫЛАСЬ ==")
bidJ, HJ = reg("rev_j")
people = []
for i in range(3):
    clJ = who(bidJ, 1000 + i, "Ждёт %d" % i)
    said(bidJ, clJ, ASK, at=ago(hours=6))
    people.append(clJ)
initiatives.scan(bidJ)
fnd = initiatives.feed(bidJ)[0]
check("находка появилась", fnd["type"] == initiatives.HOT_LEADS_UNANSWERED)
was = database.count_initiatives(bidJ, live=True)

for clJ in people:
    answered(bidJ, clJ, "Здравствуйте! Отвечаю.")
got = initiatives.scan(bidJ)
closed = database.get_initiative(fnd["id"], bidJ)
check("находка закрылась", closed["status"] == database.IN_RESOLVED, closed["status"])
check("и записала, что изменилось", "Было 3" in (closed["outcome"] or ""),
      closed["outcome"])
check("новой такой же не завелось",
      database.count_initiatives(bidJ, live=True) == was - 1,
      database.count_initiatives(bidJ, live=True))
initiatives.scan(bidJ)
check("и повторный обход её не воскрешает",
      not [i for i in initiatives.feed(bidJ)
           if i["type"] == initiatives.HOT_LEADS_UNANSWERED])


# ═══ 11. ЗАЯВКА ИЗ ЛЮБОГО ВХОДА ════════════════════════════════════════════
print("\n== 11. ЗАЯВКА НАХОДИТ ВОЗМОЖНОСТЬ, ОТКУДА БЫ НИ ПРИШЛА ==")
bidK, HK = reg("rev_k")
# 1) рукой владельца в кабинете, с указанием клиента
clK1 = who(bidK, 1100, "Через кабинет")
_, lidK1 = said(bidK, clK1, ASK)
r = c.post("/api/orders", json={"text": "Букет 15 роз", "amount": 5200,
                                "client_id": clK1["id"]}, headers=HK)
check("заявка из кабинета закрывает возможность",
      r.json().get("lead_id") == lidK1
      and database.get_lead(lidK1, bidK)["status"] == leads.WON,
      (r.json(), database.get_lead(lidK1, bidK)["status"]))

# 2) рукой владельца, но клиент узнаётся по телефону
clK2 = who(bidK, 1101, "По телефону")
database.update_client(clK2["id"], bidK, phone="+7 916 111-22-33")
_, lidK2 = said(bidK, clK2, ASK)
r = c.post("/api/orders", json={"text": "Букет 25 роз", "amount": 9000,
                                "phone": "89161112233"}, headers=HK)
check("человек узнаётся по номеру",
      database.get_lead(lidK2, bidK)["status"] == leads.WON,
      database.get_lead(lidK2, bidK)["status"])

# 3) из присланного материала (единое окно)
clK3 = who(bidK, 1102, "Из материала")
database.update_client(clK3["id"], bidK, phone="+7 916 222-33-44")
_, lidK3 = said(bidK, clK3, ASK)
oidK3, _label = entities._make_order(bidK, {"text": "Букет 11 роз", "amount": 4400,
                                            "phone": "8 916 222 33 44"})
check("заявка из единого окна закрывает возможность",
      database.get_lead(lidK3, bidK)["status"] == leads.WON,
      database.get_lead(lidK3, bidK)["status"])
check("и связана именно с ней",
      database.get_lead(lidK3, bidK)["order_id"] == oidK3)

# 4) заявка вообще без разговора — это нормально
oidK4 = database.add_order(bidK, "Заявка с улицы", amount=1000)
check("заявка без клиента ничего не ломает",
      leads.link_order(bidK, oidK4) is None)
check("и не портит воронку", database.leads_overview(bidK)["won"] == 3,
      database.leads_overview(bidK))


# ═══ 12. «КУПИЛ» ОЗНАЧАЕТ ЗАЯВКУ ═══════════════════════════════════════════
print("\n== 12. «КУПИЛ» — ЭТО ЗАЯВКА, А НЕ МНЕНИЕ ==")
bidL, HL = reg("rev_l")
clL = who(bidL, 1200, "Владимир")
_, lidL = said(bidL, clL, ASK)
was_orders = database.count_orders(bidL)
r = c.post("/api/leads/%d/status" % lidL, json={"status": "won", "amount": 7300},
           headers=HL)
d = r.json()
check("возможность выиграна", d["lead"]["status"] == leads.WON)
check("заявка заведена", database.count_orders(bidL) == was_orders + 1)
order = database.get_order(d["order_id"], bidL)
check("сумма попала в заявку", order["amount"] == 7300, order["amount"])
check("заявка знает клиента", order["client_id"] == clL["id"])
check("возможность ссылается на неё",
      database.get_lead(lidL, bidL)["order_id"] == order["id"])
check("и сумма сделки записана в возможность",
      database.get_lead(lidL, bidL)["value"] == 7300,
      database.get_lead(lidL, bidL)["value"])
check("выручка дня выросла ровно на неё",
      database.sales_today(bidL)["turnover"] == 7300,
      database.sales_today(bidL))

c.post("/api/leads/%d/status" % lidL, json={"status": "won"}, headers=HL)
check("повторное «купил» второй заявки не заводит",
      database.count_orders(bidL) == was_orders + 1, database.count_orders(bidL))

# Настоящую заявку, если она уже есть, повторно не создаём.
clL2 = who(bidL, 1201, "С готовой заявкой")
_, lidL2 = said(bidL, clL2, ASK)
oidL2 = database.add_order(bidL, "Уже оформлено", client_id=clL2["id"], amount=2000)
was_orders = database.count_orders(bidL)
c.post("/api/leads/%d/status" % lidL2,
       json={"status": "won", "order_id": oidL2}, headers=HL)
check("указанная заявка используется, а не дублируется",
      database.count_orders(bidL) == was_orders
      and database.get_lead(lidL2, bidL)["order_id"] == oidL2)


# ═══ 13. ИДЕМПОТЕНТНОСТЬ ═══════════════════════════════════════════════════
print("\n== 13. ПОВТОР НИЧЕГО НЕ УДВАИВАЕТ ==")
bidM, HM = reg("rev_m")
clM = who(bidM, 1300, "Повторный")
work_history(bidM, clM["id"])
mid1, lid1 = said(bidM, clM, ASK, at=ago(days=2))
# то же сообщение обрабатывается второй раз
lid2 = leads.from_message(bidM, clM, ASK, source="telegram", channel="telegram",
                          message_id=mid1)
check("второй возможности не появилось", lid1 == lid2
      and len(database.list_leads(bidM, client_id=clM["id"], limit=10)) == 1)

answered(bidM, clM, "Здравствуйте! 5 200 ₽.", at=ago(days=2))
r1 = followup.plan(bidM, database.get_lead(lid1, bidM))
r2 = followup.plan(bidM, database.get_lead(lid1, bidM))
check("второго черновика не появилось", r1["id"] == r2["id"]
      and len(database.list_followups(bidM, lead_id=lid1, live=True, limit=10)) == 1)

n_before = len(SENT)
followup.send(bidM, r1["id"], auto=False)
followup.send(bidM, r1["id"], auto=False)
check("дважды одно сообщение не уходит", len(SENT) == n_before + 1, len(SENT))

oidM = database.add_order(bidM, "Букет", client_id=clM["id"], amount=3000)
leads.link_order(bidM, oidM, client_id=clM["id"], amount=3000)
before = database.get_lead(lid1, bidM)["converted_at"]
leads.link_order(bidM, oidM, client_id=clM["id"], amount=3000)
check("второй раз возможность не выигрывается",
      database.get_lead(lid1, bidM)["converted_at"] == before,
      (before, database.get_lead(lid1, bidM)["converted_at"]))
check("и заявка осталась одна", database.count_orders(bidM) == 1)


# ═══ 14. СВЕРКА СОСТОЯНИЙ ══════════════════════════════════════════════════
print("\n== 14. СВЕРКА: НЕВОЗМОЖНЫХ СОСТОЯНИЙ НЕТ ==")
for name, bid in (("полная продажа", bidA), ("бизнес молчал", bidB),
                  ("клиент пропал", bidC), ("опередил касание", bidD),
                  ("отправка сорвалась", bidE), ("запрещено", bidF),
                  ("сделал сам", bidG), ("новый заход", bidH),
                  ("не сложилось", bidI), ("заявки отовсюду", bidK),
                  ("купил", bidL), ("повторы", bidM)):
    flaws = leads.reconcile(bid)
    check("после сценария «%s» противоречий нет" % name, not flaws,
          [(f["kind"], f["entity"], f["id"]) for f in flaws])

# Сверка обязана ловить то, ради чего она есть.
bidN, HN = reg("rev_n")
clN = who(bidN, 1400, "Сломанный")
_, lidN = said(bidN, clN, ASK)
database.update_lead(lidN, bidN, status="won", converted_at=database.now())
kinds = {f["kind"] for f in leads.reconcile(bidN)}
check("«выиграно без заявки» найдено", "won_without_order" in kinds, kinds)

oidN = database.add_order(bidN, "Заявка", client_id=clN["id"], amount=100)
database.update_lead(lidN, bidN, order_id=oidN)
check("после связи с заявкой противоречие исчезло",
      not leads.reconcile(bidN), leads.reconcile(bidN))

database.delete_order(oidN, bidN)
kinds = {f["kind"] for f in leads.reconcile(bidN)}
check("исчезнувшая заявка тоже находится", "won_order_missing" in kinds, kinds)

# Отправленное касание без сообщения в переписке.
bidO, HO = reg("rev_o")
clO = who(bidO, 1500, "Призрак")
_, lidO = said(bidO, clO, ASK)
fid = database.add_followup(bidO, lidO, reason=followup.CUSTOMER_NO_RESPONSE,
                            channel="telegram", client_id=clO["id"],
                            message="привет")
database.update_followup(fid, bidO, status=database.FU_SENT, sent_at=database.now())
kinds = {f["kind"] for f in leads.reconcile(bidO)}
check("«отправлено, а сообщения нет» найдено", "sent_without_message" in kinds, kinds)

database.update_followup(fid, bidO, status=database.FU_CANCELLED)
kinds = {f["kind"] for f in leads.reconcile(bidO)}
check("«отменено, но отправлено» найдено", "cancelled_but_sent" in kinds, kinds)

database.update_followup(fid, bidO, status=database.FU_DRAFT, sent_at=None,
                         outcome=followup.CONVERTED, order_id=999999)
kinds = {f["kind"] for f in leads.reconcile(bidO)}
check("«заявка есть, а заявки нет» найдено", "converted_without_order" in kinds, kinds)
check("«исход у неотправленного» найдено", "outcome_without_send" in kinds, kinds)

database.update_followup(fid, bidO, outcome=followup.REPLIED, order_id=None,
                         status=database.FU_SENT, sent_at=ago(hours=1))
kinds = {f["kind"] for f in leads.reconcile(bidO)}
check("«ответил, а ответа нет» найдено", "replied_without_message" in kinds, kinds)


# ═══ 15. ЧТО ВИДИТ ВЛАДЕЛЕЦ ════════════════════════════════════════════════
print("\n== 15. ВСЯ ЦЕПОЧКА В ОДНОМ МЕСТЕ ==")
r = c.get("/api/leads/%d" % lidB, headers=HB)
card = r.json()
check("карточка открывается", r.status_code == 200)
check("в ней сам человек", card["lead"]["client_name"], card["lead"].get("client_name"))
check("в ней чего он хочет", bool(card["lead"]["title"]))
check("в ней разговор", len(card["messages"]) >= 2, len(card["messages"]))
check("в ней оценка", card["lead"]["intent"] in (qualify.LOW, qualify.MEDIUM,
                                                 qualify.HIGH))
check("в ней объяснение приоритета",
      bool(card["lead"]["q"].get("priority_why")), card["lead"]["q"].get("priority_why"))
check("в ней касания", bool(card["followups"]))
check("в ней действия VELOR", bool(card["actions"]), card.get("actions"))
check("действия названы словами, а не кодами",
      all(a["title"] and a["status_ru"] for a in card["actions"]))
check("в ней заявка", bool(card["order"]), card.get("order"))
check("в ней результат", card["lead"]["status"] == leads.WON)

home = c.get("/api/home", headers=HB).json()
check("на главной есть продажи", "sales" in home, sorted(home)[:5])
s = home["sales"]
check("и в них все звенья цепочки",
      {"new_leads", "open_leads", "drafts", "replies", "orders", "won"} <= set(s),
      sorted(s))
check("выручка дня — из заявок", s["turnover"] == 9000, s)

r = c.get("/api/leads/%d" % lidB, headers=HA)
check("чужую возможность не открыть", r.status_code == 404, r.status_code)
r = c.post("/api/leads/%d/status" % lidB, json={"status": "lost",
                                                "lost_reason": "price"}, headers=HA)
check("и чужую не закрыть", r.status_code == 404, r.status_code)
check("она от этого не изменилась",
      database.get_lead(lidB, bidB)["status"] == leads.WON)


botcore.send_text = _real_send
print(f"\nИТОГО: успешно {ok}, провалено {fail}")
