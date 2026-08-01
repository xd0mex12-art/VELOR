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

TRIAL_DAYS = 14


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
def start(bid):
    """Запустить 14-дневный триал новому бизнесу."""
    now = _now()
    database.update_business(
        bid,
        trial_start=_fmt(now),
        trial_end=_fmt(now + datetime.timedelta(days=TRIAL_DAYS)),
        subscription_status="trial",
        trial_used=1,
    )


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
        "status": business.get("subscription_status") or "trial",
        "plan": business.get("subscription_plan") or None,
        "trial_end": business.get("trial_end"),
        "subscription_expires": business.get("subscription_expires"),
        "trial_days": TRIAL_DAYS,
        "days_left": days_left,
        "hours_left": hours_left,
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


# ---------- СТАТИСТИКА ДЛЯ ЭКРАНА ОКОНЧАНИЯ ----------
def stats(bid):
    """Что VELOR успел за триал — для красивого экрана окончания."""
    try:
        return database.trial_stats(bid)
    except Exception:
        return {"messages": 0, "orders": 0, "clients": 0, "recommendations": 0, "hours_saved": 0}
