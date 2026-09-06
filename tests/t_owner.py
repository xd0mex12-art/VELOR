# -*- coding: utf-8 -*-
"""
Свой аккаунт VELOR с полномочиями владельца.

Здесь проверяется самое дорогое, что есть в мультиарендном продукте: граница
между компаниями. Один аккаунт получает право видеть чужие данные — и ровно
поэтому каждый способ получить это право не тем аккаунтом должен быть закрыт.

Проверяем:
  1) без переменной окружения не меняется НИЧЕГО — ни один вход не получает
     лишнего, поведение продукта в точности прежнее;
  2) полномочие даёт только точное совпадение логина с переменной, и только
     вместе с верным паролем;
  3) обычный бизнес по-прежнему видит только себя, что бы он ни передал в
     параметре business_id;
  4) свой аккаунт открывает свой кабинет без всяких параметров, а чужой —
     только назвав его явно;
  5) выдать себе это полномочие через приложение нельзя: ни регистрацией с
     нужным логином постфактум, ни правкой своего профиля.
"""
import os, sys, tempfile, pathlib

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["LOG_DIR"] = str(TMP)
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "testowner"
os.environ["OWNER_PASSWORD"] = "s3cret-owner"
os.environ["JWT_SECRET"] = "test-secret-owner"
os.environ["SECRET_KEY"] = "test-box-key"
os.environ["DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
os.environ["REGISTER_MAX"] = "200"
os.environ.pop("OWNER_BUSINESS_LOGIN", None)     # начинаем с «ничего не задано»
sys.stdout.reconfigure(encoding="utf-8")
ROOT = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient                            # noqa: E402
import server, database, auth                                        # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


c = TestClient(server.app)


def reg(login, name):
    d = c.post("/api/register", json={"name": name, "login": login,
                                      "password": "pass123", "consent": True}).json()
    return d["business_id"], {"X-Auth": d["token"]}


def login(l, p="pass123"):
    return c.post("/api/business-login", json={"login": l, "password": p})


def role_of(token):
    return (auth.decode_access_token(token) or {}).get("role")


def bid_of(token):
    return (auth.decode_access_token(token) or {}).get("bid")


mine, HM = reg("velor-own", "VELOR")               # будущий свой аккаунт
alien, HA = reg("alien-shop", "Чужой магазин")     # обычный клиент
database.add_fact(alien, "service", "Секрет чужого", "это не должно утечь")

print("== БЕЗ ПЕРЕМЕННОЙ НЕ МЕНЯЕТСЯ НИЧЕГО ==")
r = login("velor-own")
check("вход обычный", r.status_code == 200, r.text)
check("роль — бизнес, а не владелец", role_of(r.json()["token"]) == "business",
      role_of(r.json()["token"]))
check("и полномочий владельца нет", r.json().get("owner") in (False, None), r.json())
own_h = {"X-Auth": r.json()["token"]}
check("сводка по клиентам закрыта",
      c.get("/api/admin/trial-overview", headers=own_h).status_code in (401, 403))
check("и чужой кабинет не открыть",
      c.get("/api/orders?business_id=%d" % alien, headers=own_h).status_code == 200)
# 200 здесь — это НЕ доступ: параметр просто игнорируется, отдаётся своё.
mine_orders = c.get("/api/orders?business_id=%d" % alien, headers=own_h).json()
alien_orders = c.get("/api/orders", headers=HA).json()
check("параметр business_id обычным аккаунтом игнорируется",
      mine_orders != alien_orders or not alien_orders.get("orders"),
      (mine_orders, alien_orders))

print("\n== ПЕРЕМЕННАЯ ЗАДАНА — ПОЛНОМОЧИЯ ПОЯВИЛИСЬ ==")
os.environ["OWNER_BUSINESS_LOGIN"] = "velor-own"
r = login("velor-own")
check("вход тем же логином и паролем", r.status_code == 200, r.text)
tok = r.json()["token"]
check("роль стала владельческой", role_of(tok) == "owner", role_of(tok))
check("но номер своего бизнеса в пропуске остался", bid_of(tok) == mine, bid_of(tok))
check("и вход честно об этом сказал", r.json().get("owner") is True, r.json())
OWN = {"X-Auth": tok}

check("сводка по клиентам открылась",
      c.get("/api/admin/trial-overview", headers=OWN).status_code == 200)
check("список всех бизнесов тоже",
      c.get("/api/admin/overview", headers=OWN).status_code == 200)

print("\n== СВОЙ КАБИНЕТ ОТКРЫВАЕТСЯ БЕЗ ПАРАМЕТРОВ ==")
# Требовать от себя номер компании там, где клиент ничего не указывает, значило
# бы сделать свой продукт неудобнее чужого.
r = c.get("/api/orders", headers=OWN)
check("свои заказы отдаются", r.status_code == 200, r.text)
r = c.get("/api/home", headers=OWN)
check("и своя главная", r.status_code == 200, r.status_code)
check("чужой кабинет открывается, но только по явному номеру",
      c.get("/api/orders?business_id=%d" % alien, headers=OWN).status_code == 200)
check("несуществующая компания — 404",
      c.get("/api/orders?business_id=999999", headers=OWN).status_code == 404)

print("\n== ГРАНИЦА ДЛЯ ОСТАЛЬНЫХ НЕ СДВИНУЛАСЬ ==")
# Главная проверка файла. Полномочие выдано одному логину — и ничего не должно
# протечь ни к кому другому.
check("чужой аккаунт не получил полномочий", role_of(login("alien-shop").json()["token"]) == "business")
check("чужому сводка по-прежнему закрыта",
      c.get("/api/admin/trial-overview", headers=HA).status_code in (401, 403))
check("и чужой список бизнесов тоже",
      c.get("/api/admin/overview", headers=HA).status_code in (401, 403))
before = c.get("/api/orders", headers=HA).json()
after = c.get("/api/orders?business_id=%d" % mine, headers=HA).json()
check("чужой аккаунт с чужим номером получает СВОИ данные", before == after, (before, after))

print("\n== ПОЛНОМОЧИЕ НЕ ВЫДАТЬ ЧЕРЕЗ ПРИЛОЖЕНИЕ ==")
# Логин задаётся переменной окружения. Значит единственный способ его получить —
# доступ к настройкам сервера. Ни регистрация, ни правка профиля не годятся.
r = c.post("/api/register", json={"name": "Самозванец", "login": "velor-own",
                                  "password": "pass123", "consent": True})
check("занять тот же логин регистрацией нельзя", r.status_code >= 400, r.status_code)
# Логин вообще не входит в форму настроек бизнеса — поля такого нет. Это
# защита сильнее любой проверки: менять нечему.
r = c.post("/api/business", headers=HA,
           json={"login": "velor-own", "name": "Чужой магазин"})
check("настройки сохраняются", r.status_code == 200, r.text)
check("но логин ими не меняется — такого поля в форме нет",
      (database.get_business(alien) or {}).get("login") == "alien-shop",
      (database.get_business(alien) or {}).get("login"))
check("после попытки чужак всё ещё без полномочий",
      role_of(login("alien-shop").json()["token"]) == "business")

print("\n== ПАРОЛЬ ПРОВЕРЯЕТСЯ КАК ОБЫЧНО ==")
check("неверный пароль не пускает даже свой логин",
      login("velor-own", "не-тот-пароль").status_code == 401)
check("несуществующий логин — тоже", login("velor-nope").status_code == 401)

print("\n== СОВПАДЕНИЕ ЛОГИНА — ТОЧНОЕ ==")
os.environ["OWNER_BUSINESS_LOGIN"] = "VELOR-OWN"
check("регистр в переменной значения не имеет",
      role_of(login("velor-own").json()["token"]) == "owner")
os.environ["OWNER_BUSINESS_LOGIN"] = "velor"          # похожий, но другой
check("похожий логин полномочий не даёт",
      role_of(login("velor-own").json()["token"]) == "business")
os.environ["OWNER_BUSINESS_LOGIN"] = ""
check("пустая переменная не делает владельцем всех подряд",
      role_of(login("velor-own").json()["token"]) == "business")
check("и тем более никого другого",
      role_of(login("alien-shop").json()["token"]) == "business")

print("\n== СТАРЫЙ ВХОД ВЛАДЕЛЬЦА ЖИВ ==")
# Это запасная дверь. Если переменную впишут с опечаткой, она — единственный
# способ попасть внутрь и починить, поэтому ломать её нельзя.
r = c.post("/api/login", json={"login": "testowner", "password": "s3cret-owner"})
check("вход по переменным окружения работает", r.status_code == 200, r.text)
check("роль владельческая", role_of(r.json()["token"]) == "owner")
check("но своего бизнеса у него нет", bid_of(r.json()["token"]) is None)
env_own = {"X-Auth": r.json()["token"]}
check("без номера компании кабинет не открывается — и это правильно",
      c.get("/api/orders", headers=env_own).status_code == 400)
check("а с номером открывается",
      c.get("/api/orders?business_id=%d" % alien, headers=env_own).status_code == 200)

print("\nИТОГО: успешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
