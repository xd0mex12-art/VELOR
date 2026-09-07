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
import logging

import database

# Разбор закупок необязателен: если он сорвётся, нить должна закончиться
# честным тупиком, а не уронить всю сводку.
log = logging.getLogger("velor")

# ── пороги. Названы и объяснены: молчаливая константа в коде — это решение,
# которое никто не принимал ────────────────────────────────────────────────
MIN_ENTRIES_TREND = 3     # меньше трёх операций в периоде — это не тренд
MIN_ENTRIES_CATEGORY = 2  # у категории окно короче, но одна запись всё равно ничего не значит
MIN_BASE_MONEY = 1000     # рост с 300 до 600 ₽ — не событие, а шум
MIN_DAYS_FOR_TREND = 14   # сравнивать периоды у бизнеса младше двух недель нечестно
CHANGE_NOTABLE = 10       # % — с этого начинается «что изменилось»
MIN_BASE_FOR_PCT = 5      # ниже этой базы процент врёт: 1 → 0 это не «−100%»
PCT_AS_TIMES = 300        # выше этого процент нечитаем: говорим «в N раз»
CHANGE_CATEGORY = 30      # % — с этого начинается разговор про категорию
SCISSORS_GAP = 15         # п.п. — разрыв «расходы против выручки», риск сам по себе
SCISSORS_SOFT = 8         # п.п. — меньший разрыв считаем риском, только если…
PROFIT_HIT = 15           # …прибыль от него действительно просела на столько %
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


def _times(pct):
    """Во сколько раз — словами, которые не выдают машину.

    «+4994%» — формально верное число, которое ничего не сообщает: глаз не
    переводит его в «в пятьдесят раз» и застревает. Порог не эстетический:
    выше трёхсот процентов доля перестаёт быть долей и становится кратностью,
    и называть её надо кратностью.

    Десятые оставляем только там, где они что-то значат: «в 2,4 раза» —
    осмысленно, «в 50,9 раза» — точность, которой в данных нет.
    """
    if pct is None:
        return ""
    k = 1 + abs(pct) / 100.0
    if k >= 10:
        n = int(round(k))
        return f"{n} {_plural(n, 'раз', 'раза', 'раз')}"
    return f"{k:.1f}".replace(".", ",").replace(",0", "") + " раза"


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
    out = []
    if not comparable:
        out += _category_moves(bid, gaps, period=days)
        gaps.append("Сравнить период не с чем: данных меньше чем за "
                    f"{MIN_DAYS_FOR_TREND} дней или в прошлом периоде пусто. "
                    + NOT_ENOUGH)
        return out

    # Прибыль идёт первой, и это не косметика. Она стоит в заголовке сводки, но
    # «что изменилось» о ней молчало: выручка −7% и расходы +5% по отдельности
    # не дотягивали до порога в 10%, а вместе роняли прибыль на 26%. Порог
    # составного числа нельзя считать по его слагаемым — владелец читал
    # «прибыль −26%» и пустой список под ним.
    #
    # Здесь же и ответ на «почему»: оба слагаемых называются рядом, даже когда
    # каждое из них само по себе разговора не стоит.
    if prev["profit"] > 0 and now["entries"] >= MIN_ENTRIES_TREND:
        p_ch = _pct(now["profit"], prev["profit"])
        if p_ch is not None and abs(p_ch) >= CHANGE_NOTABLE:
            inc_ch = _pct(now["income"], prev["income"])
            exp_ch = _pct(now["expense"], prev["expense"])
            parts = []
            if inc_ch is not None:
                parts.append(f"выручка {inc_ch:+d}%")
            if exp_ch is not None:
                parts.append(f"расходы {exp_ch:+d}%")
            if now["profit"] < 0:
                # Между прибылью и убытком не «падение на N%», а смена знака:
                # процент здесь не объясняет ничего, а звучит внушительно.
                # Говорим словами и показываем оба числа.
                title = "Прибыль сменилась убытком"
                detail = (f"Было {_money(prev['profit'])}, стало "
                          f"{_money(now['profit'])} за {days} дн.")
            else:
                title = f"Прибыль {'выросла' if p_ch > 0 else 'упала'} на {abs(p_ch)}%"
                detail = (f"{_money(now['profit'])} против {_money(prev['profit'])} "
                          f"за предыдущие {days} дн.")
            if parts:
                detail += " Сложилась из двух движений: " + ", ".join(parts) + "."
            out.append(_fact(
                title, detail,
                f"Выручка минус расходы за два периода по {days} дн.: "
                f"{_ops(now['entries'])} в текущем.",
                level="good" if p_ch > 0 and now["profit"] > 0 else "warn",
                href="finance.html",
                numbers={"now": now["profit"], "was": prev["profit"], "change": p_ch,
                         "income_change": inc_ch, "expense_change": exp_ch},
                key="profit"))

    # Дальше — то, что можно взять и проверить сегодня: какая статья расходов
    # сдвинулась. Потом общие итоги периода, проценты которых владелец уже
    # увидел в сводке выше.
    out += _category_moves(bid, gaps, period=days)

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


def _category_moves(bid, gaps, days=14, period=30):
    """
    Какая статья расходов сдвинулась — в двух окнах сразу.

    Две недели ловят всплеск: аренда, заплаченная в начале месяца, растворила
    бы скачок на доставке в конце. Но у короткого окна есть зеркальная
    слепота, и стоит она дороже. Всё, что платят раз в месяц — аренда,
    зарплата, крупная закупка, — в ПРОШЛОЕ двухнедельное окно просто не
    попадает: сравнивать не с чем, и рост такой статьи не виден никогда.
    Поэтому окон два — короткое на всплески и длинное на месячные платежи.

    Правило про число операций тоже пришлось поправить. «Одна запись — не
    тренд» верно там, где окно способно вместить несколько; для аренды одна
    запись в каждом окне — это и есть норма, и сравнивать прошлую аренду с
    нынешней совершенно правильно. Поэтому одинаковое число операций с обеих
    сторон снимает требование о минимуме: это сравнение платежа с платежом,
    а не вывод по единственной точке.
    """
    out, seen, told = [], set(), set()
    for window in (days, period):
        if window in (0, None):
            continue
        now = database.category_period(bid, "expense", window, 0)
        was = database.category_period(bid, "expense", window, window)
        for cat, cur in sorted(now.items(), key=lambda kv: -kv[1]["total"])[:6]:
            if cat in seen:
                continue          # уже сказали про эту статью в коротком окне
            old = was.get(cat)
            if not old or old["total"] < MIN_BASE_MONEY:
                continue
            change = _pct(cur["total"], old["total"])
            if change is None or abs(change) < CHANGE_CATEGORY:
                continue
            if cur["n"] < MIN_ENTRIES_CATEGORY and cur["n"] != old["n"]:
                if cat not in told:
                    told.add(cat)
                    gaps.append(f"Расходы на «{cat}» изменились, но это "
                                f"{_ops(cur['n'])} за {window} дней. " + NOT_ENOUGH)
                continue
            seen.add(cat)
            out.append(_fact(
                f"Расходы на «{cat}» {'выросли' if change > 0 else 'снизились'} "
                f"на {abs(change)}% за последние {window} дней",
                f"{_money(cur['total'])} против {_money(old['total'])} "
                f"за предыдущие {window} дней.",
                f"Расходы категории «{cat}» в разделе «Финансы»: в текущем "
                f"окне {_ops(cur['n'])}, в прошлом {_ops(old['n'])}.",
                level="warn" if change > 0 else "good", href="finance.html",
                numbers={"category": cat, "now": cur["total"], "was": old["total"],
                         "change": change, "days": window},
                # Ключ несёт саму статью. Два вывода с одинаковым ключом —
                # ловушка для всякого, кто разложит находки в словарь: второй
                # молча затрёт первый, и часть разбора исчезнет без следа.
                key="category:" + cat))
    return out[:3]


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
        prof_ch = (_pct(now["profit"], prev["profit"])
                   if comparable and prev["profit"] > 0 else None)
        # Разрыв в SCISSORS_GAP пунктов — риск сам по себе, независимо от того,
        # успел он ударить по прибыли или нет. Но одного круглого порога мало:
        # на демо-клинике расходы шли +5%, выручка −7%, разрыв 12 — и раздел
        # «Риски» оставался пустым под заголовком «прибыль −26%». Второе
        # условие говорит не про размер разрыва, а про последствие: если
        # ножницы уже съели прибыль, это риск и при меньшем разрыве.
        gap = (exp_ch - inc_ch) if (inc_ch is not None and exp_ch is not None) else None
        by_gap = gap is not None and gap >= SCISSORS_GAP
        by_hit = (gap is not None and gap >= SCISSORS_SOFT
                  and prof_ch is not None and prof_ch <= -PROFIT_HIT)
        if exp_ch is not None and exp_ch > 0 and (by_gap or by_hit):
            detail = (f"Расходы {exp_ch:+d}%, выручка {inc_ch:+d}% к предыдущим "
                      f"{days} дн. Разрыв {gap} процентных пунктов.")
            if by_hit and not by_gap:
                detail += f" Прибыль от этого просела на {abs(prof_ch)}%."
            out.append(_fact(
                "Расходы растут быстрее выручки", detail,
                f"Два периода по {days} дн. в разделе «Финансы».",
                level="urgent", href="finance.html",
                numbers={"income_change": inc_ch, "expense_change": exp_ch,
                         "gap": gap, "profit_change": prof_ch}, key="scissors"))

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
        # Ключ может нести уточнение после двоеточия («category:аренда») —
        # совет один на весь вид находки, поэтому смотрим и на вид тоже.
        tpl = _ADVICE.get(f["key"]) or _ADVICE.get(str(f["key"]).split(":", 1)[0])
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


# ── ПОЧЕМУ: НИТЬ ОТ ЗАГОЛОВКА ДО ПРИЧИНЫ ───────────────────────────────────
#
# Всё, что нужно для ответа «почему упала прибыль», Директор считал и раньше:
# движение выручки, движение расходов, какая статья выросла, сколько закрыто
# возможностей, какая конверсия, по каким причинам теряли. Но лежало это
# порознь — семь отдельных фактов, между которыми владелец должен был провести
# линию сам. Разложение «выручка −5%, расходы +5%» — это арифметика, а не
# причина: оно говорит ГДЕ искать, но не отвечает ЗАЧЕМ.
#
# Нить проходит те же цифры сверху вниз и на каждом шаге спрашивает
# «а это отчего?», пока данных хватает. Кончаются данные — так и сказано,
# и это последний шаг: тупик, названный вслух, честнее вывода, придуманного
# ради красивого финала.
#
# Модель здесь не участвует. Ни одного обращения, ни одной догадки: только
# сравнение чисел, которые уже посчитаны выше.

CHAIN_NOTABLE = 10        # % — с этого движение прибыли стоит объяснять
LEAD_NOTABLE = 10         # % — с этого стоит объяснять движение конверсии


def _signed(n):
    """Разница деньгами со знаком: «+78 700 ₽», «−22 500 ₽».

    Минус — типографский, а не дефис: в строке про деньги он читается как
    знак числа, а не как перенос.
    """
    return ("+" if n >= 0 else "−") + _money(abs(int(n or 0)))


def _step(text, source, *, gap=False):
    return {"text": text, "source": source, "gap": bool(gap)}


def _funnel_step(bid, days, out):
    """Воронка: обращений стало меньше или закрывать стали хуже."""
    now = database.leads_period(bid, days, 0)
    was = database.leads_period(bid, days, days)
    if not (now["closed"] or was["closed"]):
        out.append(_step(
            "Дальше по воронке сказать нечего: возможности не ведутся.",
            "Раздел «Возможности» пуст за оба периода.", gap=True))
        return

    conv_now, conv_was = now["conversion"], was["conversion"]
    if conv_now is not None and conv_was is not None and conv_was:
        drop = conv_was - conv_now
        if drop >= LEAD_NOTABLE:
            out.append(_step(
                f"Закрывать стали хуже: {conv_now}% вместо {conv_was}%. "
                f"Закрыто {now['closed']} против {was['closed']}.",
                f"Возможности за два периода по {days} дн.: выиграно "
                f"{now['won']}, потеряно {now['lost']}."))
            _reasons_step(now, out)
            return

    if was["created"] and now["created"] < was["created"]:
        change = _pct(now["created"], was["created"])
        out.append(_step(
            f"Обращений стало меньше: {_num(now['created'])} против "
            f"{_num(was['created'])}" + (f" ({change:+d}%)" if change is not None else "") + ".",
            f"Новые возможности за два периода по {days} дн."))
        # Почему их стало меньше — вопрос к источникам, а их у заявок нет.
        out.append(_step(
            "Откуда обращения приходили раньше и что изменилось — сказать "
            "нельзя: источник у возможностей не размечен.",
            "У возможностей заполнен канал (Telegram, ВКонтакте), но не то, "
            "что привело человека: реклама, рекомендация, карта.", gap=True))
        return

    _reasons_step(now, out)


def _reasons_step(now, out):
    """Из-за чего именно теряли. Это последний шаг, дальше данных нет."""
    reasons = now.get("lost_reasons") or {}
    if not reasons:
        out.append(_step("Причины отказов не записаны — сказать, из-за чего "
                         "теряем, нельзя.",
                         "Поле «причина» у проигранных возможностей пустое.", gap=True))
        return
    try:
        import leads as _leads
        names = _leads.LOST_REASONS
    except Exception:
        names = {}
    top = sorted(reasons.items(), key=lambda kv: -kv[1])
    said = ", ".join("%s — %d" % (names.get(k, k), v) for k, v in top[:3])
    out.append(_step("Причины отказов: " + said + ".",
                     "Причины, записанные у проигранных возможностей за период."))


def _chain(bid, days, now, prev, orders_now, orders_prev, comparable, moves):
    """
    Нить «почему». Пустая — значит объяснять нечего: прибыль не двигалась.
    """
    if not comparable or prev["profit"] <= 0 or now["entries"] < MIN_ENTRIES_TREND:
        return []
    p_ch = _pct(now["profit"], prev["profit"])
    if p_ch is None or abs(p_ch) < CHAIN_NOTABLE:
        return []

    out = [_step(
        f"Прибыль {'выросла' if p_ch > 0 else 'упала'} на {abs(p_ch)}%: "
        f"{_money(now['profit'])} против {_money(prev['profit'])}.",
        f"Выручка минус расходы за два периода по {days} дн.")]

    # Что перевесило. Считаем в рублях, а не в процентах: пять процентов
    # расходов и пять процентов выручки — это разные деньги, и решает разницу
    # именно сумма.
    d_inc = now["income"] - prev["income"]
    d_exp = now["expense"] - prev["expense"]
    out.append(_step(
        "Выручка изменилась на %s, расходы — на %s." % (_signed(d_inc), _signed(d_exp)),
        f"Суммы доходов и расходов за два периода по {days} дн."))

    expenses_lead = abs(d_exp) > abs(d_inc)
    if expenses_lead:
        # Расходы перевесили — ищем статью.
        cat = next((f for f in moves if str(f.get("key", "")).startswith("category")
                    and (f.get("numbers") or {}).get("change", 0) > 0), None)
        if cat:
            n = cat["numbers"]
            out.append(_step(
                "Сильнее всего выросла статья «%s»: %s против %s (%+d%%)."
                % (n["category"], _money(n["now"]), _money(n["was"]), n["change"]),
                "Расходы по категориям за два окна по %d дн." % n["days"]))
            _spend_step(bid, n["category"], n["days"], out)
        else:
            out.append(_step(
                "Какая именно статья выросла — сказать нельзя: заметного "
                "движения ни по одной категории не набралось.",
                "Расходы по категориям за два окна.", gap=True))
        return out

    # Выручка перевесила — идём в заявки и воронку.
    cnt_ch = _pct(orders_now["count"], orders_prev["count"])
    avg_ch = (_pct(orders_now["avg"], orders_prev["avg"])
              if orders_now["avg"] and orders_prev["avg"] else None)
    if cnt_ch is not None and cnt_ch < 0 and (avg_ch is None or abs(cnt_ch) >= abs(avg_ch)):
        out.append(_step(
            "Заявок стало меньше: %s против %s (%+d%%)."
            % (_num(orders_now["count"]), _num(orders_prev["count"]), cnt_ch),
            f"Счёт заявок за два периода по {days} дн."))
        _funnel_step(bid, days, out)
        return out

    if avg_ch is not None and avg_ch < 0:
        out.append(_step(
            "Заявок столько же, но чек ниже: %s против %s (%+d%%)."
            % (_money(orders_now["avg"]), _money(orders_prev["avg"]), avg_ch),
            "Сумма заявок делённая на их число за два периода."))
        out.append(_step(
            "Из-за чего упал чек — сказать нельзя: сравнить состав заявок по "
            "услугам за два периода не на чем.",
            "Выручка по услугам считается только за текущее окно.", gap=True))
        return out

    out.append(_step(
        "Дальше причину не видно: ни число заявок, ни средний чек заметно не "
        "менялись.",
        f"Заявки за два периода по {days} дн.", gap=True))
    return out


def _spend_step(bid, category, days, out):
    """
    Почему статья выросла: цена или объём — и чем это подтверждается.

    Раньше нить здесь заканчивалась словами «дальше данных нет». Данных
    действительно не хватает на полный ответ — количества в записях нет, — но
    на половину ответа хватает: если платежей столько же, а денег больше, то
    растёт точно не их число. Тупик остаётся тупиком, только на шаг глубже, и
    названо в нём уже не «неизвестно что», а конкретно чего не хватает.

    Если владелец дал VELOR ссылку на страницу поставщика и цена там выросла —
    догадка становится прочитанным фактом с адресом и датой. Это единственное
    место во всей нити, где VELOR смотрит наружу.
    """
    try:
        import purchases
        split = purchases.price_or_volume(bid, category, days)
        up = purchases.risen(bid, days)
    except Exception:
        log.exception("Разбор закупок не удался (biz %s)", bid)
        split, up = None, []

    if split and split["verdict"] == "price":
        out.append(_step(
            "Закупок по ней столько же — %s против %s, — а средний платёж вырос "
            "с %s до %s (%+d%%). Значит, растёт не число закупок."
            % (_num(split["ops"]), _num(split["ops_was"]),
               _money(split["average_was"]), _money(split["average"]),
               split["average_change"]),
            "Расходы по статье «%s» за два окна по %d дн.: суммы и число записей."
            % (category, days)))
    elif split and split["verdict"] == "volume":
        out.append(_step(
            "Закупать стали чаще: %s платежей против %s (%+d%%), а средний "
            "платёж почти прежний. Значит, растёт объём, а не цена."
            % (_num(split["ops"]), _num(split["ops_was"]), split["ops_change"]),
            "Расходы по статье «%s» за два окна по %d дн." % (category, days)))
        return
    elif split and split["verdict"] == "mixed":
        out.append(_step(
            "Выросло и то и другое: платежей %s против %s (%+d%%), средний "
            "платёж — с %s до %s (%+d%%)."
            % (_num(split["ops"]), _num(split["ops_was"]), split["ops_change"],
               _money(split["average_was"]), _money(split["average"]),
               split["average_change"]),
            "Расходы по статье «%s» за два окна по %d дн." % (category, days)))

    if up:
        top = up[0]
        who = (" у поставщика «%s»" % top["supplier"]) if top["supplier"] else ""
        out.append(_step(
            "И это видно на стороне поставщика: «%s»%s подорожало с %s до %s "
            "(%+d%%) — прочитано на его странице %s."
            % (top["item"], who, _money(top["was"]), _money(top["price"]),
               top["change"], str(top["seen_at"] or "")[:10]),
            "Страница поставщика: %s" % (top["url"] or "")))
        return

    out.append(_step(
        "Дальше нужна цена за единицу — в записях её нет: у расхода есть "
        "статья и сумма, но нет количества. Дайте ссылку на страницу "
        "поставщика в разделе «Закупки», и я буду читать цену сам.",
        "Наблюдаемых страниц по этой статье пока нет.", gap=True))


def _confidence(now, span, orders_now):
    """
    Насколько твёрдо стоит вся сводка.

    Не украшение и не проценты из воздуха: три состояния по двум измеримым
    вещам — сколько операций легло в расчёт и за какой срок. Обещать точность
    там, где за месяц было четыре записи, — тот же самый обман, только вежливый.
    """
    # Пороги не выдуманы под случай, а собраны из тех, что продукт уже
    # использует. «Уверенно» — это когда ОБА сравниваемых периода покрыты
    # целиком (два окна по days) и записей заметно больше, чем нужно, чтобы
    # вообще говорить о тренде. «С осторожностью» — когда сравнение уже
    # возможно, но с запасом на одну-две случайности.
    ops = int(now.get("entries") or 0) + int(orders_now.get("count") or 0)
    days = int((span or {}).get("days") or 0)
    if ops >= MIN_ENTRIES_TREND * 5 and days >= 60:
        return {"level": "high", "label": "уверенно",
                "why": "%s и %s за %d дней данных."
                       % (_ops(now.get("entries") or 0),
                          "%d заявок" % (orders_now.get("count") or 0), days)}
    if ops >= MIN_ENTRIES_TREND and days >= MIN_DAYS_FOR_TREND:
        return {"level": "medium", "label": "с осторожностью",
                "why": "Данных хватает на сравнение, но их немного: %s за %d дней."
                       % (_ops(now.get("entries") or 0), days)}
    return {"level": "low", "label": "предварительно",
            "why": "Данных мало: %s за %d дней — выводы могут поменяться."
                   % (_ops(now.get("entries") or 0), days)}


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
                "confidence": _confidence(now, span, orders_now),
                "metrics": metrics, "changed": [], "chain": [], "risks": [],
                "opportunities": [], "recommendations": [],
                "gaps": [NOT_ENOUGH + " В базе нет ни заявок, ни клиентов, ни денег."],
                "span": span, "generated_at": datetime.datetime.now().isoformat(timespec="seconds")}

    changed = _changed(bid, days, now, prev, orders_now, orders_prev,
                       clients_now, clients_prev, comparable, gaps)
    risks = _risks(bid, days, now, prev, comparable, sig, orders_now, gaps)
    opportunities = _opportunities(bid, days, now, sig, gaps)
    recommendations = _recommendations(risks, opportunities)
    # Нить проходит по уже посчитанному, поэтому идёт последней: ей нужны
    # движения категорий, которые нашёл _changed.
    chain = _chain(bid, days, now, prev, orders_now, orders_prev, comparable, changed)

    return {"days": days, "ready": True,
            "headline": _headline(days, now, comparable, prev, orders_now, risks),
            "why": _why(span, now, orders_now),
            "confidence": _confidence(now, span, orders_now),
            "metrics": metrics, "changed": changed, "chain": chain, "risks": risks,
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
            # Хвост заголовка держим коротким: это самая крупная строка на
            # экране, и лишние три слова стоят ей целой лишней строки.
            head += (f", в {_times(change)} больше прежнего"
                     if change >= PCT_AS_TIMES else
                     f", {change:+d}% к прошлому периоду")
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
