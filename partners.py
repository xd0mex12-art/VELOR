# -*- coding: utf-8 -*-
"""
ПАРТНЁРСКАЯ ПРОГРАММА — кто привёл клиента и сколько ему причитается.

Партнёр приводит клиента по ссылке `?ref=код` и получает долю (по умолчанию
15%) с КАЖДОЙ оплаты этого клиента, пока тот платит.

ДВА ПРАВИЛА, НА КОТОРЫХ ВСЁ ДЕРЖИТСЯ.

1. **Привязка ставится один раз.** При регистрации бизнеса и больше никогда.
   Именно это означает слово «пожизненно» в обещании партнёру: кто бы потом ни
   общался с клиентом, доля идёт тому, кто его привёл. Переписать привязку
   может только владелец VELOR и только если её ещё нет.

2. **Начисление в одном месте.** Комиссия считается при активации и продлении
   подписки — там, куда сходятся ОБЕ дороги денег: сегодня ручная отметка
   оплаты владельцем, завтра платёж из ЮKassa. Поэтому включение онлайн-оплаты
   не потребует трогать этот модуль.

ЧЕГО ЗДЕСЬ НЕТ И НЕ ДОЛЖНО БЫТЬ. Партнёр не входит в VELOR и не видит данных
своих клиентов — ни названий, ни переписок, ни цифр бизнеса. Наружу уходят
только количества и суммы. Это та же граница между компаниями, что и везде в
продукте, и ослаблять её ради удобства отчётности нельзя.

ДОЛЯ ФИКСИРУЕТСЯ. В каждой строке начисления хранится процент, действовавший
в тот момент. Смена доли завтра не переписывает прошлогодние начисления: это
деньги, задним числом они не меняются.
"""
import database
import plans


# ---------- СПРАВОЧНИК ----------
def create(name, code, share_pct=15, contact=None, own_business_id=None):
    """Завести партнёра. Код — то, что встанет в ссылку: латиницей, коротко."""
    code = (code or "").strip().lower()
    if not code:
        raise ValueError("Нужен код партнёра — он встанет в ссылку")
    if not (0 <= int(share_pct) <= 100):
        raise ValueError("Доля должна быть от 0 до 100 процентов")
    return database.create_partner(name=name, code=code, share_pct=int(share_pct),
                                   contact=contact, own_business_id=own_business_id)


def by_code(code):
    """Активный партнёр по коду из ссылки. Регистр не важен."""
    return database.get_partner_by_code(code, active_only=True)


def get(partner_id):
    return database.get_partner(partner_id)


def list_all():
    return database.list_partners()


def set_active(partner_id, active):
    return database.update_partner(partner_id, active=1 if active else 0)


def set_share(partner_id, share_pct):
    """Сменить долю. На уже начисленные строки не влияет — там свой процент."""
    if not (0 <= int(share_pct) <= 100):
        raise ValueError("Доля должна быть от 0 до 100 процентов")
    return database.update_partner(partner_id, share_pct=int(share_pct))


# ---------- ПРИВЯЗКА КЛИЕНТА ----------
def attach(bid, code):
    """
    Закрепить бизнес за партнёром. Возвращает True, если привязка поставлена.

    Молчаливый отказ (False) — нормальный исход, а не ошибка: неизвестный код в
    адресной строке не должен ломать человеку регистрацию.
    """
    partner = by_code(code)
    if not partner:
        return False
    if partner.get("own_business_id") and int(partner["own_business_id"]) == int(bid):
        return False                      # сам себя привести нельзя
    # Условие «партнёра ещё нет» проверяет сама база, внутри UPDATE.
    return database.set_business_partner(bid, partner["id"])


def partner_of(bid):
    """Кто привёл этот бизнес, если его вообще кто-то приводил."""
    b = database.get_business(bid) or {}
    return get(b["partner_id"]) if b.get("partner_id") else None


# ---------- НАЧИСЛЕНИЕ ----------
def amount_for(plan_key, months=1):
    """Сколько стоит подписка по каталогу. Запасной путь, если сумму не назвали."""
    return int(plans.price(plans.normalize(plan_key))) * max(1, int(months))


def accrue(bid, plan=None, months=1, amount=None):
    """
    Начислить комиссию за оплату клиента. Возвращает строку начисления или None.

    None — когда бизнес никем не приведён; это обычное дело, а не сбой.

    `amount` — сколько клиент заплатил на самом деле. Передавать его важно там,
    где цена отличается от каталожной (Founder Pilot, скидка, доплата). Если не
    передан — берётся цена тарифа из каталога, умноженная на месяцы.
    """
    partner = partner_of(bid)
    if not partner:
        return None
    months = max(1, int(months or 1))
    if amount is None:
        amount = amount_for(plan, months)
    amount = int(amount)
    if amount <= 0:
        return None
    share = int(partner.get("share_pct") or 0)
    commission = int(round(amount * share / 100.0))
    return database.add_earning(
        partner["id"], bid,
        plan=plans.normalize(plan) if plan else None,
        months=months, amount=amount, share_pct=share, commission=commission)


ADJUST = "правка"


def adjust(partner_id, business_id, commission, amount=0):
    """
    Ручная правка начислений: возврат клиенту, ошибочная отметка оплаты, доплата.

    Отдельной механики возвратов нет намеренно — их пока не было. Вместо неё
    обычная строка с суммой в минус: история остаётся видимой целиком, и в ней
    видно, что именно поправили, а не «цифра вдруг изменилась». Удалять
    начисления нельзя ничем: деньги не исчезают задним числом.
    """
    commission = int(commission)
    if not commission:
        raise ValueError("Правка на ноль ничего не меняет")
    return database.add_earning(
        int(partner_id), int(business_id), plan=ADJUST, months=1,
        amount=int(amount), share_pct=0, commission=commission)


def earnings(partner_id=None, unpaid_only=False, limit=500):
    return database.list_earnings(partner_id=partner_id, unpaid_only=unpaid_only,
                                  limit=limit)


def mark_paid(earning_ids):
    """Отметить выплату. Возвращает, сколько строк закрылось."""
    return database.mark_earnings_paid(earning_ids)


def totals(partner_id):
    """
    Итоги по партнёру: сколько привёл, сколько начислено, сколько должны.

    Только количества и суммы — ни одного признака, по которому можно было бы
    узнать, что за компании у него в клиентах.
    """
    rows = database.list_earnings(partner_id=partner_id, limit=100000)
    accrued = sum(int(r["commission"]) for r in rows)
    paid = sum(int(r["commission"]) for r in rows if r.get("paid_out_at"))
    bizs = database.partner_businesses(partner_id)
    return {
        "clients": len(bizs),
        "paying": sum(1 for b in bizs if b.get("subscription_status") == "active"),
        "payments": len(rows),
        "accrued": accrued,
        "paid_out": paid,
        "due": accrued - paid,
    }
