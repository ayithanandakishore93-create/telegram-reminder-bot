import os
import json
import re
import base64
import asyncio
import sqlite3
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from threading import Lock

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]

AI_MODEL = os.getenv("AI_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
STT_MODEL = os.getenv("STT_MODEL", "openai/whisper-large-v3")
DB_PATH = os.getenv("DB_PATH", "tasks.db")

IST = ZoneInfo("Asia/Kolkata")
db_lock = Lock()

# =========================
# DATABASE
# =========================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

with db_lock:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            repeat_minutes INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()

# =========================
# SCHEDULER
# =========================
scheduler = AsyncIOScheduler(timezone=IST)


# =========================
# HELPERS
# =========================
def now_ist() -> datetime:
    return datetime.now(IST)


def fmt_ist(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%b %d, %I:%M %p")


def db_execute(query: str, params=()):
    with db_lock:
        cursor.execute(query, params)
        conn.commit()
        return cursor


def db_fetchone(query: str, params=()):
    with db_lock:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchone()


def db_fetchall(query: str, params=()):
    with db_lock:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()


def clean_json_text(text: str) -> str:
    text = text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0).strip() if match else text


def parse_dt_local(dt_str: str) -> datetime:
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=IST)


# =========================
# OPENROUTER CHAT
# =========================
def openrouter_chat_sync(user_text: str) -> str:
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a concise, helpful assistant for a Telegram bot.",
                },
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.7,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


async def openrouter_chat(user_text: str) -> str:
    return await asyncio.to_thread(openrouter_chat_sync, user_text)


# =========================
# OPENROUTER TRANSCRIPTION
# =========================
def transcribe_voice_sync(file_path: str) -> str:
    with open(file_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")

    resp = requests.post(
        "https://openrouter.ai/api/v1/audio/transcriptions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": STT_MODEL,
            "input_audio": {
                "data": audio_b64,
                "format": "ogg",
            },
            "language": "en",
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["text"].strip()


async def transcribe_voice(file_path: str) -> str:
    return await asyncio.to_thread(transcribe_voice_sync, file_path)


# =========================
# REMINDER PARSER
# =========================
def parse_reminder_regex(text: str):
    """
    Fallback parser for common reminder formats.
    Returns dict or None.
    """
    low = text.lower().strip()
    now = now_ist()

    # remind me in 10 minutes to drink water
    m = re.match(r"remind me in (\d+)\s+(minute|minutes|hour|hours)\s+to\s+(.+)", low)
    if m:
        qty = int(m.group(1))
        unit = m.group(2)
        task = m.group(3).strip()
        minutes = qty * 60 if "hour" in unit else qty
        remind_at = now + timedelta(minutes=minutes)
        return {
            "is_reminder": True,
            "task": task,
            "remind_at": remind_at.strftime("%Y-%m-%d %H:%M"),
            "repeat_minutes": 0,
        }

    # remind me every 10 minutes to drink water
    m = re.match(r"remind me every (\d+)\s+(minute|minutes|hour|hours)\s+to\s+(.+)", low)
    if m:
        qty = int(m.group(1))
        unit = m.group(2)
        task = m.group(3).strip()
        minutes = qty * 60 if "hour" in unit else qty
        remind_at = now + timedelta(minutes=minutes)
        return {
            "is_reminder": True,
            "task": task,
            "remind_at": remind_at.strftime("%Y-%m-%d %H:%M"),
            "repeat_minutes": minutes,
        }

    # remind me tomorrow at 9am to practice communication
    m = re.match(
        r"remind me tomorrow(?: at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+to\s+(.+)",
        low,
    )
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        task = m.group(4).strip()

        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0

        target = (now + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return {
            "is_reminder": True,
            "task": task,
            "remind_at": target.strftime("%Y-%m-%d %H:%M"),
            "repeat_minutes": 0,
        }

    # remind me at 11:20 am to drink water
    m = re.match(
        r"remind me at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s+to\s+(.+)",
        low,
    )
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        task = m.group(4).strip()

        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        return {
            "is_reminder": True,
            "task": task,
            "remind_at": target.strftime("%Y-%m-%d %H:%M"),
            "repeat_minutes": 0,
        }

    return None


def parse_reminder_ai_sync(text: str):
    current = now_ist().strftime("%Y-%m-%d %H:%M")

    prompt = f"""
You are a reminder parser for a Telegram bot.

Current date/time in IST: {current}

If the user's message is a reminder request, return ONLY valid JSON in this exact format:
{{
  "is_reminder": true,
  "task": "short clear task",
  "remind_at": "YYYY-MM-DD HH:MM",
  "repeat_minutes": 0
}}

Rules:
- If the reminder repeats every X minutes/hours, set repeat_minutes to that number in minutes.
- For repeating reminders, still set remind_at to the first reminder time.
- If the user says "in 10 minutes", set remind_at to current time + 10 minutes.
- If the user says "tomorrow at 9am", set remind_at to tomorrow 09:00 IST.
- If the message is NOT a reminder, return:
{{"is_reminder": false}}

User message:
{text}
""".strip()

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "Return only JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["choices"][0]["message"]["content"].strip()
    raw = clean_json_text(raw)
    return json.loads(raw)


async def parse_reminder(text: str):
    # regex fallback first for common formats
    regex_result = parse_reminder_regex(text)
    if regex_result:
        return regex_result

    # AI parser for natural language
    try:
        ai_result = await asyncio.to_thread(parse_reminder_ai_sync, text)
        if isinstance(ai_result, dict) and ai_result.get("is_reminder"):
            return ai_result
    except Exception:
        pass

    return None


# =========================
# REMINDER STORAGE + SCHEDULING
# =========================
def create_reminder_row(chat_id: int, task: str, remind_at: str, repeat_minutes: int) -> int:
    cur = db_execute(
        """
        INSERT INTO reminders (chat_id, task, remind_at, repeat_minutes, status)
        VALUES (?, ?, ?, ?, 'pending')
        """,
        (chat_id, task, remind_at, repeat_minutes),
    )
    return cur.lastrowid


def schedule_reminder(application, reminder_id: int, remind_at: str, repeat_minutes: int):
    job_id = f"reminder_{reminder_id}"
    run_dt = parse_dt_local(remind_at)

    now = now_ist()
    if repeat_minutes > 0:
        if run_dt <= now:
            run_dt = now + timedelta(minutes=repeat_minutes)

        scheduler.add_job(
            fire_reminder,
            trigger="interval",
            minutes=repeat_minutes,
            start_date=run_dt,
            id=job_id,
            replace_existing=True,
            args=[application.bot, reminder_id],
            coalesce=True,
            misfire_grace_time=60,
        )
    else:
        if run_dt <= now:
            run_dt = now + timedelta(minutes=1)

        scheduler.add_job(
            fire_reminder,
            trigger="date",
            run_date=run_dt,
            id=job_id,
            replace_existing=True,
            args=[application.bot, reminder_id],
            misfire_grace_time=60,
        )


async def fire_reminder(bot, reminder_id: int):
    row = db_fetchone(
        "SELECT chat_id, task, repeat_minutes, status FROM reminders WHERE id=?",
        (reminder_id,),
    )
    if not row:
        return

    chat_id, task, repeat_minutes, status = row
    if status != "pending":
        return

    await bot.send_message(chat_id=chat_id, text=f"⏰ Reminder: {task}")

    # One-time reminders get marked done after firing.
    if int(repeat_minutes or 0) == 0:
        db_execute("UPDATE reminders SET status='done' WHERE id=?", (reminder_id,))


async def reschedule_pending(application):
    rows = db_fetchall(
        """
        SELECT id, remind_at, repeat_minutes
        FROM reminders
        WHERE status='pending'
        """
    )
    for reminder_id, remind_at, repeat_minutes in rows:
        try:
            schedule_reminder(application, reminder_id, remind_at, int(repeat_minutes or 0))
        except Exception:
            pass


# =========================
# MESSAGE FLOW
# =========================
async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    parsed = await parse_reminder(text)

    if parsed and parsed.get("is_reminder"):
        task = (parsed.get("task") or "").strip()
        remind_at = parsed.get("remind_at")
        repeat_minutes = int(parsed.get("repeat_minutes") or 0)

        if not task or not remind_at:
            await update.message.reply_text("❌ I couldn't understand the reminder time.")
            return

        # Normalize time
        try:
            remind_dt = parse_dt_local(remind_at)
        except Exception:
            await update.message.reply_text("❌ Invalid reminder time.")
            return

        # Save to DB
        reminder_id = create_reminder_row(
            chat_id=update.effective_chat.id,
            task=task,
            remind_at=remind_dt.strftime("%Y-%m-%d %H:%M"),
            repeat_minutes=repeat_minutes,
        )

        # Schedule job
        schedule_reminder(context.application, reminder_id, remind_dt.strftime("%Y-%m-%d %H:%M"), repeat_minutes)

        if repeat_minutes > 0:
            await update.message.reply_text(
                f"🔁 Reminder set every {repeat_minutes} minutes:\n\n{task}\nFirst at {fmt_ist(remind_dt)}"
            )
        else:
            await update.message.reply_text(
                f"⏰ Reminder set:\n\n{task}\nAt {fmt_ist(remind_dt)}"
            )
        return

    # Normal AI chat
    try:
        reply = await openrouter_chat(text)
        await update.message.reply_text(reply)
    except Exception:
        await update.message.reply_text("❌ AI error. Try again.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    await process_text(update, context, update.message.text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return

    status = await update.message.reply_text("🎤 Transcribing...")

    tg_file = await update.message.voice.get_file()
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        await tg_file.download_to_drive(tmp_path)
        transcript = await transcribe_voice(tmp_path)

        await status.edit_text(f'Heard: "{transcript}"\n\nProcessing...')
        await process_text(update, context, transcript)

    except Exception:
        await status.edit_text("❌ Voice failed to transcribe.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi. Send me a reminder or a question.\n\n"
        "Examples:\n"
        "- remind me in 10 minutes to drink water\n"
        "- remind me every 10 minutes to stand up\n"
        "- remind me tomorrow at 9am to practice communication\n"
        "- send a voice note"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = db_fetchall(
        """
        SELECT id, task, remind_at, repeat_minutes
        FROM reminders
        WHERE chat_id=? AND status='pending'
        ORDER BY remind_at ASC
        """,
        (chat_id,),
    )

    if not rows:
        await update.message.reply_text("No pending reminders.")
        return

    lines = ["Your pending reminders:\n"]
    for rid, task, remind_at, repeat_minutes in rows:
        dt = parse_dt_local(remind_at)
        if int(repeat_minutes or 0) > 0:
            lines.append(f"{rid}. {task}\n   every {repeat_minutes} min, first at {fmt_ist(dt)}")
        else:
            lines.append(f"{rid}. {task}\n   at {fmt_ist(dt)}")

    await update.message.reply_text("\n".join(lines))


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = db_fetchall(
        "SELECT id FROM reminders WHERE chat_id=? AND status='pending'",
        (chat_id,),
    )

    with db_lock:
        cursor.execute(
            "UPDATE reminders SET status='done' WHERE chat_id=? AND status='pending'",
            (chat_id,),
        )
        conn.commit()

    for (rid,) in rows:
        job = scheduler.get_job(f"reminder_{rid}")
        if job:
            scheduler.remove_job(job.id)

    await update.message.reply_text("Cleared all pending reminders.")


# =========================
# STARTUP / SHUTDOWN
# =========================
async def post_init(application):
    try:
        scheduler.start()
    except Exception:
        pass
    await reschedule_pending(application)


async def post_shutdown(application):
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


# =========================
# MAIN
# =========================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
