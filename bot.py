import os
import re
import requests
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
MODEL = "meta-llama/llama-3-8b-instruct:free"


# ================= REMINDER PARSER =================

def parse_reminder(text):
    text = text.lower()

    # in X minutes
    match = re.search(r'in (\d+) (minute|minutes|min)', text)
    if match:
        minutes = int(match.group(1))
        return ("once", minutes, text.split("to")[-1].strip())

    # every X minutes
    match = re.search(r'every (\d+) (minute|minutes|min)', text)
    if match:
        minutes = int(match.group(1))
        return ("repeat", minutes, text.split("to")[-1].strip())

    return None


# ================= REMINDER =================

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"⏰ Reminder: {job.data}"
    )


# ================= MAIN HANDLER =================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # 🔹 1. LOCAL REMINDER LOGIC (NO AI)
    if "remind me" in text.lower():
        parsed = parse_reminder(text)

        if parsed:
            mode, minutes, task = parsed

            if mode == "once":
                context.job_queue.run_once(
                    send_reminder,
                    when=minutes * 60,
                    data=task,
                    chat_id=update.effective_chat.id
                )
                await update.message.reply_text(f"⏰ Reminder set in {minutes} minutes")
                return

            if mode == "repeat":
                context.job_queue.run_repeating(
                    send_reminder,
                    interval=minutes * 60,
                    first=5,
                    data=task,
                    chat_id=update.effective_chat.id
                )
                await update.message.reply_text(f"🔁 Reminder every {minutes} minutes")
                return

        await update.message.reply_text("❌ Use: remind me in 1 minute to ...")
        return

    # 🔹 2. AI CHAT (SAFE VERSION)
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": text}],
            },
            timeout=20,
        )

        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI ERROR:", e)
        reply = "⚠️ AI is temporarily unavailable"

    await update.message.reply_text(reply)


# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    app.run_polling()


if __name__ == "__main__":
    main()
