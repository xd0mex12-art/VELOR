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


def greeting_text(bid):
    """Приветствие бизнеса для команды /start."""
    business = database.get_business(bid) or {}
    greeting = (business.get("greeting") or "").strip() or "Здравствуйте!"
    if ai.ai_available():
        return greeting + "\nПросто напишите, что вам нужно — я всё оформлю."
    return greeting


def handle_message(bid, tg_user_id, full_name, text):
    """
    Обработать текстовое сообщение клиента и вернуть текст ответа (или None).
    Полностью повторяет логику bot.ai_dialog: клиент, память диалога, лимит тарифа,
    ответ ИИ, оформление заказа, запись в ленту.
    """
    business = database.get_business(bid) or {"name": "бизнес"}

    # Триал завершён → бот на паузе: не отвечаем ИИ, не создаём клиентов/заявки.
    if trial.access(business)["read_only"]:
        return "Спасибо за сообщение! Мы свяжемся с вами в ближайшее время."

    client = database.get_or_create_client(bid, tg_user_id=tg_user_id, name=full_name)
    database.save_message(bid, client["id"], "user", text)

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
        )
        if order.get("phone") and not client.get("phone"):
            database.update_client(client["id"], bid, phone=order["phone"])
        logging.info("[biz %s] Новый заказ №%s от %s", bid, order_id, full_name)
        if not reply:
            reply = f"Готово! Ваша заявка №{order_id} принята."

    if reply:
        database.save_message(bid, client["id"], "assistant", reply)
        who = client.get("name") or full_name or "клиент"
        database.log_event(bid, "reply", f"Сотрудник ответил: {who}", reply[:200],
                           once_key=f"Сотрудник ответил: {who}")
        database.notify_plan_limit(bid)
    return reply
