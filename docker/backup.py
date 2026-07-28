# -*- coding: utf-8 -*-
"""
Резервное копирование VELOR: согласованный снимок базы (SQLite Online Backup API,
безопасно даже при работающем сервере) + архив пользовательских файлов /app/uploads.
Результат — один .tar.gz в /backups. Старые копии старше BACKUP_KEEP_DAYS удаляются.

Запускается по расписанию сервисом `backup` из docker-compose. Можно и вручную:
    docker compose run --rm backup python docker/backup.py
"""
import os
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "/app/data/assistant.db")
UPLOADS_DIR = os.getenv("UPLOADS_DIR", "/app/uploads")
BACKUP_DIR = os.getenv("BACKUP_DIR", "/backups")
KEEP_DAYS = int(os.getenv("BACKUP_KEEP_DAYS", "14"))


def _snapshot_db(dest_path: str) -> bool:
    """Согласованная копия БД через Online Backup API (не рвёт работу сервера)."""
    if not os.path.exists(DB_PATH):
        print(f"[backup] БД не найдена: {DB_PATH} — пропускаю")
        return False
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return True


def make_backup() -> str | None:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(BACKUP_DIR, f"velor-{stamp}.tar.gz")

    with tempfile.TemporaryDirectory() as tmp:
        db_copy = os.path.join(tmp, "assistant.db")
        has_db = _snapshot_db(db_copy)
        with tarfile.open(out, "w:gz") as tar:
            if has_db:
                tar.add(db_copy, arcname="assistant.db")
            if os.path.isdir(UPLOADS_DIR) and os.listdir(UPLOADS_DIR):
                tar.add(UPLOADS_DIR, arcname="uploads")
    print(f"[backup] Готово: {out} ({os.path.getsize(out)} байт)")
    return out


def prune_old():
    if not os.path.isdir(BACKUP_DIR):
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for name in os.listdir(BACKUP_DIR):
        if name.startswith("velor-") and name.endswith(".tar.gz"):
            p = os.path.join(BACKUP_DIR, name)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                print(f"[backup] Удалена старая копия: {name}")


if __name__ == "__main__":
    make_backup()
    prune_old()
