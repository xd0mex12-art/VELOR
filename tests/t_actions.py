# -*- coding: utf-8 -*-
"""
АВТОНОМНОСТЬ И ЖУРНАЛ ДЕЙСТВИЙ — VELOR действует в рамках полномочий.

Что здесь проверяется — обещания, а не строки кода:
  • по умолчанию свободы ровно столько, сколько было до этого слоя;
  • «разрешить всё» не поднимает выше потолка безопасности;
  • владелец может запретить действие, и запрет действительно работает;
  • уровень доверия каналу старше общего тумблера и не отменяется им;
  • решение принимается в одном месте, а не в четырёх копиях;
  • заблокированное записывается наравне с выполненным;
  • подтверждение — это разрешение на ОДНО действие, а не на все впредь;
  • устаревшее предложение не выполняется никогда;
  • одно действие не выполняется дважды, сколько бы обходов ни было;
  • сорвавшееся действие не считается выполненным;
  • изменение самой настройки — тоже событие журнала, с «было → стало»;
  • чужой бизнес не виден и не управляем.
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
import server, database, actions, leads, followup, sales, botcore, understanding

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


def who(bid, name, tg):
    return database.get_or_create_client(bid, tg_user_id=tg, name=name)


def work_history(bid, client_id, n=12):
    """Известные рабочие часы: иначе автоматическая отправка не пойдёт вовсе."""
    for i in range(n):
        hour = int(i * 23 / max(1, n - 1))
        database.save_message(bid, client_id, "assistant", "старый ответ %d" % i,
                              channel="telegram",
                              created_at="2020-01-0%d %02d:00:00" % (1 + i % 9, hour))


def catalog(bid):
    database.add_fact(bid, "service", "Букет на заказ", "от 3500 ₽, сборка 40 минут")


def conversation(bid, client, said, *, at, answer=None, answer_at=None):
    mid = database.save_message(bid, client["id"], "user", said,
                                channel="telegram", created_at=at)
    lid = leads.from_message(bid, client, said, source="telegram",
                             channel="telegram", message_id=mid)
    if answer:
        database.save_message(bid, client["id"], "assistant", answer,
                              channel="telegram", created_at=answer_at or at)
    return lid, mid


# ── подставной канал ───────────────────────────────────────────────────────
# Подменяем только транспорт. Проверяем настоящее: состояние записи, появление
# сообщения в переписке, содержимое журнала.
SENT = []
OUTAGE = {"on": False, "why": "Telegram не ответил"}
_real_send = botcore.send_text


def fake_send(bid, tg_user_id, text):
    if OUTAGE["on"]:
        return False, OUTAGE["why"]
    SENT.append({"bid": bid, "to": str(tg_user_id), "text": text})
    return True, ""


botcore.send_text = fake_send


# ═══ 1. РЕЕСТР ═════════════════════════════════════════════════════════════
print("\n== 1. РЕЕСТР ДЕЙСТВИЙ ==")

check("реестр не пуст", len(actions.REGISTRY) >= 10, len(actions.REGISTRY))
check("у каждого действия есть человеческое название",
      all((m.get("title") or "").strip() for m in actions.REGISTRY.values()))
check("у каждого действия есть объяснение",
      all((m.get("means") or "").strip() for m in actions.REGISTRY.values()))
check("у каждого действия есть риск",
      all(m.get("risk") in (actions.LOW, actions.MEDIUM, actions.HIGH, actions.CRITICAL)
          for m in actions.REGISTRY.values()))
check("умолчание входит в число возможных положений",
      all(m["default"] in m["choices"] for m in actions.REGISTRY.values()),
      [k for k, m in actions.REGISTRY.items() if m["default"] not in m["choices"]])
check("потолок не ниже умолчания",
      all(actions._min(m["default"], m["ceiling"]) == m["default"]
          for m in actions.REGISTRY.values()))

# Реестр обещает исполнителя — исполнитель обязан существовать.
missing = []
for aid, meta in actions.REGISTRY.items():
    if meta.get("runner") and not actions._load(aid):
        missing.append(aid)
check("обещанный исполнитель существует у каждого действия", not missing, missing)

# Ни одного действия «на будущее»: то, чего система не умеет, в списке нет.
check("массовой рассылки в реестре нет", "send_campaign" not in actions.REGISTRY)
check("возврата денег в реестре нет", "issue_refund" not in actions.REGISTRY)
check("изменения цены в реестре нет", "change_price" not in actions.REGISTRY)
check("удаления данных в реестре нет",
      not any("delete" in k for k in actions.REGISTRY))


# ═══ 2. УМОЛЧАНИЯ ══════════════════════════════════════════════════════════
print("\n== 2. ПО УМОЛЧАНИЮ — КАК БЫЛО ==")
bid1, H1 = reg("ac1")

m = actions.modes(bid1)
check("отвечать клиентам — сам", m["reply_to_customer"] == actions.AUTO, m["reply_to_customer"])
check("заводить возможности — сам", m["create_lead"] == actions.AUTO, m["create_lead"])
check("оценивать возможности — сам", m["qualify_lead"] == actions.AUTO)
check("готовить напоминания — сам", m["prepare_followup"] == actions.AUTO)
check("писать клиенту первым — только с подтверждением",
      m["send_followup"] == actions.APPROVAL, m["send_followup"])
check("записывать деньги — только с подтверждением",
      m["record_finance"] == actions.APPROVAL, m["record_finance"])
check("менять подтверждённые данные — только с подтверждением",
      m["update_knowledge"] == actions.APPROVAL, m["update_knowledge"])
check("оформлять заявки на втором уровне сам не может",
      m["create_order"] != actions.AUTO, m["create_order"])

check("владелец ничего не трогал — своей воли ещё нет", not actions.raw_modes(bid1))
check("приглушённых прав нет", not actions.muted(bid1))


# ═══ 3. РЕЖИМЫ ═════════════════════════════════════════════════════════════
print("\n== 3. АВТО / ПОДТВЕРЖДЕНИЕ / ЗАПРЕТ ==")
bid2, H2 = reg("ac2")

actions.set_mode(bid2, "create_lead", actions.DENY)
check("запрет записался", actions.modes(bid2)["create_lead"] == actions.DENY)
check("решение — запрет",
      actions.decide(bid2, "create_lead", channel="telegram") == actions.DENY)
check("и запрет действительно работает",
      not leads.may_create(bid2, "telegram"))

cl2 = who(bid2, "Марина", "9001")
lid2 = leads.from_message(bid2, cl2, "Хочу заказать букет, сколько стоит?",
                          source="telegram", channel="telegram")
check("возможность не завелась", lid2 is None, lid2)

actions.set_mode(bid2, "create_lead", actions.AUTO)
check("разрешение вернулось", actions.modes(bid2)["create_lead"] == actions.AUTO)
lid2 = leads.from_message(bid2, cl2, "Хочу заказать букет, сколько стоит?",
                          source="telegram", channel="telegram")
check("и возможность завелась", bool(lid2), lid2)

actions.set_mode(bid2, "send_followup", actions.AUTO)
check("писать первым разрешено", actions.modes(bid2)["send_followup"] == actions.AUTO)
check("право продавца выдалось вслед за тумблером",
      sales.FOLLOW_UP in set(sales.policy(bid2, "telegram")["allowed"]))
actions.set_mode(bid2, "send_followup", actions.APPROVAL)
check("вернули на подтверждение — право снова приглушено",
      sales.FOLLOW_UP not in set(sales.policy(bid2, "telegram")["allowed"]))
check("и автоматическая отправка выключена",
      not followup.may_autosend(bid2, "telegram"))

try:
    actions.set_mode(bid2, "reply_to_customer", actions.APPROVAL)
    bad = True
except actions.ActionError:
    bad = False
check("нельзя выбрать положение, которого у действия нет", not bad)

try:
    actions.set_mode(bid2, "чего-то-нет", actions.AUTO)
    bad = True
except actions.ActionError:
    bad = False
check("несуществующее действие не настраивается", not bad)


# ═══ 4. РАЗРЕШИТЬ ВСЁ / ЗАПРЕТИТЬ ВСЁ ══════════════════════════════════════
print("\n== 4. БЫСТРЫЕ РЕЖИМЫ ==")
bid3, H3 = reg("ac3")

actions.set_all(bid3, actions.AUTO)
m3 = actions.modes(bid3)
check("«разрешить всё» подняло ответы клиентам", m3["reply_to_customer"] == actions.AUTO)
check("«разрешить всё» подняло отправку касаний", m3["send_followup"] == actions.AUTO)
check("«разрешить всё» подняло оформление заявок", m3["create_order"] == actions.AUTO)
check("но деньги остались на подтверждении",
      m3["record_finance"] == actions.APPROVAL, m3["record_finance"])
check("и подтверждённые данные тоже",
      m3["update_knowledge"] == actions.APPROVAL, m3["update_knowledge"])
check("потолок не обходится и решением",
      actions.decide(bid3, "record_finance") == actions.APPROVAL)

actions.set_all(bid3, actions.DENY)
m3 = actions.modes(bid3)
check("«запретить всё» выключило ответы", m3["reply_to_customer"] == actions.DENY)
check("«запретить всё» выключило деньги", m3["record_finance"] == actions.DENY)
check("«запретить всё» выключило всё до одного",
      all(v == actions.DENY for v in m3.values()), m3)

actions.set_all(bid3, actions.APPROVAL)
m3 = actions.modes(bid3)
check("«спрашивать» там, где спрашивать негде, — это запрет",
      m3["reply_to_customer"] == actions.DENY, m3["reply_to_customer"])
check("а где есть — там подтверждение", m3["send_followup"] == actions.APPROVAL)


# ═══ 5. ПОТОЛОК И ИСПОРЧЕННАЯ НАСТРОЙКА ════════════════════════════════════
print("\n== 5. ПОТОЛОК БЕЗОПАСНОСТИ ==")
bid4, H4 = reg("ac4")

# Прямая запись в базу мимо set_mode: так выглядела бы порча данных или
# попытка обойти проверку.
database.save_ai_policy(bid4, actions.ALL,
                        modes={"record_finance": "auto", "update_knowledge": "auto"},
                        actor="взлом")
m4 = actions.modes(bid4)
check("запись денег не станет автоматической даже записью в базу",
      m4["record_finance"] == actions.APPROVAL, m4["record_finance"])
check("правка подтверждённого — тоже",
      m4["update_knowledge"] == actions.APPROVAL, m4["update_knowledge"])

database.save_ai_policy(bid4, actions.ALL, modes={"create_lead": "чепуха"},
                        actor="сбой")
check("непонятное значение игнорируется",
      actions.modes(bid4)["create_lead"] in actions.MODES)
check("и не читается как разрешение",
      "create_lead" not in actions.raw_modes(bid4))

import json as _json
with database._connect() as _conn:
    _conn.execute("UPDATE ai_policy SET modes = ? WHERE business_id = ? AND channel = ?",
                  ("{это не json", bid4, actions.ALL))
check("испорченный JSON читается как «владелец не трогал»",
      actions.raw_modes(bid4) == {}, actions.raw_modes(bid4))
check("и умолчания остаются осторожными",
      actions.modes(bid4)["record_finance"] == actions.APPROVAL)


# ═══ 6. КАНАЛ СТАРШЕ ОБЩЕГО ТУМБЛЕРА ═══════════════════════════════════════
print("\n== 6. УРОВЕНЬ ДОВЕРИЯ КАНАЛУ ==")
bid5, H5 = reg("ac5")

sales.set_policy(bid5, "telegram", level=1)
actions.set_mode(bid5, "create_lead", actions.AUTO)
check("общий тумблер не отменяет уровень «только отвечает»",
      actions.decide(bid5, "create_lead", channel="telegram") != actions.AUTO,
      sales.policy(bid5, "telegram")["allowed"])
check("и возможности в этом канале не заводятся",
      not leads.may_create(bid5, "telegram"))

sales.set_policy(bid5, "telegram", level=2)
check("подняли уровень — заводятся", leads.may_create(bid5, "telegram"))

_real_policy = sales.policy
sales.policy = lambda *a, **k: (_ for _ in ()).throw(ValueError("сломано"))
check("права канала не читаются — считаем, что нельзя",
      actions.decide(bid5, "create_lead", channel="telegram") == actions.DENY)
sales.policy = _real_policy
check("починилось — снова можно",
      actions.decide(bid5, "create_lead", channel="telegram") == actions.AUTO)


# ═══ 7. ПРЕДЛОЖЕНИЕ, ПОДТВЕРЖДЕНИЕ, ОТКАЗ ══════════════════════════════════
print("\n== 7. ОЧЕРЕДЬ РЕШЕНИЙ ==")
bid6, H6 = reg("ac6")
catalog(bid6)
cl6 = who(bid6, "Марина", "9006")
work_history(bid6, cl6["id"])
lid6, mid6 = conversation(bid6, cl6, "Хочу заказать букет, можно сегодня?", at=ago(hours=6))
database.update_lead(lid6, bid6, last_activity_at=ago(hours=6))

row6 = followup.plan(bid6, database.get_lead(lid6, bid6))
check("касание подготовлено", bool(row6) and row6["status"] == database.FU_DRAFT,
      row6 and row6["status"])

waiting = actions.pending(bid6)
check("оно попало в очередь решений", len(waiting) == 1, waiting)
a6 = waiting[0] if waiting else {}
check("названо человеческими словами", "Писать" in (a6.get("title") or ""), a6.get("title"))
check("с причиной, а не кодом", len(a6.get("reason") or "") > 20, a6.get("reason"))
check("состояние — «ждёт решения»",
      a6.get("status") == database.AC_PROPOSED, a6.get("status"))
check("текст сообщения виден и его можно поправить", a6.get("editable"), a6)
check("действующее лицо — VELOR", a6.get("actor") == "velor")
check("провенанс сохранён", (a6.get("based_on") or {}).get("lead_id") == lid6,
      a6.get("based_on"))

# Ничего ещё не отправлено — предложение это не отправка.
check("сообщение клиенту НЕ ушло", not SENT, SENT)

before_n = len(SENT)
r = c.post("/api/actions/%s/approve" % a6["id"], headers=H6,
           json={"text": "Здравствуйте, Марина! Возвращаюсь к вашему вопросу."})
check("подтверждение принято", r.status_code == 200, r.text[:200])
check("и оно действительно отправилось", r.json().get("ok"), r.text[:200])
check("сообщение ушло в канал", len(SENT) == before_n + 1, SENT)
check("ушёл текст владельца, а не наш",
      SENT and "Возвращаюсь к вашему вопросу" in SENT[-1]["text"], SENT[-1:])
after6 = database.get_action(a6["id"], bid6)
check("в журнале записано «выполнено»",
      after6["status"] == database.AC_SUCCEEDED, after6["status"])
check("и само касание считается отправленным",
      database.get_followup(row6["id"], bid6)["status"] == database.FU_SENT)
check("очередь опустела", not actions.pending(bid6))

# Повторное подтверждение не отправляет второй раз.
before_n = len(SENT)
c.post("/api/actions/%s/approve" % a6["id"], headers=H6, json={})
check("повторное подтверждение ничего не отправляет", len(SENT) == before_n, SENT)

# ── отказ ──
bid7, H7 = reg("ac7")
catalog(bid7)
cl7 = who(bid7, "Сергей", "9007")
work_history(bid7, cl7["id"])
lid7, _ = conversation(bid7, cl7, "Сколько стоит букет на юбилей?", at=ago(hours=8))
database.update_lead(lid7, bid7, last_activity_at=ago(hours=8))
followup.plan(bid7, database.get_lead(lid7, bid7))
p7 = actions.pending(bid7)
check("предложение есть", len(p7) == 1, p7)

before_n = len(SENT)
r = c.post("/api/actions/%s/reject" % p7[0]["id"], headers=H7, json={})
check("отказ принят", r.status_code == 200, r.text[:200])
check("ничего не отправлено", len(SENT) == before_n)
gone = database.get_action(p7[0]["id"], bid7)
check("в журнале записан отказ", gone["status"] == database.AC_CANCELLED, gone["status"])
check("и он объяснён словами", "отклонили" in (gone.get("result") or ""), gone.get("result"))
check("очередь пуста", not actions.pending(bid7))


# ═══ 8. АВТОМАТИЧЕСКОЕ ВЫПОЛНЕНИЕ ══════════════════════════════════════════
print("\n== 8. КОГДА РАЗРЕШЕНО — ДЕЛАЕТ САМ ==")
bid8, H8 = reg("ac8")
catalog(bid8)
actions.set_mode(bid8, "send_followup", actions.AUTO)
cl8 = who(bid8, "Анна", "9008")
work_history(bid8, cl8["id"])
lid8, _ = conversation(bid8, cl8, "Хочу заказать букет, можно сегодня?", at=ago(hours=6))
database.update_lead(lid8, bid8, last_activity_at=ago(hours=6))

row8 = followup.plan(bid8, database.get_lead(lid8, bid8))
check("автоматика разрешена — касание запланировано, а не ждёт",
      row8["status"] == database.FU_SCHEDULED, row8["status"])
check("в очереди решений его нет — решение уже принято",
      not actions.pending(bid8), actions.pending(bid8))
live8 = database.find_live_action(bid8, followup.ACTION_KEY % row8["id"])
check("но в журнале запись есть", bool(live8), live8)
check("и она помечена как автоматическая",
      (live8 or {}).get("mode") == actions.AUTOMATIC, (live8 or {}).get("mode"))

before_n = len(SENT)
followup.run(bid8)
check("обход отправил", len(SENT) == before_n + 1, SENT[-1:])
done8 = database.get_action(live8["id"], bid8)
check("журнал записал выполнение", done8["status"] == database.AC_SUCCEEDED, done8["status"])

# Второй обход подряд не отправляет то же самое.
before_n = len(SENT)
followup.run(bid8)
followup.run(bid8)
check("два обхода подряд не шлют дважды", len(SENT) == before_n, SENT[-2:])


# ═══ 9. ЗАБЛОКИРОВАННОЕ — ТОЖЕ ЗАПИСЬ ══════════════════════════════════════
print("\n== 9. ЧЕГО VELOR НЕ ДАЛИ СДЕЛАТЬ ==")
bid9, H9 = reg("ac9")
catalog(bid9)
actions.set_mode(bid9, "reply_to_customer", actions.DENY)
cl9 = who(bid9, "Пётр", "9009")

reply9 = botcore.reply_for(bid9, cl9, "Здравствуйте, сколько стоит букет?",
                           channel="telegram")
check("VELOR не ответил сам", "скоро вам ответим" in (reply9 or ""), reply9)
blocked = [a for a in actions.feed(bid9, kind="blocked")
           if a["action"] == "reply_to_customer"]
check("отказ записан в журнал", len(blocked) == 1, blocked)
check("и объяснён по-человечески",
      "отвечали вы сами" in (blocked[0].get("error") or ""), blocked[0].get("error"))
check("действующее лицо — VELOR", blocked[0]["actor"] == "velor")
check("и это не выглядит как выполненное",
      blocked[0]["status"] == database.AC_BLOCKED)

# Сообщение клиента при этом всё равно сохранено и возможность заведена.
msgs9 = database.get_client_messages(cl9["id"], bid9, limit=10)
check("сообщение клиента сохранено", any(m["role"] == "user" for m in msgs9), msgs9)
check("и возможность всё-таки завелась",
      bool(leads.open_for(bid9, cl9["id"])))

# Заводить возможности тоже запретили — блокировка видна.
actions.set_mode(bid9, "mark_lead_won", actions.DENY)
oid9 = database.add_order(bid9, "Букет", client_id=cl9["id"], amount=3500)
leads.on_order(bid9, cl9["id"], oid9, amount=3500, channel="telegram")
lead9 = leads.open_for(bid9, cl9["id"])
check("возможность не закрыта сделкой без разрешения", bool(lead9), lead9)
won_blocked = [a for a in actions.feed(bid9, kind="blocked")
               if a["action"] == "mark_lead_won"]
check("и отказ записан", len(won_blocked) == 1, won_blocked)


# ═══ 10. УСТАРЕВШЕЕ И ПОВТОРНОЕ ════════════════════════════════════════════
print("\n== 10. АКТУАЛЬНОСТЬ ==")
bid10, H10 = reg("ac10")
catalog(bid10)
cl10 = who(bid10, "Ольга", "9010")
work_history(bid10, cl10["id"])
lid10, _ = conversation(bid10, cl10, "Хочу заказать букет, можно сегодня?", at=ago(hours=6))
database.update_lead(lid10, bid10, last_activity_at=ago(hours=6))
row10 = followup.plan(bid10, database.get_lead(lid10, bid10))
p10 = actions.pending(bid10)
check("предложение ждёт", len(p10) == 1, p10)

# Клиент заговорил сам, пока предложение ждало решения.
mid10 = database.save_message(bid10, cl10["id"], "user", "Уже не актуально, спасибо",
                              channel="telegram")
leads.from_message(bid10, cl10, "Уже не актуально, спасибо", source="telegram",
                   channel="telegram", message_id=mid10)
before_n = len(SENT)
r = c.post("/api/actions/%s/approve" % p10[0]["id"], headers=H10, json={})
check("подтверждение обработано", r.status_code == 200, r.text[:200])
check("но ничего не отправлено", len(SENT) == before_n, SENT[-1:])
stale10 = database.get_action(p10[0]["id"], bid10)
check("запись помечена устаревшей или снятой",
      stale10["status"] in (database.AC_STALE, database.AC_CANCELLED,
                            database.AC_BLOCKED), stale10["status"])

# Просроченное предложение убирается отдельным проходом.
bid11, H11 = reg("ac11")
aid11 = database.add_action(bid11, "send_followup", status=database.AC_PROPOSED,
                            reason="старое предложение",
                            expires_at=ago(days=9))
check("предложение висит", len(actions.pending(bid11)) == 1)
n = actions.settle(bid11)
check("уборка нашла просроченное", n == 1, n)
check("и очередь опустела", not actions.pending(bid11))
old11 = database.get_action(aid11, bid11)
check("оно помечено устаревшим", old11["status"] == database.AC_STALE, old11["status"])
check("с объяснением", "срок" in (old11.get("error") or "").lower(), old11.get("error"))

# Один повод — одно предложение.
bid12, H12 = reg("ac12")
first = database.add_action(bid12, "save_knowledge", status=database.AC_PROPOSED,
                            dedupe_key="inbox:7:add_rules", reason="первое")
got = actions.execute(bid12, "record_finance", reason="второе такое же",
                      dedupe_key="inbox:7:add_rules")
check("второе предложение с тем же поводом снимается само",
      database.get_action(got["id"], bid12)["status"] == database.AC_CANCELLED,
      database.get_action(got["id"], bid12)["status"])
check("а первое осталось",
      database.get_action(first, bid12)["status"] == database.AC_PROPOSED)


# ═══ 11. ОДИН РАЗ И ТОЛЬКО ОДИН ════════════════════════════════════════════
print("\n== 11. ЗАХВАТ ==")
bid13, H13 = reg("ac13")
aid13 = database.add_action(bid13, "save_knowledge", status=database.AC_PROPOSED,
                            reason="захват")
check("первый захват удался", database.claim_action(aid13, bid13))
check("второй захват не удался", not database.claim_action(aid13, bid13))
check("состояние — «выполняется»",
      database.get_action(aid13, bid13)["status"] == database.AC_RUNNING)
check("закрыть можно", database.finish_action(aid13, bid13, database.AC_SUCCEEDED,
                                              result="сделано"))
check("закрыть второй раз — нельзя",
      not database.finish_action(aid13, bid13, database.AC_FAILED, error="ой"))
check("результат остался первым",
      database.get_action(aid13, bid13)["status"] == database.AC_SUCCEEDED)


# ═══ 12. СОРВАЛОСЬ — ЗНАЧИТ НЕ ВЫПОЛНЕНО ═══════════════════════════════════
print("\n== 12. НЕУДАЧА ==")
bid14, H14 = reg("ac14")
catalog(bid14)
cl14 = who(bid14, "Игорь", "9014")
work_history(bid14, cl14["id"])
lid14, _ = conversation(bid14, cl14, "Хочу заказать букет, можно сегодня?", at=ago(hours=6))
database.update_lead(lid14, bid14, last_activity_at=ago(hours=6))
followup.plan(bid14, database.get_lead(lid14, bid14))
p14 = actions.pending(bid14)

OUTAGE["on"] = True
before_n = len(SENT)
r = c.post("/api/actions/%s/approve" % p14[0]["id"], headers=H14, json={})
OUTAGE["on"] = False
check("канал не ответил — подтверждение не считается успехом",
      not r.json().get("ok"), r.text[:200])
check("ничего не отправлено", len(SENT) == before_n)
bad14 = database.get_action(p14[0]["id"], bid14)
check("в журнале не «выполнено»", bad14["status"] != database.AC_SUCCEEDED,
      bad14["status"])
check("а причина названа", bool(bad14.get("error") or bad14.get("result")), bad14)
# Обрыв связи — не приговор: попытки ещё остались, и владелец должен видеть,
# что дело не закрыто, а отложено.
check("предложение осталось в очереди", len(actions.pending(bid14)) == 1,
      actions.pending(bid14))
check("и в нём видно, что случилось в прошлый раз",
      "Telegram" in (actions.pending(bid14)[0].get("error") or ""),
      actions.pending(bid14)[0].get("error"))
before_n = len(SENT)
r = c.post("/api/actions/%s/approve" % p14[0]["id"], headers=H14, json={})
check("вторая попытка проходит, когда канал ожил", r.json().get("ok"), r.text[:200])
check("и сообщение действительно ушло", len(SENT) == before_n + 1)
check("теперь это «выполнено»",
      database.get_action(p14[0]["id"], bid14)["status"] == database.AC_SUCCEEDED)

# Исполнитель, который упал с исключением, тоже не успех.
bid15, H15 = reg("ac15")


def boom(business_id, row):
    raise RuntimeError("исполнитель сломался")


got15 = actions.execute(bid15, "save_knowledge", reason="проверка падения", run=boom)
check("падение исполнителя — это неудача", not got15["ok"], got15)
check("и записано как неудача",
      database.get_action(got15["id"], bid15)["status"] == database.AC_FAILED)
check("с текстом ошибки",
      "сломал" in (database.get_action(got15["id"], bid15).get("error") or ""))

# Исполнитель, который вернул «не получилось», не становится успехом молча.
got15b = actions.execute(bid15, "save_knowledge", reason="тихий отказ",
                         run=lambda b, r: {"ok": False, "error": "нечего записывать"})
check("молчаливый отказ не считается успехом", not got15b["ok"])
check("и записан как неудача",
      database.get_action(got15b["id"], bid15)["status"] == database.AC_FAILED)


# ═══ 13. ЖУРНАЛ ════════════════════════════════════════════════════════════
print("\n== 13. ЧТО ВИДИТ ВЛАДЕЛЕЦ ==")
bid16, H16 = reg("ac16")
catalog(bid16)
cl16 = who(bid16, "Дарья", "9016")
lid16, mid16 = conversation(bid16, cl16, "Хочу заказать букет, сколько стоит?",
                            at=ago(hours=2))

feed16 = actions.feed(bid16)
made = [a for a in feed16 if a["action"] == "create_lead"]
check("создание возможности попало в журнал", len(made) == 1, feed16)
a16 = made[0]
check("видно, кто действовал", a16["actor_ru"] == "VELOR", a16["actor_ru"])
check("видно, как именно", a16["mode_ru"] == "автоматически", a16["mode_ru"])
check("видно, когда", bool(a16["created_at"]))
check("видно, зачем", len(a16["reason"]) > 10, a16["reason"])
check("видно, над каким объектом", a16["target_type"] == "lead" and a16["target_id"] == lid16,
      (a16["target_type"], a16["target_id"]))
check("видно, на основании чего",
      (a16["based_on"] or {}).get("message_id") == mid16, a16["based_on"])
check("видно, чем кончилось", a16["status_ru"] == "выполнено", a16["status_ru"])
check("технических слов на экране нет",
      "create_lead" not in a16["title"] and "SUCCEEDED" not in a16["status_ru"],
      (a16["title"], a16["status_ru"]))

# Фильтры показывают разное.
check("фильтр «успешные» не пуст", len(actions.feed(bid16, kind="done")) >= 1)
check("фильтр «ошибки» пуст", not actions.feed(bid16, kind="failed"))
check("фильтр «автоматически» показывает автоматическое",
      all(a["mode"] == actions.AUTOMATIC for a in actions.feed(bid16, kind="auto")))
check("несуществующий фильтр не роняет журнал",
      isinstance(actions.feed(bid16, kind="чепуха"), list))

# «Было → стало» там, где изменение действительно есть.
database.update_lead(lid16, bid16, value=None)
lead16 = database.get_lead(lid16, bid16)
leads._enrich(bid16, lead16, "Бюджет 5000 рублей", interest=None)
upd = [a for a in actions.feed(bid16) if a["action"] == "update_lead"]
check("дополнение возможности записано", len(upd) == 1, upd)
check("и в нём видно, что стало",
      bool(upd and (upd[0]["after"] or {})), upd[0]["after"] if upd else None)


# ═══ 14. НАСТРОЙКА — ТОЖЕ СОБЫТИЕ ══════════════════════════════════════════
print("\n== 14. ИСТОРИЯ РЕШЕНИЙ ВЛАДЕЛЬЦА ==")
bid17, H17 = reg("ac17")

actions.set_mode(bid17, "send_followup", actions.AUTO, actor="owner")
changes = [a for a in actions.feed(bid17) if a["action"] == actions.SETTING_CHANGED]
check("изменение настройки записано", len(changes) == 1, changes)
ch = changes[0]
check("действующее лицо — владелец", ch["actor_ru"] == "Владелец", ch["actor_ru"])
check("и сделано вручную", ch["mode_ru"] == "вручную", ch["mode_ru"])
check("видно, что было",
      (ch["before"] or {}).get("send_followup") == actions.APPROVAL, ch["before"])
check("видно, что стало",
      (ch["after"] or {}).get("send_followup") == actions.AUTO, ch["after"])
check("и сказано человеческими словами",
      "Автоматически" in (ch["reason"] or ""), ch["reason"])

actions.set_all(bid17, actions.DENY, actor="owner")
bulk = [a for a in actions.feed(bid17) if a["action"] == actions.SETTING_CHANGED]
check("«запретить всё» тоже записано", len(bulk) == 2, len(bulk))
check("и названо тем, чем является",
      "отключена" in (bulk[0]["reason"] or ""), bulk[0]["reason"])

actions.set_all(bid17, actions.AUTO, actor="owner")
bulk = [a for a in actions.feed(bid17) if a["action"] == actions.SETTING_CHANGED]
check("«разрешить всё» записано с оговоркой про безопасность",
      "безопасност" in (bulk[0]["reason"] or ""), bulk[0]["reason"])
check("и итоговые значения сохранены",
      (bulk[0]["after"] or {}).get("record_finance") == actions.APPROVAL,
      bulk[0]["after"])


# ═══ 15. ЕДИНОЕ ОКНО ═══════════════════════════════════════════════════════
print("\n== 15. ПРИЁМ МАТЕРИАЛА ==")
bid18, H18 = reg("ac18")

check("каждое действие приёма знает своё полномочие",
      all(v in actions.REGISTRY for v in understanding.POLICY_ACTION.values()),
      set(understanding.POLICY_ACTION.values()) - set(actions.REGISTRY))
check("«уточнить у владельца» полномочием не является",
      "ask_user" not in understanding.POLICY_ACTION)
check("деньги в приёме — это запись финансов",
      understanding.POLICY_ACTION["create_expense"] == "record_finance")
check("прайс и правила — это память бизнеса",
      understanding.POLICY_ACTION["add_price_list"] == "save_knowledge"
      and understanding.POLICY_ACTION["add_rules"] == "save_knowledge")

# Запрет на пополнение памяти действительно останавливает авто-запись.
actions.set_mode(bid18, "save_knowledge", actions.DENY)
verdict, why = understanding._permitted(
    bid18, 1, {"action": "add_rules", "safe": True, "auto": True}, {})
check("запрещено — значит не делаем", verdict == actions.DENY, (verdict, why))
check("и причина названа", bool(why), why)

actions.set_mode(bid18, "save_knowledge", actions.APPROVAL)
verdict, why = understanding._permitted(
    bid18, 1, {"action": "add_rules", "safe": True, "auto": True}, {})
check("подтверждение — значит спрашиваем", verdict == actions.APPROVAL, verdict)

actions.set_mode(bid18, "save_knowledge", actions.AUTO)
verdict, _ = understanding._permitted(
    bid18, 1, {"action": "add_rules", "safe": True, "auto": True}, {})
check("разрешено — делаем сам", verdict == actions.AUTO, verdict)

# Действие, которое само по себе не бывает автоматическим, им и не станет.
actions.set_all(bid18, actions.AUTO)
verdict, _ = understanding._permitted(
    bid18, 1, {"action": "create_expense", "safe": False, "auto": False}, {})
check("расход не станет автоматическим при «разрешить всё»",
      verdict == actions.APPROVAL, verdict)
verdict, _ = understanding._permitted(
    bid18, 1, {"action": "add_company", "safe": True, "auto": False}, {})
check("то, что приём не делает сам, не делается и теперь",
      verdict == actions.APPROVAL, verdict)


# ═══ 16. БЕЗОПАСНОСТЬ ══════════════════════════════════════════════════════
print("\n== 16. ЧУЖОЕ ==")
bidA, HA = reg("acA")
bidB, HB = reg("acB")

mine = database.add_action(bidA, "save_knowledge", status=database.AC_PROPOSED,
                           reason="моё предложение")
check("чужое действие не читается", database.get_action(mine, bidB) is None)
check("своё читается", database.get_action(mine, bidA) is not None)

r = c.get("/api/actions/%s" % mine, headers=HB)
check("через API чужое тоже не отдаётся", r.status_code == 404, r.status_code)
r = c.post("/api/actions/%s/approve" % mine, headers=HB, json={})
check("и подтвердить его нельзя", r.status_code == 404, r.status_code)
r = c.post("/api/actions/%s/reject" % mine, headers=HB, json={})
check("и отклонить нельзя", r.status_code == 404, r.status_code)
check("после чужих попыток оно всё ещё ждёт",
      database.get_action(mine, bidA)["status"] == database.AC_PROPOSED)

r = c.get("/api/actions")
check("без ключа журнал не отдаётся", r.status_code in (401, 403), r.status_code)
r = c.post("/api/autonomy", json={"mode": "auto"})
check("без ключа автономность не меняется", r.status_code in (401, 403), r.status_code)

check("настройки одного бизнеса не видны другому",
      actions.raw_modes(bidB) == {}, actions.raw_modes(bidB))
actions.set_mode(bidA, "create_lead", actions.DENY)
check("и после изменения тоже", actions.modes(bidB)["create_lead"] == actions.AUTO,
      actions.modes(bidB)["create_lead"])

# Действие ссылается только на объекты своего бизнеса.
rows = database.list_actions(bidA, limit=200)
check("в журнале бизнеса только его записи",
      all(r["business_id"] == bidA for r in rows) if rows and "business_id" in rows[0]
      else True)

database.delete_business(bidA)
check("вместе с бизнесом ушёл и его журнал",
      not database.list_actions(bidA, limit=10))


# ═══ 17. API ═══════════════════════════════════════════════════════════════
print("\n== 17. КАБИНЕТ ==")
bidC, HC = reg("acC")

r = c.get("/api/autonomy", headers=HC)
check("настройки открываются", r.status_code == 200, r.status_code)
d = r.json()
check("отдаются все действия реестра",
      len(d.get("actions") or []) == len(actions.REGISTRY), len(d.get("actions") or []))
check("у каждого есть выбор положений",
      all(a.get("choices") for a in d["actions"]))
check("группы названы", len(d.get("groups") or []) >= 3, d.get("groups"))
check("владельцу не показывают технических уровней",
      all("level" not in a for a in d["actions"]))
check("действия с потолком помечены",
      any(a["capped"] for a in d["actions"]))

r = c.post("/api/autonomy", headers=HC, json={"action": "send_followup", "mode": "auto"})
check("одно действие меняется через API", r.status_code == 200, r.text[:200])
check("и ответ показывает новое положение",
      [a for a in r.json()["actions"] if a["action"] == "send_followup"][0]["mode"] == "auto")

r = c.post("/api/autonomy", headers=HC, json={"action": "record_finance", "mode": "auto"})
check("недопустимое положение отвергается", r.status_code == 400, r.status_code)
check("с объяснением", "режима" in r.json().get("detail", ""), r.text[:200])

r = c.post("/api/autonomy", headers=HC, json={"mode": "deny"})
check("«запретить всё» через API работает", r.status_code == 200)
check("и всё действительно запрещено",
      all(a["mode"] == "deny" for a in r.json()["actions"]))

r = c.get("/api/actions", headers=HC)
check("журнал открывается", r.status_code == 200, r.status_code)
check("и в нём есть фильтры", len(r.json().get("filters") or []) >= 5)
check("и сводка", "waiting" in (r.json().get("overview") or {}))

r = c.post("/api/actions/999999/approve", headers=HC, json={})
check("несуществующее действие — 404", r.status_code == 404, r.status_code)

# Главная показывает, что VELOR ждёт решения.
bidD, HD = reg("acD")
catalog(bidD)
clD = who(bidD, "Нина", "9020")
work_history(bidD, clD["id"])
lidD, _ = conversation(bidD, clD, "Хочу заказать букет, можно сегодня?", at=ago(hours=6))
database.update_lead(lidD, bidD, last_activity_at=ago(hours=6))
followup.plan(bidD, database.get_lead(lidD, bidD))
r = c.get("/api/home", headers=HD)
check("главная отвечает", r.status_code == 200, r.status_code)
attn = [i for i in (r.json().get("attention") or []) if "решения" in i["title"]]
check("и показывает, что ждёт решения", len(attn) == 1, r.json().get("attention"))
check("со ссылкой на страницу автономности",
      attn and attn[0]["href"] == "autonomy.html", attn)



# ═══ 18. НАСТРОЙКИ НЕ ВРУТ ═════════════════════════════════════════════════
print("\n== 18. ЧЕСТНОСТЬ ЭКРАНА ==")
bidE, HE = reg("acE")

sales.set_policy(bidE, "telegram", level=1)
sales.set_policy(bidE, "instagram", level=1)
actions.set_mode(bidE, "create_lead", actions.AUTO)
one = [a for a in actions.settings(bidE)["actions"] if a["action"] == "create_lead"][0]
check("тумблер показывает волю владельца", one["mode"] == actions.AUTO, one["mode"])
check("но экран честно говорит, что каналы этого не позволят",
      bool(one["held_note"]), one)
check("и называет, какие именно",
      "telegram" in one["held_by"] and "instagram" in one["held_by"], one["held_by"])

sales.set_policy(bidE, "telegram", level=2)
sales.set_policy(bidE, "instagram", level=2)
one = [a for a in actions.settings(bidE)["actions"] if a["action"] == "create_lead"][0]
check("подняли уровни — оговорка ушла", not one["held_note"], one["held_note"])

# Опасное право уровень не выдаёт никогда — его выдаёт только тумблер.
actions.set_mode(bidE, "send_followup", actions.AUTO)
one = [a for a in actions.settings(bidE)["actions"] if a["action"] == "send_followup"][0]
check("опасное право тумблер выдаёт сам", not one["held_note"], one)
check("и оно действительно выдано каналу",
      sales.FOLLOW_UP in set(sales.policy(bidE, "instagram")["allowed"]))
check("а create_lead тумблер каналу не выдавал",
      "create_lead" not in set(database.ai_policy(bidE, "telegram").get("grants") or []),
      database.ai_policy(bidE, "telegram").get("grants"))


# ═══ 19. ПРАВКА ПОДТВЕРЖДЁННОГО ════════════════════════════════════════════
print("\n== 19. ЧЕГО VELOR НЕ ПЕРЕПИСЫВАЕТ ==")
bidF, HF = reg("acF")

# Владелец сам записал правило и поручился за него.
fid = database.add_fact(bidF, "rule", "Доставка", "Только по Москве", verified=True)
check("правило записано и подтверждено",
      bool((database.get_fact(fid, bidF) or {}).get("verified")))

# Название записи приём берёт из заголовка материала, а текст — из разбора:
# так получается ТА ЖЕ запись «Доставка», но с другим содержанием.
item_id = database.add_inbox_item(bidF, kind="note", title="Доставка",
                                  body="Доставка: по всей России")
res = {"level": "HIGH", "confidence": 0.95, "summary": "По всей России",
       "extracted_data": {"title": "Доставка", "body": "по всей России"},
       "suggested_actions": [{"action": "add_rules", "title": "Добавить в правила",
                              "safe": True, "auto": True}]}
database.save_inbox_result(bidF, item_id, res)
try:
    understanding.apply_action(bidF, item_id, "add_rules", res, auto=True)
    blocked_ok = False
    why = ""
except understanding.ActionError as e:
    blocked_ok = True
    why = str(e)
    kind = getattr(e, "kind", None)
check("подтверждённое человеком VELOR сам не переписывает", blocked_ok, why)
check("и отказ отличим от обычной неудачи", blocked_ok and kind == "verified", why)
check("запись осталась прежней",
      (database.get_fact(fid, bidF) or {}).get("body") == "Только по Москве",
      database.get_fact(fid, bidF))
check("«переписать подтверждённое» выше подтверждения не поднимается",
      actions.REGISTRY["update_knowledge"]["ceiling"] == actions.APPROVAL)
check("и автоматическим положением у него не бывает вовсе",
      actions.AUTO not in actions.REGISTRY["update_knowledge"]["choices"])


# ═══ 20. КОНЕЦ ТРИАЛА ══════════════════════════════════════════════════════
print("\n== 20. КОГДА ПРОБНЫЙ ПЕРИОД КОНЧИЛСЯ ==")
bidG, HG = reg("acG")
actions.set_all(bidG, actions.AUTO)
check("до конца триала можно", actions.decide(bidG, "create_lead",
                                              channel="telegram") == actions.AUTO)

database.update_business(bidG, subscription_status="expired",
                         trial_end=ago(days=3))
check("после конца триала VELOR ничего не меняет",
      actions.decide(bidG, "create_lead", channel="telegram") == actions.DENY)
verdict = actions.can_execute(bidG, "send_followup", channel="telegram")
check("и это объяснено словами",
      "робный период" in (verdict["why"] or ""), verdict["why"])
check("возможности в этом состоянии не заводятся",
      not leads.may_create(bidG, "telegram"))


botcore.send_text = _real_send
print(f"\nИТОГО: успешно {ok}, провалено {fail}")
sys.exit(1 if fail else 0)
