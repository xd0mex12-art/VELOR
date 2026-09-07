# -*- coding: utf-8 -*-
"""
Закупки и поставщики: у кого владелец покупает, почём и где то же дешевле.

Здесь два разных источника правды, и путать их нельзя.

СВОИ ЗАПИСИ отвечают на вопрос «кому и сколько я плачу». Из них честно
считаются доли, зависимость от одного поставщика и движение по каждому:
столько же операций, а денег больше — значит, дорожает не объём. Чего из них
НЕ выжать — цены за единицу: в платеже лежит корзина, и «средний платёж
поставщику А выше, чем поставщику Б» — красивое число, которое не значит
ничего. Такое сравнение здесь не делается принципиально.

СТРАНИЦЫ ПОСТАВЩИКОВ отвечают на вопрос «сколько это стоит у них». Одна и та
же позиция у двух-трёх продавцов, с адресом источника и датой проверки, —
единственное сравнение поставщиков, которое можно назвать сравнением. Ссылку
даёт владелец: искать поставщика за него VELOR пока не умеет, и делать вид,
что умеет, хуже, чем не уметь.

Модель здесь не участвует ни разу. Цена — это факт с одним правильным
ответом; спрашивать его у модели значит платить за уже известное и получать
это с ошибкой.
"""
import datetime
import logging

import database
import director
import internet

log = logging.getLogger("velor")

# Доля закупок одному поставщику, после которой это уже зависимость, а не
# предпочтение. Порог высокий намеренно: у маленького бизнеса один поставщик
# на категорию — норма, и кричать об этом на каждом обходе бессмысленно.
DEPENDENCE_SHARE = 70
DEPENDENCE_MIN_SUM = 30_000     # мелкие категории под разговор не подходят
DEPENDENCE_MIN_OPS = 4          # и разовая закупка тоже

# Насколько дешевле должно быть у другого, чтобы об этом стоило говорить.
# Пять процентов — это доставка и разница в упаковке, а не выгода.
CHEAPER_PCT = 10

# Насколько число может шевельнуться, оставаясь «тем же». Десять процентов —
# это одна закупка из десяти, то есть обычная неровность месяца, а не событие.
FLAT_PCT = 10

RECHECK_HOURS = 12              # чаще перечитывать чужие страницы незачем
CHECK_CAP = 20                  # страниц за один обход — чтобы не подвесить его


# ── свои записи ────────────────────────────────────────────────────────────

def _key(row):
    """Чем отличать поставщиков: карточкой памяти, а если её нет — именем."""
    return ("id:%s" % row["sid"]) if row.get("sid") else ("name:%s" % (row.get("name") or ""))


def suppliers(business_id, days=30, offset=0):
    """
    Кому и сколько бизнес платил за окно.

    Считаем по дате операции, а не по дате записи: счёт, занесённый в понедельник
    за прошлый месяц, — расход прошлого месяца.
    """
    rows = database.purchases_by_supplier(business_id, days, offset)
    got = {}
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        k = _key(r)
        cur = got.setdefault(k, {"key": k, "id": r.get("sid"), "name": name,
                                 "total": 0, "ops": 0, "categories": {}})
        cur["total"] += int(r["total"] or 0)
        cur["ops"] += int(r["n"] or 0)
        cat = (r.get("category") or "без статьи").strip() or "без статьи"
        cur["categories"][cat] = cur["categories"].get(cat, 0) + int(r["total"] or 0)
    total = sum(s["total"] for s in got.values()) or 0
    out = list(got.values())
    for s in out:
        s["share"] = round(s["total"] * 100 / total) if total else 0
        s["average"] = round(s["total"] / s["ops"]) if s["ops"] else 0
        s["top_category"] = max(s["categories"], key=s["categories"].get) if s["categories"] else ""
    out.sort(key=lambda s: -s["total"])
    return {"total": total, "suppliers": out}


def dependence(business_id, days=90):
    """
    Категория, где почти все деньги уходят одному.

    Это не совет сменить поставщика — это ответ на вопрос «что будет, если он
    поднимет цену или пропадёт». Поэтому смотрим широкое окно: зависимость —
    свойство привычки, а не месяца.
    """
    rows = database.purchases_by_supplier(business_id, days)
    by_cat = {}
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        cat = (r.get("category") or "").strip()
        if not cat:
            continue
        box = by_cat.setdefault(cat, {"total": 0, "ops": 0, "who": {}})
        box["total"] += int(r["total"] or 0)
        box["ops"] += int(r["n"] or 0)
        box["who"][name] = box["who"].get(name, 0) + int(r["total"] or 0)
    out = []
    for cat, box in by_cat.items():
        if box["total"] < DEPENDENCE_MIN_SUM or box["ops"] < DEPENDENCE_MIN_OPS:
            continue
        if len(box["who"]) < 1:
            continue
        who = max(box["who"], key=box["who"].get)
        share = round(box["who"][who] * 100 / box["total"])
        if share < DEPENDENCE_SHARE:
            continue
        out.append({"category": cat, "supplier": who, "share": share,
                    "total": box["total"], "spent": box["who"][who],
                    "others": len(box["who"]) - 1, "days": days})
    out.sort(key=lambda d: -d["spent"])
    return out


def movement(business_id, days=30):
    """
    Что изменилось у каждого поставщика: сумма, число операций, средний платёж.

    Средний платёж сравниваем ТОЛЬКО поставщика с самим собой. У одного и того
    же продавца корзина от месяца к месяцу похожа, и рост среднего платежа при
    том же числе закупок — это уже разговор о цене. Между разными
    поставщиками то же число не значит ничего: у них разные корзины.
    """
    now_ = suppliers(business_id, days, 0)["suppliers"]
    was_ = {s["key"]: s for s in suppliers(business_id, days, days)["suppliers"]}
    out = []
    for s in now_:
        old = was_.get(s["key"])
        if not old:
            out.append({**s, "was": None, "change": None, "average_change": None,
                        "ops_change": None})
            continue
        out.append({**s, "was": old,
                    "change": _pct(s["total"], old["total"]),
                    "average_change": _pct(s["average"], old["average"]),
                    "ops_change": _pct(s["ops"], old["ops"])})
    return out


def _pct(now_v, was_v):
    if not was_v:
        return None
    return round((now_v - was_v) * 100 / was_v)


def price_or_volume(business_id, category, days=30):
    """
    Статья выросла — из-за цены или из-за объёма?

    Тот самый вопрос, на котором нить «почему» упиралась в тупик. Ответить на
    него полностью по записям нельзя: в них есть сумма платежа и его дата, но
    нет количества. Зато можно ответить НАПОЛОВИНУ, и половина эта решает
    больше, чем кажется: если закупок столько же, а денег заметно больше, то
    растёт точно не число закупок. Владельцу этого достаточно, чтобы знать,
    куда смотреть, — а VELOR при этом не выдумывает ни одной цифры.

    Возвращает {"verdict", …} или None, если сравнивать не с чем. verdict:
      price  — закупок столько же, платёж вырос;
      volume — закупок больше, а платёж прежний;
      mixed  — выросло и то и другое.
    """
    now_ = (database.category_period(business_id, "expense", days, 0) or {}).get(category)
    was_ = (database.category_period(business_id, "expense", days, days) or {}).get(category)
    if not now_ or not was_ or not was_.get("total") or not was_.get("n"):
        return None
    ops_now, ops_was = int(now_["n"] or 0), int(was_["n"] or 0)
    if not ops_now or not ops_was:
        return None
    sum_ch = _pct(now_["total"], was_["total"])
    if sum_ch is None or sum_ch <= 0:
        return None
    avg_now = round(now_["total"] / ops_now)
    avg_was = round(was_["total"] / ops_was)
    ops_ch = _pct(ops_now, ops_was) or 0
    avg_ch = _pct(avg_now, avg_was) or 0

    flat_ops = abs(ops_ch) <= FLAT_PCT
    flat_avg = abs(avg_ch) <= FLAT_PCT
    if flat_ops and avg_ch > FLAT_PCT:
        verdict = "price"
    elif flat_avg and ops_ch > FLAT_PCT:
        verdict = "volume"
    elif ops_ch > FLAT_PCT and avg_ch > FLAT_PCT:
        verdict = "mixed"
    else:
        return None
    return {"category": category, "days": days, "verdict": verdict,
            "ops": ops_now, "ops_was": ops_was, "ops_change": ops_ch,
            "average": avg_now, "average_was": avg_was, "average_change": avg_ch,
            "total": int(now_["total"]), "total_was": int(was_["total"]),
            "change": sum_ch}


def risen(business_id, days=30):
    """
    Позиции, которые подорожали на страницах поставщиков за окно.

    Это уже не догадка по платежам, а прочитанная цена с адресом и датой —
    единственное место, где VELOR может сказать «подорожало» и показать, где
    он это увидел.
    """
    edge = (datetime.datetime.utcnow()
            - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for row in database.list_price_watch(business_id):
        points = [p for p in database.price_history(row["id"], business_id, limit=50)
                  if str(p.get("seen_at") or "") >= edge]
        if len(points) < 2:
            continue
        newest, oldest = points[0], points[-1]
        if int(newest["price"]) <= int(oldest["price"]):
            continue
        out.append({"watch_id": row["id"], "item": row.get("item") or "",
                    "supplier": (row.get("supplier_name") or "").strip(),
                    "url": row.get("url"),
                    "was": int(oldest["price"]), "price": int(newest["price"]),
                    "change": _pct(int(newest["price"]), int(oldest["price"])),
                    "seen_at": newest.get("seen_at")})
    out.sort(key=lambda r: -(r["change"] or 0))
    return out


# ── страницы поставщиков ───────────────────────────────────────────────────

def _watch_public(row):
    """Наблюдаемая страница так, как она попадёт на экран."""
    return {
        "id": row["id"],
        "item": row.get("item") or "",
        "supplier": (row.get("supplier_name") or "").strip(),
        "supplier_id": row.get("supplier_id"),
        "url": row.get("url") or "",
        "price": row.get("price"),
        "price_ru": director._money(row["price"]) if row.get("price") else "",
        # Кусок страницы вокруг числа. Владелец должен видеть, ЧТО именно VELOR
        # принял за цену: чужая страница может быть устроена как угодно, и
        # цифра без контекста — это доверие на слово.
        "context": row.get("context") or "",
        "checked_at": row.get("checked_at"),
        "error": row.get("error") or "",
    }


def watches(business_id):
    """Все наблюдаемые страницы, по позициям."""
    return [_watch_public(r) for r in database.list_price_watch(business_id)]


def compare(business_id):
    """
    Сравнение поставщиков по одной и той же позиции.

    Показываем только те позиции, где сравнивать есть с чем: одна цена — это
    не сравнение, а просто цена. Разрыв считаем от дешёвого, потому что
    вопрос владельца звучит «на сколько я переплачиваю», а не «на сколько тот
    дешевле».
    """
    by_item = {}
    for w in watches(business_id):
        if not w["price"]:
            continue
        by_item.setdefault(w["item"], []).append(w)
    out = []
    for item, rows in by_item.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r["price"])
        low, high = rows[0], rows[-1]
        if low["price"] <= 0:
            continue
        gap = round((high["price"] - low["price"]) * 100 / low["price"])
        out.append({
            "item": item,
            "rows": rows,
            "cheapest": low,
            "dearest": high,
            "gap": gap,
            "diff": high["price"] - low["price"],
            "diff_ru": director._money(high["price"] - low["price"]),
        })
    out.sort(key=lambda c: -c["diff"])
    return out


def _stale(row, now):
    """Пора ли перечитывать страницу."""
    when = row.get("checked_at")
    if not when:
        return True
    try:
        seen = datetime.datetime.strptime(str(when)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return True
    return (now - seen).total_seconds() >= RECHECK_HOURS * 3600


def check(business_id, watch_id, *, by_owner=True, now=None):
    """
    Перечитать одну страницу и записать, что увидели.

    Возвращает {"ok", "price", "was", "error"}. Ошибку записываем в саму
    запись, а не только в лог: владелец должен видеть, что ссылка перестала
    открываться, — иначе он будет считать, что цена не менялась, тогда как
    VELOR просто не смог посмотреть.
    """
    row = database.get_price_watch(watch_id, business_id)
    if not row:
        return {"ok": False, "error": "Такой страницы нет."}
    was = row.get("price")
    try:
        page = internet.fetch(row["url"], by_owner=by_owner)
    except internet.WebError as e:
        database.save_price(watch_id, business_id, error=str(e),
                            checked_at=database.now())
        return {"ok": False, "was": was, "error": str(e)}
    hit = internet.price_for(page["text"], row["item"])
    if not hit:
        why = "На странице не нашёл цену рядом с «%s» — проверьте ссылку и название." % row["item"]
        database.save_price(watch_id, business_id, error=why, checked_at=database.now())
        return {"ok": False, "was": was, "error": why}
    database.save_price(watch_id, business_id, price=hit["value"],
                        context=hit["context"], error=None,
                        checked_at=database.now())
    return {"ok": True, "price": hit["value"], "was": was,
            "context": hit["context"],
            "changed": bool(was) and int(hit["value"]) != int(was)}


def round_business(business_id, *, now=None):
    """Обход страниц одного бизнеса. Возвращает, что изменилось."""
    now = now or datetime.datetime.utcnow()
    moved, checked = [], 0
    for row in database.list_price_watch(business_id):
        if checked >= CHECK_CAP:
            break
        if not _stale(row, now):
            continue
        checked += 1
        # by_owner=False: это наш собственный обход, а не нажатая кнопка, —
        # значит, сначала спрашиваем у сайта robots.txt.
        got = check(business_id, row["id"], by_owner=False, now=now)
        if got.get("changed"):
            moved.append({"item": row["item"],
                          "supplier": row.get("supplier_name") or "",
                          "was": got["was"], "price": got["price"],
                          "url": row.get("url")})
    return {"checked": checked, "moved": moved}


def round_all(*, now=None):
    """Обход по всем бизнесам — из той же получасовой петли, что и остальное."""
    total = {"businesses": 0, "checked": 0, "moved": 0}
    for biz in database.list_businesses_with_stats():
        bid = biz["id"]
        try:
            if not database.list_price_watch(bid, limit=1):
                continue
            got = round_business(bid, now=now)
        except Exception:
            log.exception("Обход цен не удался (biz %s)", bid)
            continue
        total["businesses"] += 1
        total["checked"] += got["checked"]
        total["moved"] += len(got["moved"])
    return total
