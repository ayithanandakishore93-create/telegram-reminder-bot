import os
import requests
import sqlite3
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]

# DB setup
conn = sqlite3.connect("tasks.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    text TEXT,
    time TEXT
)
""")
conn.commit()

# Scheduler
scheduler = AsyncIOScheduler()
scheduler.start()

# ===== Reminder function =====
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(chat_id=job.chat_id, text=job.data)

# ===== Message handler =====
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()

    # ===== SIMPLE REMINDER PARSER =====
    if "remind me" in text:
        try:
            # example: remind me in 1 minute to drink water
            if "in" in text and "minute" in text:
                minutes = int(text.split("in")[1].split("minute")[0].strip())
                reminder_text = text.split("to")[-1].strip()

                run_time = datetime.now() + timedelta(minutes=minutes)

                scheduler.add_job(
                    send_reminder,
                    "date",
                    run_date=run_time,
                    args=[context],
                    kwargs={"job": None},
                )

                await update.message.reply_text(f"⏰ Reminder set in {minutes} minutes")

            else:
                await update.message.reply_text("Use format: remind me in 1 minute to ...")

        except:
            await update.message.reply_text("❌ Couldn't understand reminder")

        return

    # ===== NORMAL AI CHAT =====
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "openai/gpt-3.5-turbo",
            "messages": [{"role": "user", "content": text}]
        }
    )

    reply = r.json()["choices"][0]["message"]["content"]

    await update.message.reply_text(reply)


# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, chat)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
