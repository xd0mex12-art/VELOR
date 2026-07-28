"""
Telegram-боты VELOR AI: по одному боту на каждый бизнес.

Откуда берутся боты:
  1) Каждый бизнес в базе может иметь свой токен (поле tg_bot_token) —
     задаётся в кабинете владельца. Для него поднимается отдельный бот.
  2) Если ни у кого токена нет, но в .env есть TELEGRAM_TOKEN —
     поднимаем один бот для бизнеса №1 (демо / старый режим).

Каждый бот знает свой business_id, поэтому заявки и клиенты не смешиваются.

Запуск:
  1) python setup_db.py     (один раз — создаёт базу)
  2) python bot.py
"""
import asyncio
import logging

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters,
)

import ai
import database
from config import TELEGRAM_TOKEN, DEMO_BOT_BUSINESS_ID

logging.basicConfig(level=logging.INFO)

WHAT, PHONE, DATE = range(3)   # шаги простого режима (без ИИ)


def _bid(context) -> int:
    """business_id текущего бота — сохранён в bot_data при запуске."""
    return context.application.bot_data["business_id"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bid = _bid(context)
    business = database.get_business(bid)
    greeting = business["greeting"] if business and business["greeting"] else "Здравствуйте!"
    if ai.ai_available():
        await update.message.reply_text(
            greeting + "\nПросто напишите, что вам нужно — я всё оформлю.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        keyboard = ReplyKeyboardMarkup([["✍️ Оставить заявку"]], resize_keyboard=True)
        await update.message.reply_text(greeting, reply_markup=keyboard)


# ============================================================
#  РЕЖИМ С ИИ: свободный диалог
# ============================================================

async def ai_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bid = _bid(context)
    user = update.effective_user
    text = update.message.text

    business = database.get_business(bid) or {"name": "бизнес"}
    client = database.get_or_create_client(bid, tg_user_id=user.id, name=user.full_name)

    database.save_message(bid, client["id"], "user", text)

    # Лимит тарифа: если бизнес исчерпал месячный пакет сообщений — вежливо тормозим.
    if database.plan_status(business)["over"]:
        await update.message.reply_text(
            "Спасибо за сообщение! Мы обязательно ответим — сотрудник свяжется с вами в ближайшее время."
        )
        logging.info("[biz %s] лимит тарифа исчерпан — сообщение принято без ИИ", bid)
        return

    history = database.get_history(bid, client["id"])

    client_info = {
        "имя": client.get("name"),
        "телефон": client.get("phone"),
        "день рождения": client.get("birthday"),
        "предпочтения": client.get("favorite"),
        "заметки": client.get("notes"),
    }

    await update.message.chat.send_action("typing")
    docs = database.search_chunks(bid, text)
    try:
        reply, order = ai.chat_reply(business, history, client_info, docs)
    except Exception:
        logging.exception("Ошибка ИИ")
        await update.message.reply_text("Ой, я на секунду задумалась. Напишите ещё раз, пожалуйста.")
        return

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
        logging.info("[biz %s] Новый заказ №%s от %s", bid, order_id, user.full_name)
        if not reply:
            reply = f"Готово! Ваша заявка №{order_id} принята."

    if reply:
        database.save_message(bid, client["id"], "assistant", reply)
        # В уведомления — одной строкой на клиента в день: иначе активная
        # переписка забьёт ленту сотней одинаковых записей.
        who = client.get("name") or user.full_name or "клиент"
        database.log_event(bid, "reply", f"Сотрудник ответил: {who}",
                           reply[:200], once_key=f"Сотрудник ответил: {who}")
        database.notify_plan_limit(bid)
        await update.message.reply_text(reply)


# ============================================================
#  ПРОСТОЙ РЕЖИМ (без ключа ИИ): анкета из трёх вопросов
# ============================================================

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Что хотите заказать? Опишите словами.",
                                    reply_markup=ReplyKeyboardRemove())
    return WHAT


async def order_what(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["text"] = update.message.text
    await update.message.reply_text("Ваш номер телефона для связи?")
    return PHONE


async def order_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["phone"] = update.message.text
    await update.message.reply_text("На какую дату нужен заказ?")
    return DATE


async def order_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bid = _bid(context)
    context.user_data["date"] = update.message.text
    user = update.effective_user
    client = database.get_or_create_client(bid, tg_user_id=user.id, name=user.full_name)
    order_id = database.add_order(
        business_id=bid,
        text=context.user_data["text"],
        client_id=client["id"],
        phone=context.user_data["phone"],
        date_wanted=context.user_data["date"],
    )
    await update.message.reply_text(
        f"Готово! Ваша заявка №{order_id} принята.\n"
        f"Мы свяжемся с вами по номеру {context.user_data['phone']}.",
        reply_markup=ReplyKeyboardMarkup([["✍️ Оставить заявку"]], resize_keyboard=True),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Оформление отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ============================================================
#  СБОРКА И ЗАПУСК НЕСКОЛЬКИХ БОТОВ
# ============================================================

def build_app(token: str, business_id: int, ai_mode: bool) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["business_id"] = business_id
    app.add_handler(CommandHandler("start", start))
    if ai_mode:
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_dialog))
    else:
        app.add_handler(ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("Оставить заявку"), order_start)],
            states={
                WHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_what)],
                PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_phone)],
                DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_date)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        ))
    return app


def collect_bots() -> list[tuple[str, int, str]]:
    """Список (токен, business_id, имя) для всех ботов, которые надо поднять."""
    bots = []
    seen = set()
    for b in database.list_bot_businesses():        # у кого задан токен в базе
        tok = (b.get("tg_bot_token") or "").strip()
        if tok and tok not in seen:
            seen.add(tok)
            bots.append((tok, b["id"], b["name"]))
    # запасной вариант: единственный токен из .env для демо-бизнеса — только если
    # в .env явно указан DEMO_BOT_BUSINESS_ID (иначе демо-бот не поднимаем, чтобы
    # чужие сообщения не попали в случайный бизнес).
    if (TELEGRAM_TOKEN and DEMO_BOT_BUSINESS_ID
            and TELEGRAM_TOKEN.strip() not in seen):
        bots.append((TELEGRAM_TOKEN.strip(), DEMO_BOT_BUSINESS_ID, "демо (.env)"))
    return bots


async def run_all(bots, ai_mode):
    apps = [build_app(tok, bid, ai_mode) for tok, bid, _ in bots]
    for app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()          # держим процесс живым
    finally:
        for app in apps:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


def main():
    bots = collect_bots()
    if not bots:
        raise SystemExit(
            "Нет ни одного токена бота. Задай токен бизнесу в кабинете "
            "(Настройки → Токен бота) или впиши TELEGRAM_TOKEN в .env."
        )
    ai_mode = bool(ai.ai_available() and ai.ping())
    mode = "ИИ" if ai_mode else "простой (анкета)"
    print(f"Запускаю {len(bots)} бот(а) в режиме: {mode}")
    for tok, bid, name in bots:
        print(f"  • бизнес №{bid} «{name}» — бот …{tok[-6:]}")
    print("(Ctrl+C — стоп)")
    asyncio.run(run_all(bots, ai_mode))


if __name__ == "__main__":
    main()
