# -*- coding: utf-8 -*-
"""
IdentityService — управление личностью владельца бизнеса (Owner Identity).

Зачем: раньше триал привязывался к Telegram-БОТУ (getMe → id бота). Но бот легко
пересоздать и получить новый триал. Теперь триал привязан к ЛИЧНОСТИ ВЛАДЕЛЬЦА —
устойчивому набору признаков (личный Telegram id, телефон, email), которые не
меняются при создании нового бота/браузера/компьютера.

Сервис отвечает за:
  • создание личности владельца (при регистрации);
  • поиск существующей личности (по business_id);
  • объединение способов входа (merge — дополняем новыми признаками);
  • проверку, использовался ли триал этим владельцем ранее.

Универсальность: методы идентификации telegram | phone | email | google | apple.
Сейчас реально работает telegram (через привязку личного аккаунта к боту кодом) и
email/fingerprint как вспомогательные. Архитектура готова к включению обязательного
подтверждения телефона — БЕЗ переписывания TrialService: он просто спросит
identity.trial_used(...) с новым набором признаков.
"""
import database

# Способы идентификации (под расширение). «Сила» признака определяет, блокирует ли
# он повторный триал сам по себе (strong) или лишь повышает risk_score (weak).
METHOD_TELEGRAM = "telegram"
METHOD_PHONE = "phone"
METHOD_EMAIL = "email"
METHOD_GOOGLE = "google"
METHOD_APPLE = "apple"


def get(business_id):
    """Текущая личность владельца бизнеса (или None, если ещё не заведена)."""
    return database.owner_identity_get(business_id)


def ensure(business_id, **signals):
    """Создать личность владельца или дополнить её новыми признаками (объединение
    способов входа). Возвращает id строки owner_identity. Пустые значения игнорируются."""
    return database.owner_identity_upsert(business_id, **signals)


def link_telegram(business_id, tg_user):
    """Привязать ЛИЧНЫЙ Telegram владельца (данные из объекта `from` апдейта).
    Это сильный, стабильный признак: у нового бота будет тот же владелец."""
    tg_user = tg_user or {}
    return ensure(
        business_id,
        method=METHOD_TELEGRAM,
        telegram_user_id=str(tg_user.get("id")) if tg_user.get("id") else None,
        telegram_username=tg_user.get("username"),
        first_name=tg_user.get("first_name"),
        last_name=tg_user.get("last_name"),
    )


def _signals(business_id):
    """Сильные признаки текущей личности владельца для проверки повторного триала."""
    o = get(business_id) or {}
    return {
        "telegram_user_id": o.get("telegram_user_id"),
        "phone": o.get("phone"),
        "email": o.get("email"),
        "fingerprint": o.get("fingerprint"),
    }


def trial_used(business_id):
    """
    Алгоритм проверки повторного триала по ЛИЧНОСТИ владельца.
    Возвращает (used: bool, reason: str|None).

    Блокирует, если совпал ХОТЯ БЫ ОДИН сильный признак с уже выданным триалом:
      telegram_user_id ИЛИ phone ИЛИ email,
    либо слабая комбинация одновременно: email + fingerprint.
    Fingerprint сам по себе НЕ блокирует (только risk_score).
    """
    s = _signals(business_id)
    if database.owner_trial_used(
            telegram_user_id=s["telegram_user_id"], phone=s["phone"],
            email=s["email"], exclude_business=business_id):
        return True, "личность владельца уже использовала пробный период"
    if database.owner_weak_match(email=s["email"], fingerprint=s["fingerprint"],
                                 exclude_business=business_id):
        return True, "совпали email и устройство ранее взятого триала"
    return False, None


def has_strong_signal(business_id):
    """Есть ли у владельца хоть один сильный признак личности (для запуска триала).
    Пока это личный Telegram id; позже добавится подтверждённый телефон/email."""
    s = _signals(business_id)
    return bool(s["telegram_user_id"] or s["phone"] or s["email"])


def assess_risk(business_id, fingerprint=None):
    """
    risk_score владельца — СИГНАЛ, не блокировка. Fingerprint даёт лишь подозрение
    (общие устройства), сильные совпадения дают больше. Возвращает (0..100, причины).
    """
    score, reasons = 0, []
    s = _signals(business_id)
    fp = fingerprint or s["fingerprint"]
    try:
        if fp and database.owner_fingerprint_seen(fp, exclude_business=business_id):
            score += 40
            reasons.append("устройство уже участвовало в триале")
        if s["telegram_user_id"] and database.owner_trial_used(
                telegram_user_id=s["telegram_user_id"], exclude_business=business_id):
            score += 60
            reasons.append("этот Telegram уже брал триал")
        if s["email"] and database.owner_trial_used(
                email=s["email"], exclude_business=business_id):
            score += 50
            reasons.append("этот email уже брал триал")
    except Exception:
        pass
    return min(100, score), reasons


def mark_trial_used(business_id):
    """Зафиксировать: этой личности триал ВЫДАН (навсегда, переживёт удаление)."""
    from datetime import datetime
    ensure(business_id, trial_used_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
