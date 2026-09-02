# -*- coding: utf-8 -*-
"""
Результаты: отчёты, документы и письма — то, что владелец уносит наружу.

Главные обещания, которые здесь проверяются:
  • ни одной выдуманной цифры — числа в отчёте совпадают с базой;
  • догадка модели живёт отдельным блоком и подписана догадкой;
  • PDF собирается, читается и содержит кириллицу, а не квадраты;
  • результат собирается и БЕЗ ИИ — иначе возможность работала бы через раз;
  • чужой результат недоступен: ни списком, ни по прямой ссылке на файл;
  • письмо только СОЗДАЁТСЯ. Отправлять его нечем, и продукт этого не обещает;
  • ни одного «инструмента», который отвечает 501: реестр называет только то,
    для чего есть сборщик.
"""
import os, sys, tempfile, pathlib

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
import server, database, outputs, actions, storage

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
    return d["business_id"], {"X-Auth": d["token"]}


bid, H = reg("out_main")
other, OH = reg("out_other")

# ── настоящие данные, по которым и должен считаться отчёт ──
database.update_business(bid, about="Цветочная мастерская: букеты и оформление.")
client_id = database.get_or_create_client(bid, tg_user_id=901, name="Анна Петрова")["id"]
order_id = database.add_order(bid, "Букет пионов 25 шт", client_id=client_id,
                              phone="+79990001122", amount=8500)
database.add_order(bid, "Оформление зала", client_id=client_id, amount=42000)
database.add_finance_entry(bid, "income", "Продажи", 50500, note="Оплата заказов")
database.add_finance_entry(bid, "expense", "Закупка цветов", 18000)
database.add_finance_entry(bid, "expense", "Реклама", 6000)
database.add_fact(bid, "service", "Свадебное оформление", "Арка, президиум",
                  data={"price": 42000})
database.add_fact(bid, "product", "Букет пионов", "25 шт, крафт", data={"price": 8500})


def build(kind, params=None, headers=None):
    return c.post("/api/outputs", headers=headers or H,
                  json={"kind": kind, "params": params or {}})


print("\n== РЕЕСТР ==")
cat = c.get("/api/outputs/kinds", headers=H).json()
ids = [k["id"] for k in cat["kinds"]]
check("реестр отдан", len(ids) == 4, ids)
check("у каждого вида есть сборщик",
      all(k in outputs.BUILDERS for k in outputs.KINDS), list(outputs.KINDS))
check("и наоборот — сборщика без карточки нет",
      set(outputs.BUILDERS) == set(outputs.KINDS))
check("у каждого вида описаны поля формы",
      all(isinstance(k.get("params"), list) for k in cat["kinds"]))
check("продукт не обещает отправку почты", cat["can_send_email"] is False, cat)

print("\n== СТАРЫЕ «ИНСТРУМЕНТЫ» БОЛЬШЕ НЕ ОБЕЩАЮТ НЕСУЩЕСТВУЮЩЕЕ ==")
t = c.get("/api/tools", headers=H).json()
check("список не пуст", len(t["tools"]) == 4, t)
check("и в нём нет ни одного «Скоро»",
      all(x["ready"] for x in t["tools"]), t["tools"])
check("страница переехала честно", t["moved_to"] == "results.html", t)
check("endpoint 501 исчез из кода",
      "Этот инструмент ещё готовится" not in
      open("server.py", encoding="utf-8").read())

print("\n== ОТЧЁТ ПО ПРОДАЖАМ: ЦИФРЫ ИЗ БАЗЫ ==")
r = build("sales_report", {"days": 30})
check("собрался", r.status_code == 200, r.text[:200])
rep = r.json()["item"]
check("название по-человечески", rep["title"] == "Отчёт по продажам", rep["title"])
check("подписан бизнесом", rep["subtitle"] == "out_main", rep["subtitle"])
flat = str(rep["doc"])
db_orders = database.orders_period(bid, days=30)
check("сумма заявок совпадает с базой", "50 500 ₽" in flat, flat[:300])
check("а база и правда столько же считает", db_orders["amount"] == 50500, db_orders)
check("средний чек посчитан по заявкам с суммой", "25 250 ₽" in flat)
check("названы источники данных", "Заявки" in rep["sources"], rep["sources"])
check("без ИИ помечено честно", rep["engine"] == "rules", rep["engine"])
check("и это сказано словами", "без ИИ" in rep["engine_ru"] or
      "по вашим данным" in rep["engine_ru"], rep["engine_ru"])
check("без ИИ догадок в документе нет",
      not any(b["type"] == "inference" for b in rep["doc"]["blocks"]))
check("у отчёта есть файл", rep["has_file"] is True, rep)

print("\n== PDF ==")
f = c.get(f"/api/outputs/{rep['id']}/file", headers=H)
check("файл отдаётся", f.status_code == 200, f.status_code)
check("это настоящий PDF", f.content[:5] == b"%PDF-", f.content[:16])
check("файл не пустой", len(f.content) > 3000, len(f.content))
check("отдаётся вложением", "attachment" in f.headers.get("content-disposition", ""))
try:
    from pypdf import PdfReader
    import io as _io
    text = PdfReader(_io.BytesIO(f.content)).pages[0].extract_text()
    check("кириллица читается, а не квадраты", "Отчёт по продажам" in text, text[:120])
    check("в PDF те же цифры, что на экране", "50 500" in text, text[:300])
    check("есть дата сборки", "Собран" in text)
    check("назван источник данных", "Источник данных" in text)
except ImportError:
    check("pypdf доступен для проверки PDF", False, "pypdf не установлен")

print("\n== ФИНАНСОВЫЙ ОТЧЁТ ==")
r = build("finance_report", {"days": 30})
check("собрался", r.status_code == 200, r.text[:200])
fin = r.json()["item"]
flat = str(fin["doc"])
summary = database.finance_summary(bid)
check("прибыль совпадает с базой", "26 500 ₽" in flat, flat[:300])
check("а база и правда столько считает", summary["profit"] == 26500, summary)
check("маржа посчитана один раз и там же", f"{summary['margin']}%" in flat, summary)
check("расходы разложены по категориям", "Закупка цветов" in flat)

print("\n== МАРЖА БЕЗ ВЫРУЧКИ — НЕ НОЛЬ ==")
empty_bid, EH = reg("out_empty")
database.add_finance_entry(empty_bid, "expense", "Аренда", 5000)
r = c.post("/api/outputs", headers=EH,
           json={"kind": "finance_report", "params": {"days": 30}})
flat = str(r.json()["item"]["doc"])
check("вместо «0%» сказано, что делить не на что",
      "делить не на что" in flat and "0%" not in flat, flat[:400])

print("\n== КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ ==")
r = build("offer", {"client_id": client_id})
check("собралось", r.status_code == 200, r.text[:200])
off = r.json()["item"]
check("адресовано клиенту", "Анна Петрова" in (off["subtitle"] or ""), off["subtitle"])
flat = str(off["doc"])
check("цены взяты из памяти бизнеса", "42 000 ₽" in flat and "8 500 ₽" in flat, flat[:400])
check("есть текст для копирования", bool(off["body"]), off["body"])
check("есть PDF", off["has_file"] is True)

print("\n== КП БЕЗ ПОЗИЦИЙ — ОТКАЗ С ПРИЧИНОЙ ==")
r = c.post("/api/outputs", headers=EH, json={"kind": "offer", "params": {}})
check("не собирается", r.status_code == 400, r.status_code)
check("и объяснено почему", "нет ни услуг" in r.json()["detail"], r.json())

print("\n== ПИСЬМО КЛИЕНТУ ==")
r = build("client_letter", {"order_id": order_id, "purpose": "status"})
check("собралось", r.status_code == 200, r.text[:200])
let = r.json()["item"]
check("текст готов", bool(let["body"]), let)
check("обращается по имени", "Анна Петрова" in let["body"], let["body"])
check("называет номер заявки", f"№{order_id}" in let["body"], let["body"])
check("не выдумывает сумму", "8500" not in let["body"] and "8 500" not in let["body"],
      let["body"])
check("файла у письма нет — его копируют, а не скачивают", let["has_file"] is False)
check("сказано, что VELOR его не отправил",
      any("не отправил" in str(b.get("body", "")) for b in let["doc"]["blocks"]),
      let["doc"]["blocks"])

print("\n== ОТПРАВКА ПИСЬМА: ОБХОДА НЕТ ==")
check("в реестре полномочий нет отправки почты",
      not any("email" in a or "mail" in a for a in actions.REGISTRY), list(actions.REGISTRY))
check("и права такого не появилось",
      "send_email" not in actions.REGISTRY)
check("модуль честно говорит, что канала нет", outputs.CAN_SEND_EMAIL is False)
# Отправка касаний клиенту существует и идёт через полномочия (followup.py) —
# это канал Telegram/директа, а не почта. Проверяем именно то, что обещано:
# почтового пути наружу нет ни одного.
mail_routes = [getattr(r_, "path", "") for r_ in server.app.routes
               if "mail" in getattr(r_, "path", "").lower()]
check("почтового endpoint не существует вовсе", not mail_routes, mail_routes)

print("\n== ПИСЬМО БЕЗ АДРЕСАТА ==")
r = build("client_letter", {"purpose": "reminder"})
check("не собирается", r.status_code == 400, r.status_code)
check("и объяснено почему", "адресата" in r.json()["detail"], r.json())

print("\n== ЧУЖОЕ НЕДОСТУПНО ==")
mine = rep["id"]
check("чужой результат не виден списком",
      all(i["id"] != mine for i in c.get("/api/outputs", headers=OH).json()["items"]))
check("чужой результат не открыть по id",
      c.get(f"/api/outputs/{mine}", headers=OH).status_code == 404)
check("чужой файл не скачать",
      c.get(f"/api/outputs/{mine}/file", headers=OH).status_code == 404)
check("чужой результат не удалить",
      c.post(f"/api/outputs/{mine}/delete", headers=OH).status_code == 404)
check("и он при этом цел", c.get(f"/api/outputs/{mine}", headers=H).status_code == 200)

print("\n== БЕЗ ТОКЕНА НИЧЕГО ==")
check("список закрыт", c.get("/api/outputs").status_code == 401)
check("сборка закрыта",
      c.post("/api/outputs", json={"kind": "sales_report"}).status_code == 401)
check("файл закрыт", c.get(f"/api/outputs/{mine}/file").status_code == 401)

print("\n== ИСТОРИЯ ==")
hist = c.get("/api/outputs", headers=H).json()
check("всё собранное в истории", hist["total"] == 4, hist["total"])
check("новые сверху", hist["items"][0]["kind"] == "client_letter", hist["items"][0])
check("в списке нет самого документа", "doc" not in hist["items"][0], hist["items"][0])
check("но видно, по какому запросу собрано",
      isinstance(hist["items"][0]["request"], dict), hist["items"][0]["request"])
check("фильтр по виду работает",
      c.get("/api/outputs?kind=sales_report", headers=H).json()["total"] == 1)
check("ключ файла в хранилище наружу не отдаётся",
      "file_key" not in hist["items"][0], hist["items"][0])

print("\n== УДАЛЕНИЕ УБИРАЕТ И ФАЙЛ ==")
row = database.get_output(rep["id"], bid)
key = row["file_key"]
check("файл лежит в хранилище", bool(storage.get(bid, key)))
check("удаление прошло",
      c.post(f"/api/outputs/{rep['id']}/delete", headers=H).status_code == 200)
check("записи больше нет", database.get_output(rep["id"], bid) is None)
gone = False
try:
    storage.get(bid, key)
except storage.StorageError:
    gone = True
check("и файла в хранилище тоже нет", gone)
check("повторное удаление — 404",
      c.post(f"/api/outputs/{rep['id']}/delete", headers=H).status_code == 404)

print("\n== НЕИЗВЕСТНЫЙ ВИД ==")
r = build("что_нибудь")
check("отказ, а не 500", r.status_code == 400, r.status_code)

print("\n== READ-ONLY ПОСЛЕ ТРИАЛА ==")
database.update_business(bid, trial_start="2020-01-01", trial_end="2020-01-15",
                         subscription_status="expired", trial_used=1)
check("собирать нельзя", build("sales_report", {"days": 30}).status_code == 402)
check("удалять нельзя",
      c.post(f"/api/outputs/{fin['id']}/delete", headers=H).status_code == 402)
check("но историю видно", c.get("/api/outputs", headers=H).status_code == 200)
check("и файл собранного раньше по-прежнему отдаётся",
      c.get(f"/api/outputs/{fin['id']}/file", headers=H).status_code == 200)

print("\n== УДАЛЕНИЕ КОМПАНИИ УНОСИТ И РЕЗУЛЬТАТЫ ==")
database.delete_business(other)
tmp_bid, TH = reg("out_tmp")
c.post("/api/outputs", headers=TH, json={"kind": "sales_report", "params": {"days": 7}})
check("результат у компании есть", database.count_outputs(tmp_bid) == 1)
database.delete_business(tmp_bid)
check("после удаления компании их нет", database.count_outputs(tmp_bid) == 0)

print(f"\nИТОГО: успешно {ok}, провалено {fail}")
sys.exit(1 if fail else 0)
