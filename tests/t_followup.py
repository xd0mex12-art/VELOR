# -*- coding: utf-8 -*-
"""
FOLLOW-UP — удержание возможности, а не напоминание о себе.

Что здесь проверяется — обещания, а не строки кода:
  • касание существует, только если есть настоящее событие, и оно названо;
  • «клиент ждёт ответа» и «клиент не ответил» — разные ситуации;
  • черновик не считается отправленным ни на одном шаге;
  • по умолчанию VELOR готовит текст, а отправляет человек;
  • новое слово клиента отменяет старое расписание;
  • три касания и стоп — без явного нового события;
  • отказ, «не пишите» и закрытая возможность останавливают всё немедленно;
  • сообщение не содержит ни цены, ни срока, ни скидки, которых нет в памяти;
  • два запуска планировщика не отправляют одно и то же дважды;
  • сорвавшаяся отправка отправленной не считается;
  • цепочка «касание → ответ → заявка» сохраняется, а выводы — нет.
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
# Тестов много, и каждый начинается со своего бизнеса: антиспам
# регистраций тут мешает проверять поведение, а не защищает от него.
os.environ["REGISTER_MAX"] = "500"
sys.stdout.reconfigure(encoding="utf-8")
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, leads, qualify, followup, sales, botcore

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


def work_history(bid, client_id, n=12):
    """
    Двенадцать старых исходящих — чтобы рабочие часы бизнеса были ИЗВЕСТНЫ.

    Часового пояса в системе нет; окно считается по времени, в которое бизнес
    сам отвечает клиентам. Расставляем их по всем суткам, чтобы окно накрывало
    любой час, в который запустится этот тест.
    """
    for i in range(n):
        hour = int(i * 23 / max(1, n - 1))
        database.save_message(bid, client_id, "assistant", "старый ответ %d" % i,
                              channel="telegram",
                              created_at="2020-01-0%d %02d:00:00" % (1 + i % 9, hour))


def who(bid, name, tg):
    return database.get_or_create_client(bid, tg_user_id=tg, name=name)


def conversation(bid, client, said, *, at, answer=None, answer_at=None):
    """Клиент написал, бизнес (может быть) ответил. Возвращает id возможности."""
    mid = database.save_message(bid, client["id"], "user", said,
                                channel="telegram", created_at=at)
    lid = leads.from_message(bid, client, said, source="telegram",
                             channel="telegram", message_id=mid)
    if answer:
        database.save_message(bid, client["id"], "assistant", answer,
                              channel="telegram", created_at=answer_at or at)
    return lid, mid


def catalog(bid):
    database.add_fact(bid, "service", "Букет на заказ", "от 3500 ₽, сборка 40 минут")
    database.add_fact(bid, "product", "Роза красная", "120 ₽ за штуку")


# ── подставной канал ───────────────────────────────────────────────────────
# Настоящий Telegram в тестах недоступен, и врать про успешную отправку нельзя.
# Поэтому подменяем ТРАНСПОРТ (и только его), а проверяем настоящее: что стало
# с записью, появилось ли сообщение в переписке, изменилось ли состояние.

SENT = []
OUTAGE = {"on": False, "why": "Telegram не ответил"}
_real_send = botcore.send_text


def fake_send(bid, tg_user_id, text):
    if OUTAGE["on"]:
        return False, OUTAGE["why"]
    SENT.append({"bid": bid, "to": str(tg_user_id), "text": text})
    return True, ""


botcore.send_text = fake_send


print("\n== 1. ОТКУДА БЕРЁТСЯ ПОВОД ==")
bid, H = reg("fu1")
catalog(bid)
sales.set_policy(bid, "telegram", level=2)

# Клиент спросил — бизнес молчит четвёртый час.
cl_gap = who(bid, "Марина", 9101)
work_history(bid, cl_gap["id"])
lid_gap, mid_gap = conversation(bid, cl_gap, "Хочу заказать букет, можно сегодня?",
                                at=ago(hours=4))
lead = database.get_lead(lid_gap, bid)
d = followup.decide(bid, lead)
check("клиент ждёт ответа — это business gap",
      d and d.get("reason") == followup.BUSINESS_GAP, d)
check("и повод назван словами клиента, а не кодом",
      d and "ответа не было" in (d.get("trigger") or ""), d)
check("и опирается на конкретное сообщение",
      d and d["based_on"].get("last_in_id") == mid_gap, d)

# Бизнес ответил — молчит клиент, вторые сутки.
cl_silent = who(bid, "Пётр", 9102)
lid_sil, _ = conversation(bid, cl_silent, "Сколько стоит букет?",
                          at=ago(hours=30), answer="Подберём, расскажите повод",
                          answer_at=ago(hours=29))
d = followup.decide(bid, database.get_lead(lid_sil, bid))
check("бизнес ответил, клиент молчит — это уже другая причина",
      d and d.get("reason") == followup.CUSTOMER_NO_RESPONSE, d)
check("business gap и молчание клиента не перепутаны",
      d and d["reason"] != followup.BUSINESS_GAP, d)

# Мы назвали условия — решения нет.
cl_quote = who(bid, "Ольга", 9103)
lid_q, _ = conversation(bid, cl_quote, "Сколько стоит букет на 8 марта?",
                        at=ago(hours=30),
                        answer="Да, можем сделать за 3500 ₽. Оформляем?",
                        answer_at=ago(hours=29))
d = followup.decide(bid, database.get_lead(lid_q, bid))
check("после названной цены причина отдельная — ждём решения",
      d and d.get("reason") == followup.QUOTE_PENDING, d)

# Разговор оборвался месяц назад.
cl_old = who(bid, "Игорь", 9104)
lid_old, _ = conversation(bid, cl_old, "Хочу заказать букет к свадьбе",
                          at=ago(days=30), answer="Расскажите подробнее",
                          answer_at=ago(days=30))
d = followup.decide(bid, database.get_lead(lid_old, bid))
check("оборвавшийся разговор — возврат к возможности, а не «вы не ответили»",
      d and d.get("reason") == followup.RETURN_OPPORTUNITY, d)

# Клиент сам назвал дату, и она завтра.
cl_date = who(bid, "Анна", 9105)
tomorrow = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
lid_d, _ = conversation(bid, cl_date, "Нужен букет, сколько стоит?",
                        at=ago(hours=30), answer="Подберём", answer_at=ago(hours=29))
database.update_lead(lid_d, bid, wanted_at=tomorrow)
d = followup.decide(bid, database.get_lead(lid_d, bid))
check("названная клиентом дата подходит — это отдельный повод",
      d and d.get("reason") == followup.TIMING, d)
check("и в поводе стоит именно его дата",
      d and tomorrow in (d.get("trigger") or ""), d)

# Обычный разговор без повода.
cl_fresh = who(bid, "Сергей", 9106)
lid_fresh, _ = conversation(bid, cl_fresh, "Хочу заказать букет",
                            at=ago(hours=1), answer="Сейчас подберём",
                            answer_at=ago(minutes=0) if False else ago(hours=1))
d = followup.decide(bid, database.get_lead(lid_fresh, bid))
check("свежий разговор поводом не считается — писать не о чем", d is None, d)


print("\n== 2. КОГДА ==")
check("первое касание — не раньше суток", followup.DELAYS_H[0] >= 24, followup.DELAYS_H)
check("второе — через несколько дней", 48 <= followup.DELAYS_H[1] <= 96, followup.DELAYS_H)
check("третье — примерно через неделю", 120 <= followup.DELAYS_H[2] <= 192, followup.DELAYS_H)
check("больше трёх касаний не бывает", followup.MAX_ATTEMPTS == 3, followup.MAX_ATTEMPTS)

# Молчит меньше суток — рано.
cl_early = who(bid, "Юля", 9107)
lid_e, _ = conversation(bid, cl_early, "Сколько стоит букет?",
                        at=ago(hours=10), answer="Расскажите повод",
                        answer_at=ago(hours=9))
check("через девять часов молчания писать рано",
      followup.decide(bid, database.get_lead(lid_e, bid)) is None)

# Горячий клиент без ответа — внимание немедленно, а не через сутки.
d = followup.decide(bid, database.get_lead(lid_gap, bid))
check("когда ждёт клиент, ждать сутки не нужно",
      d and d["recommended_at"] <= followup._stamp(followup._now()), d)

row_gap = followup.plan(bid, database.get_lead(lid_gap, bid))
check("касание подготовлено", bool(row_gap), row_gap)
check("и это ЧЕРНОВИК, а не отправленное",
      row_gap["status"] == database.FU_DRAFT, row_gap["status"])
check("первое касание — первое по счёту", row_gap["attempt"] == 1, row_gap)

# Тихие часы: окно считается по исходящим бизнеса, а не выдумывается.
bid_q, _HQ = reg("fu-quiet")
cl_q = who(bid_q, "Ночной", 9201)
for i in range(12):
    database.save_message(bid_q, cl_q["id"], "assistant", "ответ",
                          channel="telegram", created_at="2020-01-01 %02d:00:00" % (9 + i % 8))
win = followup.work_hours(bid_q)
check("рабочие часы взяты из настоящих ответов бизнеса",
      win["known"] and win["from"] == 9 and win["to"] == 16, win)
noon = datetime.datetime(2024, 5, 5, 12, 0, 0)
night = datetime.datetime(2024, 5, 5, 3, 0, 0)
check("днём писать можно", followup.in_work_hours(bid_q, noon)[0])
check("ночью автоматически не пишем", not followup.in_work_hours(bid_q, night)[0])
check("ночное касание переносится на утро",
      followup.next_work_time(bid_q, night).hour == 9,
      followup.next_work_time(bid_q, night))
bid_nohist, _ = reg("fu-nohist")
check("нет истории ответов — часов не выдумываем",
      not followup.work_hours(bid_nohist)["known"],
      followup.work_hours(bid_nohist))
check("и автоматически в неизвестное время не пишем",
      not followup.in_work_hours(bid_nohist, noon)[0])


print("\n== 3. КОГДА ПИСАТЬ НЕЛЬЗЯ ==")
bid2, H2 = reg("fu2")
catalog(bid2)
cl = who(bid2, "Клиент", 9301)
work_history(bid2, cl["id"])

lid_won, _ = conversation(bid2, cl, "Хочу заказать букет", at=ago(hours=40),
                          answer="Оформили", answer_at=ago(hours=39))
leads.mark_won(bid2, lid_won)
check("купил — не пишем",
      followup.stop_reason(bid2, database.get_lead(lid_won, bid2)) == "won")

cl_l = who(bid2, "Второй", 9302)
lid_lost, _ = conversation(bid2, cl_l, "Хочу заказать букет", at=ago(hours=40),
                           answer="Хорошо", answer_at=ago(hours=39))
leads.mark_lost(bid2, lid_lost, reason="price")
check("возможность закрыта — не пишем",
      followup.stop_reason(bid2, database.get_lead(lid_lost, bid2)) == "lost")

cl_r = who(bid2, "Третий", 9303)
lid_ref, _ = conversation(bid2, cl_r, "Хочу заказать букет", at=ago(hours=40),
                          answer="Хорошо", answer_at=ago(hours=39))
conversation(bid2, cl_r, "Не нужно, передумал", at=ago(hours=38))
check("отказался — не пишем",
      followup.stop_reason(bid2, database.get_lead(lid_ref, bid2)) == "refused")

cl_dnc = who(bid2, "Четвёртый", 9304)
lid_dnc, _ = conversation(bid2, cl_dnc, "Хочу заказать букет", at=ago(hours=40),
                          answer="Хорошо", answer_at=ago(hours=39))
conversation(bid2, cl_dnc, "Больше не пишите мне, пожалуйста", at=ago(hours=38))
check("попросил не писать — не пишем",
      followup.stop_reason(bid2, database.get_lead(lid_dnc, bid2)) == "do_not_contact")
sig = database.get_lead(lid_dnc, bid2)["signals"]
check("«не пишите» — отдельный сигнал, а не просто отказ",
      any(s["key"] == "do_not_contact" for s in sig), sig)

# Снято с продажи.
bid3, H3 = reg("fu3")
catalog(bid3)
fid_srv = database.add_fact(bid3, "service", "Оформление зала", "от 25000 ₽")
cl_arch = who(bid3, "Пятый", 9305)
work_history(bid3, cl_arch["id"])
lid_arch, _ = conversation(bid3, cl_arch, "Хочу заказать оформление зала",
                           at=ago(hours=40), answer="Хорошо", answer_at=ago(hours=39))
database.set_archived("memory_facts", fid_srv, bid3, on=True)
check("то, что снято с продажи, не догоняем предложением",
      followup.stop_reason(bid3, database.get_lead(lid_arch, bid3)) == "offer_unavailable",
      followup.stop_reason(bid3, database.get_lead(lid_arch, bid3)))

# Три касания и стоп.
bid4, H4 = reg("fu4")
catalog(bid4)
cl4 = who(bid4, "Шестой", 9306)
work_history(bid4, cl4["id"])
lid4, _ = conversation(bid4, cl4, "Хочу заказать букет", at=ago(days=10),
                       answer="Расскажите повод", answer_at=ago(days=10))
for n in range(3):
    fid = database.add_followup(bid4, lid4, reason=followup.CUSTOMER_NO_RESPONSE,
                                channel="telegram", client_id=cl4["id"],
                                attempt=n + 1, status=database.FU_SENT,
                                message="текст")
    database.update_followup(fid, bid4, sent_at=ago(days=8 - n * 2))
check("после трёх касаний — стоп",
      followup.stop_reason(bid4, database.get_lead(lid4, bid4)) == "limit_reached")
check("и четвёртое не готовится",
      followup.plan(bid4, database.get_lead(lid4, bid4)) is None)

# Клиент написал сам — запланированное отменяется.
bid5, H5 = reg("fu5")
catalog(bid5)
cl5 = who(bid5, "Седьмой", 9307)
work_history(bid5, cl5["id"])
lid5, _ = conversation(bid5, cl5, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
row5 = followup.plan(bid5, database.get_lead(lid5, bid5))
check("касание запланировано", bool(row5), row5)
conversation(bid5, cl5, "Да, давайте оформим", at=ago(hours=1))
after = database.get_followup(row5["id"], bid5)
check("клиент ответил — запланированное отменено",
      after["status"] == database.FU_CANCELLED, after["status"])
check("и записано, почему именно",
      after["stop_reason"] == "new_conversation", after["stop_reason"])

# Владелец отменил.
bid6, H6 = reg("fu6")
catalog(bid6)
cl6 = who(bid6, "Восьмой", 9308)
work_history(bid6, cl6["id"])
lid6, _ = conversation(bid6, cl6, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
row6 = followup.plan(bid6, database.get_lead(lid6, bid6))
r = c.post(f"/api/followups/{row6['id']}/cancel", headers=H6,
           params={"business_id": bid6})
check("владелец может сказать «не писать»", r.status_code == 200, r.text)
check("и это записано как решение",
      database.get_followup(row6["id"], bid6)["stop_reason"] == "owner_cancelled")
check("и завтра то же самое не предлагается снова",
      followup.plan(bid6, database.get_lead(lid6, bid6)) is None,
      database.list_followups(bid6, lead_id=lid6, live=True))
check("запрет назван именно так",
      followup.stop_reason(bid6, database.get_lead(lid6, bid6)) == "owner_cancelled")
# Но клиент написал сам ПОСЛЕ отмены — и это уже другой разговор.
conversation(bid6, cl6, "Всё-таки хочу заказать букет", at=ago())
check("клиент заговорил — запрет снят",
      followup.stop_reason(bid6, database.get_lead(lid6, bid6)) is None,
      followup.stop_reason(bid6, database.get_lead(lid6, bid6)))

# Закрытие возможности отменяет очередь немедленно.
bid7, H7 = reg("fu7")
catalog(bid7)
cl7 = who(bid7, "Девятый", 9309)
work_history(bid7, cl7["id"])
lid7, _ = conversation(bid7, cl7, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
row7 = followup.plan(bid7, database.get_lead(lid7, bid7))
leads.mark_won(bid7, lid7)
check("возможность закрыта — очередь очищена сразу",
      database.get_followup(row7["id"], bid7)["status"] == database.FU_CANCELLED)


print("\n== 4. ЧТО НАПИСАНО ==")
bid8, H8 = reg("fu8")
catalog(bid8)
cl8 = who(bid8, "Марина Соколова", 9401)
work_history(bid8, cl8["id"])
lid8, _ = conversation(bid8, cl8, "Нужен букет пионов на 15 августа, сколько стоит?",
                       at=ago(hours=30), answer="Можем сделать", answer_at=ago(hours=29))
row8 = followup.plan(bid8, database.get_lead(lid8, bid8))
text = row8["message"]
check("сообщение обращается к человеку по имени", "Марина" in text, text)
check("и опирается на его собственные слова",
      "пион" in text.lower(), text)
check("а не на шаблон «напоминаем о себе»",
      "напоминаем о себе" not in text.lower(), text)
check("никакой выдуманной цены",
      not qualify.re.search(r"\d[\d\s]*(?:₽|руб)", text), text)
check("никакого выдуманного наличия", "в наличии" not in text.lower(), text)
check("никакой выдуманной скидки", "скидк" not in text.lower(), text)
check("никакого выдуманного «последнего шанса»",
      "последний шанс" not in text.lower() and "только сегодня" not in text.lower(), text)
check("и никакого выдавания себя за человека",
      not followup._FORBIDDEN_CLAIMS.search(text), text)
check("честно помечено, что текст собран без модели",
      row8["based_on"].get("draft_by") == "template", row8["based_on"])

# Догадка ИИ не становится фактом в сообщении.
database.update_lead(lid8, bid8, meta={"guess": {"value": 12000}})
row_g = followup.template(bid8, database.get_lead(lid8, bid8),
                          {"reason": followup.CUSTOMER_NO_RESPONSE})
check("догадка ИИ в сообщение не попадает", "12000" not in row_g, row_g)

# Провенанс.
pub = followup.public(row8)
check("владельцу видно, почему мы пишем", bool(pub["why"]), pub)
check("и на какое сообщение это опирается",
      any("сообщение №" in w for w in pub["why"]), pub["why"])
check("и какое это касание по счёту", "из 3" in pub["attempt_ru"], pub)


print("\n== 5. КТО РАЗРЕШАЕТ ==")
check("«писать первым» — опасное право", sales.FOLLOW_UP in sales.DANGEROUS)
for n in sales.LEVELS:
    check(f"уровень {n} сам по себе писать первым НЕ разрешает",
          sales.FOLLOW_UP not in sales.LEVELS[n]["grants"], n)

bid9, H9 = reg("fu9")
catalog(bid9)
cl9 = who(bid9, "Десятый", 9501)
work_history(bid9, cl9["id"])
lid9, _ = conversation(bid9, cl9, "Хочу заказать букет", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
sales.set_policy(bid9, "telegram", level=1)
check("на первом уровне VELOR сам не отправляет",
      not followup.may_autosend(bid9, "telegram"))
row9 = followup.plan(bid9, database.get_lead(lid9, bid9))
check("и потому касание остаётся черновиком",
      row9["status"] == database.FU_DRAFT, row9["status"])
ok_send, why = followup.may_send(bid9, row9, auto=True)
check("автоматическая отправка запрещена", not ok_send, why)
check("и причина названа", "первым" in why or "разреш" in why, why)

sales.set_policy(bid9, "telegram", level=4)
check("даже полная автономия писать первым не даёт",
      not followup.may_autosend(bid9, "telegram"))
sales.set_policy(bid9, "telegram", grants=[sales.FOLLOW_UP])
check("включённое поимённо — работает", followup.may_autosend(bid9, "telegram"))

# Испорченная политика — отказ, а не «наверное можно».
_real_policy = sales.policy
sales.policy = lambda *a, **k: (_ for _ in ()).throw(ValueError("сломано"))
check("политика не читается — не отправляем",
      not followup.may_autosend(bid9, "telegram"))
sales.policy = _real_policy

# Чужие касания недоступны.
bid_other, H_other = reg("fu-other")
r = c.post(f"/api/followups/{row9['id']}/send", headers=H_other,
           params={"business_id": bid_other})
check("чужое касание не отправить", r.status_code == 404, r.status_code)
check("и не увидеть", database.get_followup(row9["id"], bid_other) is None)
r = c.get("/api/followups", params={"business_id": bid9})
check("без ключа очередь не отдаётся", r.status_code in (401, 403), r.status_code)

# Канал без адреса.
bid10, H10 = reg("fu10")
catalog(bid10)
cl10 = database.get_or_create_client(bid10, tg_user_id=None, name="Без канала")
lid10 = leads.create(bid10, title="Интересовались букетом", client_id=cl10["id"],
                     source="manual")
lead10 = database.get_lead(lid10, bid10)
reach, whyr = followup.can_reach(bid10, lead10)
check("писать некуда — так и говорим", not reach and whyr == "no_channel", whyr)


print("\n== 6. ЧЕРНОВИК ≠ ОТПРАВЛЕНО ==")
bidS, HS = reg("fu-send")
catalog(bidS)
clS = who(bidS, "Одиннадцатый", 9601)
work_history(bidS, clS["id"])
lidS, _ = conversation(bidS, clS, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
rowS = followup.plan(bidS, database.get_lead(lidS, bidS))
check("подготовленное касание не отправлено",
      rowS["status"] != database.FU_SENT and not rowS["sent_at"], rowS)
r = c.post(f"/api/followups/{rowS['id']}/approve", headers=HS,
           params={"business_id": bidS})
check("одобрение прошло", r.status_code == 200, r.text)
appr = r.json()["followup"]
check("«одобрено» — это ещё не «отправлено»",
      appr["status"] == database.FU_APPROVED and not appr["sent_at"], appr)
check("и не считается отправленным в сводке",
      followup.overview(bidS)["sent"] == 0, followup.overview(bidS))

SENT.clear()
r = c.post(f"/api/followups/{rowS['id']}/send", headers=HS,
           params={"business_id": bidS})
sent_row = r.json()["followup"]
check("владелец отправил — состояние SENT",
      sent_row["status"] == database.FU_SENT, sent_row["status"])
check("и записано время отправки", bool(sent_row["sent_at"]), sent_row)
check("сообщение действительно ушло в канал", len(SENT) == 1, SENT)
check("и именно тому человеку", SENT and SENT[0]["to"] == "9601", SENT)
msgs = database.get_messages(bidS, clS["id"]) if hasattr(database, "get_messages") \
    else database.lead_messages(bidS, lidS, limit=50)
check("и осталось в переписке",
      any((m.get("content") or "") == rowS["message"] for m in
          database.lead_messages(bidS, lidS, limit=50)),
      [m.get("content") for m in database.lead_messages(bidS, lidS, limit=50)])

# Повторная отправка того же.
SENT.clear()
r2 = c.post(f"/api/followups/{rowS['id']}/send", headers=HS,
            params={"business_id": bidS})
check("повторно то же самое не уходит", len(SENT) == 0, SENT)
check("и состояние не меняется",
      database.get_followup(rowS["id"], bidS)["status"] == database.FU_SENT)

# Два запуска планировщика подряд.
bidR, HR = reg("fu-runs")
catalog(bidR)
clR = who(bidR, "Двенадцатый", 9602)
work_history(bidR, clR["id"])
lidR, _ = conversation(bidR, clR, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
sales.set_policy(bidR, "telegram", level=2, grants=[sales.FOLLOW_UP])
SENT.clear()
r1 = followup.run(bidR)
r2 = followup.run(bidR)
check("первый обход отправил ровно одно", r1["sent"] == 1, r1)
check("и подготовил ровно одно", r1["planned"] == 1, r1)
check("второй обход ничего не готовит заново", r2["planned"] == 0, r2)
check("второй обход не отправил ничего", r2["sent"] == 0, r2)
check("и в канал ушло ровно одно сообщение", len(SENT) == 1, SENT)
check("и второго касания не появилось",
      len(database.list_followups(bidR, lead_id=lidR, limit=10)) == 1,
      database.list_followups(bidR, lead_id=lidR, limit=10))
check("автоматика умеет отправлять, когда ей разрешено",
      database.list_followups(bidR, lead_id=lidR)[0]["status"] == database.FU_SENT)

# Одобренное владельцем уходит обходом, даже когда автономия выключена:
# разрешение дано не VELOR вообще, а этому конкретному сообщению.
bidO, HO = reg("fu-appr")
catalog(bidO)
clO = who(bidO, "Одобренный", 9606)
work_history(bidO, clO["id"])
lidO, _ = conversation(bidO, clO, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
rowO = followup.plan(bidO, database.get_lead(lidO, bidO))
check("без права писать первым касание остаётся черновиком",
      rowO["status"] == database.FU_DRAFT, rowO["status"])
SENT.clear()
res = followup.run(bidO)
check("и обход его не отправляет", res["sent"] == 0 and not SENT, res)
followup.approve(bidO, rowO["id"])
check("владелец разрешил — состояние «одобрено»",
      database.get_followup(rowO["id"], bidO)["status"] == database.FU_APPROVED)
res = followup.run(bidO)
check("и теперь обход его отправляет", res["sent"] == 1, res)
check("именно то сообщение, которое владелец видел",
      SENT and SENT[0]["text"] == rowO["message"], SENT)

# Захват: второй процесс мимо не пройдёт.
bidC, HC = reg("fu-claim")
fidC = database.add_followup(bidC, 1, reason=followup.CUSTOMER_NO_RESPONSE,
                             status=database.FU_APPROVED, message="текст")
check("первый захват удался", database.claim_followup(fidC, bidC))
check("второй захват того же — нет", not database.claim_followup(fidC, bidC))
check("отправленным можно стать только из захвата",
      database.mark_followup_sent(fidC, bidC))
check("и только один раз", not database.mark_followup_sent(fidC, bidC))


print("\n== 7. КОГДА КАНАЛ МОЛЧИТ ==")
bidF, HF = reg("fu-fail")
catalog(bidF)
clF = who(bidF, "Тринадцатый", 9603)
work_history(bidF, clF["id"])
lidF, _ = conversation(bidF, clF, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
rowF = followup.plan(bidF, database.get_lead(lidF, bidF))
OUTAGE["on"] = True
after = followup.send(bidF, rowF["id"], auto=False)
check("канал молчит — отправленным не считается",
      after["status"] != database.FU_SENT and not after["sent_at"], after)
check("и причина сохранена", "не ответил" in (after["error"] or ""), after["error"])
check("попытка засчитана", after["tries"] == 1, after)
after = followup.send(bidF, rowF["id"], auto=False)
after = followup.send(bidF, rowF["id"], auto=False)
check("между неудачами состояние не меняется само",
      database.get_followup(rowF["id"], bidF)["status"] != database.FU_SCHEDULED,
      database.get_followup(rowF["id"], bidF)["status"])
check("после трёх неудач — FAILED, а не бесконечные попытки",
      after["status"] == database.FU_FAILED, after)
check("и это записано как исход",
      after["outcome"] == followup.FAILED_OUT, after["outcome"])
after = followup.send(bidF, rowF["id"], auto=False)
check("после FAILED повторов больше нет", after["tries"] == 3, after["tries"])
OUTAGE["on"] = False

# Часть обхода упала — остальное дошло.
bidP, HP = reg("fu-partial")
catalog(bidP)
sales.set_policy(bidP, "telegram", level=2, grants=[sales.FOLLOW_UP])
clP1 = who(bidP, "Первый", 9604)
work_history(bidP, clP1["id"])
clP2 = who(bidP, "Второй", 9605)
lidP1, _ = conversation(bidP, clP1, "Сколько стоит букет?", at=ago(hours=30),
                        answer="Подберём", answer_at=ago(hours=29))
lidP2, _ = conversation(bidP, clP2, "Сколько стоит роза?", at=ago(hours=30),
                        answer="Подберём", answer_at=ago(hours=29))
_deliver_real = followup._deliver


def flaky(business_id, lead, text, *, auto):
    if lead["id"] == lidP1:
        raise RuntimeError("канал упал")
    return _deliver_real(business_id, lead, text, auto=auto)


followup._deliver = flaky
SENT.clear()
res = followup.run(bidP)
followup._deliver = _deliver_real
check("сорвавшееся одно не отменяет остальных", res["sent"] == 1, res)
check("и вторая возможность получила своё касание", len(SENT) == 1, SENT)


print("\n== 8. ЧЕМ КОНЧИЛОСЬ ==")
bidA, HA = reg("fu-attr")
catalog(bidA)
clA = who(bidA, "Четырнадцатый", 9701)
work_history(bidA, clA["id"])
lidA, _ = conversation(bidA, clA, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
rowA = followup.plan(bidA, database.get_lead(lidA, bidA))
check("касание привязано к конкретной возможности",
      rowA["lead_id"] == lidA and rowA["client_id"] == clA["id"], rowA)
followup.send(bidA, rowA["id"], auto=False)
check("исход пока не записан — ещё ничего не случилось",
      not database.get_followup(rowA["id"], bidA)["outcome"])
_, mid_reply = conversation(bidA, clA, "Да, интересно, сколько по времени?",
                            at=ago(minutes=0) if False else ago(hours=1))
got = database.get_followup(rowA["id"], bidA)
check("клиент ответил после касания — это записано",
      got["outcome"] == followup.REPLIED, got["outcome"])
check("и указано, каким именно сообщением",
      got["reply_message_id"] == mid_reply, got)

oid = database.add_order(bidA, "Букет", client_id=clA["id"], amount=3500,
                         source="telegram")
leads.on_order(bidA, clA["id"], oid, amount=3500, channel="telegram")
got = database.get_followup(rowA["id"], bidA)
check("появилась заявка — цепочка сохранена",
      got["outcome"] == followup.CONVERTED and got["order_id"] == oid, got)
check("но «касание принесло деньги» нигде не утверждается",
      "revenue" not in got and "attributed" not in got, sorted(got))

# Отказ после касания.
bidN, HN = reg("fu-neg")
catalog(bidN)
clN = who(bidN, "Пятнадцатый", 9702)
work_history(bidN, clN["id"])
lidN, _ = conversation(bidN, clN, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
rowN = followup.plan(bidN, database.get_lead(lidN, bidN))
followup.send(bidN, rowN["id"], auto=False)
conversation(bidN, clN, "Дорого, нашёл в другом месте", at=ago(hours=1))
check("отказ после касания записан отказом",
      database.get_followup(rowN["id"], bidN)["outcome"] == followup.REJECTED,
      database.get_followup(rowN["id"], bidN)["outcome"])

# Молчание в ответ.
bidI, HI = reg("fu-ign")
fidI = database.add_followup(bidI, 1, reason=followup.CUSTOMER_NO_RESPONSE,
                             status=database.FU_SENT, message="текст")
database.update_followup(fidI, bidI, sent_at=ago(days=10))
followup.settle(bidI)
check("на что не ответили за неделю — «ответа не было»",
      database.get_followup(fidI, bidI)["outcome"] == followup.IGNORED)
check("и статистика не выдумана: считаются реальные записи",
      followup.overview(bidI)["sent"] == 1, followup.overview(bidI))


print("\n== 9. ЧЕРЕЗ КАБИНЕТ ==")
r = c.get("/api/followups", headers=HA, params={"business_id": bidA})
check("очередь касаний отдаётся", r.status_code == 200, r.text)
data = r.json()
check("и в ней видно причину по-русски",
      all(i["reason_ru"] for i in data["items"]), data["items"][:1])
check("и предел касаний", data["max_attempts"] == 3, data)

r = c.get(f"/api/leads/{lidA}", headers=HA, params={"business_id": bidA})
card = r.json()
check("касания видны в карточке возможности", len(card["followups"]) >= 1, card.keys())

r = c.get("/api/leads", headers=HA, params={"business_id": bidA})
lst = r.json()
check("в списке есть сводка по касаниям", "followup" in lst["stats"], lst["stats"])

# Правка текста владельцем.
bidE, HE = reg("fu-edit")
catalog(bidE)
clE = who(bidE, "Шестнадцатый", 9703)
work_history(bidE, clE["id"])
lidE, _ = conversation(bidE, clE, "Сколько стоит букет?", at=ago(hours=30),
                       answer="Подберём", answer_at=ago(hours=29))
rowE = followup.plan(bidE, database.get_lead(lidE, bidE))
r = c.post(f"/api/followups/{rowE['id']}/edit", headers=HE,
           json={"business_id": bidE, "text": "Здравствуйте! Вернусь к вашему букету."})
check("владелец может переписать текст", r.status_code == 200, r.text)
check("и это отмечено как его слова",
      r.json()["followup"]["based_on"]["draft_by"] == "owner", r.json())

# Подготовить по кнопке там, где повода нет.
bidZ, HZ = reg("fu-nopoint")
catalog(bidZ)
clZ = who(bidZ, "Семнадцатый", 9704)
work_history(bidZ, clZ["id"])
lidZ, _ = conversation(bidZ, clZ, "Хочу заказать букет", at=ago(hours=1),
                       answer="Сейчас подберём", answer_at=ago(hours=1))
r = c.post(f"/api/leads/{lidZ}/followup", headers=HZ, params={"business_id": bidZ})
check("нет повода — VELOR его не выдумывает",
      r.status_code == 200 and r.json()["followup"] is None, r.text)
check("и честно говорит, что повода нет", "не придумывает" in r.json()["why"], r.json())


print("\n== 10. ОСТАЛЬНОЕ НЕ СЛОМАЛОСЬ ==")
r = c.get("/api/leads", headers=H8, params={"business_id": bid8})
check("страница возможностей открывается", r.status_code == 200, r.status_code)
items = r.json()["items"]
check("и оценка на месте", items and items[0].get("q", {}).get("priority"), items[:1])
check("и касание приехало рядом с возможностью", "f" in items[0], items[0].keys())
r = c.get("/api/ai/policy", headers=H8,
          params={"business_id": bid8, "channel": "telegram"})
if r.status_code == 200:
    keys = {p["key"] for p in r.json().get("permissions", [])}
    check("новое право видно в настройках канала", sales.FOLLOW_UP in keys, keys)
else:
    check("настройки канала открываются", False, r.status_code)

botcore.send_text = _real_send
print(f"\nИТОГО: успешно {ok}, провалено {fail}")
sys.exit(1 if fail else 0)
