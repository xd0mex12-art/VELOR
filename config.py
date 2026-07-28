"""Настройки проекта. Читает секреты из файла .env."""
import os
from dotenv import load_dotenv

load_dotenv()  # подгружаем переменные из файла .env

# Токен Telegram-бота (берётся из .env)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Ключ ИИ (Anthropic Claude) и модель — тоже из .env.
# claude-opus-4-8 — умная (дороже), claude-haiku-4-5 — быстрая и дешёвая.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")

# Российская альтернатива: GigaChat (Сбер). Ключ авторизации из
# личного кабинета developers.sber.ru (есть бесплатный тариф).
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")

# Путь к файлу базы данных. Локально — рядом с проектом; в production задаётся
# в .env (например /app/data/assistant.db, чтобы лежал на постоянном томе).
DB_PATH = os.getenv("DB_PATH", "assistant.db")

# Каталог для логов (errors.log). Локально — текущая папка; в Docker — том /logs.
LOG_DIR = os.getenv("LOG_DIR", ".")

# Демо-бот из .env: к какому бизнесу привязать единственный токен TELEGRAM_TOKEN.
# По умолчанию НЕ задан — тогда демо-бот просто не поднимается. Это осознанная
# настройка, а не «молчаливый бизнес №1»: раньше здесь была константа
# DEFAULT_BUSINESS_ID=1, из-за которой запрос без указания компании мог случайно
# попасть в чужой бизнес. Теперь компанию всегда нужно указывать явно.
_demo = os.getenv("DEMO_BOT_BUSINESS_ID")
DEMO_BOT_BUSINESS_ID = int(_demo) if _demo and _demo.isdigit() else None

# Окружение: development (по умолчанию) или production. В проде запрещаем
# запуск со стандартными учётными данными владельца.
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV in ("production", "prod")

# Вход в личный кабинет владельца VELOR AI (логин/пароль) — только из .env.
# Значения по умолчанию admin/admin оставлены лишь для локального запуска и
# считаются небезопасными: см. owner_creds_are_default() ниже.
OWNER_LOGIN = os.getenv("OWNER_LOGIN", "admin")
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "admin")

# Явно слабые/стандартные пароли, которые нельзя оставлять в проде.
_WEAK_OWNER_PASSWORDS = {
    "admin", "password", "pass", "1234", "12345", "123456",
    "root", "changeme", "qwerty", "velor", "",
}


def owner_creds_are_default() -> bool:
    """
    True, если владелец не сменил стандартные учётные данные на безопасные:
    остался логин admin, слабый/пустой пароль или короче 6 символов.
    """
    return (
        OWNER_LOGIN.strip().lower() == "admin"
        or OWNER_PASSWORD.strip().lower() in _WEAK_OWNER_PASSWORDS
        or len(OWNER_PASSWORD.strip()) < 6
    )


def check_owner_credentials() -> str | None:
    """
    Проверить учётные данные владельца при запуске.
    В production со стандартными значениями — бросаем RuntimeError (сервер не
    стартует). В остальных случаях при небезопасных значениях возвращаем текст
    предупреждения (или None, если всё в порядке).
    """
    if not owner_creds_are_default():
        return None
    msg = (
        "Учётные данные владельца VELOR используют небезопасные значения по "
        "умолчанию (например admin/admin). Задайте OWNER_LOGIN и надёжный "
        "OWNER_PASSWORD (не менее 6 символов) в файле .env."
    )
    if IS_PRODUCTION:
        raise RuntimeError(
            "ОСТАНОВКА ЗАПУСКА (APP_ENV=production): " + msg
        )
    return msg

# ---------- JWT (аутентификация) ----------
# Секрет подписи токенов. Если не задан в .env — auth.py сгенерирует и сохранит
# его в файл .jwt_secret, чтобы токены переживали перезапуск сервера.
JWT_SECRET = os.getenv("JWT_SECRET")
# Время жизни токенов — настраивается через .env.
ACCESS_TTL_MIN = int(os.getenv("ACCESS_TTL_MIN", "30"))      # access-токен: минуты
REFRESH_TTL_DAYS = int(os.getenv("REFRESH_TTL_DAYS", "30"))  # refresh-токен: дни

# ---------- Защита от перебора паролей (rate limiting) ----------
# Параметры подобраны так, чтобы не мешать живому человеку (он редко ошибётся
# больше нескольких раз подряд), но остановить автоматический перебор.
# Вход (owner + бизнес): сколько НЕУДАЧНЫХ попыток с одного IP допускаем за окно,
# на сколько минут блокируем после превышения.
LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "8"))        # попыток за окно
LOGIN_FAIL_WINDOW_MIN = int(os.getenv("LOGIN_FAIL_WINDOW_MIN", "15"))  # окно, минуты
LOGIN_BLOCK_MIN = int(os.getenv("LOGIN_BLOCK_MIN", "15"))       # блокировка, минуты
# Регистрация: сколько аккаунтов можно создать с одного IP за окно (антиспам).
REGISTER_MAX = int(os.getenv("REGISTER_MAX", "5"))             # регистраций за окно
REGISTER_WINDOW_MIN = int(os.getenv("REGISTER_WINDOW_MIN", "60"))  # окно, минуты
