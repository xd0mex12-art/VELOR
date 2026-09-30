# -*- coding: utf-8 -*-
"""
Стенд для просмотра страниц глазами — отдельная база и тестовый вход.

Нужен, чтобы посмотреть страницу кабинета, не трогая ни боевой сервер, ни
настоящий .env. База лежит во временной папке, логин и пароль здесь нарочно
простые и годятся ТОЛЬКО для localhost: никаких настоящих ключей тут нет и
быть не должно.

    python tools/stand.py          → http://127.0.0.1:8011
"""
import os
import pathlib
import sys

STAND = pathlib.Path(os.environ.get("TEMP", ".")) / "velor_stand"
STAND.mkdir(exist_ok=True)

os.environ.setdefault("DB_PATH", str(STAND / "stand.db"))
os.environ.setdefault("LOG_DIR", str(STAND))
os.environ.setdefault("UPLOAD_DIR", str(STAND / "uploads"))
os.environ["APP_ENV"] = "development"
os.environ["OWNER_LOGIN"] = "standowner"
os.environ["OWNER_PASSWORD"] = "stand-parol-9"
os.environ["JWT_SECRET"] = "stand-jwt"
os.environ["SECRET_KEY"] = "stand-box"
os.environ["DATABASE_URL"] = ""
os.environ["DISABLE_SYNC_WORKER"] = "1"

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import uvicorn

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8011, log_level="info")
