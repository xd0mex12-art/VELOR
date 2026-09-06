# -*- coding: utf-8 -*-
"""
Демо-набор под любой бизнес: показывать можно только то, что сходится.

Этот файл сторожит набор от гниения. Демо живёт дольше, чем помнится, его
правят между делом, и однажды на показе всплывает возможность без разговора
или выигранная сделка без денег. Вопрос покупателя «а почему тут не сходится»
стоит дороже любой красивой цифры.

Набор перестал быть стоматологией: VELOR не медицинский продукт, и владелец
называет свою отрасль словами — «автосервис», «юридическая компания», «студия
керамики». Поэтому вся батарея гоняется по КАЖДОМУ профилю каталога и ещё раз
по незнакомому запросу. Отрасль, которую забыли проверить, обязательно
окажется той самой, которую покажут на звонке.

Проверяем ровно то, о чём легко соврать:
  1) собственная сверка продукта (leads.reconcile) не находит противоречий;
  2) цепочка замкнута: разговор → возможность → заявка → деньги;
  3) причины проигрыша лежат кодами, а не человеческими фразами, — иначе
     разбор «почему теряем» схлопывается в «прочее»;
  4) цены в переписке совпадают с прайсом в памяти бизнеса;
  5) заявки связаны с услугами — как их связывает живой продукт;
  6) бизнес в плюсе, месяцы сравнимы, сюжет один и тот же;
  7) находки VELOR порождает САМ — ни одна не вписана руками;
  8) уборка уносит демо и только демо.
"""
import os, sys, tempfile, pathlib

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "testowner"
os.environ["OWNER_PASSWORD"] = "s3cret-owner"
os.environ["JWT_SECRET"] = "test-secret-demo"
os.environ["SECRET_KEY"] = "test-box-key"
os.environ["DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.chdir(ROOT)

import database, leads, initiatives, director                        # noqa: E402
import demo_business, profiles                                       # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


def one(con, sql, *a):
    return con.execute(sql, a).fetchone()[0]


database.init_db()


# ── ПОДБОР ПРОФИЛЯ ──────────────────────────────────────────────────────────
print("== ВЛАДЕЛЕЦ ПИШЕТ СВОИМИ СЛОВАМИ ==")
for asked, kind in (("стоматологическая клиника", "dental"),
                    ("автосервис", "autoservice"),
                    ("Юридическая Компания", "legal"),
                    ("салон красоты и барбершоп", "beauty"),
                    ("ремонт квартир под ключ", "repair"),
                    ("интернет-магазин посуды", "shop")):
    got = profiles.profile_for(asked)
    check("«%s» → профиль %s" % (asked, kind), got["kind"] == kind, got["kind"])

# Незнакомая отрасль без модели обязана дать рабочий профиль, а не пустоту:
# на боевом сервере ключей может не быть, и именно тогда демо нужнее всего.
unknown = profiles.profile_for("студия художественной керамики")
check("незнакомая отрасль тоже даёт профиль", bool(unknown.get("services")))
check("и честно помечена общим профилем",
      unknown["source"] == "общий профиль", unknown["source"])
check("слова владельца сохранены в описании",
      "керамик" in unknown["about"].lower(), unknown["about"])
check("пустой запрос не роняет подбор", bool(profiles.profile_for("").get("services")))

print("\n== ПРОФИЛЬ ОТ МОДЕЛИ ПРОВЕРЯЕТСЯ, А НЕ ПРИНИМАЕТСЯ НА ВЕРУ ==")
# Модель придумывает справочник, а не данные. Но если она придумает его плохо,
# набор развалится тихо: на одинаковых ценах «потеряли шесть возможностей по
# цене» перестаёт что-либо значить — терять было нечего.
good = {"kind": "pottery", "title": "студия керамики «Глина»",
        "services": [{"name": "Пробное занятие", "price": 1500},
                     {"name": "Абонемент на месяц", "price": 9000},
                     {"name": "Гончарный круг, час", "price": 2000},
                     {"name": "Обжиг изделия", "price": 800},
                     {"name": "Мастер-класс для группы", "price": 25000},
                     {"name": "Корпоратив", "price": 60000}]}
check("годный профиль принимается", profiles._valid(good) is not None)
flat = dict(good, services=[dict(s, price=5000) for s in good["services"]])
check("одинаковые цены отклоняются", profiles._valid(flat) is None,
      "на плоском прайсе нечего терять")
check("три услуги — мало", profiles._valid(dict(good, services=good["services"][:3])) is None)
check("без названия — отказ", profiles._valid(dict(good, title="")) is None)
check("мусор вместо профиля — отказ", profiles._valid("не json") is None)


# ── БАТАРЕЯ ПО КАЖДОМУ ВИДУ БИЗНЕСА ─────────────────────────────────────────
def battery(request):
    got = demo_business.build(request=request)
    assert got, "не собралось: " + request
    bid = got["business_id"]
    p = profiles.profile_for(request)
    title = got["name"]
    print("\n" + "=" * 70)
    print("== %s ==" % title)

    check("бизнес заведён", bool(bid), bid)
    check("помечен как демо",
          demo_business.BADGE in (database.get_business(bid) or {}).get("about", ""))
    check("клиенты есть", database.count_clients(bid) == len(p["clients"]))
    check("возможности есть", database.count_leads(bid) >= 40, database.count_leads(bid))

    # Главная проверка. Продукт умеет сам называть невозможные состояния;
    # набор, на котором он их находит, показывать нельзя.
    flaws = leads.reconcile(bid)
    check("противоречий ноль", len(flaws) == 0, [f.get("kind") for f in flaws][:5])

    with database._connect() as con:
        check("каждая возможность помнит разговор",
              one(con, """SELECT COUNT(*) FROM leads WHERE business_id=?
                            AND first_message_id IS NULL""", bid) == 0)
        check("и последнюю его реплику тоже",
              one(con, """SELECT COUNT(*) FROM leads WHERE business_id=?
                            AND last_message_id IS NULL""", bid) == 0)
        check("сообщения существуют и принадлежат тому же клиенту",
              one(con, """SELECT COUNT(*) FROM leads l WHERE l.business_id=?
                            AND NOT EXISTS (SELECT 1 FROM messages m
                                              WHERE m.id=l.first_message_id
                                                AND m.business_id=l.business_id
                                                AND m.client_id=l.client_id)""", bid) == 0)
        check("каждая выигранная ссылается на заявку",
              one(con, """SELECT COUNT(*) FROM leads WHERE business_id=? AND status='won'
                            AND order_id IS NULL""", bid) == 0)
        check("и заявка эта существует у того же бизнеса",
              one(con, """SELECT COUNT(*) FROM leads l WHERE l.business_id=?
                            AND l.status='won'
                            AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.id=l.order_id
                                              AND o.business_id=l.business_id)""", bid) == 0)
        # Обратная связь НЕ обязана быть полной: основной поток растёт не из
        # возможностей, а из записи по телефону. Привязать их «для красоты»
        # значило бы показать конверсию, которой не было.
        loose = one(con, """SELECT COUNT(*) FROM orders o WHERE o.business_id=?
                              AND NOT EXISTS (SELECT 1 FROM leads l
                                                WHERE l.order_id=o.id)""", bid)
        check("основной поток заявок возможностями не притворяется", loose > 100, loose)

        reasons = [r[0] for r in con.execute(
            "SELECT DISTINCT lost_reason FROM leads WHERE business_id=? AND status='lost'",
            (bid,))]
        check("причины проигрыша — из справочника продукта",
              bool(reasons) and all(r in leads.LOST_REASONS for r in reasons), reasons)
        check("причин несколько — разбор потерь имеет смысл", len(reasons) >= 3, reasons)
        check("человеческая формулировка сохранена рядом",
              one(con, """SELECT COUNT(*) FROM leads WHERE business_id=? AND status='lost'
                            AND COALESCE(meta,'') <> ''""", bid) > 0)

        msgs = one(con, "SELECT COUNT(*) FROM messages WHERE business_id=?", bid)
        silent = one(con, """SELECT COUNT(*) FROM clients c WHERE c.business_id=?
                               AND NOT EXISTS (SELECT 1 FROM messages m
                                                 WHERE m.client_id=c.id)""", bid)
        check("разговоров много, а не для галочки", msgs >= 150, msgs)
        check("почти у всех клиентов есть история", silent <= 6, "молчат %d" % silent)
        check("говорят обе стороны",
              one(con, "SELECT COUNT(*) FROM messages WHERE business_id=? AND role='user'", bid) > 0
              and one(con, "SELECT COUNT(*) FROM messages WHERE business_id=? AND role='assistant'", bid) > 0)

        # Цена, названная в переписке, обязана совпадать с прайсом: расхождение
        # заметит первый же покупатель, который откроет разговор и карточку.
        misses = []
        for name, price, _ in p["services"][:3]:
            human = f"{price:,}".replace(",", " ") + " ₽"
            if one(con, """SELECT COUNT(*) FROM messages WHERE business_id=?
                             AND role='assistant' AND content LIKE ?""",
                   bid, "%" + human + "%") == 0:
                misses.append((name, human))
        check("цены в разговорах — из прайса", not misses, misses)

        inc = one(con, """SELECT COALESCE(SUM(amount),0) FROM finance_entries
                            WHERE business_id=? AND kind='income'""", bid)
        exp = one(con, """SELECT COALESCE(SUM(amount),0) FROM finance_entries
                            WHERE business_id=? AND kind='expense'""", bid)
        # Демо, на котором бизнес в минусе, показывать нельзя: обсуждать будут
        # не продукт, а придуманное банкротство. Прибыль в 60% — тоже сказка.
        check("бизнес работает в плюс", inc - exp > 0, (inc, exp))
        check("прибыль не запредельная", 0 < (inc - exp) / inc < 0.6, (inc - exp) / inc)

    # Связь заявки с услугой ставит живой продукт, и демо обязано её иметь:
    # без неё Директор пишет «какая услуга приносит больше — сказать нельзя»,
    # хотя у настоящего клиента этот вывод считается.
    svc = database.service_revenue(bid, 30)
    check("выручка по услугам считается", len(svc) >= 3, svc)
    amounts = [s["amount"] for s in svc]
    check("и услуги не слиплись в одинаковые суммы",
          len(set(amounts)) >= max(2, len(amounts) - 1), amounts)

    b = director.briefing(bid)
    check("Директор не жалуется на отсутствие связи с услугами",
          not any("не связаны с услугами" in str(g) for g in (b.get("gaps") or [])),
          b.get("gaps"))
    check("сводка готова", b.get("ready") is True, b.get("headline"))

    initiatives.scan(bid, use_ai=False)
    titles = [f.get("title") or "" for f in database.list_initiatives(bid, limit=30)]
    for word in ("перестали возвращаться", "Потеряно", "ждут ответа"):
        check("находка про «%s» появилась сама" % word,
              any(word in t for t in titles), titles[:4])
    return bid


for request in ("стоматологическая клиника", "автосервис", "юридическая компания",
                "салон красоты", "ремонт квартир", "интернет-магазин",
                "студия художественной керамики"):
    battery(request)

print("\n== НИ ОДНА НАХОДКА НЕ ВПИСАНА В НАБОР РУКАМИ ==")
# Все выведены детекторами по фактам, проверенным выше. Если какая-то
# исчезнет — значит данные перестали её порождать, и чинить надо данные,
# а не дописывать вывод.
for f in ("tools/demo_business.py", "tools/profiles.py"):
    src = pathlib.Path(f).read_text(encoding="utf-8")
    check("%s не содержит готовых выводов" % f,
          "перестали возвращаться" not in src and "Покупают реже" not in src)

print("\n== УБОРКА БЬЁТ ТОЛЬКО ПО ДЕМО ==")
alive = database.create_business(name="Настоящий клиент", about="живой бизнес",
                                 greeting="", login="t-real-one", password="pass123")
check("наборов собрано несколько", len(demo_business.find_demos()) >= 7,
      len(demo_business.find_demos()))
one_id = demo_business.find_demos()[0]["id"]
demo_business.clean(one_id)
check("по номеру уносится ровно один",
      database.get_business(one_id) is None and len(demo_business.find_demos()) >= 6)
demo_business.clean()
check("без номера — все", demo_business.find_demos() == [])
check("живой бизнес не тронут", database.get_business(alive) is not None)
demo_business.clean()
check("повторная уборка — тихий отказ, а не падение",
      database.get_business(alive) is not None)

print("\nИТОГО: успешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
