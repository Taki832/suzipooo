#!/usr/bin/env python3
import os
import re
import sqlite3
import logging
from io import BytesIO

import requests
from dotenv import load_dotenv
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

# Load env
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN") or ""
DB_PATH = "suzi_poo.db"

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("suzi_poo")

# ---------------- DB ----------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            chat_id INTEGER PRIMARY KEY,
            learn_enabled INTEGER DEFAULT 0,
            tone TEXT DEFAULT 'kind',
            lang TEXT DEFAULT 'en'
        )
    """)

    con.commit()
    con.close()

init_db()

def get_setting(chat_id, key, default=None):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(f"SELECT {key} FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else default

def set_setting(chat_id, key, val):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("SELECT tone,lang,learn_enabled FROM settings WHERE chat_id=?", (chat_id,))
    old = cur.fetchone()
    if old:
        tone, lang, learn = old
    else:
        tone, lang, learn = "kind", "en", 0

    if key == "tone": tone = val
    if key == "lang": lang = val

    cur.execute("""
        INSERT INTO settings(chat_id,learn_enabled,tone,lang)
        VALUES(?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET 
            learn_enabled=excluded.learn_enabled,
            tone=excluded.tone,
            lang=excluded.lang
    """, (chat_id, learn, tone, lang))

    con.commit()
    con.close()

# ---------------- Commands ----------------
async def cmd_start(update, context):
    await update.message.reply_text(
        "Hi! I'm Suzi Poo 🎀\n"
        "/settone kind|angry\n"
        "/setlang en|ta|hi\n"
        "Tag me to talk with me!"
    )

async def cmd_settone(update, context):
    if not context.args:
        return await update.message.reply_text("Usage: /settone kind|angry")
    tone = context.args[0].lower()
    if tone not in ("kind", "angry"):
        return await update.message.reply_text("Invalid tone.")
    set_setting(update.effective_chat.id, "tone", tone)
    await update.message.reply_text("Tone updated.")

async def cmd_setlang(update, context):
    if not context.args:
        return await update.message.reply_text("Usage: /setlang en|ta|hi")
    lang = context.args[0].lower()
    if lang not in ("en", "ta", "hi"):
        return await update.message.reply_text("Invalid language.")
    set_setting(update.effective_chat.id, "lang", lang)
    await update.message.reply_text("Language updated.")

# ---------------- Chat Handler ----------------
async def handle_message(update, context):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text
    chat_id = msg.chat.id

    bot_username = (await context.bot.get_me()).username.lower()
    mentioned = False

    if msg.entities:
        for e in msg.entities:
            if e.type == "mention":
                part = text[e.offset:e.offset+e.length]
                if part.lstrip("@").lower() == bot_username:
                    mentioned = True

    if msg.reply_to_message:
        if msg.reply_to_message.from_user.id == (await context.bot.get_me()).id:
            mentioned = True

    if mentioned:
        tone = get_setting(chat_id, "tone", "kind")
        lang = get_setting(chat_id, "lang", "en")

        fallback = {
            "en": "Hello! I am Suzi Poo!",
            "ta": "வணக்கம்! நான் Suzi Poo!",
            "hi": "नमस्ते! मैं Suzi Poo हूँ!"
        }

        ans = fallback.get(lang, fallback["en"])

        if tone == "angry":
            ans = ans.upper() + "!!"

        return await msg.reply_text(ans)

# ---------------- MAIN ----------------
def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("settone", cmd_settone))
    application.add_handler(CommandHandler("setlang", cmd_setlang))

    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    logger.info("Starting Suzi Poo...")
    application.run_polling()

if __name__ == "__main__":
    main()
