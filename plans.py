# -*- coding: utf-8 -*-
"""
ТАРИФЫ VELOR — единственный источник истины.

До этого файла каталогов было три, и они не совпадали:
  database.PLANS  — free/starter/business/pro по 0/2990/9990/24990 (лимит сообщений),
  trial.PLANS     — starter/business/enterprise по 2990/9990/24990,
  web/plans.html  — рисовал первый из них как «тариф VELOR».
Плюс две колонки под одно и то же: businesses.plan (легаси) и
businesses.subscription_plan. Цена, которую видел владелец, зависела от того,
на какую страницу он попал, — а это худший вид ошибки в продукте, который берёт
деньги. Здесь один каталог; всё остальное читает его и ничего не дублирует.

УСТРОЙСТВО. Доступ определяется не сравнением `plan == "business"`, а
ВОЗМОЖНОСТЯМИ тарифа:

    ПОДПИСКА → ТАРИФ → ВОЗМОЖНОСТИ → ДОСТУП

Из-за этого тариф можно переименовать, разделить или собрать заново, не трогая
ни одной проверки в server.py: меняется таблица, а не код.

ЧЕСТНОСТЬ. В каталоге нет ни одной возможности, которой нет в продукте. То, что
задумано, но ещё не сделано (несколько бизнес-единиц), помечено `ready=False` и
на странице тарифов показывается как «готовится», а не как доступное. Продавать
несуществующее — быстрый способ потерять первого же клиента.
"""

# ---------- ВОЗМОЖНОСТИ ----------
# Ключ → как объяснить его владельцу и есть ли он в продукте сегодня.
# `ready=False` значит: возможность объявлена, но кода за ней ещё нет.
ENTITLEMENTS = {
    "analytics_basic": {
        "title": "Базовая аналитика",
        "note": "Панель, показатели, клиенты, возможности, финансы, источники данных.",
        "ready": True,
    },
    "ai_director": {
        "title": "AI Director",
        "note": "Утренний брифинг: что происходит, где проблема, что делать дальше.",
        "ready": True,
    },
    "insights_auto": {
        "title": "Автоматическое выявление проблем",
        "note": "VELOR сам находит, где теряются деньги, и приносит находку с расчётом.",
        "ready": True,
    },
    "analytics_advanced": {
        "title": "Расширенная аналитика",
        "note": "Связи между сущностями, недельный разбор, совет директоров, понимание бизнеса.",
        "ready": True,
    },
    "multi_unit": {
        "title": "Несколько направлений и точек",
        "note": "Сравнение филиалов между собой в одном кабинете.",
        "ready": False,          # ещё не построено — показываем как «готовится»
    },
    "priority_support": {
        "title": "Приоритетная поддержка",
        "note": "Ответ в течение рабочего дня и помощь с настройкой.",
        "ready": True,           # это обещание человека, а не код
    },
}

# ---------- ТАРИФЫ ----------
# limits:
#   sources  — сколько источников данных можно подключить (0 = без ограничения)
#   history  — на сколько дней назад доступна история и графики
#   messages — сколько сообщений клиентов в месяц обрабатывает ИИ-сотрудник
PLANS = {
    "start": {
        "name": "VELOR START",
        "price": 4900,
        "tagline": "Для небольшого бизнеса.",
        "entitlements": ["analytics_basic", "ai_director"],
        "limits": {"sources": 3, "history": 90, "messages": 2000},
    },
    "business": {
        "name": "VELOR BUSINESS",
        "price": 9900,
        "tagline": "Основной тариф.",
        "popular": True,
        "entitlements": ["analytics_basic", "ai_director",
                         "insights_auto", "analytics_advanced"],
        "limits": {"sources": 10, "history": 365, "messages": 10000},
    },
    "network": {
        "name": "VELOR NETWORK",
        "price": 19900,
        "tagline": "Для компаний с несколькими направлениями и точками.",
        "entitlements": ["analytics_basic", "ai_director",
                         "insights_auto", "analytics_advanced",
                         "multi_unit", "priority_support"],
        "limits": {"sources": 0, "history": 1095, "messages": 50000},
    },
}

ORDER = ["start", "business", "network"]
DEFAULT_PLAN = "business"

# Тариф, по которому живёт пробный период и все аккаунты без подписки, заведённые
# до появления тарифной сетки. Оба случая — «человек ещё ничего не выбрал», и
# показать ему урезанный продукт значит соврать о том, что он покупает.
TRIAL_PLAN = "business"

# ---------- НАСТРОЙКА ----------
# Разовая услуга, не тариф: у неё нет срока и возможностей, она не продлевается.
SETUP = {
    "key": "setup",
    "name": "VELOR SETUP",
    "price": 15000,
    "tagline": "Дайте нам данные — мы настроим VELOR под ваш бизнес.",
    "includes": [
        "Создание бизнес-профиля",
        "Настройка целей и показателей",
        "Подключение источников данных",
        "Настройка AI Director",
        "Загрузка и структурирование знаний",
        "Подключение Telegram, если нужно",
        "Проверка данных и первичная аналитика",
        "Первый Executive Briefing",
        "Помощь в запуске",
    ],
}

# ---------- FOUNDER PILOT ----------
# Условия первых компаний. Это НЕ тариф и не должен попадать в каталог: иначе он
# однажды окажется на публичной странице и его начнут спрашивать все.
# Механика: флаг businesses.founder_pilot. Настройка бесплатна, первый месяц
# BUSINESS идёт по цене START, дальше — обычная цена.
FOUNDER_PILOT = {
    "setup_price": 0,
    "first_month_price": PLANS["start"]["price"],   # 4 900 ₽
    "then_price": PLANS["business"]["price"],       # 9 900 ₽
    "plan": "business",
}


# ---------- ЧТЕНИЕ КАТАЛОГА ----------
def normalize(key):
    """Привести значение тарифа к ключу каталога.

    Терпимо к истории: в базе живут «Старт», «business», пустые значения и
    ключи прежних каталогов (starter/pro/enterprise/free). Ни один такой
    аккаунт нельзя уронить в ошибку — их владельцы ни в чём не виноваты.
    """
    k = (key or "").strip().lower()
    if k in PLANS:
        return k
    return _LEGACY.get(k, DEFAULT_PLAN)


# Куда переезжают ключи прежних каталогов. Соответствие по смыслу, а не по
# названию: «pro» и «enterprise» были верхним тарифом — им NETWORK.
_LEGACY = {
    "free": "start",
    "старт": "start",
    "starter": "start",
    "business": "business",
    "бизнес": "business",
    "pro": "network",
    "enterprise": "network",
    "network": "network",
}


def get(key):
    """Тариф целиком по ключу (всегда что-то возвращает — см. normalize)."""
    return PLANS[normalize(key)]


def price(key):
    return get(key)["price"]


def name(key):
    return get(key)["name"]


def limits(key):
    return dict(get(key)["limits"])


def limit(key, what):
    """Один лимит тарифа. 0 означает «без ограничения»."""
    return limits(key).get(what, 0)


def entitlements(key):
    """Набор возможностей тарифа."""
    return set(get(key)["entitlements"])


def has(key, entitlement):
    """Есть ли у тарифа возможность. Единственный способ спрашивать о доступе."""
    return entitlement in entitlements(key)


def public(key=None):
    """
    Каталог для страницы тарифов. Отдаётся сервером — во фронте не должно быть
    ни одной цены и ни одного правила доступа: две копии рано или поздно
    разойдутся, и разойдутся именно в цене.
    """
    out = []
    for k in ORDER:
        p = PLANS[k]
        out.append({
            "key": k,
            "name": p["name"],
            "price": p["price"],
            "tagline": p["tagline"],
            "popular": bool(p.get("popular")),
            "current": (normalize(key) == k) if key else False,
            "limits": dict(p["limits"]),
            "features": [
                {"key": e,
                 "title": ENTITLEMENTS[e]["title"],
                 "note": ENTITLEMENTS[e]["note"],
                 "ready": ENTITLEMENTS[e]["ready"]}
                for e in p["entitlements"]
            ],
        })
    return out
