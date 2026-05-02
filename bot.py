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
DB_PATH = os.getenv("DB_PATH", "/data/jarvis.db")
TZ = ZoneInfo("Asia/Kolkata")

MAX_HISTORY_MESSAGES = 12
MAX_MEMORY_ITEMS = 20
MAX_NOTES_ITEMS = 10
MAX_IDEAS_ITEMS = 10
MAX_TASKS_ITEMS = 20

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()


def ensure_tables() -> None:
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


def table_columns(name: str) -> list[str]:
    cursor.execute(f"PRAGMA table_info({name})")
    return [row[1] for row in cursor.fetchall()]


def migrate_reminders() -> None:
    cols = set(table_columns("reminders"))
    if "trigger_time" not in cols:
        cursor.execute("ALTER TABLE reminders ADD COLUMN trigger_time TEXT")
    if "repeat_minutes" not in cols:
        cursor.execute("ALTER TABLE reminders ADD COLUMN repeat_minutes INTEGER DEFAULT 0")
    if "status" not in cols:
        cursor.execute("ALTER TABLE reminders ADD COLUMN status TEXT DEFAULT 'pending'")
    if "task" not in cols:
        cursor.execute("ALTER TABLE reminders ADD COLUMN task TEXT")
    if "created_at" not in cols:
        cursor.execute("ALTER TABLE reminders ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")

    cols = set(table_columns("reminders"))
    if "time" in cols:
        cursor.execute("""
            UPDATE reminders
            SET trigger_time = COALESCE(trigger_time, time)
            WHERE trigger_time IS NULL OR trigger_time = ''
        """)
    if "repeat" in cols:
        cursor.execute("""
            UPDATE reminders
            SET repeat_minutes = COALESCE(repeat_minutes, repeat)
            WHERE repeat_minutes IS NULL OR repeat_minutes = 0
        """)
    conn.commit()


ensure_tables()
migrate_reminders()


def now_ist() -> datetime:
    return datetime.now(TZ)


def clean_text(text: str) -> str:
    return " ".join(text.strip().split()).lower().replace(".", "")


def strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def extract_json_object(text: str) -> str | None:
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else None


def parse_date_phrase(phrase: str) -> datetime | None:
    if not phrase:
        return None

    p = phrase.strip().lower()
    now = now_ist()

    if "after lunch" in p:
        dt = now.replace(hour=14, minute=0, second=0, microsecond=0)
        return dt + timedelta(days=1) if dt <= now else dt

    if "before sleep" in p or "before sleeping" in p or "tonight" in p:
        dt = now.replace(hour=21, minute=0, second=0, microsecond=0)
        return dt + timedelta(days=1) if dt <= now else dt

    if "this evening" in p:
        dt = now.replace(hour=18, minute=0, second=0, microsecond=0)
        return dt + timedelta(days=1) if dt <= now else dt

    if "tomorrow morning" in p:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    if "tomorrow evening" in p:
        return (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)

    settings = {
        "TIMEZONE": "Asia/Kolkata",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now,
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


def looks_like_reminder(text: str) -> bool:
    t = text.lower().strip()
    return t.startswith("remind ") or t.startswith("reminder ") or "remind me" in t or "wake me" in t


def looks_like_memory(text: str) -> bool:
    t = text.lower().strip()
    return t.startswith("remember ") or t.startswith("/remember")


def looks_like_note(text: str) -> bool:
    t = text.lower().strip()
    return t.startswith("note:") or t.startswith("note ") or t.startswith("/note")


def looks_like_idea(text: str) -> bool:
    t = text.lower().strip()
    return t.startswith("idea:") or t.startswith("idea ") or t.startswith("/idea")


def looks_like_task(text: str) -> bool:
    t = text.lower().strip()
    return t.startswith("add task") or t.startswith("task:") or t.startswith("task ") or t.startswith("/task")


def extract_memory_local(text: str) -> tuple[str, str] | None:
    t = text.strip()
    low = t.lower()

    m = re.match(r"^remember (?:that )?my (.+?) is (.+)$", low, re.I)
    if m:
        return (m.group(1).strip(), m.group(2).strip())

    m = re.match(r"^remember (?:that )?i (.+)$", low, re.I)
    if m:
        return ("about_me", m.group(1).strip())

    if low.startswith("remember "):
        return ("memory", t[len("remember "):].strip())

    return None


def extract_note_text(text: str) -> str:
    return re.sub(r"^/?note[:\s]+", "", text.strip(), flags=re.I).strip()


def extract_idea_text(text: str) -> str:
    return re.sub(r"^/?idea[:\s]+", "", text.strip(), flags=re.I).strip()


def extract_task_text(text: str) -> str:
    return re.sub(r"^/?(add\s+task|task)[:\s]+", "", text.strip(), flags=re.I).strip()


def save_memory(key: str, value: str) -> int:
    cursor.execute("INSERT INTO memories (key, value) VALUES (?, ?)", (key.strip(), value.strip()))
    conn.commit()
    return cursor.lastrowid


def save_note(text: str) -> int:
    cursor.execute("INSERT INTO notes (text) VALUES (?)", (text.strip(),))
    conn.commit()
    return cursor.lastrowid


def save_idea(text: str) -> int:
    cursor.execute("INSERT INTO ideas (text) VALUES (?)", (text.strip(),))
    conn.commit()
    return cursor.lastrowid


def save_task(text: str) -> int:
    cursor.execute("INSERT INTO tasks (text, status) VALUES (?, 'pending')", (text.strip(),))
    conn.commit()
    return cursor.lastrowid


def save_chat(chat_id: int, role: str, content: str) -> None:
    cursor.execute(
        "INSERT INTO chat_history (chat_id, role, content) VALUES (?, ?, ?)",
        (chat_id, role, content),
    )
    conn.commit()


def get_recent_history(chat_id: int, limit: int = MAX_HISTORY_MESSAGES) -> list[tuple[str, str]]:
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


def memory_summary(limit: int = MAX_MEMORY_ITEMS) -> str:
    rows = cursor.execute("SELECT key, value FROM memories ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "No memory saved yet."
    return "\n".join([f"- {k}: {v}" for k, v in reversed(rows)])


def notes_summary(limit: int = MAX_NOTES_ITEMS) -> str:
    rows = cursor.execute("SELECT id, text FROM notes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "No notes saved yet."
    return "\n".join([f"{i}. {t}" for i, t in rows])


def ideas_summary(limit: int = MAX_IDEAS_ITEMS) -> str:
    rows = cursor.execute("SELECT id, text FROM ideas ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "No ideas saved yet."
    return "\n".join([f"{i}. {t}" for i, t in rows])


def tasks_summary(limit: int = MAX_TASKS_ITEMS) -> str:
    rows = cursor.execute("SELECT id, text, status FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "No tasks saved yet."
    return "\n".join([f"{i}. [{s}] {t}" for i, t, s in rows])


def openrouter_chat(messages: list[dict], temperature: float = 0.7) -> str:
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
            "messages": messages,
            "temperature": temperature,
        },
        timeout=35,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def openrouter_json(messages: list[dict], temperature: float = 0.0) -> dict | None:
    try:
        raw = openrouter_chat(messages, temperature=temperature)
        raw = strip_code_fences(raw)
        raw = extract_json_object(raw) or raw
        return json.loads(raw)
    except Exception as e:
        print("OPENROUTER JSON ERROR:", e)
        return None


def local_classify(text: str) -> str:
    low = text.lower().strip()
    if looks_like_reminder(text) or "tomorrow" in low or "at " in low or "every " in low:
        return "reminder"
    if looks_like_memory(text):
        return "memory"
    if looks_like_note(text):
        return "note"
    if looks_like_idea(text):
        return "idea"
    if looks_like_task(text):
        return "task"
    if "plan my day" in low or low == "/plan":
        return "plan"
    if "review my day" in low or low == "/review":
        return "review"
    return "chat"


def classify_message(text: str) -> dict:
    prompt = f"""
Classify the user message for a personal Telegram assistant.

User message:
{text}

Return only valid JSON.

Allowed intents:
- reminder
- memory
- note
- idea
- task
- plan
- review
- chat

Examples:

{{
  "intent": "reminder",
  "task": "drink water",
  "when_text": "in 20 minutes",
  "repeat_minutes": 0
}}

{{
  "intent": "memory",
  "key": "goal",
  "value": "build BLACKLEAF"
}}

{{
  "intent": "note",
  "text": "users hate too many options"
}}

{{
  "intent": "idea",
  "text": "build AI outfit assistant"
}}

{{
  "intent": "task",
  "text": "revise communication"
}}

{{
  "intent": "plan"
}}

{{
  "intent": "review"
}}

{{
  "intent": "chat",
  "reply": "natural helpful answer"
}}

Rules:
- Choose the best single intent.
- Keep fields short and clean.
- If unclear, use chat.
"""
    data = openrouter_json(
        [
            {"role": "system", "content": "Return only valid JSON. No markdown."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
    )
    if not data or "intent" not in data:
        return {"intent": local_classify(text)}
    data["intent"] = str(data["intent"]).strip().lower()
    return data


def ai_chat_with_context(chat_id: int, text: str) -> str:
    mem = memory_summary()
    notes = notes_summary()
    ideas = ideas_summary()
    tasks = tasks_summary()
    history = get_recent_history(chat_id)
    history_text = "\n".join([f"{role.upper()}: {content}" for role, content in history]) or "No recent history."

    system = f"""
You are Jarvis, the user's private personal AI assistant.

You must:
- be concise, direct, and useful
- remember the user context
- help with planning, reminders, productivity, startup thinking, communication, and daily life
- answer in a natural human way

User memory:
{mem}

Recent notes:
{notes}

Recent ideas:
{ideas}

Open tasks:
{tasks}

Recent conversation:
{history_text}
""".strip()

    return openrouter_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        temperature=0.7,
    )


def ai_plan_prompt() -> str:
    mem = memory_summary(12)
    tasks = tasks_summary(12)
    ideas = ideas_summary(8)

    reminders = cursor.execute(
        """
        SELECT task, trigger_time, repeat_minutes
        FROM reminders
        WHERE status='pending'
        ORDER BY trigger_time ASC
        LIMIT 10
        """
    ).fetchall()

    reminder_lines = []
    for task, trigger_time_str, repeat_minutes in reminders:
        try:
            dt = datetime.fromisoformat(trigger_time_str).astimezone(TZ)
            if int(repeat_minutes or 0) > 0:
                reminder_lines.append(f"- {task} (every {repeat_minutes} min, first at {dt.strftime('%I:%M %p')})")
            else:
                reminder_lines.append(f"- {task} (at {dt.strftime('%b %d, %I:%M %p')})")
        except Exception:
            reminder_lines.append(f"- {task}")

    return f"""
You are Jarvis, the user's planning assistant.

User memory:
{mem}

Tasks:
{tasks}

Ideas:
{ideas}

Pending reminders:
{chr(10).join(reminder_lines) if reminder_lines else 'No reminders.'}

Create a practical plan for today:
- short headline
- 5 to 8 bullet points
- order by priority
- include one startup/focus block
- include one health block
- be realistic and motivating
""".strip()


def ai_review_prompt() -> str:
    mem = memory_summary(10)
    history = get_recent_history(1, 10)
    history_text = "\n".join([f"{role.upper()}: {content}" for role, content in history]) or "No recent history."

    return f"""
You are Jarvis, the user's review coach.

User memory:
{mem}

Recent conversation:
{history_text}

Write:
- what went well
- what to improve
- 3 short questions for reflection

Be concise and practical.
""".strip()


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
        """
        SELECT id, chat_id, task, trigger_time, repeat_minutes
        FROM reminders
        WHERE status='pending'
        """
    ).fetchall()

    now = now_ist()
    for rid, chat_id, task, trigger_time_str, repeat_minutes in rows:
        try:
            trigger_time = datetime.fromisoformat(trigger_time_str)
            if trigger_time.tzinfo is None:
                trigger_time = trigger_time.replace(tzinfo=TZ)

            if int(repeat_minutes or 0) > 0:
                if trigger_time <= now:
                    trigger_time = now + timedelta(minutes=int(repeat_minutes))
            else:
                if trigger_time <= now:
                    trigger_time = now + timedelta(seconds=10)

            schedule_reminder(app, rid, chat_id, task, trigger_time, int(repeat_minutes or 0))
        except Exception as e:
            print(f"RESCHEDULE ERROR for {rid}: {e}")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi. I’m Jarvis.\n\n"
        "Try:\n"
        "- remember my goal is build BLACKLEAF\n"
        "- note: startup idea...\n"
        "- idea: AI outfit assistant\n"
        "- add task revise physics\n"
        "- remind me tomorrow at 9am to call mom\n"
        "- plan my day\n"
        "- review my day\n\n"
        "Commands:\n"
        "/memory /notes /ideas /tasks /reminders /plan /review /delete <id> /done <id> /forget <id> /clear"
    )


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(memory_summary())


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(notes_summary())


async def ideas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ideas_summary())


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(tasks_summary())


async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        try:
            dt = datetime.fromisoformat(trigger_time_str).astimezone(TZ)
            if int(repeat_minutes or 0) > 0:
                lines.append(f"{rid}. {task} — every {repeat_minutes} min, first at {dt.strftime('%b %d, %I:%M %p')}")
            else:
                lines.append(f"{rid}. {task} — at {dt.strftime('%b %d, %I:%M %p')}")
        except Exception:
            lines.append(f"{rid}. {task}")
    await update.message.reply_text("\n".join(lines))


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <reminder_id>")
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


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /done <task_id>")
        return

    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Task ID must be a number.")
        return

    row = cursor.execute("SELECT id FROM tasks WHERE id=?", (tid,)).fetchone()
    if not row:
        await update.message.reply_text("Task not found.")
        return

    cursor.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    conn.commit()
    await update.message.reply_text(f"Marked task {tid} done.")


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /forget <memory_id>")
        return

    try:
        mid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Memory ID must be a number.")
        return

    cursor.execute("DELETE FROM memories WHERE id=?", (mid,))
    conn.commit()
    await update.message.reply_text(f"Forgot memory {mid}.")


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


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reply = openrouter_chat(
            [
                {"role": "system", "content": ai_plan_prompt()},
                {"role": "user", "content": "Make my plan."},
            ],
            temperature=0.4,
        )
    except Exception as e:
        print("PLAN ERROR:", e)
        reply = "⚠️ I could not build a plan right now."
    save_chat(update.effective_chat.id, "assistant", reply)
    await update.message.reply_text(reply)


async def review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reply = openrouter_chat(
            [
                {"role": "system", "content": ai_review_prompt()},
                {"role": "user", "content": "Review my day."},
            ],
            temperature=0.4,
        )
    except Exception as e:
        print("REVIEW ERROR:", e)
        reply = "⚠️ I could not build a review right now."
    save_chat(update.effective_chat.id, "assistant", reply)
    await update.message.reply_text(reply)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    low = text.lower().strip()

    save_chat(update.effective_chat.id, "user", text)

    if low.startswith("/remember") or low.startswith("remember "):
        extracted = extract_memory_local(text)
        if not extracted:
            data = openrouter_json(
                [
                    {"role": "system", "content": "Return only JSON."},
                    {"role": "user", "content": f'Extract memory key/value from: "{text}"\nJSON: {{"key":"","value":""}}'},
                ],
                temperature=0.0,
            ) or {}
            key = (data.get("key") or "memory").strip()
            value = (data.get("value") or text.replace("remember", "", 1).strip()).strip()
        else:
            key, value = extracted

        mid = save_memory(key, value)
        reply = f"✅ Saved memory {mid}: {key} → {value}"
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if "what do you know about me" in low or low == "/memory":
        reply = memory_summary()
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if low.startswith("/note") or low.startswith("note:") or low.startswith("note "):
        note_text = extract_note_text(text) or text
        nid = save_note(note_text)
        reply = f"📝 Note saved {nid}."
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if low == "/notes":
        reply = notes_summary()
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if low.startswith("/idea") or low.startswith("idea:") or low.startswith("idea "):
        idea_text = extract_idea_text(text) or text
        iid = save_idea(idea_text)
        reply = f"💡 Idea saved {iid}."
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if low == "/ideas":
        reply = ideas_summary()
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if low.startswith("/task") or low.startswith("add task") or low.startswith("task:") or low.startswith("task "):
        task_text = extract_task_text(text) or text
        tid = save_task(task_text)
        reply = f"✅ Task saved {tid}."
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if low == "/tasks":
        reply = tasks_summary()
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if "plan my day" in low or low == "/plan":
        await plan_cmd(update, context)
        return

    if "review my day" in low or low == "/review":
        await review_cmd(update, context)
        return

    if looks_like_reminder(text):
        local = parse_reminder_local(text)
        if not local:
            routed = classify_message(text)
            if routed.get("intent") == "reminder":
                when_text = str(routed.get("when_text") or "").strip()
                repeat_minutes = int(routed.get("repeat_minutes") or 0)
                task = str(routed.get("task") or "").strip()

                trigger_time = parse_date_phrase(when_text or text)
                if not trigger_time and repeat_minutes > 0:
                    trigger_time = now_ist() + timedelta(minutes=repeat_minutes)

                if trigger_time:
                    local = {
                        "task": task or text.replace("remind me", "", 1).strip() or "Reminder",
                        "trigger_time": trigger_time,
                        "repeat_minutes": repeat_minutes,
                    }

        if local:
            task = local["task"].strip()
            trigger_time = local["trigger_time"]
            repeat_minutes = int(local["repeat_minutes"])

            rid = save_reminder(update.effective_chat.id, task, trigger_time, repeat_minutes)
            schedule_reminder(context.application, rid, update.effective_chat.id, task, trigger_time, repeat_minutes)

            if repeat_minutes > 0:
                reply = f"🔁 Reminder set every {repeat_minutes} minutes:\n{task}"
            else:
                reply = f"⏰ Reminder set:\n{task}\nAt {trigger_time.strftime('%b %d, %I:%M %p')}"

            save_chat(update.effective_chat.id, "assistant", reply)
            await update.message.reply_text(reply)
            return

        reply = "I understood it as a reminder, but I could not understand the time. Try: 'tomorrow at 9am' or 'in 2 hours'."
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    try:
        routed = classify_message(text)
    except Exception as e:
        print("CLASSIFY ERROR:", e)
        routed = {"intent": local_classify(text)}

    intent = routed.get("intent", "chat")

    if intent == "memory":
        key = str(routed.get("key") or "memory").strip()
        value = str(routed.get("value") or text).strip()
        mid = save_memory(key, value)
        reply = f"✅ Saved memory {mid}: {key} → {value}"
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if intent == "note":
        note_text = str(routed.get("text") or text).strip()
        nid = save_note(note_text)
        reply = f"📝 Note saved {nid}."
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if intent == "idea":
        idea_text = str(routed.get("text") or text).strip()
        iid = save_idea(idea_text)
        reply = f"💡 Idea saved {iid}."
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if intent == "task":
        task_text = str(routed.get("text") or text).strip()
        tid = save_task(task_text)
        reply = f"✅ Task saved {tid}."
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if intent == "plan":
        await plan_cmd(update, context)
        return

    if intent == "review":
        await review_cmd(update, context)
        return

    if intent == "chat" and routed.get("reply"):
        reply = str(routed["reply"]).strip()
        save_chat(update.effective_chat.id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    try:
        reply = ai_chat_with_context(update.effective_chat.id, text)
    except Exception as e:
        print("AI CHAT ERROR:", e)
        reply = "⚠️ AI temporarily unavailable."

    save_chat(update.effective_chat.id, "assistant", reply)
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
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("ideas", ideas_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("reminders", reminders_cmd))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("review", review_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🚀 Jarvis v2 running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
