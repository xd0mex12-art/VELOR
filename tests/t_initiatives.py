# -*- coding: utf-8 -*-
"""
VELOR ЗАМЕЧАЕТ САМ — находки, доказательства и путь до действия.

Что здесь проверяется — обещания, а не строки кода:
  • каждый из семи детекторов находит то, ради чего заведён;
  • мало данных — молчим, и это не сбой, а решение;
  • одна проблема — одна запись, сколько бы обходов ни прошло;
  • отклонённое не возвращается, пока картина не изменится существенно;
  • исчезнувшая проблема закрывается сама, с результатом;
  • у каждой находки есть проверяемое основание, а догадка подписана догадкой;
  • оценка суммы названа оценкой, а не выручкой;
  • важность зависит от времени и денег, а не от порядка обнаружения;
  • находка не даёт прав: каждое действие идёт через полномочия;
  • чужая компания не видна, не управляема и не попадает в основания.
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
import server, database, actions, initiatives, leads, followup, qualify, botcore, ai

c = TestClient(server.app)
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


def reg(login):
    r = c.post("/api/register", json={"name": login, "login": login,
                                      "password": "pass123", "consent": True})
    d = r.json()
    bid = d["business_id"]
    database.update_business(bid, tg_bot_token="1234:TESTTOKEN-" + login)
    return bid, {"X-Auth": d["token"]}


def ago(hours=0, days=0):
    return (datetime.datetime.utcnow()
            - datetime.timedelta(hours=hours, days=days)).strftime("%Y-%m-%d %H:%M:%S")


def day_ago(days):
    return (datetime.datetime.utcnow() - datetime.timedelta(days=days)).date().isoformat()


ASKS = "Хочу заказать букет, можно сегодня? Сколько стоит?"


def hot(bid, tg, *, hours=5, said=ASKS, answer=None):
    """Клиент написал сам и говорит о покупке. Ответили или нет — по желанию."""
    cl = database.get_or_create_client(bid, tg_user_id=tg, name="Клиент %d" % tg)
    mid = database.save_message(bid, cl["id"], "user", said, channel="telegram",
                                created_at=ago(hours=hours))
    lid = leads.from_message(bid, cl, said, source="telegram", channel="telegram",
                             message_id=mid)
    if answer:
        database.save_message(bid, cl["id"], "assistant", answer, channel="telegram",
                              created_at=ago(hours=max(0, hours - 1)))
    return lid, cl["id"], mid


def catalog(bid):
    database.add_fact(bid, "service", "Букет на заказ", "от 3500 ₽, сборка 40 минут")


def work_history(bid, client_id, n=12):
    """Известные рабочие часы: иначе автоматическая отправка не пойдёт вовсе."""
    for i in range(n):
        hour = int(i * 23 / max(1, n - 1))
        database.save_message(bid, client_id, "assistant", "старый ответ %d" % i,
                              channel="telegram",
                              created_at="2020-01-0%d %02d:00:00" % (1 + i % 9, hour))


def backdate_order(bid, order_id, days):
    """Заказ в прошлом. Своей двери «создай заказ вчера» у системы нет и не
    должно быть — историю здесь строим руками, как её строило бы время."""
    with database._connect() as conn:
        conn.execute("UPDATE orders SET created_at = ? WHERE id = ? AND business_id = ?",
                     (ago(days=days), order_id, bid))


def closed_leads(bid, *, won, lost, days_ago, reason="price"):
    """Столько-то закрытых возможностей в прошлом — для сравнения периодов."""
    made = []
    for i in range(won):
        lid = leads.create(bid, title="Сделка %d" % i, source="manual")
        database.update_lead(lid, bid, status="won", converted_at=ago(days=days_ago))
        made.append(lid)
    for i in range(lost):
        lid = leads.create(bid, title="Потеря %d" % i, source="manual")
        database.update_lead(lid, bid, status="lost", lost_at=ago(days=days_ago),
                             lost_reason=reason)
        made.append(lid)
    return made


# ── подставной канал ───────────────────────────────────────────────────────
SENT = []
_real_send = botcore.send_text


def fake_send(bid, tg_user_id, text):
    SENT.append({"bid": bid, "to": str(tg_user_id), "text": text})
    return True, ""


botcore.send_text = fake_send

# ── подставной ИИ ──────────────────────────────────────────────────────────
# Дорогую часть подменяем целиком и считаем вызовы: обещание «дешёвые правила
# не зовут модель» проверяется только счётчиком, иначе оно ничем не обеспечено.
CALLS = {"n": 0}
_real_avail = ai.ai_available


def fake_available():
    return True


def fake_hypothesis(business, facts_text):
    CALLS["n"] += 1
    return "Возможно, дело в том, что клиентам стали дольше отвечать."


ai.ai_available = fake_available
ai.initiative_hypothesis = fake_hypothesis


# ═══ 1. РЕЕСТР НАХОДОК ═════════════════════════════════════════════════════
print("\n== 1. РЕЕСТР ==")

check("реестр не пуст и не разросся", 5 <= len(initiatives.TYPES) <= 10,
      len(initiatives.TYPES))
check("у каждого вида есть название", all(m.get("title") for m in initiatives.TYPES.values()))
check("у каждого вида есть место, куда посмотреть",
      all(m.get("href") for m in initiatives.TYPES.values()))
check("предлагаемое действие есть в реестре действий",
      all(m["action"] in actions.REGISTRY for m in initiatives.TYPES.values()
          if m.get("action")),
      [m["action"] for m in initiatives.TYPES.values() if m.get("action")])
check("массовых рассылок среди предложений нет",
      not any((m.get("action") or "").startswith("send_campaign")
              for m in initiatives.TYPES.values()))
check("важность — та же шкала, что у возможностей",
      initiatives.PRIORITY_ORDER is qualify.PRIORITY_ORDER)
check("каждый вид знает, требует он решения или это наблюдение",
      all(initiatives.LEVEL_RU.get(lvl) for lvl in (initiatives.WATCH, initiatives.ACT)))
check("уверенность описана словами, а не процентами",
      all("%" not in v for v in initiatives.CONFIDENCE_RU.values()))
check("пороги взяты у Директора, а не выдуманы заново",
      initiatives.MIN_FIN_BASE == 1000 and initiatives.RETURN_FROM_DAYS == 30,
      (initiatives.MIN_FIN_BASE, initiatives.RETURN_FROM_DAYS))


# ═══ 2. ГОРЯЧИЕ КЛИЕНТЫ БЕЗ ОТВЕТА ═════════════════════════════════════════
print("\n== 2. ГОРЯЧИЕ КЛИЕНТЫ БЕЗ ОТВЕТА ==")
bidA, HA = reg("in_a")
catalog(bidA)
for i in range(3):
    hot(bidA, 2000 + i, hours=5)

got = initiatives.scan(bidA)
rows = initiatives.feed(bidA)
one = next((r for r in rows if r["type"] == initiatives.HOT_LEADS_UNANSWERED), None)
check("находка появилась", bool(one), [r["type"] for r in rows])
check("в заголовке настоящее число", one and "3" in one["title"], one and one["title"])
check("написано, сколько ждут дольше всех",
      one and "5 час" in one["summary"], one and one["summary"])
check("сказано, почему это важно", one and len(one["why"]) > 30)
check("это требует решения, а не наблюдения", one["level"] == initiatives.ACT)
check("важность высокая", one["priority"] in (initiatives.HIGH, initiatives.URGENT),
      one["priority"])
check("предлагается подготовить, что написать",
      one["action"] == "prepare_followup", one["action"])

# Ответили — повода нет.
bidA2, _ = reg("in_a2")
catalog(bidA2)
hot(bidA2, 2100, hours=5, answer="Здравствуйте! Сейчас посчитаю.")
initiatives.scan(bidA2)
check("ответившему бизнесу нечего сообщать",
      not any(r["type"] == initiatives.HOT_LEADS_UNANSWERED
              for r in initiatives.feed(bidA2)))

# Написал полчаса назад — это ещё не «молчим».
bidA3, _ = reg("in_a3")
catalog(bidA3)
hot(bidA3, 2200, hours=0)
initiatives.scan(bidA3)
check("свежий вопрос находкой не считается",
      not initiatives.feed(bidA3), [r["title"] for r in initiatives.feed(bidA3)])


# ═══ 3. ГОТОВЫЕ СООБЩЕНИЯ, КОТОРЫЕ НЕ УШЛИ ═════════════════════════════════
print("\n== 3. ЗАВАЛ ИЗ ЧЕРНОВИКОВ ==")
bidB, HB = reg("in_b")
catalog(bidB)
made = []
for i in range(3):
    lid, cid, _ = hot(bidB, 2300 + i, hours=30)
    work_history(bidB, cid)
    row = followup.plan(bidB, database.get_lead(lid, bidB))
    if row:
        # Время написать давно прошло: черновик лежит, а не ждёт своего часа.
        database.update_followup(row["id"], bidB, recommended_at=ago(hours=20),
                                 status=database.FU_DRAFT)
        made.append(row["id"])
check("черновики подготовлены", len(made) >= 2, made)

initiatives.scan(bidB)
back = next((r for r in initiatives.feed(bidB)
             if r["type"] == initiatives.FOLLOWUP_BACKLOG), None)
check("завал замечен", bool(back), [r["type"] for r in initiatives.feed(bidB)])
check("в основании — сами сообщения",
      back and back["evidence"]["kind"] == "followup" and len(back["evidence"]["ids"]) >= 2,
      back and back["evidence"])
check("предлагается отправить подготовленное",
      back and back["action"] == "send_followup")


# ═══ 4. ПОТЕРИ И КОНВЕРСИЯ ═════════════════════════════════════════════════
print("\n== 4. ПОТЕРИ И КОНВЕРСИЯ ==")
bidC, HC = reg("in_c")
closed_leads(bidC, won=1, lost=1, days_ago=20)          # предыдущие две недели
closed_leads(bidC, won=1, lost=4, days_ago=3)           # последние две недели
initiatives.scan(bidC)
lost = next((r for r in initiatives.feed(bidC)
             if r["type"] == initiatives.LOST_LEADS_CLUSTER), None)
check("рост потерь замечен", bool(lost), [r["type"] for r in initiatives.feed(bidC)])
check("названо, сколько потеряно", lost and "4" in lost["title"], lost and lost["title"])
check("названа частая причина",
      lost and "Дорого" in lost["summary"], lost and lost["summary"])
check("указан период сравнения",
      lost and "14 дней" in lost["evidence"]["window"], lost and lost["evidence"])
check("в основании — сами возможности",
      lost and lost["evidence"]["kind"] == "lead" and lost["evidence"]["ids"])

bidD, HD = reg("in_d")
closed_leads(bidD, won=6, lost=1, days_ago=20)          # было 86%
closed_leads(bidD, won=2, lost=6, days_ago=3)           # стало 25%
initiatives.scan(bidD)
drop = next((r for r in initiatives.feed(bidD)
             if r["type"] == initiatives.CONVERSION_DROP), None)
check("падение конверсии замечено", bool(drop), [r["type"] for r in initiatives.feed(bidD)])
check("названы обе доли", drop and "25%" in drop["title"] and "86%" in drop["title"],
      drop and drop["title"])
check("сказано, из скольких считано",
      drop and "8" in drop["summary"], drop and drop["summary"])

# Две сделки — это не конверсия.
bidD2, _ = reg("in_d2")
closed_leads(bidD2, won=1, lost=0, days_ago=20)
closed_leads(bidD2, won=0, lost=1, days_ago=3)
initiatives.scan(bidD2)
check("по двум сделкам конверсию не считаем",
      not any(r["type"] == initiatives.CONVERSION_DROP for r in initiatives.feed(bidD2)),
      [r["title"] for r in initiatives.feed(bidD2)])


# ═══ 5. ВОЗВРАТЫ, ДАННЫЕ И ДЕНЬГИ ══════════════════════════════════════════
print("\n== 5. ВОЗВРАТЫ, ДАННЫЕ, ДЕНЬГИ ==")
bidE, HE = reg("in_e")
for i in range(4):
    cl = database.get_or_create_client(bidE, tg_user_id=2400 + i, name="Постоянный %d" % i)
    oid = database.add_order(bidE, "Букет", client_id=cl["id"], amount=5000)
    backdate_order(bidE, oid, 60)
initiatives.scan(bidE)
ret = next((r for r in initiatives.feed(bidE)
            if r["type"] == initiatives.RETURNING_CLIENT), None)
check("ушедшие клиенты замечены", bool(ret), [r["type"] for r in initiatives.feed(bidE)])
check("это наблюдение, а не тревога", ret and ret["level"] == initiatives.WATCH,
      ret and ret["level"])
check("на экране это возможность", ret and ret["tag"] == "Возможность", ret and ret["tag"])
check("сумма подписана прошлым, а не обещанием",
      ret and "не обещание" in ret["impact_note"], ret and ret["impact_note"])
check("действия у неё нет — рассылку VELOR не предлагает",
      ret and not ret["can_act"])

# Клиенты, ушедшие два года назад, — это уже не «пауза».
bidE2, _ = reg("in_e2")
for i in range(4):
    cl = database.get_or_create_client(bidE2, tg_user_id=2500 + i, name="Давний %d" % i)
    oid = database.add_order(bidE2, "Букет", client_id=cl["id"], amount=5000)
    backdate_order(bidE2, oid, 700)
initiatives.scan(bidE2)
check("ушедших два года назад возвращать не предлагаем",
      not any(r["type"] == initiatives.RETURNING_CLIENT for r in initiatives.feed(bidE2)))

bidF, HF = reg("in_f")
database.add_fact(bidF, "service", "Доставка по городу", "300 ₽")
database.add_fact(bidF, "service", "Доставка  по  Городу", "500 ₽")
initiatives.scan(bidF)
conf = next((r for r in initiatives.feed(bidF)
             if r["type"] == initiatives.DATA_CONFLICT), None)
check("противоречие в памяти замечено", bool(conf),
      [r["type"] for r in initiatives.feed(bidF)])
check("показаны обе версии",
      conf and "300 ₽" in conf["summary"] and "500 ₽" in conf["summary"],
      conf and conf["summary"])
check("объяснено, чем это грозит клиенту",
      conf and "цену" in conf["why"], conf and conf["why"])
check("в основании — сами записи",
      conf and conf["evidence"]["kind"] == "fact" and len(conf["evidence"]["ids"]) == 2)

bidF2, _ = reg("in_f2")
database.add_fact(bidF2, "service", "Доставка", "300 ₽")
database.add_fact(bidF2, "service", "Доставка", "300 ₽")
initiatives.scan(bidF2)
check("одинаковые записи противоречием не считаются",
      not any(r["type"] == initiatives.DATA_CONFLICT for r in initiatives.feed(bidF2)))

bidG, HG = reg("in_g")
for i in range(4):
    database.add_finance_entry(bidG, "expense", "Закупка", 5000, op_date=day_ago(40))
    database.add_finance_entry(bidG, "income", "Продажа", 9000, op_date=day_ago(40))
for i in range(4):
    database.add_finance_entry(bidG, "expense", "Закупка", 12000, op_date=day_ago(5))
    database.add_finance_entry(bidG, "income", "Продажа", 9000, op_date=day_ago(5))
initiatives.scan(bidG)
fin = next((r for r in initiatives.feed(bidG)
            if r["type"] == initiatives.FINANCIAL_ANOMALY), None)
check("скачок расходов замечен", bool(fin), [r["type"] for r in initiatives.feed(bidG)])
check("названы обе суммы",
      fin and "48 000" in fin["summary"] and "20 000" in fin["summary"],
      fin and fin["summary"])
check("сказано, по скольким операциям", fin and "Операций" in fin["summary"])

# Рост с 300 до 600 ₽ — не событие.
bidG2, _ = reg("in_g2")
for i in range(4):
    database.add_finance_entry(bidG2, "expense", "Мелочь", 75, op_date=day_ago(40))
    database.add_finance_entry(bidG2, "expense", "Мелочь", 150, op_date=day_ago(5))
initiatives.scan(bidG2)
check("копеечный рост находкой не считается",
      not any(r["type"] == initiatives.FINANCIAL_ANOMALY for r in initiatives.feed(bidG2)),
      [r["title"] for r in initiatives.feed(bidG2)])


# ═══ 6. МОЛЧАНИЕ ПРИ НЕХВАТКЕ ДАННЫХ ═══════════════════════════════════════
print("\n== 6. КОГДА ДАННЫХ МАЛО ==")
bidH, HH = reg("in_h")
got = initiatives.scan(bidH)
check("у пустого бизнеса не ищем ничего", got["checked"] == 0, got)
check("и не выдумываем находок", not initiatives.feed(bidH))
check("сводка честно пуста", initiatives.overview(bidH)["open"] == 0)

bidH2, _ = reg("in_h2")
closed_leads(bidH2, won=1, lost=2, days_ago=3)
initiatives.scan(bidH2)
check("две потери подряд ещё не рост",
      not any(r["type"] == initiatives.LOST_LEADS_CLUSTER
              for r in initiatives.feed(bidH2)),
      [r["title"] for r in initiatives.feed(bidH2)])


# ═══ 7. ОДИН ПОВОД — ОДНА ЗАПИСЬ ═══════════════════════════════════════════
print("\n== 7. БЕЗ ПОВТОРОВ ==")
before = database.count_initiatives(bidA, live=True)
for _ in range(5):
    initiatives.scan(bidA)
check("пять обходов подряд не плодят записей",
      database.count_initiatives(bidA, live=True) == before,
      (before, database.count_initiatives(bidA, live=True)))
check("живая запись по этому поводу ровно одна",
      len(database.list_initiatives(bidA, kind=initiatives.HOT_LEADS_UNANSWERED,
                                    live=True, limit=10)) == 1)

# Проблема выросла — та же запись обновляется и снова попадает на глаза.
row0 = database.list_initiatives(bidA, kind=initiatives.HOT_LEADS_UNANSWERED,
                                 live=True)[0]
initiatives.seen(bidA, row0["id"])
for i in range(4):
    hot(bidA, 2600 + i, hours=6)
initiatives.scan(bidA)
grown = database.get_initiative(row0["id"], bidA)
check("выросшая проблема не заводит вторую запись",
      len(database.list_initiatives(bidA, kind=initiatives.HOT_LEADS_UNANSWERED,
                                    live=True, limit=10)) == 1)
check("та же запись обновилась числами", "7" in grown["title"], grown["title"])
check("и снова стала новостью", grown["status"] == database.IN_NEW, grown["status"])
check("отпечаток повода изменился",
      grown["fingerprint"] != row0["fingerprint"],
      (row0["fingerprint"], grown["fingerprint"]))


# ═══ 8. РЕШЕНИЯ ВЛАДЕЛЬЦА ══════════════════════════════════════════════════
print("\n== 8. ЧТО ОТВЕЧАЕТ ВЛАДЕЛЕЦ ==")
bidI, HI = reg("in_i")
catalog(bidI)
for i in range(3):
    hot(bidI, 2700 + i, hours=5)
initiatives.scan(bidI)
one = initiatives.feed(bidI)[0]

initiatives.acknowledge(bidI, one["id"])
after = database.get_initiative(one["id"], bidI)
check("«понял» записано", after["status"] == database.IN_ACKNOWLEDGED)
check("но проблема осталась живой",
      after["status"] in database.INITIATIVE_LIVE)
check("и в списке она всё ещё есть",
      any(r["id"] == one["id"] for r in initiatives.feed(bidI)))
check("время ответа записано", bool(after["decided_at"]))

initiatives.scan(bidI)
check("после «понял» обход не заводит вторую",
      len(database.list_initiatives(bidI, kind=initiatives.HOT_LEADS_UNANSWERED,
                                    live=True, limit=10)) == 1)

initiatives.dismiss(bidI, one["id"])
gone = database.get_initiative(one["id"], bidI)
check("«не интересно» закрывает находку", gone["status"] == database.IN_DISMISSED)
check("и объясняет, почему она закрыта", bool(gone["outcome"]), gone["outcome"])
initiatives.scan(bidI)
check("отклонённое не возвращается на следующем же обходе",
      database.count_initiatives(bidI, live=True) == 0,
      [r["title"] for r in initiatives.feed(bidI)])

# Картина изменилась существенно — молчать больше нельзя.
for i in range(8):
    hot(bidI, 2800 + i, hours=7)
initiatives.scan(bidI)
back2 = initiatives.feed(bidI)
check("выросшая вдвое проблема прорывается через отказ",
      any(r["type"] == initiatives.HOT_LEADS_UNANSWERED for r in back2),
      [r["title"] for r in back2])


# ═══ 9. ПРОБЛЕМА ИСЧЕЗЛА ═══════════════════════════════════════════════════
print("\n== 9. КОГДА ПРОБЛЕМА РЕШИЛАСЬ ==")
bidJ, HJ = reg("in_j")
catalog(bidJ)
kids = [hot(bidJ, 2900 + i, hours=5) for i in range(3)]
initiatives.scan(bidJ)
alive = initiatives.feed(bidJ)[0]
check("находка есть", alive["type"] == initiatives.HOT_LEADS_UNANSWERED)

# Бизнес ответил всем.
for lid, cid, _ in kids:
    database.save_message(bidJ, cid, "assistant", "Здравствуйте! Отвечаю.",
                          channel="telegram")
got = initiatives.scan(bidJ)
closed = database.get_initiative(alive["id"], bidJ)
check("находка закрылась сама", closed["status"] == database.IN_RESOLVED, closed["status"])
check("и записала, что изменилось", "Было 3" in (closed["outcome"] or ""),
      closed["outcome"])
check("обход это посчитал", got["resolved"] == 1, got)
check("в открытом списке её больше нет",
      not any(r["id"] == alive["id"] for r in initiatives.feed(bidJ)))
check("но в полном списке она сохранилась",
      any(r["id"] == alive["id"] for r in initiatives.feed(bidJ, only_open=False)))

# Возможность выиграна — повод тоже исчезает.
bidJ2, _ = reg("in_j2")
catalog(bidJ2)
won_lead, won_cid, _ = hot(bidJ2, 2950, hours=5)
hot(bidJ2, 2951, hours=5)
initiatives.scan(bidJ2)
check("находка о двоих есть", database.count_initiatives(bidJ2, live=True) == 1)
database.update_lead(won_lead, bidJ2, status="won", converted_at=database.now())
initiatives.scan(bidJ2)
still = database.list_initiatives(bidJ2, kind=initiatives.HOT_LEADS_UNANSWERED,
                                  live=True)
check("выигранная возможность из основания ушла",
      still and still[0]["evidence"]["numbers"]["count"] == 1,
      still and still[0]["evidence"])

# Забытая находка не висит вечно.
old = database.list_initiatives(bidJ2, live=True)[0]
database.update_initiative(old["id"], bidJ2, last_seen_at=ago(days=30))
gone_n = initiatives.settle(bidJ2)
check("находка, которую перестали подтверждать, закрывается",
      gone_n == 1 and database.get_initiative(old["id"], bidJ2)["status"]
      == database.IN_EXPIRED, gone_n)
check("и написано, почему",
      "не подтверждался" in (database.get_initiative(old["id"], bidJ2)["outcome"] or ""))


# ═══ 10. ОСНОВАНИЯ И ДОГАДКИ ═══════════════════════════════════════════════
print("\n== 10. НА ЧЁМ ЭТО СТОИТ ==")
one = initiatives.feed(bidA)[0]
check("основание названо словами", bool(one["evidence_ru"]), one["evidence_ru"])
check("в основании есть номера записей", "№" in one["evidence_ru"])
check("указан период", "Период" in one["evidence_ru"])
check("уверенность словом, а не процентом",
      one["confidence_ru"] and "%" not in one["confidence_ru"], one["confidence_ru"])
check("основание — ссылки, а не копии переписки",
      set(one["evidence"].keys()) == {"kind", "ids", "window", "numbers"},
      one["evidence"])
ids = one["evidence"]["ids"]
check("номера ведут на настоящие возможности этого бизнеса",
      all(database.get_lead(i, bidA) for i in ids), ids)

check("оценка суммы подписана оценкой",
      one["impact"] and "оценка" in one["impact_note"].lower(), one["impact_note"])
check("сумма не выдаётся за выручку",
      "не выручка" in one["impact_note"], one["impact_note"])
check("сумма равна сумме возможностей",
      one["impact"] == sum(int(database.get_lead(i, bidA).get("value")
                               or database.get_lead(i, bidA).get("estimated_value") or 0)
                           for i in ids),
      one["impact"])

# Догадка — отдельно от факта.
drop_row = database.list_initiatives(bidD, kind=initiatives.CONVERSION_DROP,
                                     live=True)[0]
check("догадка сохранена отдельным полем", bool(drop_row["hypothesis"]),
      drop_row["hypothesis"])
pub = initiatives.public(drop_row)
check("на экране она подписана догадкой",
      "догадка" in pub["hypothesis_note"], pub["hypothesis_note"])
check("в посчитанное она не подмешана",
      pub["hypothesis"] not in (pub["title"] + pub["summary"] + pub["why"]))
check("а у горячих клиентов догадки нет — причина и так известна",
      not initiatives.feed(bidA)[0]["hypothesis"])


# ═══ 11. ВАЖНОСТЬ ══════════════════════════════════════════════════════════
print("\n== 11. ЧТО ВАЖНЕЕ ==")
bidK, _ = reg("in_k")
catalog(bidK)
hot(bidK, 3000, hours=4)
initiatives.scan(bidK)
mild = initiatives.feed(bidK)[0]
check("один клиент, четыре часа — важно, но не срочно",
      mild["priority"] == initiatives.HIGH, mild["priority"])

bidL, _ = reg("in_l")
catalog(bidL)
hot(bidL, 3100, hours=15)
initiatives.scan(bidL)
sharp = initiatives.feed(bidL)[0]
check("тот же один клиент, пятнадцать часов — срочно",
      sharp["priority"] == initiatives.URGENT, sharp["priority"])
check("срочность видна словом", sharp["tag"] == "Срочно", sharp["tag"])

check("наблюдение не выдаёт себя за срочное",
      initiatives.public(database.list_initiatives(
          bidE, kind=initiatives.RETURNING_CLIENT, live=True)[0])["tag"] == "Возможность")

thin = database.list_initiatives(bidD, kind=initiatives.CONVERSION_DROP, live=True)[0]
check("на восьми сделках уверенность осторожная",
      thin["confidence"] == initiatives.LIKELY, thin["confidence"])
bidD3, _ = reg("in_d3")
closed_leads(bidD3, won=12, lost=2, days_ago=20)         # было 86%
closed_leads(bidD3, won=4, lost=12, days_ago=3)          # стало 25%
initiatives.scan(bidD3)
thick = database.list_initiatives(bidD3, kind=initiatives.CONVERSION_DROP, live=True)[0]
check("на шестнадцати — уверенная",
      thick["confidence"] == initiatives.SOLID, thick["confidence"])

# Порядок на экране — по цене вопроса, а не по времени находки.
bidM, HM = reg("in_m")
catalog(bidM)
for i in range(4):
    cl = database.get_or_create_client(bidM, tg_user_id=3200 + i, name="Возврат %d" % i)
    oid = database.add_order(bidM, "Букет", client_id=cl["id"], amount=5000)
    backdate_order(bidM, oid, 60)
hot(bidM, 3300, hours=15)
initiatives.scan(bidM)
order = [r["type"] for r in initiatives.feed(bidM)]
check("срочное стоит выше наблюдения",
      order.index(initiatives.HOT_LEADS_UNANSWERED) < order.index(initiatives.RETURNING_CLIENT),
      order)


# ═══ 12. ОТ НАХОДКИ К ДЕЙСТВИЮ ═════════════════════════════════════════════
print("\n== 12. НАХОДКА → ДЕЙСТВИЕ ==")
bidN, HN = reg("in_n")
catalog(bidN)
leads_n = [hot(bidN, 3400 + i, hours=5)[0] for i in range(3)]
initiatives.scan(bidN)
found = initiatives.feed(bidN)[0]
was_actions = database.count_actions(bidN)
res = initiatives.act(bidN, found["id"])
check("из одной находки выросло несколько действий",
      len(res["actions"]) == 3, res["actions"])
check("и это записи журнала действий",
      database.count_actions(bidN) > was_actions)
check("находка отмечена как отработанная",
      database.get_initiative(found["id"], bidN)["status"] == database.IN_ACTED)
check("связь с действиями сохранена",
      sorted(database.get_initiative(found["id"], bidN)["actions_ids"]) ==
      sorted(res["actions"]))

made_rows = [database.get_action(i, bidN) for i in res["actions"]]
check("у каждого действия — конкретный объект",
      all(r["target_type"] == "lead" and r["target_id"] in leads_n for r in made_rows),
      [(r["target_type"], r["target_id"]) for r in made_rows])
check("у каждого — человеческая причина",
      all(found["title"] in (r["reason"] or "") for r in made_rows))
check("и ссылка на находку, из которой оно выросло",
      all(r["based_on"].get("initiative_id") == found["id"] for r in made_rows),
      [r["based_on"] for r in made_rows])
check("действие разрешено и выполнено",
      all(r["status"] == database.AC_SUCCEEDED for r in made_rows),
      [(r["status"], r["error"]) for r in made_rows])
check("черновики действительно появились",
      len(database.list_followups(bidN, live=True, limit=20)) >= 3)

# Повторное нажатие не удваивает работу.
res2 = initiatives.act(bidN, found["id"])
live_after = [r for r in [database.get_action(i, bidN) for i in res2["actions"]]
              if r["status"] not in (database.AC_CANCELLED, database.AC_STALE)]
check("повторное действие не создаёт вторых черновиков",
      len(database.list_followups(bidN, live=True, limit=20)) <= 4,
      len(database.list_followups(bidN, live=True, limit=20)))
check("и повтор объяснён, а не выполнен",
      all(r["status"] != database.AC_SUCCEEDED for r in live_after)
      or not live_after, [(r["status"], r["error"]) for r in live_after])

# Черновики уже готовы — предлагать подготовить их снова нечестно.
initiatives.scan(bidN)
again = database.list_initiatives(bidN, kind=initiatives.HOT_LEADS_UNANSWERED,
                                  live=True)[0]
check("когда черновики готовы, кнопка «подготовить» пропадает",
      not initiatives.public(again)["can_act"], again["action"])
check("и владельцу сказано, что осталось",
      "отправить" in (again["action_note"] or ""), again["action_note"])
check("но сама проблема никуда не делась — клиенты всё ещё ждут",
      again["status"] in database.INITIATIVE_LIVE and again["evidence"]["numbers"]["count"] == 3,
      again["evidence"])


# ═══ 13. НАХОДКА НЕ ДАЁТ ПРАВ ══════════════════════════════════════════════
print("\n== 13. ПОЛНОМОЧИЯ ВЫШЕ НАХОДКИ ==")
bidO, HO = reg("in_o")
catalog(bidO)
actions.set_mode(bidO, "prepare_followup", actions.DENY)
hot(bidO, 3500, hours=5)
hot(bidO, 3501, hours=5)
initiatives.scan(bidO)
fnd = initiatives.feed(bidO)[0]
res = initiatives.act(bidO, fnd["id"])
rows_o = [database.get_action(i, bidO) for i in res["actions"]]
check("запрещённое не выполняется",
      all(r["status"] == database.AC_BLOCKED for r in rows_o),
      [r["status"] for r in rows_o])
check("и записано, почему",
      all("запретили" in (r["error"] or "") for r in rows_o),
      [r["error"] for r in rows_o])
check("но сама находка не сломалась — она видна",
      database.get_initiative(fnd["id"], bidO)["status"] == database.IN_ACTED)
check("владельцу сказано, что не разрешено", res["decisions"]["deny"] == 2,
      res["decisions"])

# То же действие, но «спрашивать» — попадает в очередь, а не выполняется.
bidP, HP = reg("in_p")
catalog(bidP)
for i in range(3):
    lid, cid, _ = hot(bidP, 3600 + i, hours=30)
    work_history(bidP, cid)
    row = followup.plan(bidP, database.get_lead(lid, bidP))
    if row:
        database.update_followup(row["id"], bidP, recommended_at=ago(hours=20),
                                 status=database.FU_DRAFT)
actions.set_mode(bidP, "send_followup", actions.APPROVAL)
initiatives.scan(bidP)
back = next(r for r in initiatives.feed(bidP)
            if r["type"] == initiatives.FOLLOWUP_BACKLOG)
sent_before = len(SENT)
res = initiatives.act(bidP, back["id"])
rows_p = [database.get_action(i, bidP) for i in res["actions"]]
check("действие встало в очередь, а не ушло",
      all(r["status"] == database.AC_PROPOSED for r in rows_p),
      [r["status"] for r in rows_p])
check("клиентам ничего не отправлено", len(SENT) == sent_before)
check("это видно в очереди подтверждений",
      len(actions.pending(bidP)) >= len(rows_p))

# Владелец разрешил одно из них — оно уходит.
first = rows_p[0]
actions.approve(bidP, first["id"])
check("подтверждённое ушло клиенту", len(SENT) > sent_before, SENT[-1:])
check("и отмечено выполненным",
      database.get_action(first["id"], bidP)["status"] == database.AC_SUCCEEDED,
      database.get_action(first["id"], bidP)["error"])

# Владелец отказал — действие закрыто, находка при этом цела.
second = rows_p[1]
actions.reject(bidP, second["id"])
check("отказ записан",
      database.get_action(second["id"], bidP)["status"] == database.AC_CANCELLED)
check("находка от отказа не изменилась",
      database.get_initiative(back["id"], bidP)["status"] == database.IN_ACTED)

# Устаревшее предложение не выполняется никогда.
third = rows_p[2]
fu_id = int((third["payload"] or {}).get("followup_id"))
database.update_followup(fu_id, bidP, status=database.FU_SENT)
actions.approve(bidP, third["id"])
check("устаревшее предложение не выполняется",
      database.get_action(third["id"], bidP)["status"] == database.AC_STALE,
      database.get_action(third["id"], bidP)["status"])

# У находки без действия кнопки нет и быть не должно.
try:
    initiatives.act(bidE, database.list_initiatives(
        bidE, kind=initiatives.RETURNING_CLIENT, live=True)[0]["id"])
    no_act = False
    why = ""
except initiatives.InitiativeError as e:
    no_act, why = True, str(e)
check("по наблюдению действовать нечем", no_act, why)
check("и об этом сказано словами", "нет действия" in why, why)


# ═══ 14. ОБНАРУЖЕНИЕ — НЕ ДЕЙСТВИЕ ═════════════════════════════════════════
print("\n== 14. ЗАМЕТИЛ ≠ СДЕЛАЛ ==")
bidQ, HQ = reg("in_q")
catalog(bidQ)
for i in range(3):
    hot(bidQ, 3700 + i, hours=5)
before_actions = database.count_actions(bidQ)
initiatives.scan(bidQ)
check("обнаружение не пишется в журнал действий",
      database.count_actions(bidQ) == before_actions,
      (before_actions, database.count_actions(bidQ)))
check("но остаётся в истории компании",
      any("заметил" in (e.get("title") or "")
          for e in database.list_events(bidQ, limit=20)),
      [e.get("title") for e in database.list_events(bidQ, limit=5)])
check("и в журнале действий нет записи «обнаружил»",
      not any("заметил" in (a["title"] or "") for a in actions.feed(bidQ, limit=20)))


# ═══ 15. ДЕНЬГИ НА ИИ ══════════════════════════════════════════════════════
print("\n== 15. КОГДА ЗОВЁМ МОДЕЛЬ ==")
CALLS["n"] = 0
bidR, _ = reg("in_r")
catalog(bidR)
for i in range(3):
    hot(bidR, 3800 + i, hours=5)
initiatives.scan(bidR)
check("дешёвое правило модель не зовёт", CALLS["n"] == 0, CALLS["n"])
check("находка при этом найдена", bool(initiatives.feed(bidR)))

CALLS["n"] = 0
bidS, _ = reg("in_s")
closed_leads(bidS, won=6, lost=1, days_ago=20)
closed_leads(bidS, won=2, lost=6, days_ago=3)
initiatives.scan(bidS)
check("модель зовётся только там, где «почему» не посчитать",
      CALLS["n"] >= 1, CALLS["n"])
was = CALLS["n"]
initiatives.scan(bidS)
initiatives.scan(bidS)
check("и только один раз на находку — повторные обходы не платят",
      CALLS["n"] == was, (was, CALLS["n"]))

CALLS["n"] = 0
initiatives.scan(bidR, use_ai=False)
check("обход умеет обойтись совсем без модели", CALLS["n"] == 0)


# ═══ 16. КАБИНЕТ ═══════════════════════════════════════════════════════════
print("\n== 16. ЧТО ВИДИТ ВЛАДЕЛЕЦ ==")
r = c.get("/api/initiatives", headers=HN)
d = r.json()
check("список отдаётся", r.status_code == 200 and d["items"], r.status_code)
check("в сводке есть счётчик", "open" in d["overview"])
check("показ отмечен — новостью это больше не считается",
      all(not i["fresh"] for i in c.get("/api/initiatives", headers=HN).json()["items"]))

one_id = d["items"][0]["id"]
r = c.get("/api/initiatives/%d" % one_id, headers=HN)
check("одна находка открывается целиком", r.status_code == 200
      and r.json()["initiative"]["evidence"]["ids"])

r = c.post("/api/initiatives/scan", json={}, headers=HA)
check("обход по кнопке работает", r.status_code == 200 and r.json()["ok"])

r = c.get("/api/home", headers=HN)
home = r.json()
check("главная знает про находки", home["initiatives"]["open"] >= 1,
      home.get("initiatives"))
titles = [i["title"] for i in home["attention"]]
check("и они попали в «что требует внимания»",
      any("ждут ответа" in t for t in titles), titles)
notes = [i["note"] for i in home["attention"] if "ждут ответа" in i["title"]]
check("со словом «потенциальная», а не «потеряно»",
      notes and "Потенциальная ценность" in notes[0], notes)

bidT, HT = reg("in_t")
r = c.get("/api/home", headers=HT)
check("у пустого бизнеса находок в списке внимания нет",
      not any("заметил" in i["title"] for i in r.json()["attention"]))

r = c.post("/api/initiatives/%d/ack" % one_id, json={}, headers=HN)
check("«понял» через кабинет работает", r.status_code == 200
      and r.json()["initiative"]["status"] == database.IN_ACKNOWLEDGED)
r = c.post("/api/initiatives/%d/dismiss" % one_id, json={}, headers=HN)
check("«не интересно» через кабинет работает", r.status_code == 200
      and r.json()["initiative"]["status"] == database.IN_DISMISSED)


# ═══ 17. ЧУЖОЕ ═════════════════════════════════════════════════════════════
print("\n== 17. ЧУЖАЯ КОМПАНИЯ ==")
mine = database.list_initiatives(bidB, live=True)[0]
check("в своём списке чужих находок нет",
      all(database.get_initiative(r["id"], bidB) for r in
          database.list_initiatives(bidB, live=True, limit=50)))
check("чужая находка по id не читается",
      database.get_initiative(mine["id"], bidA) is None)

r = c.get("/api/initiatives")
check("без ключа список не отдаётся", r.status_code in (401, 403), r.status_code)
r = c.get("/api/initiatives/%d" % mine["id"], headers=HA)
check("чужую находку кабинет не показывает", r.status_code == 404, r.status_code)
r = c.post("/api/initiatives/%d/dismiss" % mine["id"], json={}, headers=HA)
check("и закрыть её нельзя", r.status_code == 404, r.status_code)
r = c.post("/api/initiatives/%d/act" % mine["id"], json={}, headers=HA)
check("и действовать по ней нельзя", r.status_code in (400, 404), r.status_code)
check("чужая находка от этого не изменилась",
      database.get_initiative(mine["id"], bidB)["status"] == mine["status"])

check("в основаниях только свои записи",
      all(all(database.get_lead(i, bidA) for i in r["evidence"].get("ids") or [])
          for r in database.list_initiatives(bidA, kind=initiatives.HOT_LEADS_UNANSWERED,
                                             live=True)))
database.add_fact(bidT, "service", "Доставка", "300 ₽")
database.add_fact(bidT, "service", "Доставка", "900 ₽")
initiatives.scan(bidT)
check("у компании перед удалением находки были",
      database.count_initiatives(bidT) >= 1, database.count_initiatives(bidT))
database.delete_business(bidT)
check("удаление компании уносит её находки",
      database.count_initiatives(bidT) == 0)


ai.ai_available = _real_avail
botcore.send_text = _real_send
print(f"\nИТОГО: успешно {ok}, провалено {fail}")
