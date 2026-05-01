import os
import re
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import dateparser
from dateparser.search import search_dates
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-4o-mini")
DB_PATH = os.getenv("DB_PATH", "/data/tasks.db")
TZ = ZoneInfo("Asia/Kolkata")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    task TEXT NOT NULL,
    trigger_time TEXT NOT NULL,
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


def strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.I).strip()
    text = re.sub(r"```$", "", text.strip()).strip()
    return text


def parse_date_phrase(phrase: str) -> datetime | None:
    if not phrase:
        return None

    settings = {
        "TIMEZONE": "Asia/Kolkata",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now_ist(),
    }

    dt = dateparser.parse(phrase, settings=settings, languages=["en"])
    if dt:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)

    found = search_dates(phrase, settings=settings, languages=["en"])
    if found:
        _, dt = found[0]
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)

    return None


def parse_local_reminder(text: str):
    """
    Fast local parser for obvious reminder phrases.
    This is the reliable fallback when AI does not need to be used.
    """
    t = clean_text(text)

    patterns = [
        # remind me in 2 hours to study
        r"remind me in (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)\s*(?:to\s+)?(.+)",
        # remind me to study in 2 hours
        r"remind me(?: to)? (.+?) in (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)",
        # remind me every 10 minutes to drink water
        r"remind me every (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)\s+to\s+(.+)",
        # remind me to drink water every 10 minutes
        r"remind me(?: to)? (.+?) every (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)",
        # remind me at 11:30 am to call mom
        r"remind me at (\d{1,2})(?::(\d{2}))?\s*(am|pm)\s+to\s+(.+)",
        # remind me to call mom at 11:30 am
        r"remind me(?: to)? (.+?) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        # remind me tomorrow at 9am to study
        r"remind me tomorrow(?: at)? (\d{1,2})(?::(\d{2}))?\s*(am|pm)\s+to\s+(.+)",
        # remind me to study tomorrow at 9am
        r"remind me(?: to)? (.+?) tomorrow(?: at)? (\d{1,2})(?::(\d{2}))?\s*(am|pm)",
    ]

    for idx, pattern in enumerate(patterns):
        m = re.fullmatch(pattern, t)
        if not m:
            continue

        g = m.groups()

        # in X time
        if idx == 0:
            amount = int(g[0])
            unit = g[1]
            task = g[2].strip()
            minutes = amount * 60 if "hour" in unit or unit in ("hr", "hrs") else amount
            return {"task": task, "when_text": f"in {minutes} minutes", "repeat_minutes": 0}

        # to TASK in X time
        if idx == 1:
            task = g[0].strip()
            amount = int(g[1])
            unit = g[2]
            minutes = amount * 60 if "hour" in unit or unit in ("hr", "hrs") else amount
            return {"task": task, "when_text": f"in {minutes} minutes", "repeat_minutes": 0}

        # every X time to TASK
        if idx == 2:
            amount = int(g[0])
            unit = g[1]
            task = g[2].strip()
            minutes = amount * 60 if "hour" in unit or unit in ("hr", "hrs") else amount
            return {"task": task, "when_text": f"in {minutes} minutes", "repeat_minutes": minutes}

        # to TASK every X time
        if idx == 3:
            task = g[0].strip()
            amount = int(g[1])
            unit = g[2]
            minutes = amount * 60 if "hour" in unit or unit in ("hr", "hrs") else amount
            return {"task": task, "when_text": f"in {minutes} minutes", "repeat_minutes": minutes}

        # at TIME to TASK
        if idx == 4:
            hour = int(g[0])
            minute = int(g[1] or 0)
            ampm = g[2]
            task = g[3].strip()
            when_text = f"today at {hour}:{minute:02d} {ampm}"
            return {"task": task, "when_text": when_text, "repeat_minutes": 0}

        # to TASK at TIME
        if idx == 5:
            task = g[0].strip()
            hour = int(g[1])
            minute = int(g[2] or 0)
            ampm = g[3]
            when_text = f"today at {hour}:{minute:02d} {ampm}"
            return {"task": task, "when_text": when_text, "repeat_minutes": 0}

        # tomorrow at TIME to TASK
        if idx == 6:
            hour = int(g[0])
            minute = int(g[1] or 0)
            ampm = g[2]
            task = g[3].strip()
            when_text = f"tomorrow at {hour}:{minute:02d} {ampm}"
            return {"task": task, "when_text": when_text, "repeat_minutes": 0}

        # to TASK tomorrow at TIME
        if idx == 7:
            task = g[0].strip()
            hour = int(g[1])
            minute = int(g[2] or 0)
            ampm = g[3]
            when_text = f"tomorrow at {hour}:{minute:02d} {ampm}"
            return {"task": task, "when_text": when_text, "repeat_minutes": 0}

    return None


def openrouter_json(prompt: str) -> dict | None:
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://example.com",
                "X-Title": "JarvisBot",
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
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            raw = match.group(0)
        return json.loads(raw)
    except Exception as e:
        print("ROUTER JSON ERROR:", e)
        return None


def ai_route(text: str) -> dict:
    prompt = f"""
Classify the user message and return only JSON.

Message:
{text}

Return one of these:

1) Reminder:
{{
  "intent": "reminder",
  "task": "short task text",
  "when_text": "natural language time phrase",
  "repeat_minutes": number
}}

2) Chat:
{{
  "intent": "chat",
  "reply": "assistant answer"
}}

Rules:
- If the user wants a reminder, choose intent reminder.
- task should be short and clean.
- when_text should be the time phrase the bot can parse, like:
  "in 2 hours", "tomorrow at 9am", "next friday at 6pm", "every day at 8pm"
- repeat_minutes:
  - 0 for one-time reminders
  - 5, 10, 30, 60, 1440 for repeated reminders when the user means every N minutes/hours or every day
- If it is not a reminder, intent must be chat.
- If it is chat, reply with a natural answer.
"""
    return openrouter_json(prompt) or {"intent": "chat", "reply": "⚠️ AI temporarily unavailable."}


def ai_chat(text: str) -> str:
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://example.com",
                "X-Title": "JarvisBot",
            },
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": "Be concise, natural, and helpful."},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.7,
            },
            timeout=25,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("AI CHAT ERROR:", e)
        return "⚠️ AI temporarily unavailable."


def save_reminder(chat_id: int, task: str, trigger_time: datetime, repeat_minutes: int) -> int:
    cursor.execute(
        """
        INSERT INTO reminders (chat_id, task, trigger_time, repeat_minutes, status)
        VALUES (?, ?, ?, ?, 'pending')
        """,
        (chat_id, task, trigger_time.isoformat(), repeat_minutes),
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


def schedule_reminder(app, reminder_id: int, chat_id: int, task: str, trigger_time: datetime, repeat_minutes: int):
    job_name = f"reminder_{reminder_id}"
    data = {"reminder_id": reminder_id, "task": task, "repeat_minutes": repeat_minutes}

    now = now_ist()
    if repeat_minutes > 0:
        first = trigger_time if trigger_time > now else now + timedelta(minutes=repeat_minutes)
        app.job_queue.run_repeating(
            send_reminder,
            interval=repeat_minutes * 60,
            first=first,
            chat_id=chat_id,
            data=data,
            name=job_name,
        )
    else:
        when = trigger_time if trigger_time > now else now + timedelta(seconds=10)
        app.job_queue.run_once(
            send_reminder,
            when=when,
            chat_id=chat_id,
            data=data,
            name=job_name,
        )


def schedule_existing_jobs(app):
    rows = cursor.execute(
        "SELECT id, chat_id, task, trigger_time, repeat_minutes FROM reminders WHERE status='pending'"
    ).fetchall()

    now = now_ist()
    for rid, chat_id, task, trigger_time_str, repeat_minutes in rows:
        try:
            trigger_time = datetime.fromisoformat(trigger_time_str)
            if trigger_time.tzinfo is None:
                trigger_time = trigger_time.replace(tzinfo=TZ)

            if repeat_minutes > 0:
                if trigger_time <= now:
                    trigger_time = now + timedelta(minutes=int(repeat_minutes))
            else:
                if trigger_time <= now:
                    # overdue one-time reminders get sent shortly after restart
                    trigger_time = now + timedelta(seconds=10)

            schedule_reminder(app, rid, chat_id, task, trigger_time, int(repeat_minutes or 0))
        except Exception as e:
            print(f"RESCHEDULE ERROR for {rid}: {e}")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi. I can chat and set smart reminders.\n\n"
        "Try:\n"
        "- remind me in 2 hours to study\n"
        "- remind me tomorrow at 9am to call mom\n"
        "- remind me every day at 8pm to read\n"
        "- remind me after lunch to drink water\n\n"
        "Commands:\n"
        "/list\n"
        "/delete <id>\n"
        "/clear"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = cursor.execute(
        """
        SELECT id, task, trigger_time, repeat_minutes
        FROM reminders
        WHERE chat_id=? AND status='pending'
        ORDER BY trigger_time ASC
        """,
        (chat_id,),
    ).fetchall()

    if not rows:
        await update.message.reply_text("No pending reminders.")
        return

    lines = ["Your pending reminders:"]
    for rid, task, trigger_time_str, repeat_minutes in rows:
        dt = datetime.fromisoformat(trigger_time_str).astimezone(TZ)
        if int(repeat_minutes or 0) > 0:
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

    # 1) Local parser first for speed and reliability
    local = parse_local_reminder(text)
    if local:
        task = local["task"]
        repeat_minutes = int(local["repeat_minutes"])
        when_text = local["when_text"]
        trigger_time = parse_date_phrase(when_text)

        if not trigger_time:
            await update.message.reply_text("I could not understand the reminder time. Try a clearer time.")
            return

        rid = save_reminder(update.effective_chat.id, task, trigger_time, repeat_minutes)
        schedule_reminder(context.application, rid, update.effective_chat.id, task, trigger_time, repeat_minutes)

        if repeat_minutes > 0:
            await update.message.reply_text(f"🔁 Reminder set every {repeat_minutes} minutes:\n{task}")
        else:
            await update.message.reply_text(
                f"⏰ Reminder set:\n{task}\nAt {trigger_time.strftime('%b %d, %I:%M %p')}"
            )
        return

    # 2) AI router for natural language
    routed = ai_route(text)

    if routed.get("intent") == "reminder":
        task = (routed.get("task") or "").strip()
        when_text = (routed.get("when_text") or "").strip()
        repeat_minutes = int(routed.get("repeat_minutes") or 0)

        trigger_time = parse_date_phrase(when_text or text)

        if not trigger_time:
            # last fallback: try to infer a near-future time for repeating reminders
            if repeat_minutes > 0:
                trigger_time = now_ist() + timedelta(minutes=repeat_minutes)
            else:
                await update.message.reply_text(
                    "I understood it as a reminder, but the time is still unclear. Try adding a time like 'tomorrow at 9am'."
                )
                return

        if not task:
            task = text.replace("remind me", "").strip() or "Reminder"

        rid = save_reminder(update.effective_chat.id, task, trigger_time, repeat_minutes)
        schedule_reminder(context.application, rid, update.effective_chat.id, task, trigger_time, repeat_minutes)

        if repeat_minutes > 0:
            await update.message.reply_text(
                f"🔁 Reminder set every {repeat_minutes} minutes:\n{task}"
            )
        else:
            await update.message.reply_text(
                f"⏰ Reminder set:\n{task}\nAt {trigger_time.strftime('%b %d, %I:%M %p')}"
            )
        return

    # 3) Normal chat
    if routed.get("intent") == "chat" and routed.get("reply"):
        await update.message.reply_text(routed["reply"])
        return

    reply = ai_chat(text)
    await update.message.reply_text(reply)


async def post_init(app):
    schedule_existing_jobs(app)


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

    print("🚀 Smart reminder bot running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
