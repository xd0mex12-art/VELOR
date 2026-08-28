# -*- coding: utf-8 -*-
"""
ОЦЕНКА ВОЗМОЖНОСТИ: кому отвечать первым и почему.

Что здесь проверяется — обещания, а не строки кода:
  • намерение видно по словам клиента, и у каждого вывода есть цитата и номер
    сообщения: непроверяемой оценке верить нельзя;
  • намерение, соответствие и деньги — три разных ответа, а не один балл;
  • дорогой холодный разговор НЕ обходит дешёвый горячий;
  • оценка по своему прайсу остаётся оценкой и не выдаёт себя за слова клиента;
  • молчание бизнеса поднимает приоритет: это единственное место, где VELOR
    считает деньги, которые теряются прямо сейчас;
  • старый сигнал теряет актуальность, но не исчезает из истории;
  • слово владельца сильнее любого расчёта;
  • закрытая возможность сохраняет сигналы — чтобы потом сравнить выигранные
    с проигранными;
  • Telegram, CRM, единое окно и сама сущность лида от этого не сломались.
"""
import os, sys, tempfile, pathlib, json, datetime

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
sys.stdout.reconfigure(encoding="utf-8")
import pathlib as _pl
ROOT = str(_pl.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database, leads, qualify, botcore, ai, intake

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
    return d["business_id"], {"X-Auth": d["token"]}


def ago(hours=0, days=0):
    when = datetime.datetime.utcnow() - datetime.timedelta(hours=hours, days=days)
    return when.strftime("%Y-%m-%d %H:%M:%S")


bid, H = reg("q_main")
other, OH = reg("q_other")
ai.understand_material = lambda *a, **k: None

# Память бизнеса: без неё «наше ли это» отвечать нечем — и это тоже проверяется.
database.add_fact(bid, "service", "Букет на заказ", "от 3500 ₽, сборка 40 минут")
database.add_fact(bid, "product", "Роза красная", "120 ₽ за штуку")
database.add_fact(bid, "rule", "Доставка только по Москве", "в другие города не возим")

N = [0]


def who(name):
    N[0] += 1
    return database.get_or_create_client(bid, tg_user_id=9000 + N[0], name=name)


def say(cl, text, *, at=None, bid_=None, answer=None):
    """Клиент написал. Возвращает id возможности (или None)."""
    b = bid_ or bid
    mid = database.save_message(b, cl["id"], "user", text, channel="telegram",
                                created_at=at)
    lid = leads.from_message(b, cl, text, source="telegram", channel="telegram",
                             message_id=mid)
    if answer:
        database.save_message(b, cl["id"], "assistant", answer, channel="telegram",
                              created_at=at)
    return lid


def q(lid, b=None):
    b = b or bid
    return leads.public(database.get_lead(lid, b))["q"]


def reasons(v, key="intent_why"):
    return " ".join(v.get(key) or [])


# ============================================================
print("\n== 1. ПРЯМОЕ НАМЕРЕНИЕ КУПИТЬ ==")
a1 = who("Прямая покупка")
l1 = say(a1, "Хочу заказать букет пионов")
v1 = q(l1)
check("намерение высокое", v1["intent"] == "high", v1["intent"])
check("и объяснено настоящим сигналом",
      "просит оформить заказ" in reasons(v1), v1["intent_why"])
check("с цитатой из сообщения", "хочу заказ" in reasons(v1), v1["intent_why"])
check("и с номером сообщения", "сообщение №" in reasons(v1), v1["intent_why"])
sig = [s for s in v1["signals"] if s["key"] == "order_now"]
check("у сигнала есть происхождение",
      sig and sig[0]["message_id"] and sig[0]["at"] and sig[0]["source"] == "client",
      sig)
check("и он помечен как факт, а не догадка", sig and sig[0]["fact"] is True, sig)


print("\n== 2. ВОПРОС О ЦЕНЕ — ЭТО ЕЩЁ НЕ ПОКУПКА ==")
a2 = who("Спросил цену")
l2 = say(a2, "Сколько стоит букет?")
v2 = q(l2)
check("намерение среднее", v2["intent"] == "medium", v2["intent"])
check("и названо словом клиента", "спросил цену" in reasons(v2), v2["intent_why"])
check("это ниже прямой просьбы оформить",
      qualify.INTENT_ORDER.index(v2["intent"]) < qualify.INTENT_ORDER.index(v1["intent"]))


print("\n== 3. ОБЩИЙ ВОПРОС ==")
a3 = who("Просто спросил")
l3 = say(a3, "Здравствуйте, а вы где находитесь?")
check("возможности не завелось вовсе", l3 is None, l3)
l3b = leads.create(bid, title="Интересуются свадебными букетами",
                   client_id=a3["id"], source="manual")
v3 = q(l3b)
check("заведённая руками — низкое намерение", v3["intent"] == "low", v3["intent"])
check("и это сказано честно",
      "общий интерес" in reasons(v3) or "прямых слов" in reasons(v3), v3["intent_why"])
check("делать пока нечего", v3["action"] == "Наблюдать", v3)


print("\n== 4. ОТКАЗ СНИЖАЕТ, НО НЕ ЗАКРЫВАЕТ ==")
a4 = who("Отказался")
l4 = say(a4, "Хочу заказать букет", answer="От 3500 ₽")
before = q(l4)["intent"]
say(a4, "Дорого, не нужно")
v4 = q(l4)
check("намерение снизилось", qualify.INTENT_ORDER.index(v4["intent"])
      < qualify.INTENT_ORDER.index(before), (before, v4["intent"]))
check("и причина названа", "дорого" in reasons(v4), v4["intent_why"])
check("но возможность НЕ закрылась сама",
      database.get_lead(l4, bid)["status"] == "new",
      database.get_lead(l4, bid)["status"])
check("а действие стало другим", v4["action"] == "Спросить, что не подошло", v4)
check("приоритет опущен",
      "говорил «нет»" in " ".join(v4["priority_why"]), v4["priority_why"])


print("\n== 5. ДАТА ==")
a5 = who("С датой")
l5 = say(a5, "Хочу заказать букет на завтра")
v5 = q(l5)
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
check("названная дата извлечена", v5["wanted_at"] == tomorrow, v5["wanted_at"])
check("и она в сигналах с происхождением",
      any(s["key"] == "date_named" and s["message_id"] for s in v5["signals"]),
      v5["signals"])
a5b = who("Без даты")
l5b = say(a5b, "Хочу заказать букет")
check("выдуманной даты нет", q(l5b)["wanted_at"] is None, q(l5b)["wanted_at"])
check("«8 марта» разбирается тоже",
      qualify.wanted_date("нужно к 8 марта", datetime.date(2026, 8, 28)) == "2027-03-08",
      qualify.wanted_date("нужно к 8 марта", datetime.date(2026, 8, 28)))
check("а «когда-нибудь» — нет", qualify.wanted_date("когда-нибудь потом") is None)


print("\n== 6. БЮДЖЕТ, НАЗВАННЫЙ КЛИЕНТОМ ==")
a6 = who("С бюджетом")
l6 = say(a6, "Хочу заказать свадебный букет за 20 000 ₽")
v6 = q(l6)
check("сумма записана как факт", database.get_lead(l6, bid)["value"] == 20000)
check("и подписана словами клиента", "клиент назвал" in (v6["money_src"] or ""), v6)
check("это НЕ оценка", v6["money_estimated"] is False, v6)


print("\n== 7. НАЛИЧИЕ ==")
a7 = who("Наличие")
l7 = say(a7, "Розы есть в наличии?")
v7 = q(l7)
check("вопрос о наличии — среднее намерение", v7["intent"] == "medium", v7["intent"])
check("и он назван", "наличи" in reasons(v7), v7["intent_why"])


print("\n== 8. ПОВТОРНЫЕ СООБЩЕНИЯ ==")
a8 = who("Разговорчивый")
l8 = say(a8, "Сколько стоит букет?")
n1 = len(q(l8)["signals"])
say(a8, "А доставка есть?")
n2 = len(q(l8)["signals"])
check("одна возможность на весь разговор",
      len(database.list_leads(bid, client_id=a8["id"])) == 1)
check("но наблюдений стало больше", n2 > n1, (n1, n2))
check("каждое со своим сообщением",
      len({s["message_id"] for s in q(l8)["signals"] if s["message_id"]}) >= 2,
      q(l8)["signals"])


print("\n== 9. СИГНАЛЫ СКЛАДЫВАЮТСЯ ==")
say(a8, "Мне нужно завтра")
v8 = q(l8)
check("три конкретных вопроса подряд дают высокое намерение",
      v8["intent"] == "high", v8["intent"])
check("и это объяснено именно связкой",
      "несколько конкретных вопросов" in reasons(v8), v8["intent_why"])
check("проверка не холостая: по отдельности это среднее",
      q(l2)["intent"] == "medium", q(l2))


print("\n== 10-11. СВЕЖЕСТЬ И УСТАРЕВАНИЕ ==")
fresh = q(l1)
check("свежий разговор не понижен",
      "устарели" not in " ".join(fresh["priority_why"]), fresh["priority_why"])
old = who("Давний")
lo = say(old, "Хочу заказать букет", at=ago(days=40))
database.update_lead(lo, bid, last_activity_at=ago(days=40))
vo = q(lo)
check("старый сигнал теряет актуальность",
      "устарели" in " ".join(vo["priority_why"]), vo["priority_why"])
check("но намерение осталось прежним — история не переписывается",
      vo["intent"] == "high", vo["intent"])
check("и сами сигналы никуда не делись", vo["signals"], vo["signals"])
check("приоритет ниже, чем у такого же свежего",
      qualify.PRIORITY_ORDER.index(vo["priority"])
      < qualify.PRIORITY_ORDER.index(fresh["priority"]), (vo["priority"], fresh["priority"]))


print("\n== 12-13. ДЕНЬГИ НЕ ДЕЛАЮТ ЛИД ГОРЯЧИМ ==")
cold = who("Дорогой холодный")
lcold = say(cold, "Сколько стоит оформление зала за 100 000 ₽?", answer="Отвечу")
hot = who("Дешёвый горячий")
lhot = say(hot, "Хочу заказать розу за 120 ₽, можно сегодня?")
vc, vh = q(lcold), q(lhot)
check("у холодного сумма больше", (vc["money"] or 0) > (vh["money"] or 0),
      (vc["money"], vh["money"]))
check("но горячий важнее",
      qualify.PRIORITY_ORDER.index(vh["priority"])
      > qualify.PRIORITY_ORDER.index(vc["priority"]), (vh["priority"], vc["priority"]))
check("и это объяснено намерением, а не деньгами",
      "хочет купить" in " ".join(vh["priority_why"]), vh["priority_why"])
check("в списке горячий стоит выше", qualify.rank(vh) > qualify.rank(vc),
      (qualify.rank(vh), qualify.rank(vc)))


print("\n== 14-16. НАШЕ ЛИ ЭТО ==")
check("совпало с каталогом — высокое", q(l1)["fit"] == "high", q(l1))
check("и названо, что именно", "Букет на заказ" in reasons(q(l1), "fit_why"),
      q(l1)["fit_why"])

far = who("Другой город")
lfar = say(far, "Нужна доставка в Новосибирск, хочу заказать букет")
vf = q(lfar)
check("правило бизнеса исключает — низкое", vf["fit"] == "low", vf["fit"])
check("и процитировано его собственное правило",
      "только по Москве" in reasons(vf, "fit_why"), vf["fit_why"])
check("приоритет из-за этого понижен",
      "не то, чем мы занимаемся" in " ".join(vf["priority_why"]), vf["priority_why"])

empty, EH = reg("q_empty")
ec = database.get_or_create_client(empty, tg_user_id=7777, name="Клиент")
le = say(ec, "Хочу заказать букет", bid_=empty)
ve = q(le, empty)
check("памяти нет — честное «не знаем», а не «плохо»", ve["fit"] == "unknown", ve["fit"])
check("и сказано почему", "нет ни услуг" in reasons(ve, "fit_why"), ve["fit_why"])
check("намерение при этом считается как обычно", ve["intent"] == "high", ve["intent"])

odd = who("Не наш профиль")
lodd = say(odd, "Хочу заказать ремонт двигателя")
check("несовпадение с каталогом — это «среднее», а не приговор",
      q(lodd)["fit"] == "medium", q(lodd)["fit"])


print("\n== 17-18. ДЕНЬГИ: ФАКТ, ОЦЕНКА И ПУСТОТА ==")
vest = q(l1)
check("оценка по своему прайсу есть", vest["money"] == 3500, vest["money"])
check("и она помечена как оценка", vest["money_estimated"] is True, vest)
check("основание названо", "по прайсу" in (vest["money_src"] or ""), vest["money_src"])
check("в поле суммы её НЕ записали",
      database.get_lead(l1, bid)["value"] is None, database.get_lead(l1, bid))
check("зато она сохранена отдельно",
      database.get_lead(l1, bid)["estimated_value"] == 3500,
      database.get_lead(l1, bid))
vnone = q(lodd)
check("нечего оценивать — «не знаем», а не ноль", vnone["money"] is None, vnone)

leads.owner_update(bid, l1, {"value": 9000})
vown = q(l1)
check("сумма владельца сильнее оценки", vown["money"] == 9000, vown)
check("и подписана как его", "вы указали" in (vown["money_src"] or ""), vown)
check("и это больше не оценка", vown["money_estimated"] is False, vown)

qty = who("Оптом")
lqty = say(qty, "Хочу заказать 50 букетов")
vq = q(lqty)
check("количество учтено в оценке", vq["money"] == 3500 * 50, vq["money"])
check("и объяснено умножением", "× 50" in (vq["money_src"] or ""), vq["money_src"])
check("но в поле суммы по-прежнему пусто",
      database.get_lead(lqty, bid)["value"] is None)
# «50 роз» — это пятьдесят роз, а не пятьдесят букетов: считать по цене букета
# значит ошибиться в тридцать раз и приучить владельца не верить цифрам.
roses = who("Розы поштучно")
lroses = say(roses, "Сколько стоит 50 роз?")
vr = q(lroses)
check("количество считается по той позиции, о которой говорят",
      vr["money"] == 120 * 50, (vr["money"], vr["money_src"]))
check("и в основании названа именно она", "Роза" in (vr["money_src"] or ""),
      vr["money_src"])


print("\n== 19. ДОГАДКА МОДЕЛИ НЕ ПОДНИМАЕТ НАМЕРЕНИЕ ==")
guessy = who("Догадка")
lg = say(guessy, "Сколько стоит букет?")
qualify.observe(bid, lg, "", extra_signals=[
    {"key": "order_now", "kind": "intent", "level": "high",
     "title": "модель считает, что клиент готов", "quote": "похоже, готов"}])
vg = q(lg)
check("наблюдение модели записано", any(s["source"] == "ai" for s in vg["signals"]),
      vg["signals"])
check("но фактом не считается",
      all(not s["fact"] for s in vg["signals"] if s["source"] == "ai"), vg["signals"])
check("и намерение до высокого не подняло", vg["intent"] == "medium", vg["intent"])
check("проверка не холостая: то же слово от клиента поднимает",
      q(l1)["intent"] == "high", q(l1)["intent"])


print("\n== 20. СЛОВО ВЛАДЕЛЬЦА СИЛЬНЕЕ РАСЧЁТА ==")
own = who("Владелец знает")
lown = say(own, "Сколько стоит букет?")
check("VELOR посчитал среднее", q(lown)["intent"] == "medium")
leads.owner_update(bid, lown, {"intent": "high", "priority": "urgent"})
vow = q(lown)
check("владелец поставил высокое", vow["intent"] == "high", vow["intent"])
check("и это подписано как его решение",
      "вы поставили" in reasons(vow), vow["intent_why"])
check("приоритет тоже его", vow["priority"] == "urgent", vow["priority"])
say(own, "Спасибо, я подумаю")
check("новые сообщения его решение не переписывают",
      q(lown)["intent"] == "high", q(lown))
check("и в базе лежит именно его значение",
      database.get_lead(lown, bid)["intent"] == "high")
check("поля помечены как его",
      {"intent", "priority"} <= set(database.get_lead(lown, bid)["owner_fields"]),
      database.get_lead(lown, bid)["owner_fields"])
leads.owner_update(bid, lown, {"intent": "", "priority": ""})
check("и владелец может вернуть расчёт VELOR",
      "intent" not in database.get_lead(lown, bid)["owner_fields"],
      database.get_lead(lown, bid)["owner_fields"])
check("после возврата снова считают правила", q(lown)["intent"] == "medium", q(lown))
r = c.post(f"/api/leads/{lown}", headers=H, json={"intent": "космическое"})
check("выдуманный уровень не принимается", r.status_code == 400, r.text)


print("\n== 21. БИЗНЕС МОЛЧИТ — ЭТО ДЕНЬГИ ==")
wait = who("Ждёт ответа")
lw = say(wait, "Хочу заказать букет, можно сегодня?", at=ago(hours=4))
database.update_lead(lw, bid, last_activity_at=ago(hours=4))
vw = q(lw)
check("видно, что ответа не было", vw["unanswered"] is True, vw)
check("и сколько ждут", "4 ч" in (vw["waiting_ru"] or ""), vw["waiting_ru"])
check("приоритет — срочно", vw["priority"] == "urgent", vw["priority"])
check("и это объяснено молчанием",
      "не ответил" in " ".join(vw["priority_why"]), vw["priority_why"])
check("риск назван словами", vw["risks"], vw["risks"])
check("действие — ответить сейчас", vw["action"] == "Ответить сейчас", vw)

answered = who("Ему ответили")
la = say(answered, "Хочу заказать букет, можно сегодня?", at=ago(hours=4),
         answer="Да, конечно")
va = q(la)
check("после ответа разрыва нет", va["unanswered"] is False, va)
check("и приоритет ниже, чем у неотвеченного",
      qualify.PRIORITY_ORDER.index(va["priority"])
      <= qualify.PRIORITY_ORDER.index(vw["priority"]), (va["priority"], vw["priority"]))
check("а действие — довести до заявки", va["action"] == "Довести до заявки", va)


print("\n== 22. СРОЧНОСТЬ ПО ДАТЕ ==")
soon = who("Нужно завтра")
lsoon = say(soon, "Нужен букет завтра, сколько стоит?", answer="Отвечу")
vs = q(lsoon)
check("близкая дата поднимает приоритет",
      vs["priority"] in ("urgent", "high"), vs["priority"])
check("и она названа в причинах",
      any("нужно к" in x or "дата близко" in x for x in vs["priority_why"]),
      vs["priority_why"])
later = who("Нужно нескоро")
llater = say(later, "Нужен букет 25 декабря, сколько стоит?", answer="Отвечу")
check("далёкая дата так не поднимает",
      qualify.PRIORITY_ORDER.index(q(llater)["priority"])
      <= qualify.PRIORITY_ORDER.index(vs["priority"]),
      (q(llater)["priority"], vs["priority"]))


print("\n== 23. ИСТОРИЯ КЛИЕНТА: ОЦЕНИВАЕМ ВОЗМОЖНОСТЬ, А НЕ ЧЕЛОВЕКА ==")
ivan = who("Иван")
li1 = say(ivan, "Хочу заказать букет на день рождения")
oid = database.add_order(bid, "Букет на день рождения", client_id=ivan["id"], amount=4500)
leads.on_order(bid, ivan["id"], oid, amount=4500)
li2 = say(ivan, "Сколько стоит оформление зала?")
check("это две разные возможности", li1 != li2, (li1, li2))
check("у купленной — своя оценка", q(li1)["intent"] == "high", q(li1))
check("а её сумма подписана как пришедшая из заявки",
      "из оформленной заявки" in (q(li1)["money_src"] or ""), q(li1)["money_src"])
check("у новой — своя", q(li2)["intent"] == "medium", q(li2))
check("и клиент один", database.get_lead(li2, bid)["client_id"] == ivan["id"])


print("\n== 24-25. ЗАКРЫТЫЕ ВОЗМОЖНОСТИ ==")
vwon = q(li1)
check("купленная не требует внимания", vwon["priority"] == "low", vwon["priority"])
check("и действия у неё нет", vwon["action"] is None, vwon)
check("но сигналы сохранены для будущего сравнения", vwon["signals"], vwon)
check("и намерение осталось записанным",
      database.get_lead(li1, bid)["intent"] == "high")

lost = who("Проиграли")
llost = say(lost, "Хочу заказать букет за 5 000 ₽")
leads.mark_lost(bid, llost, reason="price")
row = database.get_lead(llost, bid)
check("у проигранной сохранена причина", row["lost_reason"] == "price")
check("и сигналы, по которым её оценивали", row["signals"], row["signals"])
check("и оценка на момент закрытия", row["intent"] == "high", row["intent"])
check("сравнить выигранные с проигранными будет по чему",
      all(database.get_lead(x, bid)["qualified_at"] for x in (li1, llost)))


print("\n== 26. ЧУЖОЕ НЕДОСТУПНО ==")
alien_c = database.get_or_create_client(other, tg_user_id=4242, name="Чужой")
alien = say(alien_c, "Хочу заказать букет прямо сейчас", bid_=other)
check("у чужого бизнеса своя оценка", q(alien, other)["intent"] == "high")
check("его возможность не видна в моём списке",
      all(i["id"] != alien for i in c.get("/api/leads", headers=H).json()["items"]))
check("и оценку чужой не поменять",
      c.post(f"/api/leads/{alien}", headers=H,
             json={"intent": "low"}).status_code == 404)
check("чужая оценка цела", q(alien, other)["intent"] == "high")
check("а «наше ли это» считается по СВОЕЙ памяти бизнеса",
      q(alien, other)["fit"] == "unknown", q(alien, other)["fit"])


print("\n== 27. TELEGRAM ==")
before = database.count_leads(bid)
botcore.handle_message(bid, 555777, "Марина", "Хочу заказать букет, можно завтра?")
check("разговор в боте завёл возможность", database.count_leads(bid) == before + 1)
fresh_lead = database.list_leads(bid, limit=1)[0]
vt = leads.public(fresh_lead)["q"]
check("и она сразу оценена", vt["intent"] == "high", vt["intent"])
check("с цитатой из телеграма", "хочу заказ" in reasons(vt), vt["intent_why"])
check("и с датой", vt["wanted_at"], vt["wanted_at"])


print("\n== 28-30. РЕГРЕССИЯ ==")
r = c.get("/api/leads", headers=H)
d = r.json()
check("список отвечает", r.status_code == 200 and d["items"], r.text)
check("в каждой строке есть оценка", all(i.get("q", {}).get("priority") for i in d["items"]),
      [i.get("q") for i in d["items"][:2]])
check("открытые отсортированы по важности",
      [qualify.rank(i["q"]) for i in d["items"]]
      == sorted([qualify.rank(i["q"]) for i in d["items"]], reverse=True),
      [(i["title"], i["q"]["priority"]) for i in d["items"][:5]])
check("счётчик «требуют внимания» есть", "attention" in d["stats"], d["stats"])
check("и он не больше числа открытых",
      d["stats"]["attention"] <= d["stats"]["open"], d["stats"])
check("словари уровней приезжают", d["levels"]["intent"] and d["levels"]["priority"],
      d.get("levels"))

card = c.get(f"/api/leads/{l1}", headers=H).json()
check("карточка отдаёт оценку", card["lead"]["q"]["intent"] == "high", card["lead"]["q"])
check("и причины к ней", card["lead"]["q"]["intent_why"], card["lead"]["q"])
check("переписка на месте", "messages" in card)

check("сущность лида цела: состояния прежние",
      set(leads.STATUSES) == {"new", "qualified", "in_progress", "won", "lost"})
check("конверсия по-прежнему считается из лидов",
      leads.overview(bid)["total"] == database.count_leads(bid))
got = intake.receive(bid, text="Кассовый чек. Итого к оплате 1850 ₽", source="web",
                     background=False)
check("единое окно принимает как раньше", got["accepted"], got)
check("и материал владельца возможностью не стал",
      database.count_leads(bid) == before + 1, database.count_leads(bid))
r = c.get("/api/clients", headers=H)
check("CRM отвечает", r.status_code == 200 and r.json()["items"], r.text)
cl_card = c.get(f"/api/clients/{ivan['id']}", headers=H).json()
check("и в карточке клиента обе его возможности", len(cl_card["leads"]) == 2,
      cl_card["leads"])


print(f"\nИТОГО: {ok} зелёных, {fail} упавших")
sys.exit(1 if fail else 0)
