# -*- coding: utf-8 -*-
"""
Демо-набор клиники: показывать можно только то, что сходится.

Этот файл сторожит набор от гниения. Демо живёт дольше, чем помнится, его
правят между делом, и однажды на показе всплывает возможность без разговора
или выигранная сделка без денег. Вопрос покупателя «а почему тут не сходится»
стоит дороже любой красивой цифры.

Проверяем ровно то, о чём легко соврать:
  1) собственная сверка продукта (leads.reconcile) не находит противоречий;
  2) цепочка замкнута: разговор → возможность → приём → деньги;
  3) причины проигрыша лежат кодами, а не человеческими фразами, — иначе
     разбор «почему теряем» схлопывается в «прочее»;
  4) цены в переписке совпадают с прайсом в памяти бизнеса;
  5) находки VELOR порождает САМ — ни одна не вписана руками;
  6) --clean уносит демо и только демо.
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

import database, leads, initiatives                                  # noqa: E402
import demo_dental                                                    # noqa: E402

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
bid = demo_dental.build("t-demo-clinic", "t-demo-pass-123")

print("== НАБОР СОБРАЛСЯ ==")
check("бизнес заведён", bool(bid), bid)
check("и помечен как демо", demo_dental.BADGE in (database.get_business(bid) or {}).get("about", ""))
check("пациенты есть", database.count_clients(bid) == len(demo_dental.PATIENTS))
check("возможности есть", database.count_leads(bid) >= 40, database.count_leads(bid))

print("\n== СВЕРКА ЗВЕНЬЕВ НЕ НАХОДИТ ПРОТИВОРЕЧИЙ ==")
# Это главная проверка файла. Продукт умеет сам называть невозможные состояния;
# набор, на котором он их находит, показывать нельзя.
flaws = leads.reconcile(bid)
check("противоречий ноль", len(flaws) == 0, [f.get("kind") for f in flaws][:5])

print("\n== ЦЕПОЧКА ЗАМКНУТА ==")
with database._connect() as con:
    check("каждая возможность помнит разговор",
          one(con, "SELECT COUNT(*) FROM leads WHERE business_id=? AND first_message_id IS NULL", bid) == 0)
    check("и последнюю его реплику тоже",
          one(con, "SELECT COUNT(*) FROM leads WHERE business_id=? AND last_message_id IS NULL", bid) == 0)
    check("сообщения эти существуют и принадлежат тому же клиенту",
          one(con, """SELECT COUNT(*) FROM leads l WHERE l.business_id=?
                        AND NOT EXISTS (SELECT 1 FROM messages m
                                          WHERE m.id=l.first_message_id
                                            AND m.business_id=l.business_id
                                            AND m.client_id=l.client_id)""", bid) == 0)
    check("каждая выигранная ссылается на заявку",
          one(con, """SELECT COUNT(*) FROM leads WHERE business_id=? AND status='won'
                        AND order_id IS NULL""", bid) == 0)
    check("и заявка эта существует у того же бизнеса",
          one(con, """SELECT COUNT(*) FROM leads l WHERE l.business_id=? AND l.status='won'
                        AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.id=l.order_id
                                          AND o.business_id=l.business_id)""", bid) == 0)
    # Обратная связь НЕ обязана быть полной: основной поток приёмов растёт не из
    # возможностей, а из записи по телефону. Привязать их «для красоты» значило
    # бы показать конверсию, которой не было.
    loose = one(con, """SELECT COUNT(*) FROM orders o WHERE o.business_id=? AND NOT EXISTS
                          (SELECT 1 FROM leads l WHERE l.order_id=o.id)""", bid)
    check("основной поток приёмов возможностями не притворяется", loose > 100, loose)

    print("\n== ПРИЧИНЫ ПРОИГРЫША — КОДАМИ ==")
    reasons = [r[0] for r in con.execute(
        "SELECT DISTINCT lost_reason FROM leads WHERE business_id=? AND status='lost'", (bid,))]
    check("все причины из справочника продукта",
          bool(reasons) and all(r in leads.LOST_REASONS for r in reasons), reasons)
    check("причин несколько — разбор потерь имеет смысл", len(reasons) >= 3, reasons)
    check("человеческая формулировка сохранена рядом",
          one(con, """SELECT COUNT(*) FROM leads WHERE business_id=? AND status='lost'
                        AND COALESCE(meta,'') <> ''""", bid) > 0)

    print("\n== ПЕРЕПИСКА ==")
    msgs = one(con, "SELECT COUNT(*) FROM messages WHERE business_id=?", bid)
    silent = one(con, """SELECT COUNT(*) FROM clients c WHERE c.business_id=? AND NOT EXISTS
                           (SELECT 1 FROM messages m WHERE m.client_id=c.id)""", bid)
    check("разговоров много, а не для галочки", msgs >= 150, msgs)
    check("почти у всех пациентов есть история", silent <= 5, "молчат %d" % silent)
    check("говорят обе стороны",
          one(con, "SELECT COUNT(*) FROM messages WHERE business_id=? AND role='user'", bid) > 0
          and one(con, "SELECT COUNT(*) FROM messages WHERE business_id=? AND role='assistant'", bid) > 0)
    # Цена, названная в переписке, обязана совпадать с прайсом: расхождение
    # заметит первый же покупатель, который откроет разговор и карточку услуги.
    for service, price in (("Имплант под ключ", 78000), ("Гигиена и чистка", 6500)):
        human = f"{price:,}".replace(",", " ") + " ₽"
        check("в разговорах звучит цена из прайса: %s" % service,
              one(con, """SELECT COUNT(*) FROM messages WHERE business_id=?
                            AND role='assistant' AND content LIKE ?""",
                  bid, "%" + human + "%") > 0, human)

    print("\n== ДЕНЬГИ ==")
    inc = one(con, "SELECT COALESCE(SUM(amount),0) FROM finance_entries WHERE business_id=? AND kind='income'", bid)
    exp = one(con, "SELECT COALESCE(SUM(amount),0) FROM finance_entries WHERE business_id=? AND kind='expense'", bid)
    check("выручка за три месяца правдоподобна", 3_000_000 <= inc <= 6_000_000, inc)
    check("клиника работает в плюс", inc - exp > 0, inc - exp)
    # Клиника в минусе — это другой демо-набор. Показывать «AI-директор нашёл
    # у вас убыток» там, где убыток нарисован нами, значит демонстрировать не
    # продукт, а собственную фантазию.
    check("прибыль не запредельная", (inc - exp) / inc < 0.6, (inc - exp) / inc)

print("\n== НАХОДКИ VELOR ПОРОЖДАЕТ САМ ==")
# Ни одна из них не записана в набор. Все выведены детекторами из initiatives.py
# по тем самым фактам, что проверены выше. Если какая-то исчезнет — значит
# данные перестали её порождать, и чинить надо данные, а не дописывать вывод.
initiatives.scan(bid)
titles = [f.get("title") or "" for f in database.list_initiatives(bid, limit=30)]
for t in titles:
    print("      •", t)
for word in ("перестали возвращаться", "Покупают реже", "Потеряно", "ждут ответа"):
    check("находка про «%s» появилась сама" % word, any(word in t for t in titles))
check("и ни одна не записана в набор руками",
      "перестали возвращаться" not in pathlib.Path("tools/demo_dental.py").read_text(encoding="utf-8"))

print("\n== УБОРКА БЬЁТ ТОЛЬКО ПО ДЕМО ==")
alive = database.create_business(name="Настоящий клиент", about="живой бизнес",
                                 greeting="", login="t-real-one", password="pass123")
demo_dental.clean()
check("демо удалено", demo_dental.find_demo() is None)
check("живой бизнес не тронут", database.get_business(alive) is not None)
demo_dental.clean()
check("повторная уборка — тихий отказ, а не падение",
      database.get_business(alive) is not None)

print("\nИТОГО: успешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
