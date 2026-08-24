"""
Слой понимания входящих: что именно прислал владелец.

Работа делится на три шага, и каждый может обойтись без следующего:

  1. ЧТЕНИЕ   — достать из материала текст (PDF, DOCX, TXT, CSV, XLSX, заметка).
  2. РАЗБОР   — понять тип, вытащить цифры, предложить действие.
  3. РЕШЕНИЕ  — насколько уверены и что с этим можно делать без человека.

Почему два разборщика. Модель умнее правил, но она может быть не подключена,
упасть, ответить мусором или выдумать сумму. Правила глупее, но они не врут и
работают всегда. Поэтому сначала считаем правилами (это же и проверка), потом
спрашиваем модель, и если модель отвечает разумно — берём её ответ, а суммы
сверяем с найденными в тексте. Так «понимание» не превращается в фантазию.

Главное правило безопасности: деньги никогда не двигаются сами. Даже при
полной уверенности расход или доход только ПРЕДЛАГАЕТСЯ — нажимает человек.
Само собой применяется лишь то, что легко отменить и что не касается денег.
"""
import json
import logging
import os
import re

import database

# ── типы материалов ────────────────────────────────────────────────────────
# Список закрытый: всё, чего здесь нет, называется UNKNOWN. Придумывать новые
# типы на ходу нельзя — иначе интерфейс и будущая автоматика разойдутся.
TYPES = (
    "EXPENSE_DOCUMENT",       # чек, счёт, накладная — подтверждение траты
    "BANK_TRANSACTION",       # выписка или скрин операции по счёту
    "BUSINESS_RULES",         # регламент, условия работы, политика
    "SERVICES_OR_PRODUCTS",   # меню, ассортимент, перечень услуг
    "PRICE_LIST",             # прайс с ценами
    "CONTRACT",               # договор
    "FINANCIAL_TRANSACTION",  # человек своими словами описал движение денег
    "VOICE_INFORMATION",      # голосовое сообщение
    "UNKNOWN",
)

TYPE_RU = {
    "EXPENSE_DOCUMENT":      "Документ о расходе",
    "BANK_TRANSACTION":      "Банковская операция",
    "BUSINESS_RULES":        "Правила работы",
    "SERVICES_OR_PRODUCTS":  "Услуги или товары",
    "PRICE_LIST":            "Прайс-лист",
    "CONTRACT":              "Договор",
    "FINANCIAL_TRANSACTION": "Движение денег",
    "VOICE_INFORMATION":     "Голосовое сообщение",
    "UNKNOWN":               "Не разобрал",
}

# ── уверенность ────────────────────────────────────────────────────────────
# Пороги вынесены в настройки: у разных бизнесов разная цена ошибки.
HIGH_AT = float(os.getenv("INBOX_CONF_HIGH", "0.85"))
MEDIUM_AT = float(os.getenv("INBOX_CONF_MEDIUM", "0.60"))
# Применять безопасные действия само, без человека. Выключается одной переменной.
AUTO_APPLY = (os.getenv("INBOX_AUTOAPPLY", "1") or "").strip() not in ("0", "false", "no")

LEVEL_RU = {"HIGH": "высокая", "MEDIUM": "средняя", "LOW": "низкая"}
NEEDS_RU = {
    "HIGH":   "можно обрабатывать",
    "MEDIUM": "нужно подтверждение",
    "LOW":    "нужно уточнение",
}


def level_of(confidence: float) -> str:
    """Уверенность числом → уровень словом."""
    c = float(confidence or 0)
    if c >= HIGH_AT:
        return "HIGH"
    if c >= MEDIUM_AT:
        return "MEDIUM"
    return "LOW"


# ── действия ───────────────────────────────────────────────────────────────
# safe  — действие легко отменить и оно не двигает деньги;
# auto  — уместно выполнить без человека, когда уверенность высокая.
# Это разные вещи. «Сохранить в базу знаний» безопасно, но делать это самому
# на каждый чек — значит засорить память бизнеса, поэтому auto=False.
ACTIONS = {
    "create_expense":  {"title": "Записать расход",         "safe": False, "auto": False},
    "create_income":   {"title": "Записать доход",          "safe": False, "auto": False},
    "add_price_list":  {"title": "Добавить в прайс",        "safe": True,  "auto": True},
    "add_services":    {"title": "Добавить в услуги",       "safe": True,  "auto": True},
    "add_rules":       {"title": "Добавить в правила",      "safe": True,  "auto": True},
    "save_document":   {"title": "Сохранить в базу знаний", "safe": True,  "auto": False},
    "ask_user":        {"title": "Уточнить у владельца",    "safe": True,  "auto": False},
}


# ============================================================
#  ШАГ 1. ЧТЕНИЕ
# ============================================================

TEXT_EXT = {"txt", "md", "csv", "tsv", "json", "xml"}
READ_LIMIT = 20000          # знаков модели хватает с запасом


def extract_text(filename: str, data: bytes) -> str:
    """
    Достать текст из файла. Пусто — значит прочитать нечем (картинка без
    распознавания, архив, повреждённый файл). Это НЕ ошибка: дальше материал
    разбирается по имени и типу, просто с меньшей уверенностью.
    """
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    try:
        if ext == "pdf":
            import io as _io
            from pypdf import PdfReader
            reader = PdfReader(_io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages)[:READ_LIMIT]
        if ext == "docx":
            import io as _io
            import docx
            d = docx.Document(_io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs)[:READ_LIMIT]
        if ext in ("xlsx", "xls", "ods"):
            import io as _io
            from openpyxl import load_workbook
            wb = load_workbook(_io.BytesIO(data), read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        rows.append(" | ".join(cells))
                    if len(rows) > 400:
                        break
            return "\n".join(rows)[:READ_LIMIT]
        if ext in TEXT_EXT:
            for enc in ("utf-8", "cp1251"):
                try:
                    return data.decode(enc)[:READ_LIMIT]
                except UnicodeDecodeError:
                    continue
    except Exception:
        # Битый файл — обычное дело. Не роняем разбор: материал всё равно
        # сохранён, просто прочитать его не вышло.
        logging.info("Inbox: не удалось прочитать %s", filename, exc_info=True)
    return ""


# ============================================================
#  ШАГ 2. РАЗБОР ПРАВИЛАМИ (всегда работает, никогда не выдумывает)
# ============================================================

# Слова-приметы. Вес — насколько слово характерно именно для этого типа.
MARKERS = {
    "EXPENSE_DOCUMENT": [
        (r"кассовый чек", 3), (r"\bчек[аиов]{0,2}\b", 2), (r"товарный чек", 3),
        (r"итого к оплате", 3), (r"сумма к оплате", 2), (r"\bндс\b", 1),
        (r"\bинн\b", 1), (r"кассир", 2), (r"фискальн", 3), (r"накладная", 2),
        (r"счёт на оплату|счет на оплату", 3),
    ],
    "BANK_TRANSACTION": [
        (r"выписка по счёт|выписка по счет", 3), (r"списание", 2), (r"зачисление", 2),
        (r"остаток по счёт|остаток по счет", 3), (r"\bсбп\b", 2), (r"перевод на карту", 2),
        (r"дата операции", 2), (r"назначение платежа", 3), (r"корр\.?\s*счёт|корр\.?\s*счет", 3),
        (r"\bбик\b", 2), (r"баланс карты", 2),
    ],
    "PRICE_LIST": [
        (r"прайс[- ]?лист", 4), (r"\bпрайс\b", 3), (r"стоимость услуг", 2),
        (r"цены на", 2), (r"тариф", 1),
    ],
    "SERVICES_OR_PRODUCTS": [
        (r"\bменю\b", 3), (r"ассортимент", 3), (r"наши услуги", 3),
        (r"каталог товаров", 3), (r"перечень услуг", 3),
    ],
    "BUSINESS_RULES": [
        (r"регламент", 4), (r"правила работы", 4), (r"\bправила\b", 2),
        (r"условия работы", 3), (r"политика", 2), (r"инструкция для сотрудник", 3),
        (r"порядок оформления", 2),
    ],
    "CONTRACT": [
        (r"\bдоговор[аеуы]?\b", 3), (r"заказчик", 2), (r"исполнитель", 2),
        (r"стороны договор", 4), (r"настоящий договор", 4), (r"реквизиты сторон", 4),
        (r"приложение №", 1),
    ],
    "FINANCIAL_TRANSACTION": [
        (r"заплатил|оплатил|заплатили|оплатили", 3), (r"перевёл|перевел|перевели", 3),
        (r"потратил|потратили", 3), (r"получил|получили", 2), (r"выручка", 2),
        (r"зарплат", 2), (r"аренд", 1), (r"закупил|закупили", 2), (r"вернул", 1),
    ],
}

# Расход это или доход — по глаголу. Нужно и правилам, и проверке ответа модели.
INCOME_WORDS = re.compile(r"получил|получили|выручк|поступил|заплатили\s+нам|оплатили\s+нам|продал|продали", re.I)
EXPENSE_WORDS = re.compile(r"заплатил|оплатил|потратил|перевёл|перевел|купил|закупил|списал", re.I)

CATEGORY_WORDS = [
    ("зарплата", r"зарплат|оклад|аванс сотрудник"),
    ("аренда", r"аренд|съём помещен|съем помещен"),
    ("реклама", r"реклам|таргет|продвижен|директ"),
    ("закупка", r"закуп|поставщик|товар для"),
    ("доставка", r"доставк|курьер|логистик|сдэк|транспорт"),
    ("налоги", r"налог|усн|страховые взнос|фнс"),
    ("услуги", r"подписк|обслуживан|сервис"),
]

# Деньги в тексте: «1 850 ₽», «1850 руб», «70 тысяч», «70 тыс.», «2.5 млн».
_SCALE = {"тысяч": 1000, "тыс": 1000, "тысячи": 1000, "тысяча": 1000,
          "к": 1000, "млн": 1000000, "миллион": 1000000, "миллиона": 1000000}
_MONEY_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(тысяч[аи]?|тыс\.?|млн|миллион[аов]*|к)?\s*"
    r"(₽|руб\.?|рублей|рубля|р\.|rub|₸|тенге|\$|usd|долларов|€|eur|евро)?",
    re.I)

CURRENCY = {"₽": "RUB", "руб": "RUB", "рублей": "RUB", "рубля": "RUB", "р.": "RUB", "rub": "RUB",
            "₸": "KZT", "тенге": "KZT",
            "$": "USD", "usd": "USD", "долларов": "USD",
            "€": "EUR", "eur": "EUR", "евро": "EUR"}


def find_money(text: str):
    """
    Все суммы из текста: [{amount, currency, raw}]. Берём только те, у которых
    есть признак денег — знак валюты или множитель («тысяч»). Голое число
    деньгами не считаем: в чеке полно количеств, дат и номеров.
    """
    out = []
    for m in _MONEY_RE.finditer(text or ""):
        num, scale, cur = m.group(1), (m.group(2) or "").lower(), (m.group(3) or "").lower()
        if not scale and not cur:
            continue
        try:
            value = float(num.replace(" ", "").replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        if scale:
            key = scale.rstrip(".").rstrip("аиов")
            value *= _SCALE.get(key, _SCALE.get(scale.rstrip("."), 1))
        if value <= 0 or value > 10 ** 12:
            continue
        out.append({"amount": int(round(value)),
                    "currency": CURRENCY.get(cur.rstrip("."), "RUB" if cur or scale else "RUB"),
                    "raw": m.group(0).strip()})
    return out


def guess_category(text: str):
    low = (text or "").lower()
    for name, pattern in CATEGORY_WORDS:
        if re.search(pattern, low):
            return name
    return None


def classify_by_rules(text: str, filename: str = "", mime: str = "", kind: str = "file"):
    """
    Тип материала по приметам. Возвращает (тип, уверенность 0..1, чем доказали).
    Уверенность правил нарочно скромная: они не читают смысл, а считают слова.
    """
    hay = ((text or "") + "\n" + (filename or "")).lower()

    if (mime or "").startswith("audio/"):
        return "VOICE_INFORMATION", 0.9, ["материал — аудио"]

    scores, hits = {}, {}
    for t, markers in MARKERS.items():
        got, why = 0, []
        for pattern, weight in markers:
            if re.search(pattern, hay):
                got += weight
                why.append(pattern.replace(r"\b", "").replace("|", " или "))
        if got:
            scores[t] = got
            hits[t] = why

    # Заметка своими словами: движение денег видно по глаголу и сумме.
    if kind == "text":
        money = find_money(text)
        if money and (EXPENSE_WORDS.search(text or "") or INCOME_WORDS.search(text or "")):
            scores["FINANCIAL_TRANSACTION"] = scores.get("FINANCIAL_TRANSACTION", 0) + 4
            hits.setdefault("FINANCIAL_TRANSACTION", []).append("сумма и глагол о деньгах")

    if not scores:
        return "UNKNOWN", 0.0, []

    best = max(scores, key=scores.get)
    score = scores[best]
    runner = sorted(scores.values(), reverse=True)
    # Если второй тип почти догоняет первый — уверенность падает: это и есть
    # честный признак того, что материал спорный.
    gap = score - (runner[1] if len(runner) > 1 else 0)
    conf = min(0.80, 0.30 + 0.09 * score + 0.05 * gap)
    if not (text or "").strip():
        conf = min(conf, 0.45)      # судим только по имени файла — этого мало
    return best, round(conf, 2), hits.get(best, [])[:4]


def extract_by_rules(kind_type: str, text: str, kind: str = "file"):
    """Что можно вытащить из текста наверняка: суммы, валюта, категория."""
    data = {}
    money = find_money(text)
    if kind_type in ("FINANCIAL_TRANSACTION", "EXPENSE_DOCUMENT", "BANK_TRANSACTION") and money:
        # В чеке итог обычно самая большая сумма; в заметке — единственная.
        top = max(money, key=lambda m: m["amount"])
        data["amount"] = top["amount"]
        data["currency"] = top["currency"]
        if len(money) > 1:
            data["amounts_found"] = len(money)
    cat = guess_category(text)
    if cat:
        data["category"] = cat
    if kind_type in ("FINANCIAL_TRANSACTION", "EXPENSE_DOCUMENT"):
        if EXPENSE_WORDS.search(text or ""):
            data["direction"] = "expense"
        elif INCOME_WORDS.search(text or ""):
            data["direction"] = "income"
        elif kind_type == "EXPENSE_DOCUMENT":
            data["direction"] = "expense"
    # Кому платили — слово после глагола, если оно с большой буквы.
    m = re.search(r"(?:заплатил|оплатил|перевёл|перевел|отдал)\w*\s+([А-ЯЁ][\w-]+)", text or "")
    if m:
        data["counterparty"] = m.group(1)
    return data


ACTION_BY_TYPE = {
    "EXPENSE_DOCUMENT":      ["create_expense", "save_document"],
    "BANK_TRANSACTION":      ["create_expense", "save_document"],
    "FINANCIAL_TRANSACTION": ["create_expense"],
    "PRICE_LIST":            ["add_price_list", "save_document"],
    "SERVICES_OR_PRODUCTS":  ["add_services", "save_document"],
    "BUSINESS_RULES":        ["add_rules", "save_document"],
    "CONTRACT":              ["save_document"],
    "VOICE_INFORMATION":     ["ask_user"],
    "UNKNOWN":               ["ask_user"],
}


def suggest(kind_type: str, extracted: dict):
    """Какие действия предложить. Направление денег меняет расход на доход."""
    names = list(ACTION_BY_TYPE.get(kind_type, ["ask_user"]))
    if extracted.get("direction") == "income":
        names = ["create_income" if n == "create_expense" else n for n in names]
    if "create_expense" in names or "create_income" in names:
        # Без суммы записывать в финансы нечего — остаётся уточнить.
        if not extracted.get("amount"):
            names = [n for n in names if n not in ("create_expense", "create_income")]
            names.append("ask_user")
    out = []
    for n in names:
        meta = ACTIONS.get(n)
        if meta:
            out.append({"action": n, "title": meta["title"],
                        "safe": meta["safe"], "auto": meta["auto"]})
    return out


# ============================================================
#  ШАГ 3. РАЗБОР МОДЕЛЬЮ + СВЕРКА
# ============================================================

def _ask_model(business, text, filename, mime, kind, image=None):
    """
    Спросить модель. Возвращает разобранный ответ или None — молча, потому что
    отсутствие ИИ это нормальный режим работы, а не сбой.
    """
    import ai
    if not ai.ai_available():
        return None
    try:
        return ai.understand_material(business, text=text, filename=filename,
                                      mime=mime, kind=kind, image=image)
    except Exception:
        logging.info("Inbox: модель не разобрала материал", exc_info=True)
        return None


def _merge(rules, model, text):
    """
    Свести ответ правил и ответ модели.

    Модель понимает смысл — её типу верим. Но цифры проверяем: если модель
    называет сумму, которой в тексте нет, эта сумма выдумана, и в финансы её
    пускать нельзя. Такую сумму выбрасываем и снижаем уверенность.
    """
    r_type, r_conf, r_why = rules
    if not model:
        return {"type": r_type, "confidence": r_conf, "engine": "rules", "evidence": r_why}

    m_type = model.get("type") if model.get("type") in TYPES else "UNKNOWN"
    m_conf = float(model.get("confidence") or 0)
    m_conf = min(0.99, max(0.0, m_conf))

    agree = (m_type == r_type and r_type != "UNKNOWN")
    if agree:
        conf = min(0.98, max(m_conf, r_conf) + 0.10)     # два разных способа сошлись
    elif r_type == "UNKNOWN":
        conf = m_conf                                      # правилам сказать нечего
    else:
        conf = min(m_conf, 0.70)                           # разошлись — не уверены

    return {"type": m_type, "confidence": round(conf, 2), "engine": "llm",
            "model_type": m_type, "rules_type": r_type,
            "agree": agree, "evidence": r_why, "raw": model}


def _check_model_amount(model_data, text):
    """
    Сверить сумму, названную моделью, с тем, что реально написано в материале.

    Возвращает (сумма_для_использования_или_None, претензия_или_None).
    Это главная защита слоя: модель охотно называет правдоподобные числа,
    которых в документе нет, а из этих чисел потом получаются записи в
    финансах. Чего нет в тексте — того не было.
    """
    if not isinstance(model_data, dict) or model_data.get("amount") is None:
        return None, None
    try:
        amount = int(model_data["amount"])
    except (TypeError, ValueError):
        return None, "сумма модели не число — отбросил"

    found = {m["amount"] for m in find_money(text or "")}
    if not found:
        if (text or "").strip():
            return None, "сумма не найдена в тексте — отбросил"
        # Текста нет вовсе (картинка без распознавания): опровергнуть нечем,
        # но и подтвердить тоже — берём, пометив как непроверенную.
        return amount, None
    if amount in found:
        return amount, None
    near = min(found, key=lambda f: abs(f - amount))
    return near, f"сумма поправлена по тексту: {amount} → {near}"


# ============================================================
#  ГЛАВНОЕ: разобрать материал
# ============================================================

def process(business_id, item_id):
    """
    Понять один материал и сохранить результат.

    Оригинал не трогаем ни при каком исходе — понимание это отдельный слой
    поверх приёмника, а не замена ему.
    """
    import storage
    item = database.get_inbox_item(item_id, business_id)
    if not item:
        return None
    business = database.get_business(business_id) or {}

    database.set_inbox_status(item_id, business_id, "PROCESSING")

    text, image, read_error = "", None, None
    try:
        if item["kind"] == "text":
            text = item.get("body") or ""
        elif item.get("storage_key"):
            data = storage.get(business_id, item["storage_key"])
            text = extract_text(item.get("filename"), data)
            if (item.get("mime") or "").startswith("image/") and len(data) <= 4 * 1024 * 1024:
                image = (item["mime"], data)   # модели с глазами покажем саму картинку
    except storage.StorageError as e:
        read_error = str(e)
    except Exception as e:
        read_error = "Не удалось прочитать материал: %s" % e

    if read_error:
        result = {"type": "UNKNOWN", "confidence": 0.0, "level": "LOW",
                  "summary": "Материал не удалось прочитать.",
                  "extracted_data": {}, "suggested_actions": suggest("UNKNOWN", {}),
                  "engine": "rules", "error": read_error, "applied": []}
        database.save_inbox_result(business_id, item_id, result)
        database.set_inbox_status(item_id, business_id, "FAILED", read_error)
        return result

    rules = classify_by_rules(text, item.get("filename"), item.get("mime"), item["kind"])
    model = _ask_model(business, text, item.get("filename"), item.get("mime"),
                       item["kind"], image=image)
    merged = _merge(rules, model, text)

    kind_type = merged["type"]
    extracted = extract_by_rules(kind_type, text, item["kind"])
    notes = []
    if model and isinstance(model.get("extracted_data"), dict):
        model_data = dict(model["extracted_data"])
        # Сумму проверяем ОТДЕЛЬНО и до слияния: иначе верная сумма от правил
        # прикрыла бы выдуманную моделью, и подлог остался бы незамеченным.
        checked, complaint = _check_model_amount(model_data, text)
        model_data.pop("amount", None)
        if complaint:
            notes.append(complaint)
        if checked is not None and not extracted.get("amount"):
            extracted["amount"] = checked
            extracted.setdefault("currency", model_data.get("currency") or "RUB")
            if not (text or "").strip():
                extracted["amount_unverified"] = True
        for k, v in model_data.items():
            if v in (None, "", []):
                continue
            extracted.setdefault(k, v)

    conf = merged["confidence"]
    if notes:
        conf = min(conf, 0.55)      # модель промахнулась по цифрам — доверия меньше
    if kind_type == "UNKNOWN":
        conf = min(conf, 0.4)

    summary = (model or {}).get("summary") or _summary_by_rules(kind_type, extracted, text)
    result = {
        "type": kind_type,
        "confidence": round(conf, 2),
        "level": level_of(conf),
        "summary": summary[:300],
        "extracted_data": extracted,
        "suggested_actions": suggest(kind_type, extracted),
        "engine": merged["engine"],
        "model": (model or {}).get("provider"),
        "error": None,
        "applied": [],
        "notes": notes,
        "evidence": merged.get("evidence") or [],
    }

    # Автоматически — только безопасное и только при высокой уверенности.
    # Деньги не двигаются сами никогда: create_expense/create_income safe=False.
    applied = []
    if AUTO_APPLY and result["level"] == "HIGH":
        for a in result["suggested_actions"]:
            if a["safe"] and a.get("auto"):
                done = apply_action(business_id, item_id, a["action"], result, auto=True)
                if done:
                    applied.append({"action": a["action"], "auto": True, "detail": done})
    result["applied"] = applied

    result_id = database.save_inbox_result(business_id, item_id, result)
    result["id"] = result_id

    status = "PROCESSED" if result["level"] == "HIGH" else "NEEDS_REVIEW"
    database.set_inbox_status(item_id, business_id, status)
    return result


def _summary_by_rules(kind_type, extracted, text):
    """Короткая фраза, когда модели нет. Ничего не выдумывает."""
    name = TYPE_RU.get(kind_type, "Материал")
    if kind_type == "UNKNOWN":
        return ("Не удалось понять, что это. Откройте материал — и скажите, "
                "куда его отнести.")
    bits = []
    if extracted.get("amount"):
        bits.append("{:,}".format(extracted["amount"]).replace(",", " ") + " "
                    + {"RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}.get(
                        extracted.get("currency", "RUB"), ""))
    if extracted.get("category"):
        bits.append("категория: " + extracted["category"])
    if extracted.get("counterparty"):
        bits.append("кому: " + extracted["counterparty"])
    return name + ((" — " + ", ".join(bits)) if bits else ".")


# ============================================================
#  ВЫПОЛНЕНИЕ ПРЕДЛОЖЕННОГО ДЕЙСТВИЯ
# ============================================================

class ActionError(Exception):
    """Действие выполнить нельзя — с объяснением для человека."""


def apply_action(business_id, item_id, action, result=None, auto=False):
    """
    Выполнить одно предложенное действие. Возвращает строку с тем, что вышло.

    auto=True — вызвано самим VELOR. В этом режиме небезопасные действия
    запрещены жёстко, а не по совести вызывающего.
    """
    meta = ACTIONS.get(action)
    if not meta:
        raise ActionError("Неизвестное действие.")
    if auto and not meta["safe"]:
        raise ActionError("Такое действие VELOR сам не выполняет.")

    res = result or database.get_inbox_result(business_id, item_id)
    if not res:
        raise ActionError("Материал ещё не разобран.")
    item = database.get_inbox_item(item_id, business_id)
    if not item:
        raise ActionError("Материал не найден.")
    data = res.get("extracted_data") or {}
    title = (item.get("title") or item.get("filename") or "материал")[:120]

    if action in ("create_expense", "create_income"):
        amount = data.get("amount")
        if not amount:
            raise ActionError("В материале нет суммы — записывать нечего.")
        kind = "income" if action == "create_income" else "expense"
        note = (res.get("summary") or title)[:160]
        database.add_finance_entry(business_id, kind, data.get("category") or "без категории",
                                   int(amount), note)
        return f"{'Доход' if kind == 'income' else 'Расход'} {int(amount)} записан в финансы"

    if action in ("add_price_list", "add_services", "add_rules"):
        kind = {"add_price_list": "product", "add_services": "service",
                "add_rules": "rule"}[action]
        body = (res.get("summary") or "")[:2000]
        database.add_fact(business_id, kind, title, body)
        return "Добавлено в память бизнеса"

    if action == "save_document":
        # Текст уже в материале; в базу знаний кладём выжимку, а не файл —
        # оригинал остаётся во входящих и никуда не девается.
        database.add_fact(business_id, "rule", title, (res.get("summary") or "")[:2000])
        return "Сохранено в базу знаний"

    if action == "ask_user":
        database.set_inbox_status(item_id, business_id, "NEEDS_REVIEW")
        return "Отмечено: нужен ваш взгляд"

    raise ActionError("Действие пока не поддержано.")
