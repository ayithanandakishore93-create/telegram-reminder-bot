import os
import logging
import sqlite3
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==== Configuration ====
BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
MODEL = os.getenv("AI_MODEL", "meta-llama/llama-3.1-8b-instruct:free")

# Timezone (default IST)
TZ = ZoneInfo("Asia/Kolkata")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==== Database Setup ====
conn = sqlite3.connect("tasks.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    time TEXT NOT NULL,
    repeat INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending'
)
""")
conn.commit()

# ==== Scheduler Setup ====
scheduler = AsyncIOScheduler(timezone=TZ)
scheduler.start()

# ==== Helper Functions ====
def parse_natural_time(text: str):
    """Simple regex parsing for common time expressions."""
    now = datetime.now(TZ)
    text_low = text.lower().strip()

    # "in X minutes"
    m = re.match(r".*in (\d+) (minute|min|hour|hr)", text_low)
    if m:
        num = int(m.group(1))
        if "hour" in m.group(2):
            run_dt = now + timedelta(hours=num)
        else:
            run_dt = now + timedelta(minutes=num)
        return {"is_reminder": True, "task": re.sub(r"remind me in \d+ (minutes|min|hours|hrs) to ", "", text_low).strip(), 
                "minutes": num * (60 if "hour" in m.group(2) else 1), "repeat": 0}

    # "every X minutes"
    m = re.match(r".*every (\d+) (minute|min|hour|hr)", text_low)
    if m:
        num = int(m.group(1))
        if "hour" in m.group(2):
            interval = num * 60
        else:
            interval = num
        task_text = re.sub(r"remind me every \d+ (minutes|min|hours|hrs) to ", "", text_low).strip()
        run_dt = now + timedelta(minutes=interval)
        return {"is_reminder": True, "task": task_text, "minutes": 0, "repeat": interval}

    # "tomorrow at HH:MM am/pm"
    m = re.match(r".*tomorrow at (\d{1,2})(?::(\d{2}))?\s*(am|pm)? to (.+)", text_low)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        task = m.group(4).strip()
        if ampm == "pm" and hour != 12: hour += 12
        if ampm == "am" and hour == 12: hour = 0
        run_dt = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return {"is_reminder": True, "task": task, "minutes": int((run_dt - now).total_seconds()//60), "repeat": 0}

    # "at HH:MM am/pm"
    m = re.match(r".* at (\d{1,2})(?::(\d{2}))?\s*(am|pm)? to (.+)", text_low)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        task = m.group(4).strip()
        if ampm == "pm" and hour != 12: hour += 12
        if ampm == "am" and hour == 12: hour = 0
        run_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_dt <= now:
            run_dt += timedelta(days=1)
        return {"is_reminder": True, "task": task, "minutes": int((run_dt - now).total_seconds()//60), "repeat": 0}

    return None

def parse_with_ai(text: str):
    """Use OpenRouter to parse the text into our JSON format."""
    prompt = f"""
Convert into JSON:
\"{text}\"
Format:
{{"is_reminder": true/false, "task": "...", "minutes": number, "repeat": number}}
"""
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}]},
            timeout=20
        )
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        logger.error("OpenRouter parse error: %s", e)
        return {"is_reminder": False}

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Callback to send the reminder message."""
    job = context.job
    await context.bot.send_message(chat_id=job.chat_id, text=f"⏰ Reminder: {job.data}")

# ==== Bot Handlers ====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *ReminderBot*: I understand natural-language reminders.\n\n"
        "Examples:\n"
        "`/start` - show this message\n"
        "`/list` - list your reminders\n"
        "`/delete <id>` - delete a reminder\n"
        "`/clear` - clear all reminders\n\n"
        "📥 Say things like:\n"
        "`Remind me in 5 minutes to check email`\n"
        "`Remind me every 10 minutes to drink water`\n"
        "`Remind me tomorrow at 9am to study`\n"
        "`Remind me at 11:30 pm to sleep`",
        parse_mode="Markdown"
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = cursor.execute(
        "SELECT id, text, time, repeat FROM reminders WHERE chat_id=? AND status='pending'", (chat_id,)
    ).fetchall()
    if not rows:
        await update.message.reply_text("You have no pending reminders.")
        return
    msg = ["*Your pending reminders:*"]
    for rid, text, timestr, rpt in rows:
        dt = datetime.fromisoformat(timestr).astimezone(TZ)
        if rpt:
            msg.append(f"`{rid}`: {text} — every {rpt} min, first at {dt.strftime('%I:%M %p')}")
        else:
            msg.append(f"`{rid}`: {text} — at {dt.strftime('%I:%M %p')}")
    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/delete <id>`")
        return
    try:
        rid = int(args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return
    row = cursor.execute("SELECT chat_id FROM reminders WHERE id=?", (rid,)).fetchone()
    if not row or row[0] != update.effective_chat.id:
        await update.message.reply_text("Reminder not found.")
        return
    cursor.execute("UPDATE reminders SET status='done' WHERE id=?", (rid,))
    conn.commit()
    job = scheduler.get_job(str(rid))
    if job: scheduler.remove_job(job.id)
    await update.message.reply_text(f"Deleted reminder `{rid}`.", parse_mode="Markdown")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = cursor.execute("SELECT id FROM reminders WHERE chat_id=? AND status='pending'", (chat_id,)).fetchall()
    if not rows:
        await update.message.reply_text("No reminders to clear.")
        return
    for (rid,) in rows:
        scheduler.remove_job(str(rid))
    cursor.execute("UPDATE reminders SET status='done' WHERE chat_id=? AND status='pending'", (chat_id,))
    conn.commit()
    await update.message.reply_text("All pending reminders cleared.")

async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Try regex parsing first
    parsed = parse_natural_time(text)
    if not parsed:
        # Fallback to AI parser
        parsed = parse_with_ai(text)
    if parsed.get("is_reminder"):
        task = parsed.get("task", "").strip()
        minutes = int(parsed.get("minutes", 0))
        repeat = int(parsed.get("repeat", 0))
        if not task or (minutes==0 and repeat==0):
            await update.message.reply_text("Could not parse reminder details.")
            return
        now = datetime.now(TZ)
        if repeat > 0:
            first_run = now + timedelta(minutes=minutes or repeat)
        else:
            first_run = now + timedelta(minutes=minutes)
        # Store in DB
        cursor.execute(
            "INSERT INTO reminders (chat_id, text, time, repeat) VALUES (?, ?, ?, ?)",
            (update.effective_chat.id, task, first_run.isoformat(), repeat)
        )
        conn.commit()
        rid = cursor.lastrowid
        # Schedule job
        if repeat > 0:
            scheduler.add_job(
                send_reminder, 'interval', minutes=repeat,
                start_date=first_run, args=[context], kwargs={"job": None},
                id=str(rid)
            )
            await update.message.reply_text(f"🔁 Every {repeat} min: *{task}*", parse_mode="Markdown")
        else:
            scheduler.add_job(
                send_reminder, 'date',
                run_date=first_run, args=[context], kwargs={"job": None},
                id=str(rid)
            )
            await update.message.reply_text(f"⏰ At {first_run.strftime('%I:%M %p')}: *{task}*", parse_mode="Markdown")
        return
    # Not a reminder -> general chat via OpenRouter
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": [{"role": "user", "content": text}]},
            timeout=20
        )
        reply = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("OpenRouter chat error: %s", e)
        reply = "⚠️ Sorry, I couldn't process that."
    await update.message.reply_text(reply)

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎤 Voice received. (Voice-to-text not supported yet.)")

# ==== Startup: Reschedule Pending ====
async def on_startup(application):
    rows = cursor.execute("SELECT id, text, time, repeat FROM reminders WHERE status='pending'").fetchall()
    for rid, text, timestr, rpt in rows:
        run_dt = datetime.fromisoformat(timestr)
        if rpt:
            scheduler.add_job(send_reminder, 'interval', minutes=rpt, start_date=run_dt, args=[application], kwargs={"job": None}, id=str(rid))
        else:
            scheduler.add_job(send_reminder, 'date', run_date=run_dt, args=[application], kwargs={"job": None}, id=str(rid))

# ==== Main ====
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
