import os
import re
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import dateparser
from dateparser.search import search_dates

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-4o-mini")

DB_PATH = os.getenv("DB_PATH", "/data/jarvis.db")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "").strip()
CALENDAR_WEBHOOK_URL = os.getenv("CALENDAR_WEBHOOK_URL", "").strip()
OBSIDIAN_DIR = os.getenv("OBSIDIAN_DIR", "").strip()

TZ = ZoneInfo("Asia/Kolkata")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# =========================
# DB
# =========================
def ensure_tables():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ideas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        rule_key TEXT,
        task TEXT NOT NULL,
        trigger_time TEXT NOT NULL,
        repeat_minutes INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()


ensure_tables()

# =========================
# TIME HELPERS
# =========================
def now_ist() -> datetime:
    return datetime.now(TZ)


def normalize_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def next_time_today_or_tomorrow(hour: int, minute: int) -> datetime:
    dt = now_ist().replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt <= now_ist():
        dt += timedelta(days=1)
    return dt


def next_weekday_datetime(weekday: int, hour: int, minute: int) -> datetime:
    now = now_ist()
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - dt.weekday()) % 7
    if days_ahead == 0 and dt <= now:
        days_ahead = 7
    return dt + timedelta(days=days_ahead)


def parse_date_phrase(text: str) -> datetime | None:
    try:
        dt = dateparser.parse(
            text,
            settings={
                "TIMEZONE": "Asia/Kolkata",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": now_ist(),
            },
            languages=["en"],
        )
        if dt:
            return normalize_dt(dt)
    except Exception:
        pass

    try:
        found = search_dates(
            text,
            settings={
                "TIMEZONE": "Asia/Kolkata",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": now_ist(),
            },
            languages=["en"],
        )
        if found:
            _, dt = found[0]
            return normalize_dt(dt)
    except Exception:
        pass

    return None

# =========================
# OPTIONAL INTEGRATIONS
# =========================
def post_webhook(url: str, payload: dict):
    if not url:
        return
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Webhook error:", e)


def write_obsidian(folder: str, title: str, content: str):
    if not OBSIDIAN_DIR:
        return
    try:
        base = Path(OBSIDIAN_DIR) / folder
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9-_ ]+", "", title).strip().replace(" ", "_")
        if not safe:
            safe = "note"
        filename = f"{now_ist().strftime('%Y-%m-%d_%H-%M-%S')}_{safe}.md"
        (base / filename).write_text(content, encoding="utf-8")
    except Exception as e:
        print("Obsidian write error:", e)

# =========================
# STORAGE
# =========================
def save_chat(chat_id: int, role: str, content: str):
    cursor.execute(
        "INSERT INTO chat_history (chat_id, role, content) VALUES (?, ?, ?)",
        (chat_id, role, content),
    )
    conn.commit()


def recent_history(chat_id: int, limit: int = 12) -> list[sqlite3.Row]:
    rows = cursor.execute(
        """
        SELECT role, content
        FROM chat_history
        WHERE chat_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (chat_id, limit),
    ).fetchall()
    return list(reversed(rows))


def save_memory(key: str, value: str):
    cursor.execute(
        "INSERT INTO memories (key, value) VALUES (?, ?)",
        (key.strip(), value.strip()),
    )
    conn.commit()
    post_webhook(N8N_WEBHOOK_URL, {"type": "memory", "key": key, "value": value})
    write_obsidian("memories", key, f"# {key}\n\n{value}")
    return cursor.lastrowid


def save_note(text: str):
    cursor.execute(
        "INSERT INTO notes (text) VALUES (?)",
        (text.strip(),),
    )
    conn.commit()
    post_webhook(N8N_WEBHOOK_URL, {"type": "note", "text": text})
    write_obsidian("notes", "note", text)
    return cursor.lastrowid


def save_idea(text: str):
    cursor.execute(
        "INSERT INTO ideas (text) VALUES (?)",
        (text.strip(),),
    )
    conn.commit()
    post_webhook(N8N_WEBHOOK_URL, {"type": "idea", "text": text})
    write_obsidian("ideas", "idea", text)
    return cursor.lastrowid


def save_task(text: str):
    cursor.execute(
        "INSERT INTO tasks (text, status) VALUES (?, 'pending')",
        (text.strip(),),
    )
    conn.commit()
    post_webhook(N8N_WEBHOOK_URL, {"type": "task", "text": text})
    return cursor.lastrowid


def get_tasks():
    return cursor.execute(
        "SELECT id, text, status FROM tasks ORDER BY id DESC LIMIT 30"
    ).fetchall()


def get_memories():
    return cursor.execute(
        "SELECT id, key, value FROM memories ORDER BY id DESC LIMIT 30"
    ).fetchall()


def get_notes():
    return cursor.execute(
        "SELECT id, text FROM notes ORDER BY id DESC LIMIT 30"
    ).fetchall()


def get_ideas():
    return cursor.execute(
        "SELECT id, text FROM ideas ORDER BY id DESC LIMIT 30"
    ).fetchall()


def save_reminder(chat_id: int, task: str, trigger_time: datetime, repeat_minutes: int = 0, rule_key: str | None = None):
    cursor.execute(
        """
        INSERT INTO reminders (chat_id, rule_key, task, trigger_time, repeat_minutes, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (
            chat_id,
            rule_key,
            task.strip(),
            normalize_dt(trigger_time).isoformat(),
            int(repeat_minutes),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def upsert_rule_reminder(chat_id: int, rule_key: str, task: str, trigger_time: datetime, repeat_minutes: int):
    row = cursor.execute(
        "SELECT id FROM reminders WHERE rule_key=?",
        (rule_key,),
    ).fetchone()

    if row:
        reminder_id = row["id"]
        cursor.execute(
            """
            UPDATE reminders
            SET chat_id=?, task=?, trigger_time=?, repeat_minutes=?, status='pending'
            WHERE id=?
            """,
            (
                chat_id,
                task.strip(),
                normalize_dt(trigger_time).isoformat(),
                int(repeat_minutes),
                reminder_id,
            ),
        )
        conn.commit()
        return reminder_id

    return save_reminder(chat_id, task, trigger_time, repeat_minutes, rule_key=rule_key)


def get_reminder(reminder_id: int):
    return cursor.execute(
        "SELECT * FROM reminders WHERE id=?",
        (reminder_id,),
    ).fetchone()


def get_pending_reminders(chat_id: int):
    return cursor.execute(
        """
        SELECT *
        FROM reminders
        WHERE chat_id=? AND status='pending'
        ORDER BY datetime(trigger_time) ASC
        """,
        (chat_id,),
    ).fetchall()


def mark_reminder_done(reminder_id: int):
    cursor.execute(
        "UPDATE reminders SET status='done' WHERE id=?",
        (reminder_id,),
    )
    conn.commit()


def delete_reminder(reminder_id: int):
    cursor.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    conn.commit()


def mark_task_done(task_id: int):
    cursor.execute(
        "UPDATE tasks SET status='done' WHERE id=?",
        (task_id,),
    )
    conn.commit()

# =========================
# OPENROUTER
# =========================
def openrouter_chat(messages: list[dict], temperature: float = 0.6) -> str:
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://example.com",
                "X-Title": "Jarvis Lite",
            },
            json={
                "model": AI_MODEL,
                "messages": messages,
                "temperature": temperature,
            },
            timeout=35,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("AI error:", e)
        return "⚠️ AI temporarily unavailable."


def classify_message(text: str) -> str:
    low = text.lower().strip()

    if any(k in low for k in ["remind me", "reminder", "wake me"]):
        return "reminder"
    if low.startswith("remember "):
        return "memory"
    if low.startswith("note:") or low.startswith("note "):
        return "note"
    if low.startswith("idea:") or low.startswith("idea "):
        return "idea"
    if low.startswith("task:") or low.startswith("add task") or low.startswith("task "):
        return "task"
    if "plan my day" in low or low == "/plan" or low == "plan":
        return "plan"
    if "review my day" in low or low == "/review" or low == "review":
        return "review"

    prompt = f"""
Classify this user message for a personal Telegram assistant.

Message:
{text}

Return only one word from:
reminder, memory, note, idea, task, plan, review, chat
"""
    try:
        out = openrouter_chat(
            [
                {"role": "system", "content": "Return only one word. No punctuation."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        ).lower().strip()
        out = re.sub(r"[^a-z]", "", out)
        if out in {"reminder", "memory", "note", "idea", "task", "plan", "review", "chat"}:
            return out
    except Exception:
        pass

    return "chat"


def ai_chat_with_context(chat_id: int, text: str) -> str:
    memories = "\n".join([f"- {r['key']}: {r['value']}" for r in get_memories()[:15]]) or "None"
    notes = "\n".join([f"- {r['text']}" for r in get_notes()[:10]]) or "None"
    ideas = "\n".join([f"- {r['text']}" for r in get_ideas()[:10]]) or "None"
    tasks = "\n".join([f"- [{r['status']}] {r['text']}" for r in get_tasks()[:15]]) or "None"
    history = recent_history(chat_id, 10)
    history_text = "\n".join([f"{r['role'].upper()}: {r['content']}" for r in history]) or "None"

    system = f"""
You are Jarvis Lite, a personal second brain for the user.
Be practical, short, and helpful.

User memory:
{memories}

Notes:
{notes}

Ideas:
{ideas}

Tasks:
{tasks}

Recent chat:
{history_text}
""".strip()

    return openrouter_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        temperature=0.65,
    )

# =========================
# REMINDER PARSING
# =========================
def extract_task_from_reminder(text: str) -> str:
    body = re.sub(r"(?i)^remind me(?: to)?\s*", "", text).strip()
    body = re.sub(r"(?i)\b(every day|everyday|daily|each day)\b", " ", body)
    body = TIME_RE.sub(" ", body)
    body = re.sub(r"(?i)\b(?:today|tomorrow|tonight|morning|evening|night|noon|midnight|at|on|in|by|after|before)\b", " ", body)
    if " to " in body.lower():
        body = body.lower().split(" to ")[-1]
    body = re.sub(r"\s+", " ", body).strip(" ,.-")
    return body or "Reminder"


def parse_reminder_items(text: str):
    low = text.lower()
    if "remind me" not in low and "wake me" not in low:
        return []

    daily = any(k in low for k in ["every day", "everyday", "daily", "each day"])
    items = []

    # 1) In X minutes/hours
    m = re.search(r"(?i)^remind me in (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)\s*(?:to\s+)?(.+)$", text.strip())
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        task = m.group(3).strip()
        minutes = amount * 60 if "hour" in unit or unit in {"hr", "hrs"} else amount
        items.append({"task": task, "when": now_ist() + timedelta(minutes=minutes), "repeat_minutes": 0})
        return items

    m = re.search(r"(?i)^remind me(?: to)? (.+?) in (\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)$", text.strip())
    if m:
        task = m.group(1).strip()
        amount = int(m.group(2))
        unit = m.group(3).lower()
        minutes = amount * 60 if "hour" in unit or unit in {"hr", "hrs"} else amount
        items.append({"task": task, "when": now_ist() + timedelta(minutes=minutes), "repeat_minutes": 0})
        return items

    # 2) Weekday recurring like "every monday at 8:30 pm"
    m = re.search(r"(?i)\b(?:every|each)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b.*", text)
    if m:
        weekday = WEEKDAY_MAP[m.group(1).lower()]
        times = TIME_RE.findall(text)
        task = extract_task_from_reminder(text)
        if times:
            for h, mi, ap in times:
                hour = int(h)
                minute = int(mi or 0)
                ap = ap.lower()
                if ap == "pm" and hour != 12:
                    hour += 12
                if ap == "am" and hour == 12:
                    hour = 0
                dt = next_weekday_datetime(weekday, hour, minute)
                items.append({"task": task, "when": dt, "repeat_minutes": 10080})
            return items

    # 3) Multiple clock times in one daily reminder
    times = TIME_RE.findall(text)
    if times:
        task = extract_task_from_reminder(text)
        repeat = 1440 if daily else 0
        for h, mi, ap in times:
            hour = int(h)
            minute = int(mi or 0)
            ap = ap.lower()
            if ap == "pm" and hour != 12:
                hour += 12
            if ap == "am" and hour == 12:
                hour = 0
            dt = now_ist().replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt <= now_ist():
                dt += timedelta(days=1)
            items.append({"task": task, "when": dt, "repeat_minutes": repeat})
        return items

    # 4) Tomorrow / specific date / in X style fallback
    cleaned = re.sub(r"(?i)^remind me(?: to)?\s*", "", text).strip()
    when_text = cleaned
    if " to " in cleaned.lower():
        when_text = cleaned.lower().split(" to ")[0].strip()
        task = cleaned.lower().split(" to ")[-1].strip()
        dt = parse_date_phrase(when_text)
        if dt:
            items.append({"task": task, "when": dt, "repeat_minutes": 0})
            return items

    dt = parse_date_phrase(cleaned)
    if dt:
        task = extract_task_from_reminder(text)
        items.append({"task": task, "when": dt, "repeat_minutes": 0})
        return items

    return []

# =========================
# SCHEDULER
# =========================
def reminder_keyboard(reminder_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Done", callback_data=f"done:{reminder_id}"),
            InlineKeyboardButton("⏰ Snooze 1m", callback_data=f"snooze:1:{reminder_id}"),
            InlineKeyboardButton("⏰ Snooze 10m", callback_data=f"snooze:10:{reminder_id}"),
        ]
    ])


def cancel_jobs(app, reminder_id: int):
    for name in (f"base_{reminder_id}", f"nag_{reminder_id}"):
        for job in app.job_queue.get_jobs_by_name(name):
            job.schedule_removal()


def schedule_base_job(app, row, first_time: datetime | None = None):
    reminder_id = int(row["id"])
    chat_id = int(row["chat_id"])
    task = row["task"]
    repeat_minutes = int(row["repeat_minutes"] or 0)
    trigger_time = normalize_dt(first_time or datetime.fromisoformat(row["trigger_time"]))
    if repeat_minutes > 0:
        job = app.job_queue.run_repeating(
            send_reminder,
            interval=timedelta(minutes=repeat_minutes),
            first=trigger_time,
            chat_id=chat_id,
            data={
                "reminder_id": reminder_id,
                "task": task,
                "repeat_minutes": repeat_minutes,
                "trigger_time": trigger_time.isoformat(),
                "is_base": True,
            },
            name=f"base_{reminder_id}",
        )
        return job
    else:
        delay = max(5, int((trigger_time - now_ist()).total_seconds()))
        job = app.job_queue.run_once(
            send_reminder,
            when=delay,
            chat_id=chat_id,
            data={
                "reminder_id": reminder_id,
                "task": task,
                "repeat_minutes": 0,
                "trigger_time": trigger_time.isoformat(),
                "is_base": True,
            },
            name=f"base_{reminder_id}",
        )
        return job


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    d = job.data
    reminder_id = int(d["reminder_id"])
    row = get_reminder(reminder_id)
    if not row or row["status"] != "pending":
        return

    task = row["task"]
    repeat_minutes = int(row["repeat_minutes"] or 0)

    await context.bot.send_message(
        chat_id=int(row["chat_id"]),
        text=f"⏰ Reminder: {task}",
        reply_markup=reminder_keyboard(reminder_id),
    )

    # keep nagging every 1 minute until user acts
    if not context.application.job_queue.get_jobs_by_name(f"nag_{reminder_id}"):
        context.application.job_queue.run_once(
            nag_reminder,
            when=60,
            chat_id=int(row["chat_id"]),
            data={"reminder_id": reminder_id},
            name=f"nag_{reminder_id}",
        )

    # For repeating reminders, the base repeating job handles future occurrences.
    # If this is the first run after an overdue start, nothing else is needed.


async def nag_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    reminder_id = int(job.data["reminder_id"])
    row = get_reminder(reminder_id)
    if not row or row["status"] != "pending":
        return

    await context.bot.send_message(
        chat_id=int(row["chat_id"]),
        text=f"⏰ Reminder: {row['task']}",
        reply_markup=reminder_keyboard(reminder_id),
    )

    context.application.job_queue.run_once(
        nag_reminder,
        when=60,
        chat_id=int(row["chat_id"]),
        data={"reminder_id": reminder_id},
        name=f"nag_{reminder_id}",
    )


def schedule_existing_reminders(app):
    rows = cursor.execute(
        """
        SELECT *
        FROM reminders
        WHERE status='pending'
        """
    ).fetchall()

    now = now_ist()
    for row in rows:
        try:
            trigger_time = normalize_dt(datetime.fromisoformat(row["trigger_time"]))
            repeat_minutes = int(row["repeat_minutes"] or 0)

            if repeat_minutes > 0:
                while trigger_time <= now:
                    trigger_time += timedelta(minutes=repeat_minutes)
                cursor.execute(
                    "UPDATE reminders SET trigger_time=? WHERE id=?",
                    (trigger_time.isoformat(), int(row["id"])),
                )
                conn.commit()
                schedule_base_job(app, row, first_time=trigger_time)
            else:
                if trigger_time <= now:
                    trigger_time = now + timedelta(seconds=10)
                schedule_base_job(app, row, first_time=trigger_time)
        except Exception as e:
            print("Reschedule error:", e)


def schedule_rule_reminder(app, chat_id: int, rule_key: str, task: str, trigger_time: datetime, repeat_minutes: int):
    reminder_id = upsert_rule_reminder(chat_id, rule_key, task, trigger_time, repeat_minutes)
    row = get_reminder(reminder_id)
    cancel_jobs(app, reminder_id)
    schedule_base_job(app, row, first_time=normalize_dt(trigger_time))
    if CALENDAR_WEBHOOK_URL:
        post_webhook(CALENDAR_WEBHOOK_URL, {
            "type": "calendar_event",
            "task": task,
            "trigger_time": normalize_dt(trigger_time).isoformat(),
            "repeat_minutes": repeat_minutes,
            "chat_id": chat_id,
        })
    return reminder_id

# =========================
# DEFAULT SCHEDULES
# =========================
def setup_daily_routine(app, chat_id: int):
    routine = [
        ("daily_wake", "Wake up and start the day", 8, 0, 1440),
        ("daily_freshen", "Freshen up and get ready", 8, 5, 1440),
        ("daily_plan", "Plan the day for 2 minutes", 8, 10, 1440),
        ("daily_breakfast", "Have breakfast", 8, 30, 1440),
        ("daily_leave", "Finish morning prep and leave for college", 8, 50, 1440),
        ("daily_college", "Start college", 9, 0, 1440),
        ("daily_gym", "Go to the gym", 16, 0, 1440),
        ("daily_gym_end", "Finish gym and return", 18, 30, 1440),
        ("daily_eat", "Eat and recover", 18, 45, 1440),
        ("daily_study", "Study for 1 hour", 20, 0, 1440),
        ("daily_review", "Review the day and plan tomorrow", 21, 30, 1440),
        ("daily_sleep", "Sleep on time", 22, 0, 1440),
    ]

    lines = ["✅ Daily routine reminders set:"]

    for rule_key, task, hour, minute, repeat in routine:
        dt = next_time_today_or_tomorrow(hour, minute)
        rid = schedule_rule_reminder(app, chat_id, rule_key, task, dt, repeat)
        lines.append(f"- {task} at {dt.strftime('%I:%M %p')} (id {rid})")

    return lines


def setup_weekly_communication(app, chat_id: int):
    weekly = [
        ("week_mon_story", 0, 20, 30, "Day 1 — Build Your Story Brain"),
        ("week_tue_fast", 1, 20, 30, "Day 2 — Fast Thinking Engine"),
        ("week_wed_improv", 2, 20, 30, "Day 3 — Improvisation Training"),
        ("week_thu_founder", 3, 20, 30, "Day 4 — Founder Story Creation"),
        ("week_fri_persuasion", 4, 20, 30, "Day 5 — Persuasion Techniques"),
        ("week_sat_pressure", 5, 20, 30, "Day 6 — Pressure Communication"),
        ("week_sun_sim", 6, 20, 30, "Day 7 — Founder Simulation"),
        ("week_plan_next", 6, 21, 0, "Plan next week"),
    ]

    lines = ["✅ Weekly reminders set:"]
    for rule_key, weekday, hour, minute, task in weekly:
        dt = next_weekday_datetime(weekday, hour, minute)
        rid = schedule_rule_reminder(app, chat_id, rule_key, task, dt, 10080)
        lines.append(f"- {task} at {dt.strftime('%a %I:%M %p')} (id {rid})")
    return lines


def fixed_plan_blocks():
    return [
        ("08:00", "Wake up"),
        ("08:05", "Freshen up / bath"),
        ("08:10", "Plan the day for 2 minutes"),
        ("08:30", "Breakfast"),
        ("08:50", "Leave for college"),
        ("09:00", "College"),
        ("16:00", "Gym"),
        ("18:30", "Finish gym and return"),
        ("18:45", "Eat and recover"),
        ("20:00", "Study for 1 hour"),
        ("21:30", "Review the day and plan tomorrow"),
        ("22:00", "Sleep on time"),
    ]

# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Hi, I’m Jarvis Lite.\n\n"
        "Try:\n"
        "- remember my goal is build BLACKLEAF\n"
        "- note: startup idea\n"
        "- idea: AI outfit assistant\n"
        "- task: revise communication\n"
        "- remind me tomorrow at 9am to call mom\n"
        "- set them\n"
        "- plan my day\n"
        "- show reminders\n\n"
        "Commands:\n"
        "/memory /notes /ideas /tasks /reminders /plan /weekplan /review /setup_lite /done <task_id> /delete <reminder_id> /clear"
    )
    await update.message.reply_text(msg)


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_memories()
    if not rows:
        await update.message.reply_text("No memory yet.")
        return
    text = "🧠 Memories:\n\n" + "\n".join([f"{r['id']}. [{r['key']}] {r['value']}" for r in rows])
    await update.message.reply_text(text[:3900])


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_notes()
    if not rows:
        await update.message.reply_text("No notes yet.")
        return
    text = "📝 Notes:\n\n" + "\n".join([f"{r['id']}. {r['text']}" for r in rows])
    await update.message.reply_text(text[:3900])


async def ideas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_ideas()
    if not rows:
        await update.message.reply_text("No ideas yet.")
        return
    text = "💡 Ideas:\n\n" + "\n".join([f"{r['id']}. {r['text']}" for r in rows])
    await update.message.reply_text(text[:3900])


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_tasks()
    if not rows:
        await update.message.reply_text("No tasks yet.")
        return
    text = "📌 Tasks:\n\n" + "\n".join([f"{r['id']}. [{r['status']}] {r['text']}" for r in rows])
    await update.message.reply_text(text[:3900])


async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_pending_reminders(chat_id)
    if not rows:
        await update.message.reply_text("No pending reminders.")
        return

    lines = ["⏰ Pending reminders:"]
    for r in rows:
        dt = normalize_dt(datetime.fromisoformat(r["trigger_time"]))
        rep = int(r["repeat_minutes"] or 0)
        if rep > 0:
            lines.append(f"{r['id']}. {r['task']} — every {rep} min — next at {dt.strftime('%d %b %I:%M %p')}")
        else:
            lines.append(f"{r['id']}. {r['task']} — at {dt.strftime('%d %b %I:%M %p')}")
    await update.message.reply_text("\n".join(lines)[:3900])


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /delete <reminder_id>")
        return
    try:
        rid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Reminder id must be a number.")
        return

    row = get_reminder(rid)
    if not row or row["chat_id"] != update.effective_chat.id:
        await update.message.reply_text("Reminder not found.")
        return

    cancel_jobs(context.application, rid)
    mark_reminder_done(rid)
    await update.message.reply_text(f"Deleted reminder {rid}.")


async def done_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /done <task_id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Task id must be a number.")
        return
    mark_task_done(tid)
    await update.message.reply_text(f"Marked task {tid} done.")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_pending_reminders(chat_id)
    for r in rows:
        cancel_jobs(context.application, int(r["id"]))
        mark_reminder_done(int(r["id"]))
    await update.message.reply_text("All pending reminders cleared.")


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    blocks = fixed_plan_blocks()
    text = "📅 Jarvis Lite Plan\n\n" + "\n".join([f"{t} — {task}" for t, task in blocks])
    text += "\n\nSend `set them` to create the routine reminders."
    await update.message.reply_text(text)


async def weekplan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "🗓 Weekly communication practice:",
        "Mon 8:30 PM — Day 1: Build Your Story Brain",
        "Tue 8:30 PM — Day 2: Fast Thinking Engine",
        "Wed 8:30 PM — Day 3: Improvisation Training",
        "Thu 8:30 PM — Day 4: Founder Story Creation",
        "Fri 8:30 PM — Day 5: Persuasion Techniques",
        "Sat 8:30 PM — Day 6: Pressure Communication",
        "Sun 8:30 PM — Day 7: Founder Simulation",
        "Sun 9:00 PM — Plan next week",
    ]
    await update.message.reply_text("\n".join(lines))


async def review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mem = "\n".join([f"- {r['key']}: {r['value']}" for r in get_memories()[:10]]) or "None"
    tasks = "\n".join([f"- [{r['status']}] {r['text']}" for r in get_tasks()[:10]]) or "None"
    text = (
        "📘 Daily review\n\n"
        "1) What went well?\n"
        "2) What should improve?\n"
        "3) What is the first task for tomorrow?\n\n"
        "Recent memory:\n"
        f"{mem}\n\n"
        "Recent tasks:\n"
        f"{tasks}"
    )
    await update.message.reply_text(text[:3900])


async def setup_lite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lines = []
    lines += setup_daily_routine(context.application, chat_id)
    lines += setup_weekly_communication(context.application, chat_id)
    await update.message.reply_text("\n".join(lines)[:3900])


# =========================
# CALLBACKS
# =========================
async def reminder_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    parts = q.data.split(":")
    action = parts[0]

    if action == "done":
        reminder_id = int(parts[1])
        row = get_reminder(reminder_id)
        if not row:
            await q.edit_message_text("Already gone.")
            return

        cancel_jobs(context.application, reminder_id)

        # recurring reminders stay active; one-time reminders are completed
        if int(row["repeat_minutes"] or 0) == 0:
            mark_reminder_done(reminder_id)
            await q.edit_message_text("✅ Done.")
        else:
            await q.edit_message_text("✅ Done for this occurrence.")
        return

    if action == "snooze":
        minutes = int(parts[1])
        reminder_id = int(parts[2])
        row = get_reminder(reminder_id)
        if not row:
            await q.edit_message_text("Reminder not found.")
            return

        cancel_jobs(context.application, reminder_id)

        when = now_ist() + timedelta(minutes=minutes)
        context.application.job_queue.run_once(
            send_reminder,
            when=max(5, int((when - now_ist()).total_seconds())),
            chat_id=int(row["chat_id"]),
            data={
                "reminder_id": reminder_id,
                "task": row["task"],
                "repeat_minutes": int(row["repeat_minutes"] or 0),
                "trigger_time": when.isoformat(),
                "is_base": False,  # snooze is not the base repeating job
            },
            name=f"base_{reminder_id}",
        )

        await q.edit_message_text(f"⏰ Snoozed for {minutes} minute(s).")
        return


# =========================
# TEXT HANDLER
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    low = text.lower().strip()

    save_chat(update.effective_chat.id, "user", text)

    # quick setup command
    if low in {"set them", "setup lite", "setup", "set up", "start jarvis lite"}:
        await setup_lite_cmd(update, context)
        return

    # memory
    if low.startswith("remember "):
        m = re.match(r"(?i)^remember (?:my )?(.+?) is (.+)$", text)
        if m:
            key = m.group(1).strip()
            value = m.group(2).strip()
        else:
            key = "memory"
            value = text[len("remember "):].strip()
        save_memory(key, value)
        await update.message.reply_text("🧠 Memory saved.")
        return

    if "what do you know about me" in low:
        rows = get_memories()
        text_out = "🧠 What I know:\n\n" + ("\n".join([f"- {r['key']}: {r['value']}" for r in rows[:20]]) or "Nothing yet.")
        await update.message.reply_text(text_out[:3900])
        return

    # notes
    if low.startswith("note:") or low.startswith("note "):
        save_note(text.split(":", 1)[-1].strip())
        await update.message.reply_text("📝 Note saved.")
        return

    # ideas
    if low.startswith("idea:") or low.startswith("idea "):
        save_idea(text.split(":", 1)[-1].strip())
        await update.message.reply_text("💡 Idea saved.")
        return

    # tasks
    if low.startswith("task:") or low.startswith("add task") or low.startswith("task "):
        task_text = text.split(":", 1)[-1].strip()
        if low.startswith("add task"):
            task_text = text[len("add task"):].strip(" :-")
        save_task(task_text)
        await update.message.reply_text("📌 Task saved.")
        return

    if "plan my day" in low:
        await plan_cmd(update, context)
        return

    if "review my day" in low:
        await review_cmd(update, context)
        return

    if "week plan" in low or low == "/weekplan":
        await weekplan_cmd(update, context)
        return

    # reminders
    if any(k in low for k in ["remind me", "reminder", "wake me"]):
        items = parse_reminder_items(text)

        if items:
            replies = []
            for item in items:
                dt = normalize_dt(item["when"])
                rid = save_reminder(update.effective_chat.id, item["task"], dt, item["repeat_minutes"])
                row = get_reminder(rid)

                delay = max(5, int((dt - now_ist()).total_seconds()))
                if int(item["repeat_minutes"] or 0) > 0:
                    context.application.job_queue.run_repeating(
                        send_reminder,
                        interval=timedelta(minutes=int(item["repeat_minutes"])),
                        first=dt,
                        chat_id=update.effective_chat.id,
                        data={
                            "reminder_id": rid,
                            "task": item["task"],
                            "repeat_minutes": int(item["repeat_minutes"]),
                            "trigger_time": dt.isoformat(),
                            "is_base": True,
                        },
                        name=f"base_{rid}",
                    )
                    replies.append(f"🔁 Reminder set: {item['task']} at {dt.strftime('%I:%M %p')} (daily/weekly repeat)")
                else:
                    context.application.job_queue.run_once(
                        send_reminder,
                        when=delay,
                        chat_id=update.effective_chat.id,
                        data={
                            "reminder_id": rid,
                            "task": item["task"],
                            "repeat_minutes": 0,
                            "trigger_time": dt.isoformat(),
                            "is_base": True,
                        },
                        name=f"base_{rid}",
                    )
                    replies.append(f"⏰ Reminder set: {item['task']} at {dt.strftime('%b %d, %I:%M %p')}")

            await update.message.reply_text("\n".join(replies)[:3900])
            return

        # fallback: ask AI to classify, then parse again
        intent = classify_message(text)
        if intent == "reminder":
            dt = parse_date_phrase(text)
            task = extract_task_from_reminder(text)
            if dt:
                rid = save_reminder(update.effective_chat.id, task, dt, 0)
                delay = max(5, int((dt - now_ist()).total_seconds()))
                context.application.job_queue.run_once(
                    send_reminder,
                    when=delay,
                    chat_id=update.effective_chat.id,
                    data={
                        "reminder_id": rid,
                        "task": task,
                        "repeat_minutes": 0,
                        "trigger_time": dt.isoformat(),
                        "is_base": True,
                    },
                    name=f"base_{rid}",
                )
                await update.message.reply_text(f"⏰ Reminder set: {task} at {dt.strftime('%b %d, %I:%M %p')}")
                return

        await update.message.reply_text("I understood it as a reminder, but I could not parse the time.")
        return

    # classification
    intent = classify_message(text)

    if intent == "memory":
        save_memory("memory", text)
        await update.message.reply_text("🧠 Memory saved.")
        return

    if intent == "note":
        save_note(text)
        await update.message.reply_text("📝 Note saved.")
        return

    if intent == "idea":
        save_idea(text)
        await update.message.reply_text("💡 Idea saved.")
        return

    if intent == "task":
        save_task(text)
        await update.message.reply_text("📌 Task saved.")
        return

    if intent == "plan":
        await plan_cmd(update, context)
        return

    if intent == "review":
        await review_cmd(update, context)
        return

    # normal chat
    reply = ai_chat_with_context(update.effective_chat.id, text)
    save_chat(update.effective_chat.id, "assistant", reply)
    await update.message.reply_text(reply[:3900])

# =========================
# STARTUP
# =========================
async def post_init(app):
    schedule_existing_reminders(app)

# =========================
# MAIN
# =========================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("ideas", ideas_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("reminders", reminders_cmd))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("weekplan", weekplan_cmd))
    app.add_handler(CommandHandler("review", review_cmd))
    app.add_handler(CommandHandler("setup_lite", setup_lite_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("done", done_task_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(reminder_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🚀 Jarvis Lite running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
