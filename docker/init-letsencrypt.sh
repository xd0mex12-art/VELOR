#!/bin/sh
# Первичный выпуск HTTPS-сертификата Let's Encrypt.
# Запусти ОДИН раз после того, как домен направлен на сервер (A-запись) и заполнен .env:
#     sh docker/init-letsencrypt.sh
# Дальше сертификат продлевается автоматически сервисом certbot.
set -e

# берём DOMAIN и LETSENCRYPT_EMAIL из .env
set -a; . ./.env; set +a
: "${DOMAIN:?Укажи DOMAIN в .env}"
: "${LETSENCRYPT_EMAIL:?Укажи LETSENCRYPT_EMAIL в .env}"

CONF="./certbot/conf"
WWW="./certbot/www"
LIVE="$CONF/live/$DOMAIN"

mkdir -p "$LIVE" "$WWW"

echo "1/4 Создаю временный самоподписанный сертификат (чтобы nginx смог стартовать)…"
docker run --rm -v "$PWD/certbot/conf:/etc/letsencrypt" certbot/certbot:latest \
  sh -c "openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout '/etc/letsencrypt/live/$DOMAIN/privkey.pem' \
    -out '/etc/letsencrypt/live/$DOMAIN/fullchain.pem' \
    -subj '/CN=$DOMAIN'" || true

echo "2/4 Поднимаю nginx…"
docker compose up -d nginx

echo "3/4 Запрашиваю настоящий сертификат…"
docker run --rm \
  -v "$PWD/certbot/conf:/etc/letsencrypt" \
  -v "$PWD/certbot/www:/var/www/certbot" \
  certbot/certbot:latest certonly --webroot -w /var/www/certbot \
  -d "$DOMAIN" --email "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email --force-renewal

echo "4/4 Перезапускаю nginx с настоящим сертификатом…"
docker compose exec nginx nginx -s reload || docker compose restart nginx

echo "Готово. HTTPS для $DOMAIN активен."
