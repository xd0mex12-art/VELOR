# -*- coding: utf-8 -*-
"""
AI Director — ответ на вопрос «что происходит с бизнесом».

Главный принцип: ни одного вывода без цифры и ни одной цифры без источника.
Каждая строка, которую видит владелец, собрана из конкретных записей его же
базы, и рядом написано, из каких именно: за какой период, по скольким
операциям, что с чем сравнивалось. Вывод, который нельзя проверить, владельцу
не помогает — он только выглядит как помощь.

Отсюда второй принцип: молчать, когда данных мало. У бизнеса, который живёт в
системе четыре дня, нет тренда; процент, посчитанный по одной операции, — это
не рост, а совпадение. В таких местах Директор говорит «Недостаточно данных
для вывода» и объясняет, чего не хватает. Красивая пустая аналитика хуже
честного молчания: по ней принимают решения.

Модель здесь не участвует. Не потому, что она плоха, а потому, что цифры —
это то место, где выдумка недопустима, а проверить её на глаз владелец не
сможет. Все формулировки собираются из шаблонов по посчитанным числам.
"""
import datetime

import database

# ── пороги. Названы и объяснены: молчаливая константа в коде — это решение,
# которое никто не принимал ────────────────────────────────────────────────
MIN_ENTRIES_TREND = 3     # меньше трёх операций в периоде — это не тренд
MIN_ENTRIES_CATEGORY = 2  # у категории окно короче, но одна запись всё равно ничего не значит
MIN_BASE_MONEY = 1000     # рост с 300 до 600 ₽ — не событие, а шум
MIN_DAYS_FOR_TREND = 14   # сравнивать периоды у бизнеса младше двух недель нечестно
CHANGE_NOTABLE = 10       # % — с этого начинается «что изменилось»
MIN_BASE_FOR_PCT = 5      # ниже этой базы процент врёт: 1 → 0 это не «−100%»
CHANGE_CATEGORY = 30      # % — с этого начинается разговор про категорию
DEPEND_CLIENT = 40        # % заказов у одного клиента — уже зависимость
DEPEND_SOURCE = 60        # % выручки из одного источника
PAYROLL_HEAVY = 50        # % расходов, уходящих людям
SLEEP_DAYS = 30           # сколько дней молчания делает клиента спящим

NOT_ENOUGH = "Недостаточно данных для вывода."
# Значение, которого нет. В слоте числа стоит знак, а не фраза: объяснение
# живёт строкой ниже и не обязано повторяться в каждой ячейке.
NO_VALUE = "—"


def _money(n):
    return "{:,}".format(int(n or 0)).replace(",", " ") + " ₽"


def _num(n):
    return "{:,}".format(int(n or 0)).replace(",", " ")


def _plural(n, one, few, many):
    a, b = abs(int(n)) % 100, abs(int(n)) % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    return one if b == 1 else many


def _ops(n):
    return f"{_num(n)} {_plural(n, 'операция', 'операции', 'операций')}"


def _pct_ok(was):
    """Можно ли вообще говорить процентами.

    Процент — это отношение, и на маленькой базе он теряет смысл: переход
    с одной заявки на ноль честнее назвать «был 1, стало 0», чем «−100%».
    Порог не эстетический: под ним одна единица меняет показатель на десятки
    процентов, и владелец принимает решение по шуму.
    """
    return (was or 0) >= MIN_BASE_FOR_PCT


def _pct(now, was):
    """Изменение в процентах. Не от чего считать — значит, нечего сказать."""
    if not was:
        return None
    return round((now - was) * 100 / was)


def _fact(title, detail, source, *, level="info", href="", numbers=None, key=""):
    """
    Один вывод Директора.

    source — не украшение, а обязательная часть: это адрес, по которому
    владелец может пойти и пересчитать всё сам.
    """
    return {"key": key, "title": title, "detail": detail, "level": level,
            "href": href, "source": source, "numbers": numbers or {}}


# ── ИСПОЛНИТЕЛЬНАЯ СВОДКА ──────────────────────────────────────────────────

def _metric(key, label, value, unit, source, *, href="", delta=None,
            good_up=True, gap="", hint=""):
    return {"key": key, "label": label, "value": value, "unit": unit,
            "display": (NO_VALUE if value is None else
                        (_money(value) if unit == "₽" else
                         (f"{value}%" if unit == "%" else _num(value)))),
            "delta": delta, "good_up": good_up, "source": source,
            "href": href, "gap": gap, "hint": hint,
            "enough": value is not None}


def _briefing_metrics(bid, days, now, prev, orders_now, orders_prev,
                      clients_now, clients_prev, comparable):
    """
    Шесть чисел, которые отвечают на «как идут дела».

    Ноль и «нет данных» — разные вещи. Выручка 0 при нуле операций это не
    результат, а пустая база, и показывать её как достижение нельзя.
    """
    was = f"за предыдущие {days} дн." if comparable else ""
    period = f"за {days} дн."

    income_src = (f"Сумма доходов {period}: {_ops(now['income_n'])} в разделе «Финансы»"
                  if now["income_n"] else "Появится, как только запишете первый доход в «Финансах»")
    expense_src = (f"Сумма расходов {period}: {_ops(now['expense_n'])} в разделе «Финансы»"
                   if now["expense_n"] else "Появится, как только запишете первый расход в «Финансах»")

    has_money = now["entries"] > 0
    income = now["income"] if has_money else None
    expense = now["expense"] if has_money else None
    profit = (now["income"] - now["expense"]) if has_money else None
    margin = (round(now["profit"] * 100 / now["income"], 1)
              if now["income"] else None)

    return [
        _metric("revenue", "Выручка", income, "₽", income_src, href="finance.html",
                delta=_pct(now["income"], prev["income"]) if comparable else None,
                gap="" if has_money else "В финансах пока нет ни одной операции.",
                hint=was),
        _metric("expenses", "Расходы", expense, "₽", expense_src, href="finance.html",
                delta=_pct(now["expense"], prev["expense"]) if comparable else None,
                good_up=False,
                gap="" if has_money else "В финансах пока нет ни одной операции."),
        _metric("profit", "Прибыль", profit, "₽",
                (f"Выручка минус расходы {period}: {_money(now['income'])} − "
                 f"{_money(now['expense'])}") if has_money else
                "Считается сама: доходы минус расходы", href="finance.html",
                delta=_pct(now["profit"], prev["profit"]) if comparable and prev["profit"] > 0 else None,
                gap="" if has_money else "Считать не из чего: операций нет."),
        _metric("margin", "Маржа", margin, "%",
                (f"Прибыль делённая на выручку {period}: {_money(now['profit'])} / "
                 f"{_money(now['income'])}") if now["income"] else
                "Считается сама: прибыль делённая на выручку",
                href="finance.html", hint="доля прибыли в выручке",
                gap="" if now["income"] else
                    "Маржа считается от выручки. Выручки за период нет — процента не существует."),
        # Счётчики людей и заявок сравниваем процентом только тогда, когда
        # база достаточно велика. На единицах процент врёт: «−100%» под нулём
        # клиентов означает «был один» — и выглядит катастрофой, которой нет.
        # Ниже порога говорим прямо, сколько было.
        _metric("clients_new", "Новые клиенты", clients_now, "",
                f"Клиенты, заведённые {period}: раздел «Клиенты»", href="clients.html",
                delta=_pct(clients_now, clients_prev)
                      if comparable and _pct_ok(clients_prev) else None,
                hint=(f"за предыдущие {days} дн.: {_num(clients_prev)}"
                      if comparable and not _pct_ok(clients_prev) else "")),
        _metric("orders_new", "Новые заказы", orders_now["count"], "",
                f"Заявки, созданные {period}: раздел «Заявки»"
                + (f", сумма проставлена у {orders_now['with_amount']} из "
                   f"{orders_now['count']}" if orders_now["count"] else ""),
                href="orders.html",
                delta=_pct(orders_now["count"], orders_prev["count"])
                      if comparable and _pct_ok(orders_prev["count"]) else None,
                hint=(f"за предыдущие {days} дн.: {_num(orders_prev['count'])}"
                      if comparable and not _pct_ok(orders_prev["count"]) else "")),
    ]


# ── ЧТО ИЗМЕНИЛОСЬ ─────────────────────────────────────────────────────────

def _changed(bid, days, now, prev, orders_now, orders_prev,
             clients_now, clients_prev, comparable, gaps):
    # Порядок не случаен: сначала то, что можно взять и проверить сегодня
    # (какая статья расходов сдвинулась), потом общие итоги периода — их
    # проценты владелец уже увидел в сводке выше.
    out = _category_moves(bid, gaps)
    if not comparable:
        gaps.append("Сравнить период не с чем: данных меньше чем за "
                    f"{MIN_DAYS_FOR_TREND} дней или в прошлом периоде пусто. "
                    + NOT_ENOUGH)
        return out

    for key, label, cur_v, prev_v, cur_n, href, good_up in (
            ("revenue", "Выручка", now["income"], prev["income"], now["income_n"],
             "finance.html", True),
            ("expenses", "Расходы", now["expense"], prev["expense"], now["expense_n"],
             "finance.html", False)):
        change = _pct(cur_v, prev_v)
        if change is None or abs(change) < CHANGE_NOTABLE:
            continue
        if cur_n < MIN_ENTRIES_TREND or prev_v < MIN_BASE_MONEY:
            gaps.append(f"{label}: изменение есть, но считать его тредом рано — "
                        f"{_ops(cur_n)} за период. " + NOT_ENOUGH)
            continue
        word = "выросла" if change > 0 else "снизилась"
        if key == "expenses":
            word = "выросли" if change > 0 else "снизились"
        out.append(_fact(
            f"{label} {word} на {abs(change)}%",
            f"{_money(cur_v)} за {days} дн. против {_money(prev_v)} за предыдущие {days}.",
            f"Сумма по разделу «Финансы»: {_ops(cur_n)} в текущем периоде.",
            level="good" if (change > 0) == good_up else "warn",
            href=href, numbers={"now": cur_v, "was": prev_v, "change": change},
            key=key))


    for key, label, cur_v, prev_v, href, one, few, many in (
            ("orders", "Заявок", orders_now["count"], orders_prev["count"], "orders.html",
             "заявка", "заявки", "заявок"),
            ("clients", "Новых клиентов", clients_now, clients_prev, "clients.html",
             "клиент", "клиента", "клиентов")):
        change = _pct(cur_v, prev_v)
        if change is None or abs(change) < CHANGE_NOTABLE:
            continue
        if cur_v + prev_v < MIN_ENTRIES_TREND:
            continue          # два против одного — это не «рост на 100%»
        word = "стало больше на" if change > 0 else "стало меньше на"
        out.append(_fact(
            f"{label} {word} {abs(change)}%",
            f"{_num(cur_v)} {_plural(cur_v, one, few, many)} за {days} дн. "
            f"против {_num(prev_v)} за предыдущие {days}.",
            f"Счёт записей в разделе «{'Заявки' if key == 'orders' else 'Клиенты'}».",
            level="good" if change > 0 else "warn", href=href,
            numbers={"now": cur_v, "was": prev_v, "change": change}, key=key))

    # Средний чек — цифра, которую владелец чувствует лучше оборота.
    if orders_now["avg"] and orders_prev["avg"]:
        change = _pct(orders_now["avg"], orders_prev["avg"])
        if change is not None and abs(change) >= CHANGE_NOTABLE:
            out.append(_fact(
                f"Средний чек {'вырос' if change > 0 else 'снизился'} на {abs(change)}%",
                f"{_money(orders_now['avg'])} против {_money(orders_prev['avg'])}.",
                f"Сумма заявок делённая на их число: {orders_now['with_amount']} "
                f"{_plural(orders_now['with_amount'], 'заявка', 'заявки', 'заявок')} "
                f"с проставленной суммой.",
                level="good" if change > 0 else "warn", href="orders.html",
                numbers={"now": orders_now["avg"], "was": orders_prev["avg"],
                         "change": change}, key="avg_check"))
    return out


def _category_moves(bid, gaps, days=14):
    """
    Какая статья расходов сдвинулась за две недели.

    Две недели — потому что месячное окно прячет всплеск: аренда, заплаченная
    в начале месяца, растворяет скачок на доставке в конце.
    """
    out = []
    now = database.category_period(bid, "expense", days, 0)
    was = database.category_period(bid, "expense", days, days)
    for cat, cur in sorted(now.items(), key=lambda kv: -kv[1]["total"])[:6]:
        old = was.get(cat)
        if not old or old["total"] < MIN_BASE_MONEY:
            continue
        change = _pct(cur["total"], old["total"])
        if change is None or abs(change) < CHANGE_CATEGORY:
            continue
        if cur["n"] < MIN_ENTRIES_CATEGORY:
            gaps.append(f"Расходы на «{cat}» изменились, но это {_ops(cur['n'])} "
                        f"за {days} дней. " + NOT_ENOUGH)
            continue
        out.append(_fact(
            f"Расходы на «{cat}» {'выросли' if change > 0 else 'снизились'} "
            f"на {abs(change)}% за последние {days} дней",
            f"{_money(cur['total'])} против {_money(old['total'])} за предыдущие {days} дней.",
            f"Расходы категории «{cat}» в разделе «Финансы»: {_ops(cur['n'])} "
            f"в текущем окне против {_ops(old['n'])} в прошлом.",
            level="warn" if change > 0 else "good", href="finance.html",
            numbers={"category": cat, "now": cur["total"], "was": old["total"],
                     "change": change, "days": days}, key="category"))
    return out[:2]


# ── РИСКИ ──────────────────────────────────────────────────────────────────

def _risks(bid, days, now, prev, comparable, sig, orders_now, gaps):
    out = []
    if now["entries"] < MIN_ENTRIES_TREND:
        gaps.append(f"Рисков по деньгам не ищу: за {days} дн. всего "
                    f"{_ops(now['entries'])}. " + NOT_ENOUGH)
    else:
        if now["profit"] < 0:
            out.append(_fact(
                "Бизнес работает в минус",
                f"Расходы {_money(now['expense'])} больше выручки "
                f"{_money(now['income'])}. Разница {_money(abs(now['profit']))}.",
                f"Сумма операций за {days} дн.: {_ops(now['entries'])}.",
                level="urgent", href="finance.html",
                numbers={"income": now["income"], "expense": now["expense"],
                         "profit": now["profit"]}, key="loss"))

        inc_ch = _pct(now["income"], prev["income"]) if comparable else None
        exp_ch = _pct(now["expense"], prev["expense"]) if comparable else None
        if (inc_ch is not None and exp_ch is not None
                and exp_ch - inc_ch >= 15 and exp_ch > 0):
            out.append(_fact(
                "Расходы растут быстрее выручки",
                f"Расходы {exp_ch:+d}%, выручка {inc_ch:+d}% к предыдущим {days} дн. "
                f"Разрыв {exp_ch - inc_ch} процентных пунктов.",
                f"Два периода по {days} дн. в разделе «Финансы».",
                level="urgent", href="finance.html",
                numbers={"income_change": inc_ch, "expense_change": exp_ch}, key="scissors"))

        # Убыточные категории: тратим больше, чем зарабатываем на этом же.
        for l in (database.growth_signals(bid).get("losing") or [])[:1]:
            out.append(_fact(
                f"Категория «{l['category']}» уходит в минус",
                f"Доход {_money(l['income'])} против расхода {_money(l['expense'])}.",
                "Сравнение доходов и расходов с одной категорией за всё время.",
                level="warn", href="finance.html",
                numbers={"category": l["category"], "income": l["income"],
                         "expense": l["expense"]}, key="losing"))

    pay = database.payroll_period(bid, days)
    if pay["total"] and now["expense"]:
        share = round(pay["total"] * 100 / now["expense"])
        if share >= PAYROLL_HEAVY:
            out.append(_fact(
                f"На людей уходит {share}% всех расходов",
                f"{_money(pay['total'])} из {_money(now['expense'])} за {days} дн."
                + (f" Выплат: {pay['n']}." if pay["n"] else ""),
                "Операции с привязанным сотрудником или пометкой «зарплата» "
                "в разделе «Финансы».",
                level="warn", href="finance.html",
                numbers={"payroll": pay["total"], "expense": now["expense"],
                         "share": share}, key="payroll"))

    share = sig.get("top_client_share")
    if share is not None and share >= DEPEND_CLIENT:
        out.append(_fact(
            f"Один клиент даёт {share}% заказов",
            "Если он уйдёт, выручка просядет сразу и заметно.",
            "Доля заказов самого частого клиента от всех заказов с указанным клиентом.",
            level="warn", href="clients.html",
            numbers={"share": share}, key="one_client"))

    src = sig.get("top_source")
    if src and src.get("share", 0) >= DEPEND_SOURCE and sig.get("sources", 0) > 1:
        out.append(_fact(
            f"«{src['category']}» приносит {src['share']}% выручки",
            "Бизнес держится на одном направлении.",
            "Доля категории в сумме всех доходов за всё время.",
            level="warn", href="finance.html",
            numbers={"category": src["category"], "share": src["share"]}, key="one_source"))

    if sig.get("stale_orders"):
        n = sig["stale_orders"]
        out.append(_fact(
            f"{_num(n)} {_plural(n, 'заявка ждёт', 'заявки ждут', 'заявок ждут')} "
            f"дольше трёх дней",
            "Клиент считает, что о нём забыли.",
            "Заявки со статусом «новый», созданные больше трёх дней назад.",
            level="urgent", href="orders.html", numbers={"count": n}, key="stale"))

    no_amount = orders_now["count"] - orders_now["with_amount"]
    if no_amount and orders_now["count"]:
        out.append(_fact(
            f"У {_num(no_amount)} из {_num(orders_now['count'])} заявок нет суммы",
            "Пока сумма не проставлена, выручка и средний чек занижены — "
            "все выводы ниже считаются без этих заявок.",
            f"Заявки за {days} дн. с нулевой суммой.",
            level="warn", href="orders.html",
            numbers={"without": no_amount, "total": orders_now["count"]}, key="no_amount"))

    order = {"urgent": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: order.get(f["level"], 3))
    return out[:5]


# ── ВОЗМОЖНОСТИ ────────────────────────────────────────────────────────────

def _opportunities(bid, days, now, sig, gaps):
    out = []
    services = database.service_revenue(bid, days)
    if services:
        top = services[0]
        if top["amount"]:
            out.append(_fact(
                f"«{top['title']}» приносит больше всех: {_money(top['amount'])}",
                f"{_num(top['orders'])} {_plural(top['orders'], 'заявка', 'заявки', 'заявок')} "
                f"за {days} дн. Это направление уже работает — его и стоит усиливать.",
                "Сумма заявок, в составе которых есть эта позиция "
                "(связь «заявка включает услугу»).",
                level="good", href="knowledge.html",
                numbers={"title": top["title"], "amount": top["amount"],
                         "orders": top["orders"]}, key="top_service"))
    else:
        gaps.append("Какая услуга приносит больше — сказать нельзя: заявки не "
                    "связаны с услугами. " + NOT_ENOUGH)

    sleeping = database.sleeping_clients(bid, SLEEP_DAYS, limit=5)
    paying = [c for c in sleeping if c["spent"]]
    if paying:
        total = sum(c["spent"] for c in paying)
        names = ", ".join(c["name"] or "без имени" for c in paying[:3])
        out.append(_fact(
            f"{_num(len(paying))} {_plural(len(paying), 'клиент не возвращался', 'клиента не возвращались', 'клиентов не возвращались')} "
            f"больше {SLEEP_DAYS} дней",
            f"Вместе они принесли {_money(total)}. Например: {names}.",
            f"Клиенты с заказами, у которых последний заказ старше {SLEEP_DAYS} дней.",
            level="info", href="clients.html",
            numbers={"count": len(paying), "spent": total}, key="sleeping"))

    inc_cats = database.category_period(bid, "income", days, 0)
    was_cats = database.category_period(bid, "income", days, days)
    for cat, cur in sorted(inc_cats.items(), key=lambda kv: -kv[1]["total"])[:3]:
        old = was_cats.get(cat)
        if not old or old["total"] < MIN_BASE_MONEY or cur["n"] < MIN_ENTRIES_CATEGORY:
            continue
        change = _pct(cur["total"], old["total"])
        if change and change >= CHANGE_CATEGORY:
            out.append(_fact(
                f"Доход по «{cat}» вырос на {change}%",
                f"{_money(cur['total'])} против {_money(old['total'])} "
                f"за предыдущие {days} дн.",
                f"Доходы категории «{cat}» в разделе «Финансы»: {_ops(cur['n'])}.",
                level="good", href="finance.html",
                numbers={"category": cat, "now": cur["total"], "was": old["total"],
                         "change": change}, key="income_up"))
            break
    return out[:4]


# ── РЕКОМЕНДАЦИИ ───────────────────────────────────────────────────────────
# Рекомендация не появляется сама по себе: она отвечает на конкретный риск или
# возможность и наследует их цифры. Совет без основания — это мнение, а мнения
# у Директора нет.

_ADVICE = {
    "loss": ("Сократить расходы или поднять цены",
             "Бизнес в минусе {profit_abs}. Начните с самой крупной статьи расходов.",
             "finance.html"),
    "scissors": ("Разобрать, что подорожало",
                 "Расходы обгоняют выручку на {gap} п.п. Сравните статьи за два периода.",
                 "finance.html"),
    "category": ("Проверить расходы на «{category}»",
                 "Рост на {change}% за {days} дней — {now} против {was}.",
                 "finance.html"),
    "payroll": ("Посчитать отдачу от фонда оплаты",
                "{share}% расходов уходит людям. Сопоставьте с выручкой на человека.",
                "finance.html"),
    "one_client": ("Расширить клиентскую базу",
                   "{share}% заказов — один клиент. Одного ухода хватит, чтобы просесть.",
                   "clients.html"),
    "stale": ("Ответить на зависшие заявки",
              "{count} ждут дольше трёх дней.", "orders.html"),
    "no_amount": ("Проставить суммы в заявках",
                  "У {without} из {total} суммы нет — выручка считается неполной.",
                  "orders.html"),
    "sleeping": ("Вернуть спящих клиентов",
                 "{count} не возвращались больше месяца, а принесли {spent}.",
                 "clients.html"),
    "top_service": ("Усилить «{title}»",
                    "Направление уже приносит {amount} за период.", "knowledge.html"),
    "losing": ("Пересчитать цену на «{category}»",
               "По этой категории расход {expense} против дохода {income}.",
               "finance.html"),
}


def _recommendations(risks, opportunities):
    out = []
    for f in list(risks) + list(opportunities):
        tpl = _ADVICE.get(f["key"])
        if not tpl:
            continue
        title, detail, href = tpl
        n = dict(f["numbers"])
        # Числа в совете — те же самые, что в основании. Пересчитывать их
        # заново значило бы завести второй ответ на тот же вопрос.
        fmt = {k: (_money(v) if isinstance(v, int) and k in
                   ("now", "was", "amount", "spent", "expense", "income", "profit_abs")
                   else v) for k, v in n.items()}
        fmt.setdefault("profit_abs", _money(abs(n.get("profit", 0))))
        fmt.setdefault("gap", abs((n.get("expense_change") or 0) - (n.get("income_change") or 0)))
        fmt.setdefault("days", n.get("days", 14))
        try:
            out.append(_fact(title.format(**fmt), detail.format(**fmt),
                             "Основание: " + f["title"] + ".",
                             level="info", href=href, numbers=n, key="do_" + f["key"]))
        except KeyError:
            continue
    return out[:4]


# ── СБОРКА ─────────────────────────────────────────────────────────────────

def briefing(business_id, days=30):
    """
    Что происходит с бизнесом — одним ответом.

    Возвращает шесть чисел исполнительной сводки и четыре раздела разбора.
    Всё, чего посчитать не удалось, попадает в gaps с объяснением: владелец
    должен видеть не только выводы, но и границы, за которыми VELOR молчит.
    """
    bid = business_id
    span = database.data_span(bid)
    now = database.money_period(bid, days, 0)
    prev = database.money_period(bid, days, days)
    orders_now = database.orders_period(bid, days, 0)
    orders_prev = database.orders_period(bid, days, days)
    clients_now = database.counts_period(bid, "clients", days, 0)
    clients_prev = database.counts_period(bid, "clients", days, days)
    sig = database.risk_signals(bid)

    # Сравнивать периоды можно, только если прошлый период вообще был.
    comparable = bool(
        span["days"] >= MIN_DAYS_FOR_TREND
        and (prev["entries"] or orders_prev["count"] or clients_prev))

    gaps = []
    metrics = _briefing_metrics(bid, days, now, prev, orders_now, orders_prev,
                                clients_now, clients_prev, comparable)
    has_any = bool(now["entries"] or orders_now["count"] or clients_now or span["rows"])

    if not has_any:
        return {"days": days, "ready": False,
                "headline": "Данных пока нет — и придумывать их я не стану.",
                "why": "Как только появятся заявки, клиенты или операции, я начну "
                       "считать и сравнивать периоды.",
                "metrics": metrics, "changed": [], "risks": [], "opportunities": [],
                "recommendations": [],
                "gaps": [NOT_ENOUGH + " В базе нет ни заявок, ни клиентов, ни денег."],
                "span": span, "generated_at": datetime.datetime.now().isoformat(timespec="seconds")}

    changed = _changed(bid, days, now, prev, orders_now, orders_prev,
                       clients_now, clients_prev, comparable, gaps)
    risks = _risks(bid, days, now, prev, comparable, sig, orders_now, gaps)
    opportunities = _opportunities(bid, days, now, sig, gaps)
    recommendations = _recommendations(risks, opportunities)

    return {"days": days, "ready": True,
            "headline": _headline(days, now, comparable, prev, orders_now, risks),
            "why": _why(span, now, orders_now),
            "metrics": metrics, "changed": changed, "risks": risks,
            "opportunities": opportunities, "recommendations": recommendations,
            "gaps": gaps, "span": span,
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds")}


def _headline(days, now, comparable, prev, orders_now, risks):
    """Одна строка в ответ на вопрос страницы. Только то, что посчитано."""
    if now["entries"]:
        head = (f"Прибыль {_money(now['profit'])} за {days} дн. "
                f"при выручке {_money(now['income'])}")
        change = _pct(now["profit"], prev["profit"]) if comparable and prev["profit"] > 0 else None
        if change is not None:
            head += f", {change:+d}% к прошлому периоду"
        return head + "."
    if orders_now["count"]:
        return (f"Заявок за {days} дн.: {_num(orders_now['count'])}. "
                "Деньги в системе не записаны — прибыль считать не из чего.")
    return "Движения за период нет: ни операций, ни заявок."


def _why(span, now, orders_now):
    """На чём стоит вся сводка — чтобы масштаб выводов был виден сразу."""
    bits = []
    if now["entries"]:
        bits.append(_ops(now["entries"]))
    if orders_now["count"]:
        bits.append(f"{_num(orders_now['count'])} "
                    + _plural(orders_now["count"], "заявка", "заявки", "заявок"))
    base = ", ".join(bits) if bits else "пока без записей"
    since = f" Данные с {span['first_day']}." if span.get("first_day") else ""
    return f"Считано по: {base}.{since}"
