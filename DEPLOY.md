# VELOR — развёртывание на сервере (production)

Полное руководство: установка, настройка, запуск, обновление, резервные копии и
восстановление. Рассчитано на чистый VPS с Ubuntu 22.04/24.04.

---

## Что внутри

Проект поднимается в контейнерах Docker одной командой. Сервисы:

| Сервис    | Что делает                                             |
|-----------|--------------------------------------------------------|
| `web`     | Сайт и API (FastAPI + uvicorn), внутренний порт 8000   |
| `bot`     | Telegram-бот (long polling)                            |
| `nginx`   | Reverse proxy: HTTPS, редирект HTTP→HTTPS, gzip, кеш, безопасные заголовки |
| `certbot` | Автопродление TLS-сертификата Let's Encrypt            |
| `backup`  | Ежедневная резервная копия базы и файлов               |

Структура каталогов на сервере:

```
VELOR/
├── app-код (server.py, bot.py, web/, …)   # образ приложения
├── docker/        # Dockerfile, entrypoint, backup, restore, init-letsencrypt
├── nginx/         # конфиг reverse proxy
├── data/          # база assistant.db (постоянный том)
├── logs/          # errors.log + логи nginx
├── uploads/       # пользовательские файлы (резерв)
├── backups/       # архивы резервных копий
├── certbot/       # TLS-сертификаты
├── docker-compose.yml
└── .env           # ВСЕ секреты (создаёшь сам из .env.production.example)
```

---

## 1. Подготовка сервера

```bash
# обновить систему
sudo apt update && sudo apt upgrade -y

# установить Docker + Compose
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER    # чтобы docker работал без sudo (перезайди в сессию)

# firewall: пускаем только SSH и веб
sudo ufw allow OpenSSH
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

Направь домен на сервер: в DNS создай **A-запись** `velor.example.ru → IP-сервера`.
Дождись, пока `ping velor.example.ru` показывает нужный IP.

---

## 2. Загрузка проекта

Скопируй проект на сервер (git clone или scp) в папку, например `~/VELOR`, и зайди в неё:

```bash
cd ~/VELOR
```

---

## 3. Настройка .env

```bash
cp .env.production.example .env
nano .env
```

Заполни обязательно:

- `DOMAIN` — твой домен, `LETSENCRYPT_EMAIL` — почта для сертификата;
- `APP_ENV=production`;
- `OWNER_LOGIN` и `OWNER_PASSWORD` — вход владельца (пароль надёжный, ≥6 символов);
- `JWT_SECRET` — сгенерируй: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`;
- `ANTHROPIC_API_KEY` и/или `GIGACHAT_AUTH_KEY` — ключи ИИ.

Закрой файл от чужих глаз:

```bash
chmod 600 .env
chmod 700 data backups
```

> Токены Telegram-ботов обычно задаются каждому бизнесу прямо в кабинете
> (Настройки → Токен бота), поэтому `TELEGRAM_TOKEN` можно оставить пустым.

---

## 4. Первый запуск (HTTPS + сервисы)

Выпусти сертификат (один раз) и подними всё:

```bash
sh docker/init-letsencrypt.sh      # выпуск TLS-сертификата
docker compose up -d --build       # сборка и запуск всех сервисов
```

Проверь, что всё поднялось:

```bash
docker compose ps          # у всех статус running/healthy
docker compose logs -f web # логи сервера (Ctrl+C — выйти)
```

Открой `https://velor.example.ru` — должен открыться сайт.
Кабинет владельца: `https://velor.example.ru/login.html`.

---

## 5. Обновление (выкатка новой версии)

```bash
cd ~/VELOR
git pull                       # или залей новые файлы
docker compose up -d --build   # пересобрать и перезапустить
```

База, логи и загрузки лежат в томах `data/`, `logs/`, `uploads/` и при обновлении
не теряются.

### Postgres вместо SQLite (обязательно для хостингов с эфемерным диском)

На Docker-развёртывании SQLite лежит на постоянном томе `data/` — этого достаточно.
Но на платформах вроде **Render free** диск эфемерный: SQLite стирается при каждом
передеплое. Чтобы данные клиента жили постоянно, задайте `DATABASE_URL` — приложение
само переключится на Postgres (адаптер уже встроен, менять код не нужно):

```
DATABASE_URL=postgresql://USER:PASSWORD@HOST:6543/postgres
```

Строку возьмите в Supabase → Project Settings → Database → Connection string →
**Transaction pooler** (порт 6543). Пустой `DATABASE_URL` = прежний режим SQLite.

---

## 6. Резервное копирование

Копии создаются **автоматически** раз в сутки (сервис `backup`) и складываются в
`backups/` в виде `velor-ГГГГММДД-ЧЧММСС.tar.gz` (база + файлы). Хранятся
`BACKUP_KEEP_DAYS` дней (по умолчанию 14).

Сделать копию прямо сейчас:

```bash
docker compose run --rm backup python docker/backup.py
```

Скачать копию к себе на компьютер (с локальной машины):

```bash
scp user@server:~/VELOR/backups/velor-*.tar.gz ./
```

> Совет: раз в неделю копируй свежий архив с сервера в другое место — тогда данные
> переживут даже потерю самого сервера.

---

## 7. Восстановление после сбоя

```bash
# посмотреть доступные копии
ls -1 backups/

# восстановить из выбранной копии
sh docker/restore.sh backups/velor-20260726-030000.tar.gz
```

Скрипт остановит сервисы, вернёт базу и файлы из архива (старую базу сохранит
рядом как `assistant.db.before-restore`) и снова поднимет сервисы.

Если сервер погиб полностью: подними новый по шагам 1–4, положи архив в `backups/`
и выполни `restore.sh`.

---

## Полезные команды

```bash
docker compose ps                 # статус сервисов
docker compose logs -f web        # логи сервера
docker compose logs -f bot        # логи бота
docker compose restart web        # перезапустить сервер
docker compose down               # остановить всё
docker compose up -d              # поднять всё
```

Журнал ошибок также виден в кабинете владельца на странице «Журнал ошибок».

---

## Диагностика

- **Сайт не открывается по HTTPS** — проверь A-запись домена и что `init-letsencrypt.sh`
  отработал без ошибок; посмотри `docker compose logs nginx`.
- **502 Bad Gateway** — сервис `web` ещё поднимается или упал: `docker compose logs web`.
- **Бот не отвечает** — задан ли токен бизнесу в кабинете; `docker compose logs bot`.
- **Сертификат не выпускается** — 80-й порт должен быть открыт и свободен, домен —
  указывать на этот сервер.
