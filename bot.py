"""
Telegram-бот с Claude + PostgreSQL
Запуск: python bot.py
"""

import os
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from claude_agent import ClaudeAgent

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Загрузка конфига ────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_IDS = set(map(int, os.environ.get("ALLOWED_USER_IDS", "").split(",")))

agent = ClaudeAgent()

# ─── Middleware: защита доступа ──────────────────────────────────────────────

def restricted(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            await update.message.reply_text("⛔ Нет доступа.")
            logger.warning("Blocked user_id=%s", uid)
            return
        return await func(update, ctx)
    return wrapper

# ─── Хранилище истории диалогов (в памяти, на сессию) ────────────────────────

conversation_history: dict[int, list] = {}

# ─── Хендлеры ────────────────────────────────────────────────────────────────

@restricted
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я Claude — умею работать с вашей PostgreSQL БД.\n\n"
        "Примеры запросов:\n"
        "• Покажи все таблицы\n"
        "• Добавь пользователя Иван, email ivan@mail.ru\n"
        "• Измени статус заказа #42 на 'выполнен'\n"
        "• Удали запись с id=5 из таблицы logs\n\n"
        "/clear — сбросить историю диалога"
    )

@restricted
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conversation_history.pop(update.effective_user.id, None)
    await update.message.reply_text("🗑 История диалога очищена.")

@restricted
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    text  = update.message.text

    history = conversation_history.setdefault(uid, [])

    # Показываем "печатает..."
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")

    try:
        reply, history = await asyncio.to_thread(agent.chat, text, history)
        conversation_history[uid] = history[-20:]  # храним последние 10 пар
    except Exception as e:
        logger.exception("Agent error")
        reply = f"❌ Ошибка агента: {e}"

    # Telegram не рендерит MD таблицы — шлём как обычный текст
    await update.message.reply_text(reply)

# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started. Allowed users: %s", ALLOWED_USER_IDS or "ALL")
    app.run_polling()

if __name__ == "__main__":
    main()
