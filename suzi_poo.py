#!/usr/bin/env python3
"""
suzi_poo.py
Single-file Telegram bot for Railway & local runs.

Features:
- Opt-in learning from groups (store messages in SQLite)
- Mention replies (@suzi_poo) using markovify when available
- Reminders (/remind)
- Voice messages (/voice) - uses gTTS if installed on host, otherwise falls back to text.
- Image search (/pic) via DuckDuckGo quick lookup
- YouTube audio (/song) using yt-dlp (size-guard)
- Language (en/ta/hi) and tone (kind/angry) per chat
- Graceful behavior when optional libs missing (so it won't crash)
"""

import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime
from io import BytesIO

# optional libs
try:
    import markovify
except Exception:
    markovify = None

try:
    from gtts import gTTS
except Exception:
    gTTS = None

try:
    import yt_dlp
except Exception:
    yt_dlp = None

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

# load .env if present (for local dev). On Railway set env vars in the project UI.
load_dotenv()

# ------------ CONFIG -------------
TOKEN = os.getenv("TELEGRAM_TOKEN") or ""  # MUST set in Railway variables
# Optional: local dev only
# ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY")  # optional TTS provider (not implemented here)
DB_PATH = "suzi_poo.db"
MARKOV_LINES = 2000
YT_MAX_BYTES = 40_000_000  # safety read cap (40 MB)

if not TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN in environment (Railway variables).")

# ------------ logging -------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("suzi_poo")

# ------------ DB helpers & init -------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            learn_enabled INTEGER DEFAULT 0,
            tone TEXT DEFAULT 'kind',
            lang TEXT DEFAULT 'en'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
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
        rows = cur.fetchall()
        con.close()
        return rows
    con.commit()
    con.close()

# message storage
def store_message(chat_id, user_id, username, text):
    try:
        db_query("INSERT INTO messages (chat_id, user_id, username, text) VALUES (?, ?, ?, ?)",
                 (chat_id, user_id, username, text[:4000]))
    except Exception:
        logger.exception("store_message failed")

# settings helpers
def is_learning_enabled(chat_id):
    r = db_query("SELECT learn_enabled FROM settings WHERE chat_id=?", (chat_id,), fetch=True)
    return bool(r and r[0][0] == 1)

def set_learning(chat_id, enabled: bool):
    db_query("""
        INSERT INTO settings (chat_id, learn_enabled, tone, lang)
        VALUES (?, ?, COALESCE((SELECT tone FROM settings WHERE chat_id=?),'kind'),
                    COALESCE((SELECT lang FROM settings WHERE chat_id=?),'en'))
        ON CONFLICT(chat_id) DO UPDATE SET learn_enabled=excluded.learn_enabled
    """, (chat_id, 1 if enabled else 0, chat_id, chat_id))

def get_setting(chat_id, key, default=None):
    rows = db_query(f"SELECT {key} FROM settings WHERE chat_id=?", (chat_id,), fetch=True)
    return rows[0][0] if rows else default

def set_setting(chat_id, key, val):
    # read existing
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT tone, lang, learn_enabled FROM settings WHERE chat_id=?", (chat_id,))
    existing = cur.fetchone()
    if existing:
        tone, lang, learn_enabled = existing
    else:
        tone, lang, learn_enabled = ("kind", "en", 0)
    if key == "tone":
        tone = val
    elif key == "lang":
        lang = val
    cur.execute("""
        INSERT INTO settings (chat_id, learn_enabled, tone, lang)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET learn_enabled=excluded.learn_enabled, tone=excluded.tone, lang=excluded.lang
    """, (chat_id, learn_enabled, tone, lang))
    con.commit()
    con.close()

# reminders
def add_reminder(chat_id, user_id, message, remind_at):
    db_query("INSERT INTO reminders (chat_id, user_id, message, remind_at) VALUES (?, ?, ?, ?)",
             (chat_id, user_id, message, remind_at.isoformat()))

def get_due_reminders(before_dt):
    return db_query("SELECT id, chat_id, user_id, message FROM reminders WHERE remind_at <= ?", (before_dt.isoformat(),), fetch=True)

def delete_reminder(rem_id):
    db_query("DELETE FROM reminders WHERE id=?", (rem_id,))

# -------- Markov builder --------
def build_markov(chat_id):
    if markovify is None:
        return None
    rows = db_query("SELECT text FROM messages WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
                    (chat_id, MARKOV_LINES), fetch=True)
    if not rows:
        return None
    text = "\n".join(r[0] for r in rows if r[0])
    if not text.strip():
        return None
    try:
        model = markovify.NewlineText(text, state_size=2)
        return model
    except Exception:
        logger.exception("markovify build failed")
        return None

# -------- Tone & language helpers --------
def apply_tone(text, tone):
    if tone == "angry":
        t = text.strip()
        if not t:
            return "HMM."
        return t.upper() + "!"
    return text

# -------- Image fetch (DuckDuckGo quick) --------
def fetch_image(query):
    try:
        r = requests.get("https://duckduckgo.com/", params={"q": query}, timeout=8)
        m = re.search(r'vqd=([\d-]+)&', r.text)
        token = m.group(1) if m else None
        if not token:
            m2 = re.search(r'vqd=\'([^\']+)\'', r.text)
            token = m2.group(1) if m2 else None
        if not token:
            return None
        headers = {"referer": "https://duckduckgo.com/"}
        params = {"l": "us-en", "o": "json", "q": query, "vqd": token, "f": ",,,,"}
        j = requests.get("https://duckduckgo.com/i.js", params=params, headers=headers, timeout=8).json()
        if not j.get("results"):
            return None
        img_url = j["results"][0]["image"]
        img_bytes = requests.get(img_url, timeout=12).content
        return img_bytes
    except Exception:
        logger.exception("fetch_image failed")
        return None

# -------- YouTube audio (yt-dlp) --------
def download_youtube_audio(query):
    if yt_dlp is None:
        return None, "yt-dlp not installed"
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "outtmpl": "/tmp/suzipoo.%(ext)s",
        "cachedir": False,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    if not re.match(r"https?://", query):
        query = f"ytsearch1:{query}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=True)
            # try to find produced mp3
            mp3_candidate = None
            for f in os.listdir("/tmp"):
                if f.startswith("suzipoo") and (f.endswith(".mp3") or f.endswith(".m4a") or f.endswith(".webm") or f.endswith(".opus")):
                    mp3_candidate = "/tmp/" + f
                    break
            if not mp3_candidate:
                # best-effort fallback
                mp3_candidate = None
            if not mp3_candidate:
                return None, "Could not find downloaded audio file"
            # read up to cap
            with open(mp3_candidate, "rb") as fh:
                b = fh.read(YT_MAX_BYTES)
            try:
                os.remove(mp3_candidate)
            except Exception:
                pass
            return b, None
    except Exception as e:
        logger.exception("yt-dlp failed")
        return None, str(e)

# -------- Reminders scheduler --------
scheduler = AsyncIOScheduler()

async def check_reminders(app):
    now = datetime.utcnow()
    due = get_due_reminders(now)
    for rem in due:
        rem_id, chat_id, user_id, message = rem
        try:
            await app.bot.send_message(chat_id=chat_id, text=f"🔔 Reminder: {message}")
        except Exception:
            pass
        delete_reminder(rem_id)

# -------- Telegram handlers --------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi — I'm Suzi Poo 🐣\n"
        "Commands:\n"
        "/learn_on (admin only)\n"
        "/learn_off\n"
        "/settone <kind|angry>\n"
        "/setlang <en|ta|hi>\n"
        "/remind <YYYY-MM-DD HH:MM> | <message>\n"
        "/voice <text>\n"
        "/pic <query>\n"
        "/song <youtube url or query>\n\n"
        "Mention me (@your_bot_username) to get a reply."
    )

async def cmd_learn_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type in ("group", "supergroup"):
        try:
            member = await chat.get_member(user.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("Only group admins can enable learning.")
                return
        except Exception:
            pass
    set_learning(chat.id, True)
    await update.message.reply_text("Learning ENABLED for this chat.")

async def cmd_learn_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type in ("group", "supergroup"):
        try:
            member = await chat.get_member(user.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("Only group admins can disable learning.")
                return
        except Exception:
            pass
    set_learning(chat.id, False)
    await update.message.reply_text("Learning DISABLED for this chat.")

async def cmd_settone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /settone kind|angry")
        return
    t = args[0].lower()
    if t not in ("kind", "angry"):
        await update.message.reply_text("Tone must be kind or angry.")
        return
    set_setting(update.effective_chat.id, "tone", t)
    await update.message.reply_text(f"Tone set to {t}")

async def cmd_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /setlang en|ta|hi")
        return
    l = args[0].lower()
    if l not in ("en", "ta", "hi"):
        await update.message.reply_text("Language must be en, ta, or hi")
        return
    set_setting(update.effective_chat.id, "lang", l)
    await update.message.reply_text(f"Language set to {l}")

async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rest = update.message.text.partition(" ")[2].strip()
    if "|" not in rest:
        await update.message.reply_text("Usage: /remind <YYYY-MM-DD HH:MM> | <message>")
        return
    when_part, _, msg = rest.partition("|")
    when_part = when_part.strip(); msg = msg.strip()
    try:
        dt = datetime.fromisoformat(when_part)
    except Exception:
        try:
            dt = datetime.strptime(when_part, "%Y-%m-%d %H:%M")
        except Exception:
            await update.message.reply_text("Couldn't parse datetime. Use 'YYYY-MM-DD HH:MM'")
            return
    add_reminder(update.effective_chat.id, update.effective_user.id, msg, dt)
    await update.message.reply_text(f"Reminder set for {dt.isoformat()} (UTC assumed).")

async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /voice <text>")
        return
    # try gTTS if available
    if gTTS is None:
        # TTS not installed on host. Explain alternatives.
        await update.message.reply_text(
            "Voice not available on this host. To enable voice you can:\n"
            "• Deploy on a host that supports gTTS/ffmpeg, and add 'gtts' to requirements.txt.\n"
            "• Or provide an external TTS API (ElevenLabs / Google Cloud TTS) and I can integrate it.\n\nSending text instead:\n\n" + text
        )
        return
    try:
        tts = gTTS(text=text, lang=get_setting(update.effective_chat.id, "lang", "en"))
        bio = BytesIO()
        tts.write_to_fp(bio)
        bio.seek(0)
        await update.message.reply_voice(voice=bio)
    except Exception as e:
        logger.exception("gTTS failed")
        await update.message.reply_text("TTS generation failed, sending text:\n\n" + text)

async def cmd_pic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.partition(" ")[2].strip()
    if not query:
        await update.message.reply_text("Usage: /pic <query>")
        return
    await update.message.reply_text(f"Searching for: {query} ...")
    b = fetch_image(query)
    if not b:
        await update.message.reply_text("Couldn't find an image.")
        return
    bio = BytesIO(b); bio.name = "image.jpg"
    await update.message.reply_photo(photo=bio)

async def cmd_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.partition(" ")[2].strip()
    if not q:
        await update.message.reply_text("Usage: /song <youtube url or search>")
        return
    await update.message.reply_text("Fetching audio (may take a while)...")
    audio_bytes, err = await asyncio.get_event_loop().run_in_executor(None, download_youtube_audio, q)
    if err:
        await update.message.reply_text("Failed to fetch audio: " + err)
        return
    if not audio_bytes:
        await update.message.reply_text("No audio returned.")
        return
    bio = BytesIO(audio_bytes); bio.name = "song.mp3"
    try:
        await update.message.reply_audio(audio=bio)
    except Exception:
        await update.message.reply_text("File too large to send. Sending YouTube search link instead: https://www.youtube.com/results?search_query=" + requests.utils.requote_uri(q))

# Main message handler: store messages, detect mention and reply
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat = update.effective_chat
    user = update.effective_user
    text = msg.text.strip()

    # store messages if learning enabled
    try:
        if is_learning_enabled(chat.id) and not user.is_bot:
            store_message(chat.id, user.id, user.username or user.full_name, text)
    except Exception:
        logger.exception("store message error")

    # detect mention
    bot_username = (await context.bot.get_me()).username.lower()
    mentioned = False
    if msg.entities:
        for ent in msg.entities:
            if ent.type == "mention":
                ent_text = text[ent.offset:ent.offset+ent.length]
                if ent_text.lstrip("@").lower() == bot_username:
                    mentioned = True
            if ent.type == "text_mention" and ent.user and ent.user.id == (await context.bot.get_me()).id:
                mentioned = True
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == (await context.bot.get_me()).id:
        mentioned = True

    if mentioned:
        tone = get_setting(chat.id, "tone", "kind")
        lang = get_setting(chat.id, "lang", "en")
        # try markov reply
        model = build_markov(chat.id)
        if model:
            for _ in range(6):
                try:
                    candidate = model.make_sentence(tries=50)
                    if candidate:
                        await update.message.reply_text(apply_tone(candidate, tone))
                        return
                except Exception:
                    continue
        # fallback phrase by language
        fallbacks = {
            "en": "Hello! I am Suzi Poo. Ask me anything.",
            "ta": "வணக்கம்! நான் Suzi Poo. கேளுங்கள்.",
            "hi": "नमस्ते! मैं Suzi Poo हूँ। पूछिए।"
        }
        await update.message.reply_text(apply_tone(fallbacks.get(lang, fallbacks["en"]), tone))
        return

    # automatic angry detection: if message is shouting and tone=angry for chat
    if text.isupper() and len(text) > 3 and get_setting(chat.id, "tone", "kind") == "angry":
        await update.message.reply_text("😡 Calm down please!")
        return

# -------- start/runner -----------
async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("learn_on", cmd_learn_on))
    app.add_handler(CommandHandler("learn_off", cmd_learn_off))
    app.add_handler(CommandHandler("settone", cmd_settone))
    app.add_handler(CommandHandler("setlang", cmd_setlang))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("pic", cmd_pic))
    app.add_handler(CommandHandler("song", cmd_song))

    # messages
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_handler))

    # start scheduler
    scheduler.add_job(lambda: asyncio.create_task(check_reminders(app)), "interval", seconds=30)
    scheduler.start()

    logger.info("Starting Suzi Poo...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.wait_stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Exited")
