# Telegram Bot — Deploy to Render, Heroku, and Koyeb

This project is configured for long polling. Use a **Render worker** (no port) or **Heroku/Koyeb web** (binds to `$PORT`).

## Env vars
TELEGRAM_BOT_TOKEN (required), OWNER_ID, ADMIN_IDS, MANDATORY_CHANNELS, CONTACT_ADMIN_LINK, LOG_CHANNEL_ID, DATA_FILE.

## Render (Worker)
- Create a **Worker** service from repo (or `render.yaml`).
- Add **Persistent Disk** at `/data`.
- Set env vars.
- Start command: `python bot.py`.

## Heroku
- `Procfile` uses `web: python bot.py` (binds `$PORT` via lightweight Flask server).
- Set env vars and deploy.

## Koyeb
- Buildpack runtime, set env vars, runs `python bot.py`, exposes `/health`.

