#!/usr/bin/env python3
import os
import sqlite3
import logging
import asyncio
from datetime import datetime
from io import BytesIO

import requests
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
DB_PATH = "suzi_poo.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("suzi")

# ---------------------------- DATABASE ----------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, chat_id INT, text TEXT)")
    con.commit()
    con.close()

init_db()

# ---------------------------- COMMANDS ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Suzi Poo is online!")

async def pic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = "cat"
    try:
        data = requests.get("https://picsum.photos/400").content
        bio = BytesIO(data)
        bio.name = "img.jpg"
        await update.message.reply_photo(photo=bio)
    except:
        await update.message.reply_text("Image error.")

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Reminders disabled in lite mode.")

# ---------------------------- MESSAGE HANDLER ----------------------------
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    await update.message.reply_text(f"Suzi saw: {msg}")

# ---------------------------- MAIN ----------------------------
async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pic", pic))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    logger.info("Suzi Poo running...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
