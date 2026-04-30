# Reminder Bot — Setup Guide

## Step 1: Create your Telegram bot
1. Open Telegram, search for @BotFather
2. Send /newbot
3. Pick a name (e.g. "Nandu Reminder Bot")
4. Copy the token it gives you — this is your BOT_TOKEN

## Step 2: Get a free Gemini API key
1. Go to https://aistudio.google.com/app/apikey
2. Click "Create API key"
3. Copy it — this is your GEMINI_KEY

## Step 3: Deploy to Railway
1. Go to https://railway.app and sign up (free)
2. Click "New Project" → "Deploy from GitHub repo"
3. Push this folder to a GitHub repo first, then connect it
   OR use "Deploy from template" → choose empty project → upload files

4. In Railway project settings → Variables, add:
   BOT_TOKEN  = (your token from Step 1)
   GEMINI_KEY = (your key from Step 2)
   DB_PATH    = /data/tasks.db

5. In Railway → go to your service → Storage → Add Volume
   Mount path: /data
   (This makes sure your tasks survive restarts)

6. Railway will auto-detect the Procfile and run the bot

## Step 4: Use the bot
Send any of these to your bot on Telegram:

  "Remind me to submit the assignment tomorrow at 5pm"
  "Call Adarsh tonight at 8"
  "Team meeting Friday 10am"
  [voice note saying any of the above]

Commands:
  /list  — see all pending reminders
  /clear — clear all reminders

When a reminder fires, tap:
  Done      → marks it complete
  Snooze 15m → reminds you in 15 minutes
  Snooze 1h  → reminds you in 1 hour

## That's it. The bot runs 24/7 for free.
