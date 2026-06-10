# TradingView Telegram Signal Bot

A Python Telegram bot that receives TradingView webhook alerts and forwards them as formatted messages to a Telegram chat/channel. Supports manual commands for control and signal history.

## Run & Operate

- `python artifacts/telegram-bot/bot.py` — run the bot (starts both Flask webhook server + Telegram polling)
- Workflow: **Telegram Trading Bot** — auto-managed, runs on port 6000

## Stack

- Python 3
- `python-telegram-bot` — Telegram Bot API polling
- `flask` — webhook HTTP server
- Signal history stored as JSON in `artifacts/telegram-bot/data/signals.json`
- State (active/paused) stored in `artifacts/telegram-bot/data/state.json`

## Where things live

- `artifacts/telegram-bot/bot.py` — main bot file (all logic in one file)
- `artifacts/telegram-bot/data/signals.json` — persisted signal log (last 500)
- `artifacts/telegram-bot/data/state.json` — active/paused state

## Architecture decisions

- Bot runs both Flask (webhook receiver) and Telegram polling in a single process using threads
- Signal forwarding uses a fresh asyncio event loop per request (avoids event loop conflicts with python-telegram-bot's internal loop)
- Signals are stored as raw JSON with a received_at timestamp — last 500 kept, oldest dropped
- Bot active/paused state persists to disk so it survives restarts

## Product

- Receives TradingView alerts via HTTP webhook POST to `/webhook/tradingview`
- Formats and forwards signals to a configured Telegram chat with emoji, price info, timeframe, strategy name
- Telegram commands: `/start`, `/stop`, `/status`, `/history`, `/clear`, `/help`
- Health endpoint at `/health`, signal log at `/signals`

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- Port 6000 is used for the webhook Flask server (8080 is taken by mockup-sandbox)
- The webhook URL for TradingView must use the public Replit domain, not localhost
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be set as Replit secrets

## Secrets required

- `TELEGRAM_BOT_TOKEN` — from @BotFather on Telegram
- `TELEGRAM_CHAT_ID` — the chat/group/channel ID to send signals to

## TradingView Setup

Webhook URL: `https://<your-repl-domain>/webhook/tradingview`

Alert message JSON template:
```json
{"action":"{{strategy.order.action}}","ticker":"{{ticker}}","price":"{{close}}","timeframe":"{{interval}}","strategy":"Your Strategy Name","comment":"{{strategy.order.comment}}"}
```

## Telegram Commands

- `/start` — Activate bot & show webhook URL
- `/stop` — Pause signal forwarding
- `/status` — Show status and signal count
- `/history` — Last 10 signals
- `/clear` — Clear signal history
- `/help` — Show help

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
