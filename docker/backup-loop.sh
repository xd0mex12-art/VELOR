#!/bin/sh
# Периодический запуск резервного копирования. Интервал — BACKUP_INTERVAL_HOURS.
# Первую копию делаем сразу при старте, дальше — по расписанию.
set -e
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
while true; do
  python docker/backup.py || echo "[backup] Ошибка при копировании, попробую в следующий раз"
  sleep "$(( INTERVAL_HOURS * 3600 ))"
done
