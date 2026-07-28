-- СХЕМА БАЗЫ ДАННЫХ (структура таблиц).
-- Главная идея универсальности: НИЧЕГО про конкретный бизнес не зашито в код.
-- Любой бизнес (Флоренция, кофейня, барбершоп...) — это строка в таблице businesses.
-- Все заказы и клиенты привязаны к business_id, поэтому данные разных бизнесов не смешиваются.

-- 1) БИЗНЕСЫ (тенанты). Каждая строка = один подключённый бизнес.
CREATE TABLE IF NOT EXISTS businesses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,          -- название бизнеса
    about        TEXT,                      -- чем занимается (для ИИ: «кофейня», «барбершоп»...)
    tg_bot_token TEXT,                      -- (на будущее) свой бот у каждого бизнеса
    greeting     TEXT,                      -- приветствие клиенту в чате
    created_at   TEXT    DEFAULT (datetime('now'))
);

-- 2) КЛИЕНТЫ. Привязаны к бизнесу. Один и тот же человек у разных
--    бизнесов — это РАЗНЫЕ строки (данные изолированы).
CREATE TABLE IF NOT EXISTS clients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    tg_user_id   INTEGER,                   -- id пользователя в Telegram
    name         TEXT,
    phone        TEXT,
    created_at   TEXT    DEFAULT (datetime('now')),
    UNIQUE(business_id, tg_user_id)         -- один клиент на бизнес не дублируется
);

-- 2а) ИСТОРИЯ СООБЩЕНИЙ. Нужна, чтобы ИИ помнил разговор с клиентом.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    client_id    INTEGER NOT NULL REFERENCES clients(id),
    role         TEXT    NOT NULL,           -- 'user' (клиент) или 'assistant' (ИИ)
    content      TEXT    NOT NULL,
    created_at   TEXT    DEFAULT (datetime('now'))
);

-- 3) ЗАКАЗЫ. Тоже привязаны к бизнесу и (если известен) к клиенту.
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    client_id    INTEGER REFERENCES clients(id),
    text         TEXT    NOT NULL,          -- что заказали (пока просто текст)
    phone        TEXT,
    address      TEXT,
    date_wanted  TEXT,                      -- на какую дату нужен заказ
    status       TEXT    DEFAULT 'новый',   -- новый / принят / выполнен / отменён
    created_at   TEXT    DEFAULT (datetime('now'))
);
