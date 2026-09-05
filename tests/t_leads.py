# -*- coding: utf-8 -*-
"""
ЛИД — настоящая сущность, а не строчка в заметках клиента.

Что здесь проверяется — обещания, а не строки кода:
  • возможность существует отдельно: у неё свой id, свой бизнес, своё состояние;
  • один человек может иметь несколько возможностей, но не несколько карточек;
  • десять сообщений одного разговора не превращаются в десять возможностей;
  • обычный вопрос («где вы находитесь?») возможностью не становится;
  • закрытая возможность помнит, чем кончилась: заявкой или причиной отказа;
  • догадка ИИ не становится подтверждённым фактом;
  • поле, которое правил владелец, автоматика не переписывает;
  • чужие возможности недоступны ни одним из входов;
  • Telegram, единое окно и CRM от этого не сломались.
"""
import os, sys, tempfile, pathlib, json

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
import server, database, entities, leads, botcore, ai, sales, identity, intake

c = TestClient(server.app)
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


def reg(login, token=None):
    r = c.post("/api/register", json={"name": login, "login": login,
                                      "password": "pass123", "consent": True})
    d = r.json()
    bid = d["business_id"]
    if token:
        database.update_business(bid, tg_bot_token=token)
    return bid, {"X-Auth": d["token"]}


def client_of(bid, name, tg=None):
    return database.get_or_create_client(bid, tg_user_id=tg or abs(hash(name)) % 10 ** 8,
                                         name=name)


bid, H = reg("leads_main", "111:MAIN")
other, OH = reg("leads_other", "222:OTHER")

# Модели нет: воронка обязана работать правилами. Это и есть тот случай, когда
# «ИИ не подключён» — обычное состояние, а не авария.
ai.understand_material = lambda *a, **k: None


# ============================================================
print("\n== 1. КОММЕРЧЕСКОЕ НАМЕРЕНИЕ УЗНАЁТСЯ ПРАВИЛАМИ ==")
YES = ["Сколько стоит маникюр? Хочу записаться завтра.",
       "Мне нужен букет на 8 марта",
       "Хочу заказать букет на свадьбу",
       "Можно записаться на стрижку?",
       "Есть в наличии?",
       "Почём розы?",
       "Сколько будет стоить 100 цветов?"]
NO = ["Здравствуйте", "Спасибо", "Ок", "Где вы находитесь?",
      "Какой у вас график работы?", "Как до вас доехать?",
      "Спасибо, ничего не нужно"]
for t in YES:
    got, why = leads.intent(t)
    check("намерение видно: «%s»" % t[:34], got, why)
for t in NO:
    got, why = leads.intent(t)
    check("не намерение: «%s»" % t[:34], not got, why)
check("и решение объяснено словами", leads.intent(YES[0])[1], leads.intent(YES[0]))


# ============================================================
print("\n== 2. ВОЗМОЖНОСТЬ — ОТДЕЛЬНАЯ ЗАПИСЬ ==")
ivan = client_of(bid, "Иван Петров")
mid = database.save_message(bid, ivan["id"], "user", "Сколько стоит букет пионов?",
                            channel="telegram")
lid = leads.from_message(bid, ivan, "Сколько стоит букет пионов?",
                         source="telegram", channel="telegram", message_id=mid)
lead = database.get_lead(lid, bid)
check("у возможности есть свой id", bool(lid) and lead["id"] == lid, lead)
check("она привязана к бизнесу", lead["business_id"] == bid, lead)
check("и к клиенту", lead["client_id"] == ivan["id"], lead)
check("источник записан", lead["source"] == "telegram", lead)
check("канал записан", lead["channel"] == "telegram", lead)
check("состояние — реальное поле", lead["status"] == "new", lead)
check("сумму никто не выдумал", lead["value"] is None, lead)
check("видно, почему это возможность", lead["meta"].get("why"), lead["meta"])
check("возможность НЕ строчка в заметках клиента",
      "Интерес" not in (database.get_client(ivan["id"], bid).get("notes") or ""),
      database.get_client(ivan["id"], bid).get("notes"))
check("сообщение осталось сообщением",
      len(database.get_client_messages(ivan["id"], bid)) == 1,
      database.get_client_messages(ivan["id"], bid))
check("возможность помнит, с какого сообщения началась",
      lead["first_message_id"] == mid, lead)


# ============================================================
print("\n== 3. ОДИН РАЗГОВОР — ОДНА ВОЗМОЖНОСТЬ ==")
for reply in ["А доставка есть?", "А можно завтра?", "Сколько стоит доставка?"]:
    m = database.save_message(bid, ivan["id"], "user", reply, channel="telegram")
    leads.from_message(bid, ivan, reply, source="telegram", channel="telegram",
                       message_id=m)
mine = database.list_leads(bid, client_id=ivan["id"])
check("четыре сообщения — одна возможность", len(mine) == 1, mine)
check("но активность отмечена",
      database.get_lead(lid, bid)["last_message_id"] > mid,
      database.get_lead(lid, bid))

print("\n-- обычный вопрос возможности не создаёт --")
gost = client_of(bid, "Прохожий")
for t in ["Здравствуйте", "Где вы находитесь?", "Спасибо"]:
    leads.from_message(bid, gost, t, source="telegram", channel="telegram")
check("ни одной ложной возможности",
      database.list_leads(bid, client_id=gost["id"]) == [],
      database.list_leads(bid, client_id=gost["id"]))
check("а клиент при этом заведён", bool(database.get_client(gost["id"], bid)))


# ============================================================
print("\n== 4. КУПИЛ: СВЯЗЬ С СУЩЕСТВУЮЩЕЙ ЗАЯВКОЙ ==")
oid = database.add_order(bid, "Букет пионов", client_id=ivan["id"], amount=4500,
                         source="telegram")
won_id = leads.on_order(bid, ivan["id"], oid, amount=4500, channel="telegram")
lead = database.get_lead(lid, bid)
check("возможность закрылась той же заявкой", won_id == lid, won_id)
check("состояние — купил", lead["status"] == "won", lead)
check("дата конверсии записана", bool(lead["converted_at"]), lead)
check("ссылка на заявку — на существующую", lead["order_id"] == oid, lead)
check("новой системы заказов не завелось",
      database.get_order(oid, bid)["text"] == "Букет пионов",
      database.get_order(oid, bid))
check("сумма взята из заявки, а не придумана", lead["value"] == 4500, lead)


# ============================================================
print("\n== 5. НОВАЯ ВОЗМОЖНОСТЬ ТОГО ЖЕ ЧЕЛОВЕКА ==")
m2 = database.save_message(bid, ivan["id"], "user",
                           "Хочу заказать букет на свадьбу", channel="telegram")
lid2 = leads.from_message(bid, ivan, "Хочу заказать букет на свадьбу",
                          source="telegram", channel="telegram", message_id=m2)
check("после закрытой — новая возможность", lid2 and lid2 != lid, (lid, lid2))
check("а клиент остался прежним",
      database.get_lead(lid2, bid)["client_id"] == ivan["id"])
check("второго клиента не завелось",
      len([r for r in database.list_clients(bid, limit=100)
           if (r.get("name") or "") == "Иван Петров"]) == 1,
      database.list_clients(bid, limit=100))
check("у человека теперь две возможности",
      len(database.leads_of_client(bid, ivan["id"])) == 2,
      database.leads_of_client(bid, ivan["id"]))


# ============================================================
print("\n== 6. НЕ СЛОЖИЛОСЬ: ПРИЧИНА ОБЯЗАТЕЛЬНА ==")
leads.mark_lost(bid, lid2, reason="price")
lead2 = database.get_lead(lid2, bid)
check("состояние — не сложилось", lead2["status"] == "lost", lead2)
check("причина записана", lead2["lost_reason"] == "price", lead2)
check("и дата закрытия тоже", bool(lead2["lost_at"]), lead2)
check("причина переводится словами",
      leads.public(lead2)["lost_reason_ru"] == "Дорого", leads.public(lead2))

nine = client_of(bid, "Без причины")
l9 = leads.from_message(bid, nine, "Сколько стоит доставка роз?", source="telegram")
leads.mark_lost(bid, l9, reason=None)
check("неизвестная причина — «другое», а не правдоподобная версия",
      database.get_lead(l9, bid)["lost_reason"] == "other",
      database.get_lead(l9, bid))
leads.mark_lost(bid, l9, reason="выдуманная")
check("несуществующая причина не принимается",
      database.get_lead(l9, bid)["lost_reason"] == "other",
      database.get_lead(l9, bid))


# ============================================================
print("\n== 7. ДОГАДКА НЕ СТАНОВИТСЯ ФАКТОМ ==")
olga = client_of(bid, "Ольга")
lo = leads.from_message(bid, olga, "Хочу заказать букет", source="instagram")
res = leads.apply_ai(bid, lo, {"value": 15000, "interest": "букет на свадьбу"})
row = database.get_lead(lo, bid)
check("предположённая сумма в карточку НЕ записана", row["value"] is None, row)
check("но она видна как догадка",
      (row["meta"].get("guess") or {}).get("value") == 15000, row["meta"])
check("догадка помечена и наружу",
      leads.public(row)["guess"].get("value") == 15000, leads.public(row))
check("в отчёте разбора видно, что это догадка",
      "value" in res["guessed"] and "value" not in res["written"], res)

# А вот сказанное вслух — факт, и он записывается.
res2 = leads.apply_ai(bid, lo, {"interest": "букет на свадьбу"}, stated=("interest",))
check("сказанное клиентом записывается",
      database.get_lead(lo, bid)["interest"] == "букет на свадьбу",
      database.get_lead(lo, bid))
check("и это отмечено как записанное", "interest" in res2["written"], res2)

print("\n-- сумму, названную самим клиентом, берём --")
petr = client_of(bid, "Пётр")
lp = leads.from_message(bid, petr, "Хочу заказать букет за 15 000 ₽", source="telegram")
check("названная клиентом сумма записана",
      database.get_lead(lp, bid)["value"] == 15000, database.get_lead(lp, bid))
check("и валюта вместе с ней",
      database.get_lead(lp, bid)["currency"] == "RUB", database.get_lead(lp, bid))
kolvo = client_of(bid, "Количество")
lk = leads.from_message(bid, kolvo, "Сколько будет стоить 100 цветов?", source="telegram")
check("количество суммой не считается",
      database.get_lead(lk, bid)["value"] is None, database.get_lead(lk, bid))


# ============================================================
print("\n== 8. ПРАВКА ВЛАДЕЛЬЦА СИЛЬНЕЕ АВТОМАТИКИ ==")
leads.owner_update(bid, lo, {"value": 20000})
check("владелец поставил сумму", database.get_lead(lo, bid)["value"] == 20000)
check("поле помечено как его",
      "value" in database.get_lead(lo, bid)["owner_fields"],
      database.get_lead(lo, bid)["owner_fields"])
res3 = leads.apply_ai(bid, lo, {"value": 15000}, stated=("value",))
check("ИИ не переписал подтверждённую сумму",
      database.get_lead(lo, bid)["value"] == 20000, database.get_lead(lo, bid))
check("и честно сказал, что не стал", "value" in res3["blocked"], res3)

leads.owner_update(bid, lo, {"interest": "букет для мамы"})
leads.apply_ai(bid, lo, {"interest": "букет на свадьбу"}, stated=("interest",))
check("поправленное описание тоже не переписывается",
      database.get_lead(lo, bid)["interest"] == "букет для мамы",
      database.get_lead(lo, bid))

print("\n-- разговор продолжается, а правку не трогает --")
leads.from_message(bid, olga, "А можно за 30 000?", source="instagram")
check("новая сумма из разговора не затирает правку владельца",
      database.get_lead(lo, bid)["value"] == 20000, database.get_lead(lo, bid))
check("владелец может убрать сумму совсем",
      leads.owner_update(bid, lo, {"value": ""})["value"] is None,
      database.get_lead(lo, bid))


# ============================================================
print("\n== 9. РУЧНАЯ ВОЗМОЖНОСТЬ ==")
r = c.post("/api/leads", headers=H, json={
    "title": "Свадебный букет для Анны", "value": 20000,
    "interest": "встретил на выставке, хочет пионы"})
check("владелец завёл возможность руками", r.status_code == 200, r.text)
manual = r.json()["lead"]
check("без выдуманного сообщения от клиента", manual["client_id"] is None, manual)
check("источник — вручную", manual["source"] == "manual", manual)
check("сумма записана, потому что её назвали", manual["value"] == 20000, manual)
check("и сразу защищена как подтверждённая",
      "value" in manual["owner_fields"], manual)
r = c.post("/api/leads", headers=H, json={"title": "  "})
check("пустую возможность завести нельзя", r.status_code == 400, r.text)
r = c.post("/api/leads", headers=H, json={"title": "Чужой клиент", "client_id": 99999})
check("к несуществующему клиенту не привяжешь", r.status_code == 404, r.text)


# ============================================================
print("\n== 10. API: СПИСОК, КАРТОЧКА, СОСТОЯНИЕ ==")
r = c.get("/api/leads", headers=H)
d = r.json()
check("список отдаётся", r.status_code == 200 and d["items"], r.text)
check("вместе с воронкой числами", d["stats"]["total"] >= 5, d["stats"])
check("и со словарями состояний", len(d["statuses"]) == 5, d["statuses"])
check("причины проигрыша тоже приезжают", len(d["reasons"]) >= 5, d["reasons"])
check("состояние переведено на человеческий",
      all(i.get("status_ru") for i in d["items"]), d["items"][:2])

r = c.get("/api/leads?status=open", headers=H)
opened = r.json()["items"]
check("срез «в работе» показывает только открытые",
      all(i["open"] for i in opened), opened)
r = c.get("/api/leads?status=won", headers=H)
check("а срез «купили» — только купивших",
      all(i["status"] == "won" for i in r.json()["items"]), r.json()["items"])

# «Ждут ответа» — канонический счётчик: клиент написал последним, ответа нет.
# На него опирается не только эта страница, но и живое поле кабинета, поэтому
# он обязан считаться по ВСЕМУ срезу открытых, а не по показанной странице:
# главная просит одну строку и берёт из ответа только цифры.
one = c.get("/api/leads?status=open&limit=1", headers=H).json()
allp = c.get("/api/leads?status=open&limit=200", headers=H).json()
check("«ждут ответа» приезжает числом",
      isinstance(one["stats"].get("waiting"), int), one["stats"])
check("и считается по всему срезу, а не по странице",
      len(one["items"]) == 1 and one["stats"]["waiting"] == allp["stats"]["waiting"],
      (len(one["items"]), one["stats"]["waiting"], allp["stats"]["waiting"]))
check("считается только среди открытых",
      one["stats"]["waiting"] <= len(allp["items"]),
      (one["stats"]["waiting"], len(allp["items"])))

r = c.get(f"/api/leads/{lid}", headers=H)
card = r.json()
check("карточка открывается", r.status_code == 200, r.text)
check("в ней виден разговор, а не его копия",
      card["messages"] and card["messages"][0]["content"].startswith("Сколько стоит"),
      card["messages"])
# Разговор живёт в messages, а не внутри лида: в карточке лежит только то, ради
# чего он заведён (чего человек хочет), а весь остальной обмен репликами —
# ссылками. Копия разошлась бы с оригиналом, и стало бы непонятно, где правда.
check("переписка не продублирована в карточку",
      "А можно завтра?" not in json.dumps(card["lead"], ensure_ascii=False)
      and "А можно завтра?" in json.dumps(card["messages"], ensure_ascii=False),
      card["lead"])
check("зато видно, где разговор начался и где кончился",
      card["lead"]["first_message_id"] and card["lead"]["last_message_id"]
      and card["lead"]["last_message_id"] > card["lead"]["first_message_id"],
      card["lead"])
check("видна история изменений", card["history"], card["history"])
check("и заявка, в которую всё вылилось",
      card["order"] and card["order"]["id"] == oid, card["order"])

r = c.get("/api/leads/999999", headers=H)
check("несуществующая возможность — 404", r.status_code == 404, r.text)


print("\n-- отметить исход через интерфейс --")
anna = client_of(bid, "Анна")
la = leads.from_message(bid, anna, "Можно записаться на маникюр?", source="telegram")
r = c.post(f"/api/leads/{la}/status", headers=H, json={"status": "in_progress"})
check("состояние меняется", r.json()["lead"]["status"] == "in_progress", r.text)
r = c.post(f"/api/leads/{la}/status", headers=H, json={"status": "won"})
check("«купил» отмечается кнопкой", r.json()["lead"]["status"] == "won", r.text)
check("и дата конверсии проставилась", r.json()["lead"]["converted_at"], r.text)
r = c.post(f"/api/leads/{la}/status", headers=H,
           json={"status": "lost", "lost_reason": "no_response"})
check("переоткрыть в «не сложилось» можно",
      r.json()["lead"]["status"] == "lost", r.text)
check("и дата конверсии снята — иначе она врала бы в отчёте",
      r.json()["lead"]["converted_at"] is None, r.text)
r = c.post(f"/api/leads/{la}/status", headers=H, json={"status": "придумал"})
check("выдуманное состояние не принимается", r.status_code == 400, r.text)
r = c.post(f"/api/leads/{la}/status", headers=H, json={"status": "won", "order_id": 999999})
check("привязать чужую/несуществующую заявку нельзя", r.status_code == 404, r.text)


# ============================================================
print("\n== 11. ВОРОНКА СЧИТАЕТСЯ ИЗ ВОЗМОЖНОСТЕЙ ==")
st = leads.overview(bid)
check("всего = сумма состояний",
      st["total"] == st["new"] + st["qualified"] + st["in_progress"] + st["won"] + st["lost"],
      st)
check("открытые — это три состояния",
      st["open"] == st["new"] + st["qualified"] + st["in_progress"], st)
check("конверсия считается от закрытых, а не от всех",
      st["conversion"] == (round(st["won"] * 100 / st["closed"]) if st["closed"] else 0),
      st)
check("и совпадает с тем, что говорит база",
      st["total"] == database.count_leads(bid), st)
check("второго способа посчитать лиды нет: цифра одна",
      c.get("/api/leads", headers=H).json()["stats"]["total"] == st["total"])
check("проверка не холостая: возможностей действительно несколько",
      st["total"] >= 5 and st["closed"] >= 2, st)
check("и ни один интерес не осел в заметках клиентов",
      not any("Интерес:" in (r.get("notes") or "")
              for r in database.list_clients(bid, limit=200)),
      [r.get("notes") for r in database.list_clients(bid, limit=200) if r.get("notes")])


# ============================================================
print("\n== 12. ЧУЖОЕ НЕДОСТУПНО ==")
chuzhoy = client_of(other, "Чужой клиент")
alien = leads.from_message(other, chuzhoy, "Сколько стоит стрижка?", source="telegram")
check("у чужого бизнеса своя возможность", bool(alien))
check("в моём списке её нет",
      all(i["id"] != alien for i in c.get("/api/leads", headers=H).json()["items"]))
check("по прямой ссылке не открыть",
      c.get(f"/api/leads/{alien}", headers=H).status_code == 404)
check("состояние чужой не поменять",
      c.post(f"/api/leads/{alien}/status", headers=H, json={"status": "won"}).status_code == 404)
check("и не поправить",
      c.post(f"/api/leads/{alien}", headers=H, json={"title": "моё"}).status_code == 404)
check("и не удалить",
      c.post(f"/api/leads/{alien}/delete", headers=H).status_code == 404)
check("чужая возможность цела", database.get_lead(alien, other)["status"] == "new")
check("чужой бизнес не видит моих",
      all(i["id"] != lid for i in c.get("/api/leads", headers=OH).json()["items"]))
check("в чужую воронку мои не попали",
      leads.overview(other)["total"] == database.count_leads(other),
      leads.overview(other))
check("без входа список не отдаётся", c.get("/api/leads").status_code == 401)


# ============================================================
print("\n== 13. РЕЕСТР ЗАПИСЕЙ ЗНАЕТ ВОЗМОЖНОСТЬ ==")
check("вид зарегистрирован", "lead" in entities.ENTITIES)
sch = entities.schema("lead")
check("у него есть форма и место", sch["fields"] and sch["where"] == "leads.html", sch)
eid, text, clean = entities.create(bid, "lead", {"title": "Оформление витрины"})
check("создаётся общей дверью", bool(eid) and "Возможность" in text, text)
check("и читается ею же",
      entities.read(bid, "lead", eid)["title"] == "Оформление витрины",
      entities.read(bid, "lead", eid))
same = entities.match(bid, "lead", {"title": "Оформление витрины"})
check("та же возможность узнаётся, а не удваивается", same == eid, (same, eid))
_, _, _, what = entities.create_or_update(bid, "lead", {"title": "Оформление витрины"})
check("повторное подтверждение обновляет, а не плодит", what == "updated", what)
check("возможность попала в память бизнеса",
      any(x["entity"] == "lead" for x in entities.listing(bid, "lead")),
      entities.listing(bid, "lead"))
check("у возможности есть история происхождения",
      database.memory_links(bid, "lead", eid), eid)


# ============================================================
print("\n== 14. TELEGRAM: КЛИЕНТ ДАЁТ ВОЗМОЖНОСТЬ, ВЛАДЕЛЕЦ — НЕТ ==")
before = database.count_leads(bid)
botcore.handle_message(bid, 555001, "Марина", "Сколько стоит доставка букета?")
check("вопрос клиента завёл возможность",
      database.count_leads(bid) == before + 1, database.count_leads(bid))
tg_client = [x for x in database.list_clients(bid, limit=200) if x["name"] == "Марина"]
check("и клиент появился в базе", tg_client, tg_client)
check("сообщение осталось в переписке",
      database.get_client_messages(tg_client[0]["id"], bid), tg_client)

before = database.count_leads(bid)
botcore.handle_message(bid, 555002, "Прохожий2", "Здравствуйте, вы работаете сегодня?")
check("обычный вопрос возможности не завёл",
      database.count_leads(bid) == before, database.count_leads(bid))

print("\n-- владелец шлёт материал: воронка не трогается --")
identity.link_telegram(bid, {"id": "777777", "first_name": "Хозяин"})
database.update_business(bid, owner_verified=1)
check("владелец опознаётся сервером", botcore.is_owner(bid, "777777"))
before = database.count_leads(bid)
answer = botcore.from_owner(bid, text="У нас новая услуга — экспресс-маникюр 2500 ₽",
                            actor_id=777777)
check("материал владельца принят единым окном", "Принял" in answer or "Готово" in answer, answer)
check("и возможностью не стал", database.count_leads(bid) == before,
      database.count_leads(bid))


# ============================================================
print("\n== 15. РЕГРЕССИЯ: ЕДИНОЕ ОКНО И CRM ЦЕЛЫ ==")
got = intake.receive(bid, text="Кассовый чек. Итого к оплате 1850 ₽", source="web",
                     background=False)
check("единое окно принимает как раньше", got["accepted"], got)
check("и повтор всё так же ловится",
      intake.receive(bid, text="Кассовый чек. Итого к оплате 1850 ₽", source="web",
                     background=False)["duplicates"], got)

r = c.get("/api/clients", headers=H)
check("список клиентов отвечает", r.status_code == 200 and r.json()["items"], r.text)
card = c.get(f"/api/clients/{ivan['id']}", headers=H).json()
check("в карточке клиента видны его возможности", len(card["leads"]) == 2, card["leads"])
check("и заказы на месте", card["orders"], card["orders"])
check("и переписка на месте", card["messages"], card["messages"])
check("заказы бизнеса отвечают", c.get("/api/orders", headers=H).status_code == 200)


# ============================================================
print("\n== 16. УДАЛЕНИЕ И ГРАНИЦЫ ==")
tmp_id = leads.create(bid, title="Ошибочная запись", source="manual")
check("удалить свою можно",
      c.post(f"/api/leads/{tmp_id}/delete", headers=H).status_code == 200)
check("после удаления её нет", database.get_lead(tmp_id, bid) is None)
check("клиент и переписка от этого не пострадали",
      database.get_client(ivan["id"], bid) and database.get_client_messages(ivan["id"], bid))
check("удалить дважды нельзя",
      c.post(f"/api/leads/{tmp_id}/delete", headers=H).status_code == 404)

print("\n-- окно разговора --")
stale = client_of(bid, "Давний")
ls = leads.create(bid, title="Старый разговор", client_id=stale["id"], source="telegram")
database.update_lead(ls, bid, last_activity_at="2020-01-01 00:00:00")
check("остывший разговор не продолжают",
      leads.open_for(bid, stale["id"]) is None, leads.open_for(bid, stale["id"]))
ls2 = leads.from_message(bid, stale, "Хочу заказать букет", source="telegram")
check("а заводят новую возможность", ls2 and ls2 != ls, (ls, ls2))
check("старую при этом не закрыли самовольно",
      database.get_lead(ls, bid)["status"] == "new", database.get_lead(ls, bid))


print("\n== 17. НАСТРОЙКА КАНАЛА СИЛЬНЕЕ ВОРОНКИ ==")
# «Только отвечает» обещает владельцу, что VELOR ничего не создаёт. Записать
# возможность в обход этого обещания — значит соврать в собственных настройках.
quiet = client_of(bid, "Тихий канал")
sales.set_policy(bid, "telegram", level=1, actor="business")
check("на первом уровне заводить лида нельзя",
      not leads.may_create(bid, "telegram"), sales.policy(bid, "telegram"))
before = database.count_leads(bid)
leads.from_message(bid, quiet, "Сколько стоит букет пионов?", source="telegram",
                   channel="telegram")
check("и возможность не завелась", database.count_leads(bid) == before,
      database.count_leads(bid))
check("но сообщение всё равно можно сохранить — оно не запрещено",
      database.save_message(bid, quiet["id"], "user", "Сколько стоит?", channel="telegram"))
sales.set_policy(bid, "telegram", level=2, actor="business")
check("на втором уровне — можно", leads.may_create(bid, "telegram"))
lq = leads.from_message(bid, quiet, "Сколько стоит букет пионов?", source="telegram",
                        channel="telegram")
check("и возможность появилась", bool(lq) and database.count_leads(bid) == before + 1,
      database.count_leads(bid))
check("а руками владелец заводит возможность при любом уровне",
      bool(leads.create(bid, title="Записал сам", source="manual")))


print("\n-- удаление бизнеса уносит его воронку --")
gone, GH = reg("leads_gone")
gc_ = client_of(gone, "Уходящий")
leads.from_message(gone, gc_, "Сколько стоит букет?", source="telegram")
check("у бизнеса была возможность", database.count_leads(gone) == 1)
database.delete_business(gone)
check("после удаления компании её возможности не остались",
      database.count_leads(gone) == 0, database.count_leads(gone))
check("а мои на месте", database.count_leads(bid) > 0)


print(f"\nИТОГО: {ok} зелёных, {fail} упавших")
sys.exit(1 if fail else 0)
