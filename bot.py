"""
Telegram <-> agent glue.

Requires:
- TELEGRAM_BOT_TOKEN
- GROQ_API_KEY

Uses polling. Best deployed as a Render Background Worker.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

from db import init_db, get_conn
import agent

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kirana-bot")

# In-memory conversation history
CONVERSATIONS: dict[int, list] = {}


def get_or_create_owner(conn, chat_id: str) -> int:
    row = conn.execute(
        "SELECT id FROM owners WHERE telegram_chat_id=?",
        (chat_id,),
    ).fetchone()

    if row:
        return row["id"]

    cur = conn.execute(
        "INSERT INTO owners (telegram_chat_id) VALUES (?)",
        (chat_id,),
    )
    return cur.lastrowid


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    text = update.message.text
    update_id = update.update_id

    conn = get_conn()
    owner_id = get_or_create_owner(conn, chat_id)

    if text.strip() == "/new":
        CONVERSATIONS.pop(update.effective_chat.id, None)
        await update.message.reply_text(
            "Started a new chat. (Your shop's preferences and data are unchanged.)"
        )
        return

    history = CONVERSATIONS.get(update.effective_chat.id, [])

    MAX_HISTORY_MESSAGES = 8
    history = history[-MAX_HISTORY_MESSAGES:]

    try:
        reply_text, updated_history, generated_files = agent.run_turn(
            owner_id,
            update_id,
            text,
            history,
        )

        CONVERSATIONS[update.effective_chat.id] = updated_history

    except Exception:
        log.exception("run_turn crashed unexpectedly")

        await update.message.reply_text(
            "Sorry, something went wrong on my end just now. Please try again."
        )
        return

    await update.message.reply_text(reply_text or "(done)")

    for path in generated_files:
        if os.path.exists(path):
            with open(path, "rb") as f:
                await update.message.reply_document(f)


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    init_db()

    app = Application.builder().token(token).build()

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    log.info("Bot starting (polling)...")

    app.run_polling(
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()