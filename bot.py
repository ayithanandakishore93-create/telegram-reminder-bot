import os
import re
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
AI_MODEL = os.getenv("AI_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
DB_PATH = os.getenv("DB_PATH", "tasks.db")
TZ = ZoneInfo("Asia/Kolkata")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    task TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    repeat_minutes INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()


def now_ist() -> datetime:
    return datetime.now(TZ)


def clean_text(text: str) -> str:
    return " ".join(text.strip().split()).lower().replace(".", "")


def to_24h(hour: int, minute: int, ampm: str | None) -> tuple[int, int]:
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    return hour, minute


def next_datetime(hour: int, minute: int, tomorrow: bool = False) -> datetime:
    now = now_ist()
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tomorrow:
        dt += timedelta(days=1)
    elif dt <= now:
        dt += timedelta(days=1)
    return dt


def parse_reminder(text: str):
    t = clean_text(text)

    # remind me in 1 minute to drink water
    m = re.fullmatch(
        r"remind me in (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)\s*(?:to\s+)?(.+)",
        t,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        task = m.group(3).strip()
        minutes = n * 60 if "hour" in unit or unit in ("hr", "hrs") else n
        remind_at = now_ist() + timedelta(minutes=minutes)
        return {"task": task, "remind_at": remind_at, "repeat_minutes": 0}

    # remind me to drink water in 1 minute
    m = re.fullmatch(
        r"remind me(?: to)? (.+?) in (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)",
        t,
    )
    if m:
        task = m.group(1).strip()
        n = int(m.group(2))
        unit = m.group(3)
        minutes = n * 60 if "hour" in unit or unit in ("hr", "hrs") else n
        remind_at = now_ist() + timedelta(minutes=minutes)
        return {"task": task, "remind_at": remind_at, "repeat_minutes": 0}

    # remind me every 10 minutes to drink water
    m = re.fullmatch(
        r"remind me every (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)\s+to\s+(.+)",
        t,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        task = m.group(3).strip()
        repeat_minutes = n * 60 if "hour" in unit or unit in ("hr", "hrs") else n
        remind_at = now_ist() + timedelta(minutes=repeat_minutes)
        return {"task": task, "remind_at": remind_at, "repeat_minutes": repeat_minutes}

    # remind me to drink water every 10 minutes
    m = re.fullmatch(
        r"remind me(?: to)? (.+?) every (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)",
        t,
    )
    if m:
        task = m.group(1).strip()
        n = int(m.group(2))
        unit = m.group(3)
        repeat_minutes = n * 60 if "hour" in unit or unit in ("hr", "hrs") else n
        remind_at = now_ist() + timedelta(minutes=repeat_minutes)
        return {"task": task, "remind_at": remind_at, "repeat_minutes": repeat_minutes}

    # remind me at 11:30 am to call mom
    m = re.fullmatch(
        r"remind me at (\d{1,2})(?::(\d{2}))?\s*(am|pm)\s+to\s+(.+)",
        t,
    )
    if m:
        hour, minute = to_24h(int(m.group(1)), int(m.group(2) or 0), m.group(3))
        task = m.group(4).strip()
        remind_at = next_datetime(hour, minute)
        return {"task": task, "remind_at": remind_at, "repeat_minutes": 0}

    # remind me to call mom at 11:30 am
    m = re.fullmatch(
        r"remind me(?: to)? (.+?) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        t,
    )
    if m:
        task = m.group(1).strip()
        hour, minute = to_24h(int(m.group(2)), int(m.group(3) or 0), m.group(4))
        remind_at = next_datetime(hour, minute)
        return {"task": task, "remind_at": remind_at, "repeat_minutes": 0}

    # remind me tomorrow at 9am to practice communication
    m = re.fullmatch(
        r"remind me tomorrow(?: at)? (\d{1,2})(?::(\d{2}))?\s*(am|pm)\s+to\s+(.+)",
        t,
    )
    if m:
        hour, minute = to_24h(int(m.group(1)), int(m.group(2) or 0), m.group(3))
        task = m.group(4).strip()
        remind_at = next_datetime(hour, minute, tomorrow=True)
        return {"task": task, "remind_at": remind_at, "repeat_minutes": 0}

    # remind me to practice communication tomorrow at 9am
    m = re.fullmatch(
        r"remind me(?: to)? (.+?) tomorrow(?: at)? (\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        t,
    )
    if m:
        task = m.group(1).strip()
        hour, minute = to_24h(int(m.group(2)), int(m.group(3) or 0), m.group(4))
        remind_at = next_datetime(hour, minute, tomorrow=True)
        return {"task": task, "remind_at": remind_at, "repeat_minutes": 0}

    return None


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.I).strip()
        text = re.sub(r"```$", "", text.strip()).strip()
    return text


def openrouter_json(prompt: str) -> dict:
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "Return only valid JSON. No markdown."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        },
        timeout=25,
    )
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    raw = strip_code_fences(raw)
    return json.loads(raw)


def route_intent(text: str) -> dict:
    prompt = f"""
Classify this message and return only JSON.

Message:
{text}

Return one of these:

1) Reminder:
{{
  "intent": "reminder",
  "task": "short task text",
  "remind_at": "ISO-8601 datetime with timezone or null",
  "repeat_minutes": number
}}

2) Chat:
{{
  "intent": "chat",
  "reply": "assistant answer"
}}

Rules:
- If it is a reminder request, return intent reminder.
- If it is a general question, return intent chat.
- If you cannot parse reminder time confidently, still return intent reminder with remind_at null.
- Use Asia/Kolkata time.
"""
    return openrouter_json(prompt)


def ai_chat(text: str) -> str:
    prompt = f"You are a helpful Telegram assistant. Answer the user naturally.\n\nUser: {text}"
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "Be concise, helpful, and natural."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
        },
        timeout=25,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def save_reminder(chat_id: int, task: str, remind_at: datetime, repeat_minutes: int) -> int:
    cursor.execute(
        """
        INSERT INTO reminders (chat_id, task, remind_at, repeat_minutes, status)
        VALUES (?, ?, ?, ?, 'pending')
        """,
        (chat_id, task, remind_at.isoformat(), repeat_minutes),
    )
    conn.commit()
    return cursor.lastrowid


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    reminder_id = data.get("reminder_id")
    task = data.get("task", "Reminder")
    repeat_minutes = int(data.get("repeat_minutes", 0) or 0)

    await context.bot.send_message(chat_id=job.chat_id, text=f"⏰ Reminder: {task}")

    if reminder_id and repeat_minutes == 0:
        cursor.execute("UPDATE reminders SET status='done' WHERE id=?", (reminder_id,))
        conn.commit()


def schedule_reminder(app, reminder_id: int, chat_id: int, task: str, remind_at: datetime, repeat_minutes: int):
    data = {"reminder_id": reminder_id, "task": task, "repeat_minutes": repeat_minutes}
    job_name = f"reminder_{reminder_id}"

    if repeat_minutes > 0:
        app.job_queue.run_repeating(
            send_reminder,
            interval=repeat_minutes * 60,
            first=remind_at,
            chat_id=chat_id,
            data=data,
            name=job_name,
        )
    else:
        app.job_queue.run_once(
            send_reminder,
            when=remind_at,
            chat_id=chat_id,
            data=data,
            name=job_name,
        )


async def post_init(app):
    rows = cursor.execute(
        "SELECT id, chat_id, task, remind_at, repeat_minutes FROM reminders WHERE status='pending'"
    ).fetchall()

    now = now_ist()
    for rid, chat_id, task, remind_at_str, repeat_minutes in rows:
        try:
            remind_at = datetime.fromisoformat(remind_at_str)
            if repeat_minutes > 0 and remind_at <= now:
                remind_at = now + timedelta(minutes=int(repeat_minutes))
            elif repeat_minutes == 0 and remind_at <= now:
                remind_at = now + timedelta(seconds=10)
            schedule_reminder(app, rid, chat_id, task, remind_at, int(repeat_minutes or 0))
        except Exception as e:
            print(f"Startup reschedule error for reminder {rid}: {e}")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi. I can chat and make reminders.\n\n"
        "Examples:\n"
        "- remind me in 1 minute to drink water\n"
        "- remind me to call mom in 10 minutes\n"
        "- remind me every 10 minutes to stretch\n"
        "- remind me at 11:30 am to sleep\n"
        "- remind me tomorrow at 9am to study\n\n"
        "Commands:\n"
        "/list\n"
        "/delete <id>\n"
        "/clear"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = cursor.execute(
        """
        SELECT id, task, remind_at, repeat_minutes
        FROM reminders
        WHERE chat_id=? AND status='pending'
        ORDER BY remind_at ASC
        """,
        (chat_id,),
    ).fetchall()

    if not rows:
        await update.message.reply_text("No pending reminders.")
        return

    lines = ["Your pending reminders:"]
    for rid, task, remind_at_str, repeat_minutes in rows:
        dt = datetime.fromisoformat(remind_at_str).astimezone(TZ)
        if repeat_minutes and int(repeat_minutes) > 0:
            lines.append(f"{rid}. {task} — every {repeat_minutes} min, first at {dt.strftime('%b %d, %I:%M %p')}")
        else:
            lines.append(f"{rid}. {task} — at {dt.strftime('%b %d, %I:%M %p')}")

    await update.message.reply_text("\n".join(lines))


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <id>")
        return

    try:
        rid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return

    row = cursor.execute("SELECT chat_id FROM reminders WHERE id=?", (rid,)).fetchone()
    if not row or row[0] != update.effective_chat.id:
        await update.message.reply_text("Reminder not found.")
        return

    cursor.execute("UPDATE reminders SET status='done' WHERE id=?", (rid,))
    conn.commit()

    for job in context.application.job_queue.get_jobs_by_name(f"reminder_{rid}"):
        job.schedule_removal()

    await update.message.reply_text(f"Deleted reminder {rid}.")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = cursor.execute(
        "SELECT id FROM reminders WHERE chat_id=? AND status='pending'",
        (chat_id,),
    ).fetchall()

    if not rows:
        await update.message.reply_text("No reminders to clear.")
        return

    for (rid,) in rows:
        for job in context.application.job_queue.get_jobs_by_name(f"reminder_{rid}"):
            job.schedule_removal()

    cursor.execute("UPDATE reminders SET status='done' WHERE chat_id=? AND status='pending'", (chat_id,))
    conn.commit()

    await update.message.reply_text("All pending reminders cleared.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # 1) Fast local parser first
    parsed = parse_reminder(text)
    if parsed:
        task = parsed["task"]
        remind_at = parsed["remind_at"]
        repeat_minutes = int(parsed["repeat_minutes"])

        reminder_id = save_reminder(update.effective_chat.id, task, remind_at, repeat_minutes)
        schedule_reminder(context.application, reminder_id, update.effective_chat.id, task, remind_at, repeat_minutes)

        if repeat_minutes > 0:
            await update.message.reply_text(f"🔁 Reminder set every {repeat_minutes} minutes:\n{task}")
        else:
            await update.message.reply_text(
                f"⏰ Reminder set:\n{task}\nAt {remind_at.astimezone(TZ).strftime('%b %d, %I:%M %p')}"
            )
        return

    # 2) AI router
    try:
        route = route_intent(text)
    except Exception:
        route = {"intent": "chat"}

    if route.get("intent") == "reminder":
        task = (route.get("task") or "").strip()
        repeat_minutes = int(route.get("repeat_minutes") or 0)

        remind_at = route.get("remind_at")
        if remind_at:
            try:
                remind_dt = datetime.fromisoformat(remind_at)
                if remind_dt.tzinfo is None:
                    remind_dt = remind_dt.replace(tzinfo=TZ)
            except Exception:
                remind_dt = now_ist() + timedelta(minutes=1)
        else:
            remind_dt = now_ist() + timedelta(minutes=1)

        if not task:
            task = text.replace("remind me", "").strip() or "Reminder"

        reminder_id = save_reminder(update.effective_chat.id, task, remind_dt, repeat_minutes)
        schedule_reminder(context.application, reminder_id, update.effective_chat.id, task, remind_dt, repeat_minutes)

        if repeat_minutes > 0:
            await update.message.reply_text(f"🔁 Reminder set every {repeat_minutes} minutes:\n{task}")
        else:
            await update.message.reply_text(
                f"⏰ Reminder set:\n{task}\nAt {remind_dt.astimezone(TZ).strftime('%b %d, %I:%M %p')}"
            )
        return

    # 3) Chat
    try:
        reply = route.get("reply") if route.get("intent") == "chat" and route.get("reply") else ai_chat(text)
    except Exception:
        reply = "⚠️ AI error. Try again."

    await update.message.reply_text(reply)


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
