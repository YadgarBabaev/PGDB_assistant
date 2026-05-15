"""
Telegram-бот с Claude + PostgreSQL (мульти-БД)
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

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_IDS = set(map(int, os.environ.get("ALLOWED_USER_IDS", "").split(",")))

# Загружаем все базы из переменных окружения
DATABASES: dict[int, dict] = {}
db_names_raw = os.environ.get("DB_NAMES", "")
db_names = [n.strip() for n in db_names_raw.split(",")] if db_names_raw else []

i = 1
while True:
    url = os.environ.get(f"DATABASE_URL_{i}")
    if not url:
        break
    schemas_raw = os.environ.get(f"DB_SCHEMAS_{i}", "public")
    schemas = [s.strip() for s in schemas_raw.split(",")]
    DATABASES[i] = {
        "url":     url,
        "name":    db_names[i - 1] if i - 1 < len(db_names) else f"DB {i}",
        "schemas": schemas,
    }
    i += 1

if not DATABASES:
    url = os.environ.get("DATABASE_URL")
    if url:
        schemas_raw = os.environ.get("DB_SCHEMAS_1", "public")
        schemas = [s.strip() for s in schemas_raw.split(",")]
        DATABASES[1] = {
            "url":     url,
            "name":    db_names[0] if db_names else "DB 1",
            "schemas": schemas,
        }

logger.info("Loaded %d database(s): %s", len(DATABASES), {k: v["name"] for k, v in DATABASES.items()})

agent = ClaudeAgent()

# ─── Состояние пользователей ─────────────────────────────────────────────────

user_state: dict[int, dict] = {}

def get_state(uid: int) -> dict:
    if uid not in user_state:
        user_state[uid] = {"db_index": 1, "history": []}
    return user_state[uid]

# ─── Middleware ───────────────────────────────────────────────────────────────

def restricted(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            await update.message.reply_text("⛔ Нет доступа.")
            return
        return await func(update, ctx)
    return wrapper

# ─── Хендлеры ────────────────────────────────────────────────────────────────

@restricted
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    current = DATABASES.get(state["db_index"], {}).get("name", "—")
    db_list = "\n".join(f"  /db {k} — {v['name']}" for k, v in DATABASES.items())

    await update.message.reply_text(
        f"👋 Привет! Я Claude — управляю вашими PostgreSQL базами.\n\n"
        f"Текущая база: {current}\n\n"
        f"Доступные базы:\n{db_list}\n\n"
        f"Команды:\n"
        f"/db <номер> — переключить базу\n"
        f"/status — текущая база\n"
        f"/clear — сбросить историю диалога"
    )

@restricted
async def cmd_db(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    args = ctx.args

    if not args:
        db_list = "\n".join(f"  /db {k} — {v['name']}" for k, v in DATABASES.items())
        current = DATABASES.get(state["db_index"], {}).get("name", "—")
        await update.message.reply_text(f"Текущая: {current}\n\nДоступные:\n{db_list}")
        return

    try:
        idx = int(args[0])
    except ValueError:
        await update.message.reply_text("Использование: /db <номер>  например /db 2")
        return

    if idx not in DATABASES:
        db_list = "\n".join(f"  {k} — {v['name']}" for k, v in DATABASES.items())
        await update.message.reply_text(f"Нет базы с номером {idx}.\n\nДоступные:\n{db_list}")
        return

    state["db_index"] = idx
    state["history"]  = []
    name = DATABASES[idx]["name"]
    await update.message.reply_text(f"✅ Переключено на {name}\nИстория диалога сброшена.")

@restricted
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = get_state(uid)
    db = DATABASES.get(state["db_index"], {})
    await update.message.reply_text(
        f"📊 Текущая база: {db.get('name', '—')} (#{state['db_index']})"
    )

@restricted
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_state(update.effective_user.id)["history"] = []
    await update.message.reply_text("🗑 История диалога очищена.")

@restricted
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    text  = update.message.text
    state = get_state(uid)

    db = DATABASES.get(state["db_index"], {})
    db_url     = db.get("url")
    db_schemas = db.get("schemas", ["public"])
    if not db_url:
        await update.message.reply_text("⚠️ База не выбрана. Используй /db <номер>")
        return

    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")

    try:
        reply, history, pending_file = await asyncio.to_thread(
            agent.chat, text, state["history"], db_url, db_schemas
        )
        state["history"] = history[-20:]
    except Exception as e:
        logger.exception("Agent error")
        reply = f"❌ Ошибка агента: {e}"
        pending_file = None

    # Отправляем файл если есть
    if pending_file:
        import io as _io
        buf = _io.BytesIO(pending_file["data"])
        buf.name = pending_file["filename"]
        # Всегда отправляем как документ — избегаем ограничений Telegram на размер фото
        await update.message.reply_document(document=buf, filename=pending_file["filename"])

    # Отправляем текст всегда, кроме служебного маркера __FILE__
    if reply and not reply.startswith("__FILE__"):
        await update.message.reply_text(reply)
    elif not pending_file:
        await update.message.reply_text("⚠️ Нет ответа от агента. Попробуй /clear и повтори.")

# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("db",     cmd_db))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started. DBs: %s", list(DATABASES.keys()))
    app.run_polling()

if __name__ == "__main__":
    main()
