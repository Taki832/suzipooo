#!/usr/bin/env python3
import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime
from io import BytesIO

# Optional libraries
try:
    import markovify
except:
    markovify = None

try:
    import yt_dlp
except:
    yt_dlp = None

import requests
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

# Load .env (for local use)
load_dotenv()

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_TOKEN") or ""
DB_PATH = "suzi_poo.db"
MARKOV_LINES = 2000
YT_MAX_BYTES = 40_000_000

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set! Add it in Railway Variables.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("suzi_poo")

# -------------------- DB INIT --------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            chat_id INTEGER PRIMARY KEY,
            learn_enabled INTEGER DEFAULT 0,
            tone TEXT DEFAULT 'kind',
            lang TEXT DEFAULT 'en'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            user_id INTEGER,
            message TEXT,
            remind_at TIMESTAMP
        )
    """)

    con.commit()
    con.close()

init_db()

def db_query(query, params=(), fetch=False):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(query, params)
    if fetch:
        res = cur.fetchall()
        con.close()
        return res
    con.commit()
    con.close()


# -------------------- MESSAGE STORAGE --------------------
def store_message(chat_id, user_id, username, text):
    try:
        db_query(
            "INSERT INTO messages(chat_id,user_id,username,text) VALUES(?,?,?,?)",
            (chat_id, user_id, username, text[:4000])
        )
    except:
        logger.exception("store_message failed")


# -------------------- SETTINGS --------------------
def is_learning_enabled(chat_id):
    r = db_query("SELECT learn_enabled FROM settings WHERE chat_id=?", (chat_id,), fetch=True)
    return bool(r and r[0][0] == 1)

def set_learning(chat_id, enabled):
    db_query("""
        INSERT INTO settings(chat_id,learn_enabled,tone,lang)
        VALUES(?,?,COALESCE((SELECT tone FROM settings WHERE chat_id=?),'kind'),
                   COALESCE((SELECT lang FROM settings WHERE chat_id=?),'en'))
        ON CONFLICT(chat_id) DO UPDATE SET learn_enabled=excluded.learn_enabled
    """, (chat_id, 1 if enabled else 0, chat_id, chat_id))

def get_setting(chat_id, key, default=None):
    r = db_query(f"SELECT {key} FROM settings WHERE chat_id=?", (chat_id,), fetch=True)
    return r[0][0] if r else default

def set_setting(chat_id, key, val):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("SELECT tone,lang,learn_enabled FROM settings WHERE chat_id=?", (chat_id,))
    old = cur.fetchone()
    if old:
        tone, lang, learn = old
    else:
        tone, lang, learn = "kind", "en", 0

    if key == "tone":
        tone = val
    elif key == "lang":
        lang = val

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


# -------------------- REMINDERS --------------------
def add_reminder(chat_id, user_id, message, dt):
    db_query("INSERT INTO reminders(chat_id,user_id,message,remind_at) VALUES(?,?,?,?)",
             (chat_id, user_id, message, dt.isoformat()))

def get_due_reminders(now):
    return db_query("SELECT id,chat_id,user_id,message FROM reminders WHERE remind_at <= ?", 
                     (now.isoformat(),), fetch=True)

def delete_reminder(rid):
    db_query("DELETE FROM reminders WHERE id=?", (rid,))


# -------------------- MARKOV --------------------
def build_markov(chat_id):
    if markovify is None:
        return None
    rows = db_query("SELECT text FROM messages WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
                    (chat_id, MARKOV_LINES), fetch=True)
    text = "\n".join(r[0] for r in rows if r[0])

    if not text.strip():
        return None

    try:
        return markovify.NewlineText(text, state_size=2)
    except:
        return None


# -------------------- TONE APPLY --------------------
def apply_tone(text, tone):
    if tone == "angry":
        return text.upper() + "!"
    return text


# -------------------- IMAGE FETCH --------------------
def fetch_image(query):
    try:
        req = requests.get("https://duckduckgo.com/", params={"q": query}, timeout=10)
        m = re.search(r'vqd=([\d-]+)&', req.text)
        if not m:
            m = re.search(r"vqd='([^']+)'", req.text)
        if not m:
            return None
        token = m.group(1)

        j = requests.get(
            "https://duckduckgo.com/i.js",
            params={"l": "us-en", "o": "json", "q": query, "vqd": token, "f": ",,,,,"},
            timeout=10
        ).json()

        url = j["results"][0]["image"]
        return requests.get(url, timeout=10).content
    except:
        return None


# -------------------- YTDLP (audio) --------------------
def dl_youtube(query):
    if yt_dlp is None:
        return None, "yt-dlp not installed"

    if not re.match(r"https?://", query):
        query = f"ytsearch1:{query}"

    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "outtmpl": "/tmp/sz.%(ext)s",
        "cachedir": False,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(query, download=True)

        file = None
        for f in os.listdir("/tmp"):
            if f.startswith("sz.") and f.endswith(".mp3"):
                file = "/tmp/" + f
                break

        if not file:
            return None, "No file found"

        data = open(file, "rb").read(YT_MAX_BYTES)
        os.remove(file)
        return data, None

    except Exception as e:
        return None, str(e)


# -------------------- APSCHEDULER JOB --------------------
async def check_reminders_job():
    now = datetime.utcnow()
    due = get_due_reminders(now)
    for rid, chat_id, user_id, msg in due:
        try:
            await application.bot.send_message(chat_id=chat_id, text=f"🔔 Reminder: {msg}")
        except:
            pass
        delete_reminder(rid)


scheduler = AsyncIOScheduler()


# -------------------- COMMANDS --------------------
async def cmd_start(update, context):
    await update.message.reply_text(
        "Hi! I'm Suzi Poo 🎀\n"
        "/learn_on\n"
        "/learn_off\n"
        "/settone kind|angry\n"
        "/setlang en|ta|hi\n"
        "/remind YYYY-MM-DD HH:MM | message\n"
        "/pic cat\n"
        "/song never gonna give you up\n"
        "Tag me to get a reply!"
    )


async def cmd_learn_on(update, context):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type in ("group", "supergroup"):
        try:
            m = await chat.get_member(user.id)
            if m.status not in ("administrator", "creator"):
                return await update.message.reply_text("Admins only.")
        except:
            pass

    set_learning(chat.id, True)
    await update.message.reply_text("Learning ENABLED.")


async def cmd_learn_off(update, context):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type in ("group", "supergroup"):
        try:
            m = await chat.get_member(user.id)
            if m.status not in ("administrator", "creator"):
                return await update.message.reply_text("Admins only.")
        except:
            pass

    set_learning(chat.id, False)
    await update.message.reply_text("Learning DISABLED.")


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


async def cmd_remind(update, context):
    text = update.message.text.partition(" ")[2]
    if "|" not in text:
        return await update.message.reply_text("Usage: /remind YYYY-MM-DD HH:MM | message")
    when, _, msg = text.partition("|")
    when = when.strip()
    msg = msg.strip()

    try:
        dt = datetime.fromisoformat(when)
    except:
        return await update.message.reply_text("Invalid datetime.")

    add_reminder(update.effective_chat.id, update.effective_user.id, msg, dt)
    await update.message.reply_text(f"Reminder set for {dt}.")


async def cmd_pic(update, context):
    query = update.message.text.partition(" ")[2].strip()
    if not query:
        return await update.message.reply_text("Usage: /pic cat")
    img = fetch_image(query)
    if not img:
        return await update.message.reply_text("No image found.")
    bio = BytesIO(img)
    bio.name = "pic.jpg"
    await update.message.reply_photo(photo=bio)


async def cmd_song(update, context):
    q = update.message.text.partition(" ")[2].strip()
    if not q:
        return await update.message.reply_text("Usage: /song query")
    data, err = await asyncio.get_event_loop().run_in_executor(None, dl_youtube, q)
    if err:
        return await update.message.reply_text("Failed: " + err)
    if not data:
        return await update.message.reply_text("No audio.")
    bio = BytesIO(data)
    bio.name = "song.mp3"
    try:
        await update.message.reply_audio(audio=bio)
    except:
        await update.message.reply_text("File too large.")


# -------------------- MESSAGE HANDLER --------------------
async def handle_message(update, context):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text
    chat_id = msg.chat.id
    user = update.effective_user

    # Learning
    if is_learning_enabled(chat_id) and not user.is_bot:
        store_message(chat_id, user.id, user.username or user.full_name, text)

    # Mention?
    bot_username = (await context.bot.get_me()).username.lower()
    mentioned = False

    if msg.entities:
        for e in msg.entities:
            if e.type == "mention":
                part = text[e.offset:e.offset+e.length]
                if part.lstrip("@").lower() == bot_username:
                    mentioned = True
            if e.type == "text_mention" and e.user and e.user.id == (await context.bot.get_me()).id:
                mentioned = True

    if msg.reply_to_message:
        if msg.reply_to_message.from_user.id == (await context.bot.get_me()).id:
            mentioned = True

    if mentioned:
        tone = get_setting(chat_id, "tone", "kind")
        lang = get_setting(chat_id, "lang", "en")

        model = build_markov(chat_id)
        if model:
            for _ in range(6):
                try:
                    sentence = model.make_sentence(tries=50)
                    if sentence:
                        return await msg.reply_text(apply_tone(sentence, tone))
                except:
                    pass

        fallback = {
            "en": "Hello! I am Suzi Poo!",
            "ta": "வணக்கம்! நான் Suzi Poo!",
            "hi": "नमस्ते! मैं Suzi Poo हूँ!"
        }
        return await msg.reply_text(apply_tone(fallback.get(lang, fallback["en"]), tone))

    # If angry tone & caps
    if text.isupper() and len(text) >= 4 and get_setting(chat_id, "tone", "kind") == "angry":
        await msg.reply_text("🔥 Calm down da!")
        return


# -------------------- MAIN LOOP --------------------
async def main():
    global application

    application = ApplicationBuilder().token(TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("learn_on", cmd_learn_on))
    application.add_handler(CommandHandler("learn_off", cmd_learn_off))
    application.add_handler(CommandHandler("settone", cmd_settone))
    application.add_handler(CommandHandler("setlang", cmd_setlang))
    application.add_handler(CommandHandler("remind", cmd_remind))
    application.add_handler(CommandHandler("pic", cmd_pic))
    application.add_handler(CommandHandler("song", cmd_song))

    # Messages
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    # Schedule reminder check
    scheduler.add_job(check_reminders_job, "interval", seconds=30)
    scheduler.start()

    logger.info("Starting Suzi Poo...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await application.updater.wait_stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except:
        pass

