import os
import sqlite3
import json
import re
import tempfile
from datetime import datetime, timedelta

import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_KEY"]
DB_PATH    = os.environ.get("DB_PATH", "tasks.db")

genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ── Database ──────────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id      INTEGER NOT NULL,
        task         TEXT    NOT NULL,
        remind_at    TEXT,
        status       TEXT    DEFAULT 'pending',
        snooze_count INTEGER DEFAULT 0
    )
""")
conn.commit()

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

# ── AI prompts ────────────────────────────────────────────────────────────────
EXTRACT_PROMPT = """Today is {today} (IST).
Extract the reminder task and time from the message below.
Return ONLY valid JSON — no markdown, no explanation, nothing else.

Format:
{{"task": "short clear task", "remind_at": "YYYY-MM-DD HH:MM or null"}}

Rules:
- "tomorrow" = next calendar day
- "tonight" = today at 21:00
- "morning" = 08:00, "afternoon" = 14:00, "evening" = 18:00
- If no time given, set remind_at to null

Message: {message}"""

TRANSCRIBE_PROMPT = "Transcribe this voice message exactly as spoken. Return only the transcription, nothing else."

# ── Helpers ───────────────────────────────────────────────────────────────────
async def ai_extract(text: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d %A %I:%M %p")
    prompt = EXTRACT_PROMPT.format(today=today, message=text)
    response = model.generate_content(prompt)
    raw = re.sub(r"```json|```", "", response.text).strip()
    return json.loads(raw)

async def ai_transcribe(file_path: str) -> str:
    audio = genai.upload_file(file_path)
    response = model.generate_content([TRANSCRIBE_PROMPT, audio])
    return response.text.strip()

def reminder_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Done",      callback_data=f"done_{task_id}"),
        InlineKeyboardButton("Snooze 15m", callback_data=f"snooze_15_{task_id}"),
        InlineKeyboardButton("Snooze 1h",  callback_data=f"snooze_60_{task_id}"),
    ]])

async def fire_reminder(bot, chat_id: int, task_id: int, task: str):
    cursor.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
    row = cursor.fetchone()
    if not row or row[0] == "done":
        return
    await bot.send_message(
        chat_id=chat_id,
        text=f"Reminder: {task}",
        reply_markup=reminder_keyboard(task_id)
    )

async def schedule(app, task_id: int, task: str, remind_at_str: str, chat_id: int):
    remind_at = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M")
    if remind_at <= datetime.now():
        return
    job_id = f"task_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        fire_reminder,
        trigger="date",
        run_date=remind_at,
        id=job_id,
        args=[app.bot, chat_id, task_id, task]
    )

async def save_and_schedule(app, chat_id: int, text: str) -> str:
    extracted  = await ai_extract(text)
    task       = extracted["task"]
    remind_at  = extracted.get("remind_at")

    cursor.execute(
        "INSERT INTO tasks (chat_id, task, remind_at) VALUES (?, ?, ?)",
        (chat_id, task, remind_at)
    )
    conn.commit()
    task_id = cursor.lastrowid

    if remind_at:
        await schedule(app, task_id, task, remind_at, chat_id)
        dt = datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        return f"Saved! I'll remind you:\n\n{task}\n{dt.strftime('%b %d at %I:%M %p')}"
    else:
        return f"Saved: {task}\n\nNo time detected — send again with a time like 'tomorrow at 5pm'."

# ── Handlers ──────────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("On it...")
    try:
        reply = await save_and_schedule(context.application, chat_id, update.message.text)
        await msg.edit_text(reply)
    except Exception as e:
        await msg.edit_text(
            "Couldn't parse that. Try:\n'Remind me to call mom tomorrow at 6pm'"
        )

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("Transcribing...")
    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            await tg_file.download_to_drive(f.name)
            transcript = await ai_transcribe(f.name)
        await msg.edit_text(f'Heard: "{transcript}"\n\nProcessing...')
        reply = await save_and_schedule(context.application, chat_id, transcript)
        await msg.edit_text(reply)
    except Exception:
        await msg.edit_text("Couldn't process the voice note. Try sending text.")

async def handle_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cursor.execute(
        "SELECT id, task, remind_at, snooze_count FROM tasks "
        "WHERE chat_id=? AND status='pending' ORDER BY remind_at",
        (chat_id,)
    )
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("No pending reminders!")
        return
    lines = ["Your pending reminders:\n"]
    for task_id, task, remind_at, snooze_count in rows:
        if remind_at:
            dt = datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
            time_str = dt.strftime("%b %d at %I:%M %p")
        else:
            time_str = "No time set"
        snooze_note = f" (snoozed {snooze_count}x)" if snooze_count else ""
        lines.append(f"{task_id}. {task}\n   {time_str}{snooze_note}")
    await update.message.reply_text("\n".join(lines))

async def handle_done_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cursor.execute("UPDATE tasks SET status='done' WHERE chat_id=? AND status='pending'", (chat_id,))
    conn.commit()
    await update.message.reply_text("Cleared all pending reminders.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("done_"):
        task_id = int(data.split("_")[1])
        cursor.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        conn.commit()
        job_id = f"task_{task_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        await query.edit_message_text("Done! Marked complete.")

    elif data.startswith("snooze_"):
        _, minutes_str, task_id_str = data.split("_")
        minutes = int(minutes_str)
        task_id = int(task_id_str)
        cursor.execute(
            "SELECT task, snooze_count, chat_id FROM tasks WHERE id=?", (task_id,)
        )
        row = cursor.fetchone()
        if not row:
            return
        task, snooze_count, chat_id = row
        new_time     = datetime.now() + timedelta(minutes=minutes)
        new_time_str = new_time.strftime("%Y-%m-%d %H:%M")
        cursor.execute(
            "UPDATE tasks SET remind_at=?, snooze_count=? WHERE id=?",
            (new_time_str, snooze_count + 1, task_id)
        )
        conn.commit()
        await schedule(context.application, task_id, task, new_time_str, chat_id)
        label = "15 minutes" if minutes == 15 else "1 hour"
        await query.edit_message_text(
            f"Snoozed {label}. Reminding you at {new_time.strftime('%I:%M %p')}."
        )

# ── Startup: reschedule tasks that survived a restart ─────────────────────────
async def reschedule_on_startup(app: Application):
    cursor.execute(
        "SELECT id, chat_id, task, remind_at FROM tasks "
        "WHERE status='pending' AND remind_at IS NOT NULL"
    )
    for task_id, chat_id, task, remind_at in cursor.fetchall():
        try:
            await schedule(app, task_id, task, remind_at, chat_id)
        except Exception:
            pass

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("list",    handle_list))
    app.add_handler(CommandHandler("clear",   handle_done_all))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))

    scheduler.start()
    app.post_init = reschedule_on_startup
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
