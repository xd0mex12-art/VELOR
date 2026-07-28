#!/bin/sh
# Общая точка входа для веб-сервера и бота: гарантируем каталоги и таблицы БД,
# затем запускаем переданную команду (uvicorn или python bot.py).
set -e

mkdir -p "${LOG_DIR:-/app/logs}" "$(dirname "${DB_PATH:-/app/data/assistant.db}")" /app/uploads

# Создать/дополнить таблицы (идемпотентно). Бот сам БД не инициализирует —
# поэтому делаем это здесь, чтобы оба сервиса стартовали с готовой схемой.
python -c "import database; database.init_db()"

exec "$@"
