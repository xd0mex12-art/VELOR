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
    "EXPENSE_DOCUMENT",       # чек — подтверждение траты
    "INVOICE",                # счёт на оплату
    "WAYBILL",                # накладная
    "SALARY_PAYMENT",         # ведомость, расчётный листок, выплата сотруднику
    "REFUND",                 # возврат денег
    "INCOME_DOCUMENT",        # подтверждение поступления
    "BANK_TRANSACTION",       # выписка или скрин операции по счёту
    "BUSINESS_RULES",         # регламент, условия работы, политика
    "SERVICES_OR_PRODUCTS",   # меню, ассортимент, перечень услуг
    "PRICE_LIST",             # прайс с ценами
    "CONTRACT",               # договор
    "FINANCIAL_TRANSACTION",  # человек своими словами описал движение денег
    "EMPLOYEE_INFORMATION",   # трудовой договор, резюме, данные о сотруднике
    "SUPPLIER_INFORMATION",   # поставщик: условия, контакты, предложение
    "COMPANY_INFO",           # реквизиты, адрес, режим работы — о самой компании
    "GOAL_STATEMENT",         # владелец сказал, чего хочет достичь
    "VOICE_INFORMATION",      # голосовое сообщение
    "UNKNOWN",
)

TYPE_RU = {
    "EXPENSE_DOCUMENT":      "Документ о расходе",
    "INVOICE":               "Счёт на оплату",
    "WAYBILL":               "Накладная",
    "SALARY_PAYMENT":        "Выплата зарплаты",
    "REFUND":                "Возврат денег",
    "INCOME_DOCUMENT":       "Документ о доходе",
    "BANK_TRANSACTION":      "Банковская операция",
    "BUSINESS_RULES":        "Правила работы",
    "SERVICES_OR_PRODUCTS":  "Услуги или товары",
    "PRICE_LIST":            "Прайс-лист",
    "CONTRACT":              "Договор",
    "FINANCIAL_TRANSACTION": "Движение денег",
    "EMPLOYEE_INFORMATION":  "Сведения о сотруднике",
    "SUPPLIER_INFORMATION":  "Поставщик",
    "COMPANY_INFO":          "Сведения о компании",
    "GOAL_STATEMENT":        "Цель бизнеса",
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
    "create_client":   {"title": "Завести клиента",         "safe": True,  "auto": False},
    "create_order":    {"title": "Создать заявку",          "safe": False, "auto": False},
    "add_price_list":  {"title": "Добавить в прайс",        "safe": True,  "auto": True},
    "add_services":    {"title": "Добавить в услуги",       "safe": True,  "auto": True},
    "add_rules":       {"title": "Добавить в правила",      "safe": True,  "auto": True},
    "add_goal":        {"title": "Поставить цель",          "safe": True,  "auto": False},
    # Сотрудник и поставщик — живые люди и партнёры. Запись безопасна (её легко
    # исправить), но заводить её без ведома владельца нельзя: ошибка в имени
    # или в должности потом всплывёт в ответе клиенту.
    "add_employee":    {"title": "Добавить сотрудника",      "safe": True,  "auto": False},
    "add_supplier":    {"title": "Добавить поставщика",      "safe": True,  "auto": False},
    "add_company":     {"title": "Записать о компании",      "safe": True,  "auto": False},
    "save_document":   {"title": "Сохранить в базу знаний", "safe": True,  "auto": False},
    "ask_user":        {"title": "Уточнить у владельца",    "safe": True,  "auto": False},
}

# Какое действие какой вид записи создаёт. ask_user не создаёт ничего — это
# признание, что решение за человеком, а не работа.
ENTITY_BY_ACTION = {
    "create_expense": "expense",
    "create_income":  "income",
    "create_client":  "client",
    "create_order":   "order",
    "add_price_list": "product",
    "add_services":   "service",
    "add_rules":      "rule",
    "add_goal":       "goal",
    "add_employee":   "employee",
    "add_supplier":   "supplier",
    "add_company":    "company",
    "save_document":  "rule",
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
        (r"\bинн\b", 1), (r"кассир", 2), (r"фискальн", 3),
    ],
    # Счёт и накладная раньше считались чеком. Разница не косметическая: чек —
    # это уже потраченные деньги, счёт — ещё не потраченные, а накладная — про
    # товар. Владельцу важно различать, что из этого уже ушло со счёта.
    "INVOICE": [
        (r"счёт на оплату|счет на оплату", 5), (r"счёт №|счет №", 4),
        (r"выставлен счёт|выставлен счет", 4), (r"получатель платежа", 3),
        (r"плательщик", 2), (r"счёт-фактура|счет-фактура", 4),
    ],
    "WAYBILL": [
        (r"товарная накладная", 5), (r"\bторг-?\s?12\b", 5), (r"накладная", 4),
        (r"грузополучател", 4), (r"грузоотправител", 4), (r"отпустил", 2),
    ],
    "SALARY_PAYMENT": [
        (r"расч[ёе]тный листок", 5), (r"платёжная ведомость|платежная ведомость", 5),
        (r"ведомость на выплату", 5), (r"выплата (зарплаты|заработной платы)", 4),
        (r"начислено к выплате", 4), (r"аванс за", 3),
    ],
    "REFUND": [
        (r"возврат средств", 5), (r"чек возврата", 5), (r"возврат[а-я]*", 4),
        (r"вернули деньги", 4), (r"отмена заказа", 3), (r"рефанд", 3),
    ],
    "INCOME_DOCUMENT": [
        (r"приходный (кассовый )?ордер", 5), (r"оплата от клиента", 4),
        (r"оплачено клиентом", 4), (r"поступление средств", 4),
        (r"акт выполненных работ", 3), (r"выручка за", 3),
    ],
    "BANK_TRANSACTION": [
        (r"выписка по счёт|выписка по счет", 3), (r"списание", 2), (r"зачисление", 2),
        (r"остаток по счёт|остаток по счет", 3), (r"\bсбп\b", 2), (r"перевод на карту", 2),
        (r"дата операции", 2), (r"назначение платежа", 3), (r"корр\.?\s*счёт|корр\.?\s*счет", 3),
        (r"перевод\w*", 2),
        (r"\bбик\b", 2), (r"баланс карты", 2),
    ],
    "PRICE_LIST": [
        (r"прайс[- ]?лист", 4), (r"\bпрайс\b", 3), (r"стоимость услуг", 2),
        (r"цены на", 2), (r"тариф", 1),
    ],
    "SERVICES_OR_PRODUCTS": [
        (r"\bменю\b", 3), (r"ассортимент", 3), (r"наши услуги", 3),
        (r"каталог товаров", 3), (r"перечень услуг", 3),
        # Так владелец говорит боту, а не пишет в документе. Раньше эти слова
        # не значили ничего, и «у нас новая услуга — маникюр 2500» без модели
        # уходило в UNKNOWN: приметы знали только язык бумаг.
        (r"нов(ая|ый|енькая)\s+(услуг|товар|позици)", 3),
        (r"добав(ь|ьте|ил[аи]?|или)\s+(услуг|товар|позици)", 3),
        (r"(появилась|ввели|запустили|теперь есть)\s+(услуг|нов)", 2),
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
    "EMPLOYEE_INFORMATION": [
        (r"трудов(ой|ого) договор", 4), (r"штатное расписание", 4),
        (r"приказ о при[ёе]ме", 4), (r"должностн\w+ (инструкц|обязанност)", 3),
        (r"\bрезюме\b", 3), (r"должность", 3), (r"испытательный срок", 3),
        (r"табельный номер", 3), (r"\bсотрудник\w*\b", 2), (r"\bработник\w*\b", 2),
        (r"\bоклад\b", 2), (r"\bф\.?и\.?о\.?\b", 2), (r"принят на работу", 4),
    ],
    "SUPPLIER_INFORMATION": [
        (r"поставщик\w*", 4), (r"условия поставки", 4), (r"коммерческое предложение", 3),
        (r"минимальный заказ", 3), (r"отсрочка платежа", 3), (r"оптов\w+", 2),
        (r"срок поставки", 3),
    ],
    "COMPANY_INFO": [
        (r"реквизиты компании|наши реквизиты", 4), (r"юридический адрес", 4),
        (r"\bогрн\w*\b", 3), (r"\bкпп\b", 3), (r"режим работы", 3),
        (r"график работы", 3), (r"о компании", 3), (r"\bреквизиты\b", 2),
        (r"фактический адрес", 3),
    ],
    "GOAL_STATEMENT": [
        (r"хочу выйти на", 5), (r"хочу зарабатывать", 5), (r"цель на \w+", 4),
        (r"\bцель\b|\bцели\b", 3), (r"план на (месяц|квартал|год)", 3),
        (r"выйти на", 2), (r"планиру\w+", 2), (r"к концу (месяца|года|квартала)", 2),
        (r"довести до", 2), (r"хочу довести", 4),
    ],
    "FINANCIAL_TRANSACTION": [
        (r"заплатил|оплатил|заплатили|оплатили", 3), (r"перевёл|перевел|перевели", 3),
        (r"потратил|потратили", 3), (r"получил|получили", 2), (r"выручка", 2),
        (r"зарплат", 2), (r"аренд", 1), (r"закупил|закупили", 2), (r"вернул", 1),
    ],
}

# Расход это или доход — по глаголу. Нужно и правилам, и проверке ответа модели.
INCOME_WORDS = re.compile(r"получил|получили|выручк|поступил|заплатили\s+нам|оплатили\s+нам|продал|продали", re.I)
# «Хочу выйти на 500 тысяч» — это намерение, а не операция. Без этой проверки
# сумма в тексте о будущем считалась движением денег и спорила с целью на
# равных, а спор двух типов честно опускает уверенность до низкой.
INTENT_WORDS = re.compile(r"хочу|хотим|планиру|цель|цели|нужно выйти|давай(те)? выйдем|"
                          r"к концу (месяца|года|квартала)|в планах", re.I)
# Деньги двигают не только глаголом. «Перевод Иванову 70 000» — это операция,
# хотя ни «заплатил», ни «получил» здесь нет.
MONEY_NOUNS = re.compile(r"перевод|оплат|плат[её]ж|выплат|возврат|поступлен|списан", re.I)
# Слова, которыми называют то, что бизнес ПРОДАЁТ. Нужны, чтобы отличить
# «маникюр 2500» (позиция прайса) от «отдал 2500» (движение денег): цифра в
# обоих случаях одна и та же, а смысл противоположный.
OFFER_WORDS = re.compile(r"услуг|товар|позици|прайс|цен[аыуе]|стоимость|тариф", re.I)
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


# ── прайс построчно ────────────────────────────────────────────────────────
# Прайс, сохранённый одной записью «Прайс-лист 2026», памяти бизнеса ничего не
# даёт: продавец не сможет назвать цену услуги, а владелец — увидеть, что
# именно изменилось. Поэтому список разбирается на строки: название и цена.
#
# Правилами, а не моделью. Прайс — это ровно тот случай, где строчка «Маникюр
# 1500» не требует понимания, зато выдуманная цена стоит дорого. Модель может
# добавить к найденному свои строки (поле items), но проверяются они так же.

# Разделители между названием и ценой: тире, двоеточие, точки-заполнители,
# вертикальная черта, табуляция или просто пробелы в конце строки.
_ITEM_RE = re.compile(
    r"^\s*(?:[-•*–—]\s*)?"                        # маркер списка, если есть
    r"(?P<name>.*?[^\s.\-–—:|\t])"                # название
    r"\s*(?:[-–—:|]|\.{2,}|\t|\s)\s*"             # разделитель
    r"(?P<price>\d[\d  ]*(?:[.,]\d{1,2})?)"  # цена
    r"\s*(?P<cur>₽|руб\w*\.?|р\.|rub|тыс\w*\.?)?\s*$",  # валюта или множитель
    re.I)

# Хвост названия, после которого цена — не цена позиции. «Работаем с 1000 до
# 2000» разбирается идеально и означает совсем не то: последнее слово
# названия оказывается предлогом, и это надёжный признак.
_ITEM_TAIL = re.compile(r"(?:^|\s)(до|от|с|со|по|на|за|в|и|или|через|около)$", re.I)

# Вступление перед названием: «У нас новая услуга — экспресс-маникюр 2500».
# Владелец пишет так почти всегда, и без обрезки в память попадает услуга с
# названием в виде целого предложения.
_ITEM_INTRO = re.compile(
    r"^.{0,60}?(?:услуг\w*|товар\w*|позици\w*|прайс\w*|новинк\w*|"
    r"добавил\w*|появил\w*|теперь|ввели|запустили)\b[^\w]{0,4}"
    r"\s*[-–—:]\s*", re.I)

# Строки, которые ценой не являются, хотя выглядят похоже.
_ITEM_SKIP = re.compile(
    r"^\s*(итого|всего|сумма|ндс|к оплате|скидка|прайс|цены?|наименование|"
    r"стоимость|услуг[аи]|товар[ы]?|№|тел|телефон|инн|кпп|огрн)\b", re.I)

ITEMS_LIMIT = 100          # длиннее — это уже не прайс, а выгрузка склада
NAME_MIN, NAME_MAX = 2, 120


def extract_items(text: str):
    """
    Позиции прайса: [{title, price, currency, raw}]. Пусто — не прайс.

    Берём только строки, где цена стоит В КОНЦЕ: «Маникюр 1500» — позиция, а
    «Работаем с 1500 до 2000» — режим работы, и цены там нет. Строки-итоги и
    шапку таблицы пропускаем поимённо: «Итого 12000» позицией прайса не
    является ни в одном прайсе.
    """
    out, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 300 or _ITEM_SKIP.match(line):
            continue
        m = _ITEM_RE.match(line)
        if not m:
            continue
        name = re.sub(r"[\s.\-–—:|]+$", "", m.group("name")).strip(" \t.-–—:|")
        name = _ITEM_INTRO.sub("", name).strip(" \t.-–—:|")
        if not (NAME_MIN <= len(name) <= NAME_MAX):
            continue
        if _ITEM_TAIL.search(name):
            continue                     # «работаем с 1000 до 2000» — не позиция
        if not re.search(r"[а-яёa-z]", name, re.I):
            continue                     # «12 3500» — это таблица чисел, не прайс
        raw = m.group("price").replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            price = float(raw)
        except ValueError:
            continue
        cur = (m.group("cur") or "").lower()
        if cur.startswith("тыс"):
            price *= 1000
        if price <= 0 or price > 10 ** 9:
            continue
        key = name.lower().replace("ё", "е")
        if key in seen:
            continue                     # та же позиция дважды в одном файле
        seen.add(key)
        out.append({"title": name, "price": int(round(price)),
                    "currency": CURRENCY.get(cur.rstrip("."), "RUB"), "raw": line})
        if len(out) >= ITEMS_LIMIT:
            break
    # Одна строка — это не прайс, а фраза с ценой: «экспресс-маникюр 2500».
    # Такой случай тоже нужен, поэтому одиночку возвращаем, но решение, что с
    # ней делать, принимается выше по типу материала.
    return out


# Дата в документе: «24.08.2026», «24.08.26», «2026-08-24». Без неё операция
# ложится на день загрузки, а чек мог пролежать в кармане неделю.
_DATE_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\b|\b(\d{4})-(\d{2})-(\d{2})\b")


def find_date(text: str):
    """Первая правдоподобная дата из текста в виде ГГГГ-ММ-ДД. Нет — None."""
    import datetime as _dt
    for m in _DATE_RE.finditer(text or ""):
        try:
            if m.group(4):
                y, mo, d = int(m.group(4)), int(m.group(5)), int(m.group(6))
            else:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if y < 100:
                    y += 2000
            if not (2000 <= y <= 2100):
                continue
            return _dt.date(y, mo, d).isoformat()
        except ValueError:
            continue          # 32.13.2026 — не дата, идём дальше
    return None


def guess_category(text: str):
    low = (text or "").lower()
    for name, pattern in CATEGORY_WORDS:
        if re.search(pattern, low):
            return name
    return None


def _norm_name(s):
    return re.sub(r"[^а-яёa-z]", "", (s or "").lower())


# Окончания, которые русский язык меняет: «Петрова» в тексте станет
# «Петровой», «Петрову», «Петровы». Основу берём без них, иначе система
# узнаёт только те имена, которые стоят в именительном падеже.
_NAME_TAIL = "аяеийоуыьюё"


def _stem_name(word: str) -> str:
    st = _norm_name(word)
    while len(st) > 4 and st[-1] in _NAME_TAIL:
        st = st[:-1]
    return st


def _name_match(known: str, text_low: str):
    """
    Узнать в тексте знакомое имя. Русские падежи («Иванову», «Петровой»)
    ловим по основе: сравниваем начало слова, а не слово целиком.
    """
    for word in re.split(r"[\s,;]+", known or ""):
        stem = _stem_name(word)
        if len(stem) < 4:
            continue          # инициалы и предлоги совпадут с чем угодно
        if re.search(r"\b" + re.escape(stem) + r"[а-яё]{0,3}\b", text_low):
            return True
    return False


def match_people(business_id, text, extracted):
    """
    Кто участвовал в операции — по памяти бизнеса.

    Это и есть разница между «перевод 70 000» и «зарплата Иванову». Знание
    берём из уже подтверждённых записей: сотрудников, поставщиков, клиентов.
    Ничего не выдумываем — если совпадения нет, поле остаётся пустым.
    """
    hay = ((extracted.get("counterparty") or "") + " " + (text or "")).lower()
    found = {}
    try:
        people = [("employee_id", r) for r in database.list_facts(business_id, "employee")]
        people += [("supplier_id", r) for r in database.list_facts(business_id, "supplier")]
        clients = [("client_id", r) for r in database.list_clients(business_id, limit=200)]
    except Exception:
        return found
    for field, row in people:
        if field in found:
            continue
        if _name_match(row.get("title") or "", hay):
            found[field] = row["id"]
            found["counterparty"] = row.get("title")
    for field, row in clients:
        if field in found or "employee_id" in found:
            continue
        if _name_match(row.get("name") or "", hay):
            found[field] = row["id"]
            found.setdefault("counterparty", row.get("name"))
    return found


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

    # Заметка своими словами: движение денег видно по глаголу и сумме. Но если
    # человек говорит о будущем — это цель, и в финансы такую сумму пускать
    # нельзя ни при какой уверенности.
    if kind == "text":
        money = find_money(text)
        intent = bool(INTENT_WORDS.search(text or ""))
        if money and intent:
            scores["GOAL_STATEMENT"] = scores.get("GOAL_STATEMENT", 0) + 4
            hits.setdefault("GOAL_STATEMENT", []).append("сумма и намерение, а не факт")
        elif money and (EXPENSE_WORDS.search(text or "") or INCOME_WORDS.search(text or "")
                        or MONEY_NOUNS.search(text or "")):
            scores["FINANCIAL_TRANSACTION"] = scores.get("FINANCIAL_TRANSACTION", 0) + 4
            hits.setdefault("FINANCIAL_TRANSACTION", []).append("сумма и слово о деньгах")
        # Названная позиция с ценой рядом со словом об услуге или товаре — это
        # прайс, пусть и в одну строку. Признак строгий: цену ищет тот же
        # разборщик, что и в файле прайса, и просто «отдал 2500» его не пройдёт.
        if OFFER_WORDS.search(text or "") and extract_items(text):
            scores["SERVICES_OR_PRODUCTS"] = scores.get("SERVICES_OR_PRODUCTS", 0) + 4
            hits.setdefault("SERVICES_OR_PRODUCTS", []).append(
                "названа позиция с ценой")

    if not scores:
        return "UNKNOWN", 0.0, []

    # Денежный тип без суммы — почти всегда ложное срабатывание слова: «наши
    # правила: возврат в течение 24 часов» это правила, а не возврат денег.
    # Если в тексте нет ни одной суммы, а рядом стоит неденежный кандидат,
    # выбираем его: документ о деньгах без денег не бывает.
    if not find_money(text) and len(scores) > 1:
        rest = {t: v for t, v in scores.items() if t not in MONEY_TYPES}
        if rest and max(scores, key=scores.get) in MONEY_TYPES:
            top_money = max(v for t, v in scores.items() if t in MONEY_TYPES)
            if max(rest.values()) >= top_money - 2:
                scores = rest
                hits.setdefault(max(rest, key=rest.get), []).append(
                    "в тексте нет суммы — это не документ о деньгах")

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
    if kind_type in MONEY_TYPES and money:
        # В чеке итог обычно самая большая сумма; в заметке — единственная.
        top = max(money, key=lambda m: m["amount"])
        data["amount"] = top["amount"]
        data["currency"] = top["currency"]
        if len(money) > 1:
            data["amounts_found"] = len(money)
    # Категория расхода имеет смысл только для денег. В трудовом договоре слово
    # «оклад» тоже встречается, но приписывать сотруднику «категория: зарплата»
    # — значит засорять карточку смыслом, которого в ней нет.
    if kind_type in MONEY_TYPES:
        cat = guess_category(text)
        if cat:
            data["category"] = cat
        doc = DOC_BY_TYPE.get(kind_type)
        if doc:
            data["doc_type"] = doc
        when = find_date(text)
        if when:
            data["op_date"] = when
    if kind_type in ("FINANCIAL_TRANSACTION", "EXPENSE_DOCUMENT"):
        if EXPENSE_WORDS.search(text or ""):
            data["direction"] = "expense"
        elif INCOME_WORDS.search(text or ""):
            data["direction"] = "income"
        elif kind_type == "EXPENSE_DOCUMENT":
            data["direction"] = "expense"
    elif kind_type in ("INVOICE", "WAYBILL", "SALARY_PAYMENT"):
        data["direction"] = "expense"
    elif kind_type == "INCOME_DOCUMENT":
        data["direction"] = "income"
    elif kind_type == "REFUND":
        data["direction"] = "income" if _REFUND_IN.search(text or "") else "expense"
    if kind_type == "SALARY_PAYMENT":
        data.setdefault("category", "зарплата")
    if kind_type == "REFUND":
        data["category"] = "возврат"
    # Кому платили — слово после глагола, если оно с большой буквы.
    m = re.search(r"(?:заплатил|оплатил|перевёл|перевел|отдал)\w*\s+([А-ЯЁ][\w-]+)", text or "")
    if m:
        data["counterparty"] = m.group(1)

    # Прайс и перечень услуг разбираются построчно: одна запись «Прайс-лист»
    # памяти бизнеса не даёт ничего, продавцу — тем более.
    if kind_type in ITEM_TYPES:
        items = extract_items(text)
        if items:
            data["items"] = items
            data["items_count"] = len(items)
            data.pop("amount", None)     # цена позиции — не сумма материала
            data.pop("category", None)

    if kind_type == "GOAL_STATEMENT" and money:
        # Цель — это самая большая названная цифра: «выйти на 500 тысяч».
        data["target"] = max(m["amount"] for m in money)
        data["metric"] = _goal_metric(text)
        data.pop("direction", None)          # цель не движение денег
    if kind_type == "EMPLOYEE_INFORMATION":
        data.update(_employee_fields(text))
    if kind_type == "SUPPLIER_INFORMATION":
        who = _org_name(text) or _after(text, r"поставщик[а-я]*")
        if who:
            data["supplier"] = who
        phone = _phone(text)
        if phone:
            data["phone"] = phone
    if kind_type == "COMPANY_INFO":
        for key, pattern in (("inn", r"\bинн\b"), ("ogrn", r"\bогрн\w*\b"),
                             ("kpp", r"\bкпп\b")):
            m2 = re.search(pattern + r"[:\s]*([0-9]{8,15})", text or "", re.I)
            if m2:
                data[key] = m2.group(1)
        addr = re.search(r"(?:юридический|фактический)\s+адрес[:\s]*([^\n]{5,120})",
                         text or "", re.I)
        if addr:
            data["address"] = addr.group(1).strip(" .;,")
    return data


# Показатель цели: по словам, которыми человек её описал. Не угадали — доход,
# самый частый случай, и его видно в форме подтверждения.
GOAL_METRIC_WORDS = [
    ("profit", r"прибыл"), ("clients", r"клиент"), ("orders", r"заявок|заявк|заказов"),
    ("subscribers", r"подписчик"), ("income", r"выручк|доход|оборот|зарабат"),
]


def _goal_metric(text):
    low = (text or "").lower()
    for metric, pattern in GOAL_METRIC_WORDS:
        if re.search(pattern, low):
            return metric
    return "income"


_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
_FIO_RE = re.compile(r"\b([А-ЯЁ][а-яё]+)\s+([А-ЯЁ][а-яё]+)(?:\s+([А-ЯЁ][а-яё]+))?\b")
_ORG_RE = re.compile(r"(ООО|ИП|АО|ЗАО|ПАО)\s*[«\"]?([^»\"\n,;]{2,60})[»\"]?", re.I)


def _phone(text):
    m = _PHONE_RE.search(text or "")
    return m.group(0).strip() if m else None


def _org_name(text):
    m = _ORG_RE.search(text or "")
    return (m.group(1).upper() + " " + m.group(2).strip()) if m else None


def _after(text, pattern):
    """Слово или название сразу после ключевого слова: «Поставщик: Ромашка»."""
    m = re.search(pattern + r"[:\s]+([^\n,;.]{2,60})", text or "", re.I)
    return m.group(1).strip() if m else None


def _employee_fields(text):
    """Имя, должность и контакт сотрудника — только то, что прямо написано."""
    out = {}
    named = re.search(r"(?:ф\.?и\.?о\.?|сотрудник|работник|принят[аы]? на работу)"
                      r"[:\s]+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})", text or "", re.I)
    if named:
        out["person"] = named.group(1).strip()
    else:
        m = _FIO_RE.search(text or "")
        if m:
            out["person"] = " ".join(x for x in m.groups() if x)
    role = _after(text, r"должность")
    if role:
        out["role"] = role
    phone = _phone(text)
    if phone:
        out["phone"] = phone
    salary = re.search(r"(?:оклад|заработная плата|зарплата)[:\s]*([\d   ]{3,12})",
                       text or "", re.I)
    if salary:
        digits = re.sub(r"\D", "", salary.group(1))
        if digits:
            out["salary"] = int(digits)
    return out


# Каким документом подтверждена операция. Тип материала → тип документа в
# финансах: по нему потом видно, чем именно подтверждена цифра.
DOC_BY_TYPE = {
    "EXPENSE_DOCUMENT":      "receipt",
    "BANK_TRANSACTION":      "bank",
    "INVOICE":               "invoice",
    "WAYBILL":               "waybill",
    "SALARY_PAYMENT":        "salary",
    "REFUND":                "refund",
    "INCOME_DOCUMENT":       "income",
    "FINANCIAL_TRANSACTION": None,
}

# Типы, у которых направление денег понятно из самого документа.
MONEY_TYPES = ("EXPENSE_DOCUMENT", "BANK_TRANSACTION", "FINANCIAL_TRANSACTION",
               "INVOICE", "WAYBILL", "SALARY_PAYMENT", "REFUND", "INCOME_DOCUMENT")

# Типы, которые содержат СПИСОК позиций, а не одну запись.
ITEM_TYPES = ("PRICE_LIST", "SERVICES_OR_PRODUCTS")

# Какой вид записи получается из позиции такого списка.
ENTITY_BY_ITEM_ACTION = {"add_price_list": "product", "add_services": "service"}

# Возврат бывает в обе стороны: клиенту вернули мы (деньги ушли) или вернул нам
# поставщик (деньги пришли). Решает формулировка, а не догадка.
_REFUND_IN = re.compile(r"вернул\w*\s+нам|возврат от|поставщик вернул|нам вернули", re.I)


ACTION_BY_TYPE = {
    "EXPENSE_DOCUMENT":      ["create_expense", "save_document"],
    "INVOICE":               ["create_expense", "save_document"],
    "WAYBILL":               ["create_expense", "save_document"],
    "SALARY_PAYMENT":        ["create_expense"],
    "REFUND":                ["create_expense"],
    "INCOME_DOCUMENT":       ["create_expense", "save_document"],
    "BANK_TRANSACTION":      ["create_expense", "save_document"],
    "FINANCIAL_TRANSACTION": ["create_expense"],
    "PRICE_LIST":            ["add_price_list", "save_document"],
    "SERVICES_OR_PRODUCTS":  ["add_services", "save_document"],
    "BUSINESS_RULES":        ["add_rules", "save_document"],
    "CONTRACT":              ["save_document"],
    "EMPLOYEE_INFORMATION":  ["add_employee", "save_document"],
    "SUPPLIER_INFORMATION":  ["add_supplier", "save_document"],
    "COMPANY_INFO":          ["add_company", "save_document"],
    "GOAL_STATEMENT":        ["add_goal"],
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
        if not meta:
            continue
        act = {"action": n, "title": meta["title"],
               "safe": meta["safe"], "auto": meta["auto"]}
        # Предложение по деньгам должно быть конкретным. «Записать расход» —
        # это вопрос «какой?»; «Расход 70 000 ₽ · зарплата» — уже ответ, и
        # человеку остаётся только согласиться или поправить.
        if n in ("create_expense", "create_income") and extracted.get("amount"):
            word = "Расход" if n == "create_expense" else "Доход"
            money = "{:,}".format(int(extracted["amount"])).replace(",", " ") + " ₽"
            tail = " · " + extracted["category"] if extracted.get("category") else ""
            act["title"] = f"{word} {money}{tail}"
            act["confirm_title"] = f"Подтвердить: {word.lower()} {money}{tail}"
        out.append(act)
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

def process(business_id, item_id, auto=None):
    """
    Понять один материал и сохранить результат.

    Оригинал не трогаем ни при каком исходе — понимание это отдельный слой
    поверх приёмника, а не замена ему.

    auto=None — как настроено глобально (INBOX_AUTOAPPLY). auto=False —
    ничего не применять самому, только разобрать и предложить. Второе нужно
    каналам, у которых автономия VELOR ограничена владельцем: разбор от этого
    не меняется, меняется только право записать результат без нажатия.
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

    # Кто участвовал — из памяти бизнеса. Делаем это ПОСЛЕ модели: узнанный
    # сотрудник надёжнее любой догадки, и он же уточняет категорию.
    people = match_people(business_id, text, extracted)
    if people:
        extracted.update(people)
        if people.get("employee_id") and not extracted.get("category"):
            extracted["category"] = "зарплата"
        if people.get("employee_id") and kind_type in ("BANK_TRANSACTION",
                                                       "FINANCIAL_TRANSACTION"):
            extracted.setdefault("doc_type", "salary")
        if people.get("supplier_id") and not extracted.get("category"):
            extracted["category"] = "закупка"

    # К чему относится движение денег: заявка по номеру в тексте или
    # единственная открытая заявка узнанного клиента. Догадка приходит с
    # объяснением, поэтому в карточке видно, ПОЧЕМУ VELOR так решил.
    relations = []
    if kind_type in MONEY_TYPES:
        try:
            import graph
            related, relations = graph.relate_money(business_id, text, extracted)
            for k, v in (related or {}).items():
                extracted.setdefault(k, v)
        except Exception:
            relations = []

    conf = merged["confidence"]
    if people and kind_type in MONEY_TYPES:
        # Узнали человека по своей же памяти — это не догадка, а совпадение с
        # подтверждённой записью. Небольшая, но честная прибавка к уверенности.
        conf = min(0.95, conf + 0.10)
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
        # Почему VELOR решил, что операция относится к этой заявке. Связь без
        # объяснения владелец проверить не может, а значит, и доверять ей не
        # должен.
        "relations": relations,
    }

    # Автоматически — только безопасное и только при высокой уверенности.
    # Деньги не двигаются сами никогда: create_expense/create_income safe=False.
    applied, pending_links = [], []
    may_auto = AUTO_APPLY if auto is None else bool(auto)
    if may_auto and result["level"] == "HIGH":
        for a in result["suggested_actions"]:
            if a["safe"] and a.get("auto"):
                try:
                    text, entity, entity_id, clean, made = apply_action(
                        business_id, item_id, a["action"], result, auto=True)
                except ActionError as e:
                    # Не применили — и человек должен прочитать ПОЧЕМУ. «Ничего
                    # не изменилось» без объяснения выглядит как поломка, хотя
                    # чаще всего это сработавшая защита.
                    result["notes"] = list(result.get("notes") or []) + [str(e)]
                    continue
                said_type, said_id = named_result(entity, entity_id, made)
                applied.append({"action": a["action"], "auto": True, "detail": text,
                                "entity_type": said_type, "entity_id": said_id,
                                "items": len(made) or None})
                # Автоприменение — тоже решение, и в истории оно должно быть
                # видно наравне с нажатиями человека.
                decision_id = database.add_inbox_decision(
                    business_id, item_id, "auto", action=a["action"],
                    entity_type=said_type, entity_id=said_id,
                    original=clean, corrected=clean, actor="velor", note=text)
                # И в памяти бизнеса тоже: знание, добытое самим VELOR, должно
                # быть так же прослеживаемо, как подтверждённое человеком.
                # Записываем ПОСЛЕ сохранения разбора — иначе в связи не на что
                # сослаться, и «что именно он понял» пришлось бы искать вручную.
                # Прайс создаёт не одну запись, а список — связь пишем на
                # каждую позицию. Иначе у девяти услуг из десяти происхождение
                # оказалось бы неизвестным.
                for made_one in (made or []):
                    pending_links.append({
                        "entity": made_one["entity_type"],
                        "entity_id": made_one["entity_id"],
                        "decision_id": decision_id,
                        "note": ("Обновлено из прайса: " if made_one["what"] == "updated"
                                 else "Из прайса: ") + made_one["title"][:120]})
                if entity and entity_id and not made:
                    pending_links.append({"entity": entity, "entity_id": entity_id,
                                          "decision_id": decision_id, "note": text})
    result["applied"] = applied

    result_id = database.save_inbox_result(business_id, item_id, result)
    result["id"] = result_id
    for link in pending_links:
        database.add_memory_link(
            business_id, link["entity"], link["entity_id"], event="created",
            source_kind="inbox", item_id=item_id, result_id=result_id,
            decision_id=link["decision_id"], confidence=result["confidence"],
            actor="velor", note=link["note"])

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
    if extracted.get("target"):
        bits.append("{:,}".format(extracted["target"]).replace(",", " "))
    if extracted.get("amount"):
        bits.append("{:,}".format(extracted["amount"]).replace(",", " ") + " "
                    + {"RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}.get(
                        extracted.get("currency", "RUB"), ""))
    if extracted.get("category"):
        bits.append("категория: " + extracted["category"])
    if extracted.get("counterparty"):
        bits.append(("сотрудник: " if extracted.get("employee_id") else "кому: ")
                    + str(extracted["counterparty"]))
    if extracted.get("op_date"):
        bits.append("дата: " + extracted["op_date"])
    return name + ((" — " + ", ".join(bits)) if bits else ".")


def prefill(action, result, item):
    """
    Чем заполнить форму подтверждения: что ИИ вытащил, разложенное по полям
    конкретного вида записи. Это же значение считается «оригиналом ИИ» в
    аудите — с ним потом сравнивают правки человека.
    """
    entity = ENTITY_BY_ACTION.get(action)
    if not entity:
        return None
    data = dict((result or {}).get("extracted_data") or {})
    title = ((item or {}).get("title") or (item or {}).get("filename") or "материал")[:120]
    summary = ((result or {}).get("summary") or "")[:300]

    out = {}
    if entity in ("expense", "income"):
        if data.get("amount"):
            out["amount"] = data["amount"]
        if data.get("category"):
            out["category"] = data["category"]
        for key in ("op_date", "counterparty", "doc_type",
                    "employee_id", "supplier_id", "client_id"):
            if data.get(key):
                out[key] = data[key]
        out["source"] = "inbox"
        # Описанием берём то, что прислал человек (или имя файла), а не
        # пересказ разбора: в ленте финансов рядом и так стоят сумма,
        # категория и имя, и повторять их третий раз незачем.
        out["note"] = title or summary
    elif entity in ("product", "service", "rule"):
        out["title"] = title
        out["body"] = summary
    elif entity == "client":
        out["name"] = data.get("counterparty") or title
        if data.get("phone"):
            out["phone"] = data["phone"]
        out["notes"] = summary
    elif entity == "order":
        out["text"] = summary or title
        if data.get("amount"):
            out["amount"] = data["amount"]
        if data.get("phone"):
            out["phone"] = data["phone"]
        if data.get("date"):
            out["date_wanted"] = data["date"]
    elif entity == "goal":
        # Название цели — слова самого владельца, а не пересказ. «Цель бизнеса»
        # в списке целей не говорит ни о чём.
        out["title"] = title
        target = data.get("target") or data.get("amount")
        if target:
            out["target"] = target
        out["metric"] = data.get("metric") or "income"
        if data.get("deadline"):
            out["deadline"] = data["deadline"]
    elif entity == "employee":
        out["title"] = data.get("person") or title
        if data.get("role"):
            out["role"] = data["role"]
        if data.get("phone"):
            out["contact"] = data["phone"]
        # В заметку кладём факты из документа, а не пересказ его типа: строка
        # «Сведения о сотруднике» не говорит о человеке ничего.
        bits = []
        if data.get("salary"):
            bits.append("Оклад: {:,}".format(int(data["salary"])).replace(",", " "))
        out["body"] = "; ".join(bits)
    elif entity == "supplier":
        out["title"] = data.get("supplier") or data.get("counterparty") or title
        if data.get("phone"):
            out["contact"] = data["phone"]
        out["body"] = summary
    elif entity == "company":
        out["title"] = title
        bits = [f"{k.upper()}: {data[k]}" for k in ("inn", "kpp", "ogrn") if data.get(k)]
        if data.get("address"):
            bits.append("Адрес: " + data["address"])
        out["body"] = "; ".join(bits) or summary
    return {k: v for k, v in out.items() if v not in (None, "")}


def diff(before, after):
    """Что именно человек изменил: поле → {было, стало}. Пусто — не менял."""
    changed = {}
    for key in set(list((before or {}).keys()) + list((after or {}).keys())):
        was, now = (before or {}).get(key), (after or {}).get(key)
        if str(was if was is not None else "") != str(now if now is not None else ""):
            changed[key] = {"was": was, "now": now}
    return changed


# ============================================================
#  ВЫПОЛНЕНИЕ ПРЕДЛОЖЕННОГО ДЕЙСТВИЯ
# ============================================================

class ActionError(Exception):
    """Действие выполнить нельзя — с объяснением для человека."""


def _price_body(item):
    """Строка цены для тела записи. Валюту пишем только чужую — рубль подразумевается."""
    money = "{:,}".format(int(item.get("price") or 0)).replace(",", " ")
    cur = (item.get("currency") or "RUB").upper()
    return money + (" ₽" if cur == "RUB" else " " + cur)


def _apply_items(business_id, entity, items, verified):
    """
    Применить список позиций прайса. Возвращает (текст, что_сделано).

    Позиция, которую владелец уже подтверждал, автоматикой не переписывается:
    цена, за которую человек поручился, важнее свежего разбора. Такие позиции
    попадают в «оставлено» и видны в карточке — решать по ним человеку.
    """
    import entities

    made, created, updated, blocked, failed = [], 0, 0, 0, 0
    for it in items[:ITEMS_LIMIT]:
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        values = {"title": title[:NAME_MAX], "body": _price_body(it)}
        try:
            eid, _text, clean, what = entities.create_or_update(
                business_id, entity, values, verified=verified)
        except entities.EntityError:
            failed += 1
            continue
        if what == "created":
            created += 1
        elif what == "updated":
            updated += 1
        else:
            blocked += 1
        made.append({"entity_type": entity, "entity_id": eid, "what": what,
                     "title": title, "values": clean})

    word = "Товары" if entity == "product" else "Услуги"
    bits = []
    if created:
        bits.append("добавлено %d" % created)
    if updated:
        bits.append("обновлено %d" % updated)
    if blocked:
        bits.append("оставлено без изменений %d (их подтверждали руками)" % blocked)
    if failed:
        bits.append("не разобрано %d" % failed)
    if not bits:
        raise ActionError("В этом материале не нашлось ни одной позиции с ценой.")
    if not (created or updated):
        # Ничего не изменилось: все позиции уже есть и подтверждены человеком.
        # Записывать это как выполненное действие нельзя — в истории появилось
        # бы «применено», за которым не стоит ни одной правки.
        raise ActionError(
            "Все позиции уже есть в памяти, и их подтверждали руками — "
            "VELOR ничего не менял.")
    return word + ": " + ", ".join(bits), made


def named_result(entity, entity_id, made):
    """
    Что записать в журнал решений как «созданное».

    У журнала одна пара «вид + id» на решение, а прайс создаёт список. Когда
    позиция одна — называем её: журнал должен вести к записи. Когда их
    двенадцать, честный ответ — «двенадцать», и он уже есть в описании
    решения, а сами записи связаны с материалом через происхождение.
    """
    if made:
        return entity, (made[0]["entity_id"] if len(made) == 1 else None)
    return entity, entity_id


def apply_action(business_id, item_id, action, result=None, auto=False, data=None):
    """
    Выполнить одно предложенное действие.
    Возвращает (текст, вид, id, значения, список_позиций).

    Последний элемент пуст почти всегда: он не пуст только у прайса и перечня
    услуг, где одно действие создаёт не одну запись, а столько, сколько строк
    в списке. Отдельной ветки для этого нет: вызывающий пишет в аудит по
    каждой позиции ровно так же, как по одной записи.

    auto=True — вызвано самим VELOR. В этом режиме небезопасные действия
    запрещены жёстко, а не по совести вызывающего, и всё созданное помечается
    неподтверждённым: человек этого не читал.

    data — значения из формы подтверждения. Не передали — берём то, что
    предложил ИИ. Создание в обоих случаях идёт через одну фабрику, поэтому
    «подтвердил как есть» и «исправил и подтвердил» отличаются только тем,
    что попадёт в аудит.
    """
    import entities

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

    if action == "ask_user":
        database.set_inbox_status(item_id, business_id, "NEEDS_REVIEW")
        return "Отмечено: нужен ваш взгляд", None, None, {}, []

    # Подтверждённым считается только то, что прошло через руки человека.
    verified = not auto

    entity = ENTITY_BY_ITEM_ACTION.get(action)
    if entity:
        # Человек прислал заполненную форму — значит, он смотрел на конкретную
        # запись и правил именно её. Подменять его ввод разбором всего файла
        # нельзя: это отняло бы у него последнее слово. Список берётся либо из
        # самой формы, либо из разбора, когда форму не присылали вовсе.
        items = (data or {}).get("items") if isinstance(data, dict) else None
        if not items and data is None:
            items = (res.get("extracted_data") or {}).get("items")
        if isinstance(items, list) and items:
            text, made = _apply_items(business_id, entity, items, verified)
            return text, entity, None, {"items": items}, made
        # Позиций не нашлось — материал сохраняется целиком, как раньше.

    entity = ENTITY_BY_ACTION.get(action)
    if not entity:
        raise ActionError("Действие пока не поддержано.")

    values = data if data is not None else prefill(action, res, item)
    try:
        entity_id, text, clean, what = entities.create_or_update(
            business_id, entity, values or {}, verified=verified)
    except entities.EntityError as e:
        raise ActionError(str(e))
    if what == "blocked":
        raise ActionError(
            "Такая запись уже есть, и её подтверждал человек — "
            "VELOR не переписывает её сам. Откройте запись и измените вручную.")
    if what == "updated":
        text = "Обновлено — " + text[0].lower() + text[1:] if text else "Обновлено"
    return text, entity, entity_id, clean, []
