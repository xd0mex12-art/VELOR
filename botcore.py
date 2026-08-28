# -*- coding: utf-8 -*-
"""
Ядро обработки входящего сообщения клиента — БЕЗ привязки к Telegram-фреймворку.
Одну и ту же логику используют и bot.py (long polling, отдельный процесс), и
webhook-эндпоинт в server.py (бот живёт внутри веб-сервиса, без отдельного воркера —
важно для бесплатного хостинга). Так поведение бота одинаково в обоих режимах.
"""
import logging

import ai
import database
import trial

log = logging.getLogger("velor.botcore")


# ── кто написал ────────────────────────────────────────────────────────────
# У бота бизнеса два совершенно разных собеседника, и путать их нельзя ни в
# одну сторону. Клиент пишет, чтобы купить, — его ведёт продавец. Владелец
# пишет, чтобы что-то рассказать о своём деле, — это материал, и он должен
# попасть в ту же дверь, что и загрузка из кабинета.
#
# Личность владельца определяет СЕРВЕР по своей же записи (owner_identity),
# а не сообщение. Никакой пометки «я владелец» в тексте не существует и не
# должно: иначе любой клиент назначил бы себя владельцем одной строкой.

def owner_telegram_id(bid):
    """Личный Telegram-id владельца, если он подтверждён. Иначе None."""
    try:
        business = database.get_business(bid) or {}
        if not business.get("owner_verified"):
            return None
        row = database.owner_identity_get(bid) or {}
        return (row.get("telegram_user_id") or "").strip() or None
    except Exception:
        log.exception("Не удалось прочитать личность владельца (biz %s)", bid)
        return None


def is_owner(bid, tg_user_id):
    """
    Это владелец бизнеса? Сравниваем с подтверждённой привязкой.

    Строго по id: username меняется, имя совпадает у тысяч людей, а личный id
    в Telegram неизменен. Не подтверждён владелец — значит, владельца нет, и
    все пишущие боту считаются клиентами. Это безопасный отказ: худшее, что
    случится, — материал владельца уйдёт в разговор с продавцом, а не в
    память бизнеса.
    """
    known = owner_telegram_id(bid)
    return bool(known) and str(tg_user_id or "").strip() == known


def greeting_text(bid):
    """Приветствие бизнеса для команды /start."""
    business = database.get_business(bid) or {}
    greeting = (business.get("greeting") or "").strip() or "Здравствуйте!"
    if ai.ai_available():
        return greeting + "\nПросто напишите, что вам нужно — я всё оформлю."
    return greeting


def from_owner(bid, *, text=None, files=None, actor_id=None, channel="telegram"):
    """
    Материал от владельца — в ту же дверь, что и загрузка из кабинета.

    Здесь нет ни разбора, ни записи в память: всё это уже написано и живёт в
    intake/understanding/entities. Задача этой функции ровно одна — не завести
    второй приёмник, а позвать существующий и пересказать его ответ словами.

    Возвращает текст для отправителя. Ничего не бросает: канал не должен
    падать из-за того, что материал не удалось разобрать.
    """
    import intake

    business = database.get_business(bid) or {}
    # Триал закончился — данные целы, но менять их нельзя. То же правило, что
    # и в кабинете (require_active), и формулировка та же.
    if trial.access(business)["read_only"]:
        return ("Пробный период завершён — новые материалы я пока не принимаю. "
                "Всё, что было, на месте.")

    try:
        got = intake.receive(
            bid, text=text, files=files or [], source=channel,
            actor="owner", actor_id=actor_id,
            meta={"channel": channel},
            # Право записать понятое БЕЗ нажатия человека даёт уровень
            # автономии канала, а не факт, что сообщение пришло от владельца.
            auto=intake.may_autoapply(bid, channel))
    except intake.IntakeError as e:
        return str(e)
    except Exception:
        log.exception("Приём из канала %s не удался (biz %s)", channel, bid)
        return ("Не смог обработать. Данные не менял — попробуйте ещё раз "
                "или загрузите через кабинет.")

    try:
        return intake.report(bid, got)
    except Exception:
        log.exception("Не удалось описать результат приёма (biz %s)", bid)
        # Материал принят — об этом надо сказать, даже если красиво не вышло.
        return "Принял. Загляните во «Входящие» — там видно, что я понял."


def handle_message(bid, tg_user_id, full_name, text):
    """
    Обработать текстовое сообщение клиента из Telegram и вернуть ответ (или None).
    Полностью повторяет логику bot.ai_dialog: клиент, память диалога, лимит тарифа,
    ответ ИИ, оформление заказа, запись в ленту.
    """
    business = database.get_business(bid) or {"name": "бизнес"}

    # Триал завершён → бот на паузе: не отвечаем ИИ, не создаём клиентов/заявки.
    if trial.access(business)["read_only"]:
        return "Спасибо за сообщение! Мы свяжемся с вами в ближайшее время."

    client = database.get_or_create_client(bid, tg_user_id=tg_user_id, name=full_name)
    return reply_for(bid, client, text, channel="telegram")


def reply_for(bid, client, text, channel=None, save_incoming=True):
    """
    Ответ клиенту, кем бы он ни написал.

    Раньше эта логика жила внутри телеграмного обработчика и знала про tg_user_id.
    Каналов стало два, а разговор у бизнеса с клиентом один: и память диалога, и
    заявки, и лента событий должны получаться одинаковыми независимо от того,
    пришло сообщение в бот или в директ Instagram. Поэтому канал здесь — просто
    пометка на сообщении, а не отдельная ветка поведения.

    save_incoming=False, если входящее уже записано вызывающей стороной: в
    Instagram сообщение сохраняется раньше, чтобы оно осталось в истории даже
    когда отвечать нельзя (триал кончился, вложение без текста, разговор ведёт
    человек).
    """
    business = database.get_business(bid) or {"name": "бизнес"}
    full_name = client.get("name") or "клиент"
    mid = None
    if save_incoming:
        mid = database.save_message(bid, client["id"], "user", text, channel=channel)

    # Сообщение клиента остаётся сообщением — его никуда не переносят. Но если
    # в нём слышно коммерческое намерение, над перепиской появляется отдельная
    # запись: возможность. Она заводится ДО всех проверок ниже, потому что
    # именно при выключенном ИИ и кончившемся лимите владельцу важнее всего
    # видеть, кто спрашивал цену, пока продавец молчал.
    try:
        import leads
        leads.from_message(bid, client, text, source=channel or "telegram",
                           channel=channel, message_id=mid)
    except Exception:
        logging.exception("Возможность из разговора не завелась (biz %s)", bid)

    # Лимит тарифа исчерпан — вежливо принимаем без ИИ.
    if database.plan_status(business)["over"]:
        return ("Спасибо за сообщение! Мы обязательно ответим — "
                "сотрудник свяжется с вами в ближайшее время.")

    # Нет ключа ИИ — принимаем как обращение, ответит человек (без анкеты: в webhook
    # нет пошагового состояния; полный режим-анкета остаётся в bot.py при polling).
    if not ai.ai_available():
        who = client.get("name") or full_name or "клиент"
        database.log_event(bid, "reply", f"Новое обращение: {who}", (text or "")[:200],
                           once_key=f"Новое обращение: {who}")
        return "Спасибо! Мы получили ваше сообщение и скоро вам ответим."

    history = database.get_history(bid, client["id"])
    client_info = {
        "имя": client.get("name"),
        "телефон": client.get("phone"),
        "день рождения": client.get("birthday"),
        "предпочтения": client.get("favorite"),
        "заметки": client.get("notes"),
    }
    # НОВЫЙ СЛОЙ: Context Engine собирает контекст (знания, документы-RAG, досье
    # клиента) и извлекает заказ. Обратная совместимость: при ЛЮБОЙ ошибке —
    # прежний путь ai.chat_reply; при его сбое — вежливая заглушка, как раньше.
    try:
        import context_engine
        reply, order = context_engine.respond_chat(
            bid, history, client_info=client_info, client_id=client["id"])
    except Exception:
        logging.exception("Context Engine (webhook) упал — откат на ai.chat_reply (biz %s)", bid)
        try:
            docs = database.search_chunks(bid, text)
            reply, order = ai.chat_reply(business, history, client_info, docs)
        except Exception:
            logging.exception("Ошибка ИИ (webhook, biz %s)", bid)
            return "Ой, я на секунду задумалась. Напишите ещё раз, пожалуйста."

    if order:
        order_id = database.add_order(
            business_id=bid,
            text=order.get("text") or text,
            client_id=client["id"],
            phone=order.get("phone"),
            address=order.get("address"),
            date_wanted=order.get("date_wanted"),
            amount=order.get("amount"),
            source=channel,
        )
        if order.get("phone") and not client.get("phone"):
            database.update_client(client["id"], bid, phone=order["phone"])
        # Заявка оформлена — возможность стала сделкой. Заявка при этом
        # остаётся там же, где была: лид только ссылается на неё.
        try:
            import leads
            leads.on_order(bid, client["id"], order_id,
                           amount=order.get("amount"), channel=channel)
        except Exception:
            logging.exception("Заявка не связалась с возможностью (biz %s)", bid)
        logging.info("[biz %s] Новый заказ №%s от %s", bid, order_id, full_name)
        if not reply:
            reply = f"Готово! Ваша заявка №{order_id} принята."

    if reply:
        database.save_message(bid, client["id"], "assistant", reply, channel=channel)
        who = client.get("name") or full_name or "клиент"
        database.log_event(bid, "reply", f"Сотрудник ответил: {who}", reply[:200],
                           once_key=f"Сотрудник ответил: {who}")
        database.notify_plan_limit(bid)
    return reply
