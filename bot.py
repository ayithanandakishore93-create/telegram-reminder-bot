import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import dateparser

from supabase import create_client

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

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TZ = ZoneInfo("Asia/Kolkata")

# =========================
# HELPERS
# =========================

def now_ist():
    return datetime.now(TZ)


def parse_date_phrase(text):
    return dateparser.parse(
        text,
        settings={
            "TIMEZONE": "Asia/Kolkata",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )


TIME_RE = re.compile(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', re.I)


# =========================
# SUPABASE
# =========================

def save_memory(user_id, category, content):
    supabase.table("memories").insert({
        "user_id": str(user_id),
        "category": category,
        "content": content
    }).execute()


def get_memories(user_id):
    r = (
        supabase.table("memories")
        .select("*")
        .eq("user_id", str(user_id))
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    )

    return r.data


def save_task(user_id, task):
    supabase.table("tasks").insert({
        "user_id": str(user_id),
        "task": task,
        "status": "pending"
    }).execute()


def get_tasks(user_id):
    r = (
        supabase.table("tasks")
        .select("*")
        .eq("user_id", str(user_id))
        .eq("status", "pending")
        .execute()
    )

    return r.data


def save_reminder(user_id, task, trigger_time, repeat_minutes=0):
    r = supabase.table("reminders").insert({
        "user_id": str(user_id),
        "task": task,
        "trigger_time": trigger_time.isoformat(),
        "repeat_minutes": repeat_minutes,
        "status": "pending"
    }).execute()

    return r.data[0]["id"]


def mark_done(reminder_id):
    supabase.table("reminders").update({
        "status": "done"
    }).eq("id", reminder_id).execute()


# =========================
# AI
# =========================

def ask_ai(prompt):

    SYSTEM = """
You are Jarvis Lite.

You are a personal AI assistant.

You help with:
- planning
- reminders
- tasks
- ideas
- productivity
- startup thinking

Keep answers short and useful.
"""

    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://jarvis-lite.app",
                "X-Title": "Jarvis Lite",
            },
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=40,
        )

        data = r.json()

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI ERROR:", e)
        return "⚠️ AI temporarily unavailable"


# =========================
# REMINDER SYSTEM
# =========================

def reminder_keyboard(reminder_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Done",
                callback_data=f"done:{reminder_id}"
            ),

            InlineKeyboardButton(
                "⏰ Snooze 1m",
                callback_data=f"snooze:1:{reminder_id}"
            ),

            InlineKeyboardButton(
                "⏰ Snooze 10m",
                callback_data=f"snooze:10:{reminder_id}"
            ),
        ]
    ])


def cancel_jobs(app, reminder_id):

    for name in [
        f"repeat_{reminder_id}",
        f"reminder_{reminder_id}",
    ]:

        for job in app.job_queue.get_jobs_by_name(name):
            job.schedule_removal()


async def send_reminder(context):

    job = context.job

    reminder_id = job.data["reminder_id"]
    task = job.data["task"]

    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"⏰ Reminder: {task}",
        reply_markup=reminder_keyboard(reminder_id),
    )

    # repeat every 1 min until action
    context.job_queue.run_once(
        repeat_reminder,
        when=60,
        chat_id=job.chat_id,
        data={
            "reminder_id": reminder_id,
            "task": task,
        },
        name=f"repeat_{reminder_id}"
    )


async def repeat_reminder(context):

    job = context.job

    reminder_id = job.data["reminder_id"]
    task = job.data["task"]

    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"⏰ Reminder: {task}",
        reply_markup=reminder_keyboard(reminder_id),
    )

    context.job_queue.run_once(
        repeat_reminder,
        when=60,
        chat_id=job.chat_id,
        data={
            "reminder_id": reminder_id,
            "task": task,
        },
        name=f"repeat_{reminder_id}"
    )


async def reminder_buttons(update, context):

    q = update.callback_query

    await q.answer()

    parts = q.data.split(":")

    action = parts[0]

    # DONE

    if action == "done":

        reminder_id = int(parts[1])

        cancel_jobs(context.application, reminder_id)

        mark_done(reminder_id)

        await q.edit_message_text("✅ Reminder completed")

        return

    # SNOOZE

    if action == "snooze":

        minutes = int(parts[1])

        reminder_id = int(parts[2])

        cancel_jobs(context.application, reminder_id)

        row = (
            supabase.table("reminders")
            .select("*")
            .eq("id", reminder_id)
            .execute()
        )

        if not row.data:
            return

        item = row.data[0]

        when = now_ist() + timedelta(minutes=minutes)

        context.application.job_queue.run_once(
            send_reminder,
            when=when,
            chat_id=int(item["user_id"]),
            data={
                "reminder_id": reminder_id,
                "task": item["task"],
            },
            name=f"reminder_{reminder_id}"
        )

        await q.edit_message_text(
            f"⏰ Snoozed for {minutes} minute(s)"
        )


# =========================
# PARSE REMINDERS
# =========================

def parse_reminder(text):

    low = text.lower()

    if "remind me" not in low:
        return []

    repeat_daily = (
        "everyday" in low
        or "every day" in low
        or "daily" in low
    )

    task = re.sub(
        r'(?i)^remind me to',
        '',
        text
    ).strip()

    task = re.split(
        r'(?i)\bat\b|\bon\b',
        task
    )[0].strip()

    times = []

    for h, m, ap in TIME_RE.findall(low):

        hour = int(h)
        minute = int(m or 0)

        if ap.lower() == "pm" and hour != 12:
            hour += 12

        if ap.lower() == "am" and hour == 12:
            hour = 0

        dt = now_ist().replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        if dt <= now_ist():
            dt += timedelta(days=1)

        times.append(dt)

    items = []

    for dt in times:

        items.append({
            "task": task,
            "when": dt,
            "repeat": 1440 if repeat_daily else 0
        })

    return items


# =========================
# DAILY PLAN
# =========================

def build_schedule():

    return [
        ("08:00", "Wake up"),
        ("08:05", "Freshen up"),
        ("08:30", "Breakfast"),
        ("09:00", "College"),
        ("16:00", "Gym"),
        ("18:30", "Dinner"),
        ("20:00", "Study"),
        ("21:30", "Review day"),
        ("22:00", "Sleep"),
    ]


async def create_daily_plan(update, context):

    plan = build_schedule()

    lines = ["📅 Jarvis Lite Schedule\n"]

    for time_s, task in plan:

        lines.append(f"{time_s} — {task}")

        hour = int(time_s.split(":")[0])
        minute = int(time_s.split(":")[1])

        dt = now_ist().replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        if dt <= now_ist():
            dt += timedelta(days=1)

        rid = save_reminder(
            update.effective_chat.id,
            task,
            dt,
            1440
        )

        context.job_queue.run_once(
            send_reminder,
            when=dt,
            chat_id=update.effective_chat.id,
            data={
                "reminder_id": rid,
                "task": task,
            },
            name=f"reminder_{rid}"
        )

    await update.message.reply_text(
        "\n".join(lines)
    )


# =========================
# COMMANDS
# =========================

async def start(update, context):

    await update.message.reply_text(
        "🚀 Jarvis Lite activated"
    )


async def memory_cmd(update, context):

    items = get_memories(update.effective_chat.id)

    if not items:
        await update.message.reply_text("No memory yet.")
        return

    text = "🧠 Memories:\n\n"

    for i in items:
        text += f"- [{i['category']}] {i['content']}\n"

    await update.message.reply_text(text)


async def tasks_cmd(update, context):

    items = get_tasks(update.effective_chat.id)

    if not items:
        await update.message.reply_text("No tasks.")
        return

    text = "📌 Tasks:\n\n"

    for i in items:
        text += f"- {i['task']}\n"

    await update.message.reply_text(text)


# =========================
# MAIN CHAT
# =========================

async def chat(update, context):

    text = update.message.text

    # MEMORY

    if text.lower().startswith("remember"):

        content = text.replace("remember", "").strip()

        save_memory(
            update.effective_chat.id,
            "memory",
            content
        )

        await update.message.reply_text(
            "🧠 Memory saved"
        )

        return

    # NOTE

    if text.lower().startswith("note:"):

        content = text.replace("note:", "").strip()

        save_memory(
            update.effective_chat.id,
            "note",
            content
        )

        await update.message.reply_text(
            "📝 Note saved"
        )

        return

    # IDEA

    if text.lower().startswith("idea:"):

        content = text.replace("idea:", "").strip()

        save_memory(
            update.effective_chat.id,
            "idea",
            content
        )

        await update.message.reply_text(
            "💡 Idea saved"
        )

        return

    # TASK

    if text.lower().startswith("task:"):

        task = text.replace("task:", "").strip()

        save_task(
            update.effective_chat.id,
            task
        )

        await update.message.reply_text(
            "📌 Task added"
        )

        return

    # PLAN

    if "plan my day" in text.lower():

        await create_daily_plan(update, context)

        return

    # REMINDER

    reminders = parse_reminder(text)

    if reminders:

        replies = []

        for item in reminders:

            rid = save_reminder(
                update.effective_chat.id,
                item["task"],
                item["when"],
                item["repeat"]
            )

            context.application.job_queue.run_once(
                send_reminder,
                when=item["when"],
                chat_id=update.effective_chat.id,
                data={
                    "reminder_id": rid,
                    "task": item["task"],
                },
                name=f"reminder_{rid}"
            )

            replies.append(
                f"⏰ Reminder set for {item['when'].strftime('%I:%M %p')}"
            )

        await update.message.reply_text(
            "\n".join(replies)
        )

        return

    # NORMAL AI CHAT

    reply = ask_ai(text)

    await update.message.reply_text(reply)


# =========================
# MAIN
# =========================

def main():

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))

    app.add_handler(
        CallbackQueryHandler(reminder_buttons)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            chat
        )
    )

    print("🚀 Jarvis Lite running...")

    app.run_polling()


if __name__ == "__main__":
    main()
