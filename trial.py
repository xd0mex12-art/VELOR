# -*- coding: utf-8 -*-
"""
TrialService — единственное место, где живёт логика пробного периода и подписки.
Вся система (эндпоинты, бот, фронт через /api/trial) спрашивает состояние ТОЛЬКО тут.
Никаких «ручных» проверок дат по проекту.

Модель B2B SaaS (как Linear/Notion/HubSpot):
  • новый бизнес получает 14 дней ПОЛНОГО доступа (никаких ограничений);
  • по окончании аккаунт и данные СОХРАНЯЮТСЯ, но активные операции (ИИ, бот,
    создание клиентов/заявок/документов, AI-директор) блокируются — режим «только чтение»;
  • разблокировка — оформлением подписки.

Защита от абьюза: вечный реестр trial_registry (переживает удаление компании) +
device fingerprint. Поля email/telegram в реестре заложены под будущее подтверждение
(им нужна почтовая инфраструктура / verify-флоу бота — включатся, когда появятся).
"""
import datetime

import database
import identity

TRIAL_DAYS = 14

# Каталог подписок (архитектура под будущий Stripe/ЮKassa). Активация сейчас —
# вручную владельцем через activate_subscription(); платёжка позже вызовет ту же
# функцию, ничего не переписывая.
PLANS = {
    "starter":    {"name": "Starter",    "price": 2990,  "note": "1 AI-сотрудник, база знаний, CRM"},
    "business":   {"name": "Business",   "price": 9990,  "note": "до 5 сотрудников, аналитика, финансы, контент"},
    "enterprise": {"name": "Enterprise", "price": 24990, "note": "расширенные лимиты, интеграции, приоритет"},
}


def plan_name(key):
    return (PLANS.get((key or "").lower()) or {}).get("name") or (key or "—")


# ---------- время (храним текстом 'YYYY-MM-DD HH:MM:SS' UTC, как вся база) ----------
def _now():
    return datetime.datetime.utcnow()


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


# ---------- ЖИЗНЕННЫЙ ЦИКЛ ----------
def register_state(bid):
    """Сразу после регистрации: аккаунт в онбординге, отсчёт триала ЕЩЁ НЕ ИДЁТ.
    14 дней стартуют только по кнопке «Запустить VELOR» (см. launch)."""
    database.update_business(bid, subscription_status="onboarding")


def launch(bid, telegram_id=None):
    """Кнопка «Запустить VELOR»: с этого момента идёт отсчёт 14 дней.

    Триал привязывается к ЛИЧНОСТИ ВЛАДЕЛЬЦА (owner_identity), а не к боту:
    фиксируем факт выдачи на личности (переживёт удаление/пересоздание бота).
    Legacy-реестр trial_registry тоже пополняем (обратная совместимость)."""
    now = _now()
    database.update_business(
        bid,
        trial_start=_fmt(now),
        trial_end=_fmt(now + datetime.timedelta(days=TRIAL_DAYS)),
        subscription_status="trial",
        trial_used=1,
    )
    # Главное: помечаем триал выданным на личности владельца.
    try:
        identity.mark_trial_used(bid)
    except Exception:
        pass
    # Legacy-реестр (совместимость со старой защитой по признаку).
    if telegram_id:
        database.record_trial_usage(bid, telegram=telegram_id)


# Совместимость: старое имя start() = немедленный запуск (не используется в
# регистрации, оставлено, чтобы не ломать возможные вызовы).
def start(bid):
    launch(bid)


def activate_subscription(bid, plan="business", months=1):
    """Активировать платную подписку (владелец делает после оплаты)."""
    now = _now()
    database.update_business(
        bid,
        subscription_status="active",
        subscription_plan=plan,
        subscription_started=_fmt(now),
        subscription_expires=_fmt(now + datetime.timedelta(days=30 * max(1, int(months)))),
    )


def extend_trial(bid, days=7):
    """Продлить триал (админка)."""
    b = database.get_business(bid) or {}
    base = _parse(b.get("trial_end")) or _now()
    if base < _now():
        base = _now()
    database.update_business(
        bid, trial_end=_fmt(base + datetime.timedelta(days=int(days))),
        subscription_status="trial")


def set_trial_end(bid, date_str):
    """Задать точную дату окончания триала (админка). Формат 'YYYY-MM-DD [HH:MM:SS]'."""
    database.update_business(bid, trial_end=date_str, subscription_status="trial")


def disable(bid):
    """Завершить триал прямо сейчас — аккаунт уходит в режим «только чтение»."""
    database.update_business(
        bid, trial_end=_fmt(_now() - datetime.timedelta(minutes=1)),
        subscription_status="expired")


# ---------- ЦЕНТРАЛЬНАЯ ПРОВЕРКА ДОСТУПА ----------
def access(business):
    """
    Единственная точка правды. Возвращает состояние аккаунта и что ему разрешено.
    phase: trial | subscribed | locked | legacy
    """
    business = business or {}
    status = (business.get("subscription_status") or "trial").strip().lower()
    now = _now()

    # онбординг: зарегистрирован, но триал ещё НЕ запущен (кнопкой). Полный доступ
    # к настройке; отсчёт 14 дней не идёт. needs_launch=True.
    if status == "onboarding":
        return _state("onboarding", True, business, now)

    if status == "active":
        exp = _parse(business.get("subscription_expires"))
        if exp is None or now < exp:
            return _state("subscribed", True, business, now)
        return _state("locked", False, business, now)

    tend = _parse(business.get("trial_end"))
    if status in ("trial", "") and tend and now < tend:
        return _state("trial", True, business, now)
    if status in ("trial", "") and tend is None:
        # легаси-аккаунт без триал-дат — не ломаем существующих, считаем активным
        return _state("legacy", True, business, now)
    return _state("locked", False, business, now)


def _state(phase, active, business, now):
    tend = _parse(business.get("trial_end"))
    days_left = hours_left = None
    if phase == "trial" and tend:
        secs = (tend - now).total_seconds()
        hours_left = max(0, int(secs // 3600))
        days_left = max(0, int(secs // 86400))
    return {
        "phase": phase,
        "active": active,
        "read_only": not active,
        "launched": phase in ("trial", "subscribed", "legacy"),
        "needs_launch": phase == "onboarding",
        "status": business.get("subscription_status") or "trial",
        "plan": business.get("subscription_plan") or None,
        "trial_end": business.get("trial_end"),
        "subscription_expires": business.get("subscription_expires"),
        "trial_days": TRIAL_DAYS,
        "days_left": days_left,
        "hours_left": hours_left,
        "risk_score": business.get("risk_score") or 0,
        "notice": _notice(phase, hours_left),
    }


def _notice(phase, hours_left):
    """Ненавязчивое уведомление о скором окончании (3 дня / 1 день / <24ч)."""
    if phase != "trial" or hours_left is None:
        return None
    if hours_left < 24:
        return {"level": "urgent", "text": "До окончания пробного периода осталось менее 24 часов."}
    days = hours_left // 24
    if days <= 1:
        return {"level": "soon", "text": "Пробный период заканчивается завтра."}
    if days <= 3:
        return {"level": "info", "text": f"Пробный период заканчивается через {days} дн."}
    return None


def is_active(bid):
    return access(database.get_business(bid)).get("active", False)


# ---------- ЗАЩИТА ОТ АБЬЮЗА (вечный реестр + fingerprint) ----------
def already_used(fingerprint=None, email=None, telegram=None):
    """Выдавался ли уже триал на эти признаки. email/telegram — на будущее."""
    try:
        return database.trial_used_before(fingerprint=fingerprint, email=email, telegram=telegram)
    except Exception:
        return False


def record_usage(bid, fingerprint=None, email=None, telegram=None, ip=None):
    """Записать факт выдачи триала — навсегда, даже если компанию удалят."""
    try:
        database.record_trial_usage(bid, fingerprint=fingerprint, email=email,
                                    telegram=telegram, ip=ip)
    except Exception:
        pass


def assess_risk(fingerprint=None, email=None, telegram=None):
    """
    Оценка риска повторного триала — СИГНАЛ, а не блокировка. Fingerprint даёт лишь
    подозрение (общие устройства); жёстко режем только по Telegram (см. launch).
    Возвращает (risk_score 0..100, список причин).
    """
    score, reasons = 0, []
    try:
        if fingerprint and database.trial_used_before(fingerprint=fingerprint):
            score += 40
            reasons.append("совпал отпечаток устройства")
        if telegram and database.trial_used_before(telegram=telegram):
            score += 60
            reasons.append("этот Telegram уже брал триал")
        if email and database.trial_used_before(email=email):
            score += 50
            reasons.append("этот email уже брал триал")
    except Exception:
        pass
    return min(100, score), reasons


def telegram_used(telegram_id):
    """Legacy: проверка по id БОТА (оставлена для совместимости, слабая защита —
    бота легко пересоздать). Реальная защита — owner_used() по личности владельца."""
    return already_used(telegram=telegram_id) if telegram_id else False


# ---------- ЗАЩИТА ПО ЛИЧНОСТИ ВЛАДЕЛЬЦА (главная) ----------
def owner_used(bid):
    """Использовал ли этот ВЛАДЕЛЕЦ триал ранее. Возвращает (used, reason).
    Делегирует IdentityService (алгоритм: telegram_user_id | phone | email |
    email+fingerprint). Именно это блокирует «новый бот → новый триал»."""
    try:
        return identity.trial_used(bid)
    except Exception:
        return False, None


def owner_verified(bid):
    """Есть ли у владельца сильный признак личности, чтобы запускать триал."""
    try:
        return identity.has_strong_signal(bid)
    except Exception:
        return False


# ---------- СТАТИСТИКА ДЛЯ ЭКРАНА ОКОНЧАНИЯ ----------
def stats(bid):
    """Что VELOR успел за триал — для красивого экрана окончания."""
    try:
        return database.trial_stats(bid)
    except Exception:
        return {"messages": 0, "orders": 0, "clients": 0, "recommendations": 0, "hours_saved": 0}
