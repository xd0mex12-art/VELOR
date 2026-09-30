# -*- coding: utf-8 -*-
"""
Дверной звонок сервиса: /api/alive.

Проверяем ровно две вещи, обе стоили нам суток простоя 2026-09-27:

  1. Пока база жива — ручка отвечает 200. По ней раз в 6 часов стучится
     keepalive из GitHub Actions, и именно этот запрос не даёт бесплатному
     Supabase уснуть.
  2. Когда база мертва — сервер НЕ умирает, а честно отвечает 503 с причиной.
     Раньше падение базы убивало приложение до старта, и снаружи была видна
     только бесконечная страница загрузки Render.
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
os.environ["GIGACHAT_AUTH_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"
sys.stdout.reconfigure(encoding="utf-8")

ROOT = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient
import server, database

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  OK  ", name)
    else:
        fail += 1; print("  FAIL", name, extra)


c = TestClient(server.app)

# ── 1. База жива ───────────────────────────────────────────────────────────
r = c.get("/api/alive")
check("живая база → 200", r.status_code == 200, r.text)
check("живая база → ok=True", r.status_code == 200 and r.json().get("ok") is True, r.text)

check("database.ping() не падает", database.ping() is True)

# Вход не требуется: запрос выше сделан без заголовка x-auth и получил 200 —
# это и есть доказательство, отдельной проверки не нужно.

# ── 2. База мертва ─────────────────────────────────────────────────────────
# Подменяем ping на падающий — сервер должен пережить это и объяснить причину.
_real_ping = database.ping


def _dead_ping():
    raise RuntimeError("tenant/user postgres.xxx not found")


database.ping = _dead_ping
try:
    r = c.get("/api/alive")
    check("мёртвая база → 503", r.status_code == 503, r.text)
    check("мёртвая база → причина в ответе",
          r.status_code == 503 and "tenant" in r.text, r.text)
    check("мёртвая база → сервер продолжает отвечать",
          c.get("/api/public/plans").status_code == 200)
finally:
    database.ping = _real_ping

# ── 3. Ошибка старта видна на /api/alive ───────────────────────────────────
# Если бы init_db упал при запуске, DB_ERROR был бы заполнен и ручка сказала
# бы об этом, не трогая базу вовсе.
_real_err = server.DB_ERROR
server.DB_ERROR = "не удалось подключиться при старте"
try:
    r = c.get("/api/alive")
    check("ошибка старта → 503", r.status_code == 503, r.text)
    check("ошибка старта → текст причины",
          "при старте" in r.text, r.text)
finally:
    server.DB_ERROR = _real_err

check("после восстановления снова 200", c.get("/api/alive").status_code == 200)

print("\nуспешно %d, провалено %d" % (ok, fail))
sys.exit(1 if fail else 0)
