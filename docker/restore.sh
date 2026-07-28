#!/bin/sh
# Восстановление VELOR из резервной копии.
# Использование:
#     sh docker/restore.sh backups/velor-20260726-030000.tar.gz
# Останавливает сервисы, разворачивает БД и файлы из архива, поднимает обратно.
set -e

ARCHIVE="$1"
[ -n "$ARCHIVE" ] || { echo "Укажи путь к архиву: sh docker/restore.sh backups/velor-….tar.gz"; exit 1; }
[ -f "$ARCHIVE" ] || { echo "Файл не найден: $ARCHIVE"; exit 1; }

echo "Останавливаю сервисы, использующие базу…"
docker compose stop web bot backup

echo "На всякий случай сохраняю текущую базу рядом (assistant.db.before-restore)…"
[ -f ./data/assistant.db ] && cp ./data/assistant.db ./data/assistant.db.before-restore || true

TMP="$(mktemp -d)"
tar -xzf "$ARCHIVE" -C "$TMP"

if [ -f "$TMP/assistant.db" ]; then
  mkdir -p ./data
  cp "$TMP/assistant.db" ./data/assistant.db
  echo "База восстановлена."
fi
if [ -d "$TMP/uploads" ]; then
  mkdir -p ./uploads
  cp -r "$TMP/uploads/." ./uploads/
  echo "Файлы восстановлены."
fi
rm -rf "$TMP"

echo "Поднимаю сервисы…"
docker compose up -d web bot backup
echo "Готово. Проверь сайт и вход."
