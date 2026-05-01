import os
import re
import requests
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]

# ✅ USE STABLE MODEL (no :free)
MODEL = "meta-llama/llama-3-8b-instruct"


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


# ================= AI CALL =================

def ask_ai(text):
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",

                # 🔥 REQUIRED FOR OPENROUTER (THIS FIXES YOUR ISSUE)
                "HTTP-Referer": "https://example.com",
                "X-Title": "JarvisBot"
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "user", "content": text}
                ],
            },
            timeout=30,
        )

        print("STATUS:", r.status_code)
        print("RAW:", r.text)

        if r.status_code != 200:
            return "⚠️ AI server error. Try again."

        data = r.json()

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI ERROR:", e)
        return "⚠️ AI temporarily unavailable"


# ================= MAIN HANDLER =================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # 🔹 1. REMINDER (NO AI)
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

        await update.message.reply_text("❌ Try: remind me in 1 minute to call mom")
        return

    # 🔹 2. AI CHAT
    reply = ask_ai(text)
    await update.message.reply_text(reply)


# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    app.run_polling()


if __name__ == "__main__":
    main()
