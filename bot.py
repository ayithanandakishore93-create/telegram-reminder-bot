import os
import requests
import sqlite3
import json
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
MODEL = "meta-llama/llama-3.1-8b-instruct:free"

# ===== DB =====
conn = sqlite3.connect("tasks.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    text TEXT,
    time TEXT,
    repeat INTEGER
)
""")
conn.commit()

# ===== SCHEDULER =====
scheduler = AsyncIOScheduler()
scheduler.start()

# ===== AI PARSER =====
def parse_with_ai(user_text):
    prompt = f"""
Convert into JSON only:

"{user_text}"

Format:
{{
"is_reminder": true/false,
"task": "...",
"minutes": number,
"repeat": number
}}
"""

    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=20
        )

        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    except Exception as e:
        print("PARSE ERROR:", e)
        return {"is_reminder": False}

# ===== SEND REMINDER =====
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"⏰ Reminder: {job.data}"
    )

# ===== CHAT =====
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    data = parse_with_ai(text)

    if data.get("is_reminder"):
        task = data.get("task", "")
        minutes = int(data.get("minutes", 0))
        repeat = int(data.get("repeat", 0))

        if repeat > 0:
            context.job_queue.run_repeating(
                send_reminder,
                interval=repeat * 60,
                first=5,
                data=task,
                chat_id=update.effective_chat.id
            )
            await update.message.reply_text(f"🔁 Every {repeat} min: {task}")
            return

        if minutes > 0:
            context.job_queue.run_once(
                send_reminder,
                when=minutes * 60,
                data=task,
                chat_id=update.effective_chat.id
            )
            await update.message.reply_text(f"⏰ In {minutes} min: {task}")
            return

    # ===== AI fallback =====
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": text}]
            },
            timeout=20
        )

        reply = r.json()["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI ERROR:", e)
        reply = "⚠️ AI error. Try again."

    await update.message.reply_text(reply)

# ===== VOICE (DISABLED) =====
async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎤 Voice not supported yet (no Whisper API)")

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_handler(MessageHandler(filters.VOICE, voice))

    app.run_polling()

if __name__ == "__main__":
    main()
