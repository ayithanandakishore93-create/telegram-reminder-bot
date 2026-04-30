# Reminder Bot — Setup Guide

## Step 1: Create your Telegram bot (5 min)
1. Open Telegram → search @BotFather
2. Send /newbot → pick a name
3. Copy the token → this is your BOT_TOKEN

## Step 2: Get your OpenRouter key (2 min)
1. Go to https://openrouter.ai
2. Sign up → go to Keys → Create Key
3. Copy it → this is your OPENROUTER_KEY
4. The default model (Llama 3.3 70B) is FREE on OpenRouter

## Step 3: Deploy to Railway (10 min)
1. Push this folder to a GitHub repo
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Connect your repo
4. Go to Variables tab, add these:
   BOT_TOKEN      = (from Step 1)
   OPENROUTER_KEY = (from Step 2)
   DB_PATH        = /data/tasks.db
5. Go to your service → Storage → Add Volume → mount at /data
   (keeps your tasks if Railway restarts)
6. Railway auto-detects the Procfile and starts the bot

## Step 4: Use it
Send your bot on Telegram:
  "Remind me to submit the BLACKLEAF deck tomorrow at 5pm"
  "Call Adarsh tonight at 8"
  "Team standup Friday 10am"
  [send a voice note — it transcribes automatically]

Commands:
  /list  → see all pending reminders
  /clear → clear everything

When a reminder fires, tap:
  Done       → marked complete
  Snooze 15m → reminds you in 15 minutes
  Snooze 1h  → reminds you in 1 hour

## Want a different AI model?
Add this env variable in Railway:
  AI_MODEL = google/gemini-flash-1.5        (fast, cheap)
  AI_MODEL = anthropic/claude-haiku-4-5     (best quality)
  AI_MODEL = meta-llama/llama-3.3-70b-instruct:free  (default, free)

Full model list: https://openrouter.ai/models
