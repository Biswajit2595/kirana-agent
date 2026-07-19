"""
Telegram <-> agent glue. This file needs a real TELEGRAM_BOT_TOKEN and
ANTHROPIC_API_KEY (env vars) to actually run -- see .env.example and README.

Key detail: update.update_id is what makes idempotency real. Telegram
guarantees at-least-once delivery, so this same handler WILL occasionally
fire twice for the same update -- that's exactly the case agent.py's
idempotency_key derivation is built to absorb.
"""
import os
import logging
from dotenv import load_dotenv
load_dotenv()  # reads .env into environment variables before anything else runs

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

from db import init_db, get_conn
import agent

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kirana-bot")

# In-memory per-chat conversation history. This is intentionally NOT where
# preferences live -- see agent.py, preferences come from the DB every turn.
# This dict resets on process restart, which is correct: conversation
# history is ephemeral, only the DB is durable.
CONVERSATIONS: dict[int, list] = {}


def get_or_create_owner(conn, chat_id: str) -> int:
    row = conn.execute("SELECT id FROM owners WHERE telegram_chat_id=?", (chat_id,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO owners (telegram_chat_id) VALUES (?)", (chat_id,))
    return cur.lastrowid


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    text = update.message.text
    update_id = update.update_id  # <-- the whole idempotency story starts here

    conn = get_conn()
    owner_id = get_or_create_owner(conn, chat_id)

    if text.strip() == "/new":
        CONVERSATIONS.pop(update.effective_chat.id, None)
        await update.message.reply_text("Started a new chat. (Your shop's preferences and data are unchanged.)")
        return

    history = CONVERSATIONS.get(update.effective_chat.id, [])
    # Bound token usage per turn: only the most recent exchanges are resent to
    # the model. Preferences are NOT affected by this -- they're hydrated
    # fresh from the DB every turn (see agent.py), independent of this
    # trimmed conversational history. This exists purely to stretch a small
    # daily token budget (e.g. Groq's free tier) across more testing turns.
    MAX_HISTORY_MESSAGES = 8
    history = history[-MAX_HISTORY_MESSAGES:]

    try:
        reply_text, updated_history, generated_files = agent.run_turn(owner_id, update_id, text, history)
        CONVERSATIONS[update.effective_chat.id] = updated_history
    except Exception:
        # Last-resort safety net -- run_turn already handles the common
        # failure modes internally (see agent.py), but if something entirely
        # unexpected slips through, the owner should never be left with
        # total silence. Conversation history is intentionally NOT updated
        # here, so the next message retries cleanly from the last good state.
        log.exception("run_turn crashed unexpectedly")
        await update.message.reply_text(
            "Sorry, something went wrong on my end just now -- please try that again."
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
