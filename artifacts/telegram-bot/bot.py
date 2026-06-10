import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PORT = int(os.environ.get("PORT", 6000))

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SIGNALS_FILE = DATA_DIR / "signals.json"
STATE_FILE = DATA_DIR / "state.json"

flask_app = Flask(__name__)
telegram_app: Application = None
bot_active = True


def load_signals() -> list:
    if SIGNALS_FILE.exists():
        try:
            return json.loads(SIGNALS_FILE.read_text())
        except Exception:
            return []
    return []


def save_signal(signal: dict):
    signals = load_signals()
    signals.append(signal)
    signals = signals[-500:]
    SIGNALS_FILE.write_text(json.dumps(signals, indent=2))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"active": True}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


def get_bot_active() -> bool:
    return load_state().get("active", True)


def set_bot_active(value: bool):
    save_state({"active": value})


def format_signal_message(data: dict) -> str:
    action = data.get("action", "").upper()
    ticker = data.get("ticker", data.get("symbol", "UNKNOWN")).upper()
    price = data.get("price", data.get("close", "N/A"))
    timeframe = data.get("timeframe", data.get("interval", ""))
    strategy = data.get("strategy", data.get("strategy_name", ""))
    comment = data.get("comment", data.get("message", ""))
    exchange = data.get("exchange", "")
    volume = data.get("volume", "")
    high = data.get("high", "")
    low = data.get("low", "")
    open_price = data.get("open", "")

    if action in ("BUY", "LONG"):
        emoji = "🟢"
        direction = "BUY / LONG"
    elif action in ("SELL", "SHORT"):
        emoji = "🔴"
        direction = "SELL / SHORT"
    elif action in ("CLOSE", "EXIT"):
        emoji = "⚪"
        direction = "CLOSE / EXIT"
    else:
        emoji = "📊"
        direction = action or "SIGNAL"

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"{emoji} <b>{direction}</b> — <code>{ticker}</code>",
        "",
    ]

    if price and price != "N/A":
        lines.append(f"💰 <b>Price:</b> <code>{price}</code>")
    if open_price:
        lines.append(f"📂 <b>Open:</b> <code>{open_price}</code>")
    if high:
        lines.append(f"📈 <b>High:</b> <code>{high}</code>")
    if low:
        lines.append(f"📉 <b>Low:</b> <code>{low}</code>")
    if volume:
        lines.append(f"📦 <b>Volume:</b> <code>{volume}</code>")
    if exchange:
        lines.append(f"🏦 <b>Exchange:</b> {exchange}")
    if timeframe:
        lines.append(f"⏱ <b>Timeframe:</b> {timeframe}")
    if strategy:
        lines.append(f"🧠 <b>Strategy:</b> {strategy}")
    if comment:
        lines.append(f"💬 <b>Note:</b> {comment}")

    lines += ["", f"🕐 {now}"]
    return "\n".join(lines)


async def send_telegram_message(text: str):
    bot = Bot(token=BOT_TOKEN)
    await bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


@flask_app.route("/webhook/tradingview", methods=["POST"])
def tradingview_webhook():
    if not get_bot_active():
        return jsonify({"status": "ignored", "reason": "bot is paused"}), 200

    try:
        if request.is_json:
            data = request.get_json(force=True)
        else:
            raw = request.data.decode("utf-8").strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"message": raw, "action": "SIGNAL"}
    except Exception as e:
        logger.error(f"Failed to parse webhook: {e}")
        return jsonify({"error": "invalid payload"}), 400

    if not data:
        return jsonify({"error": "empty payload"}), 400

    signal = {
        "received_at": datetime.utcnow().isoformat(),
        "data": data,
    }
    save_signal(signal)

    message = format_signal_message(data)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(send_telegram_message(message))
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return jsonify({"status": "error", "reason": str(e)}), 500
    finally:
        loop.close()

    logger.info(f"Signal forwarded: {data.get('action','?')} {data.get('ticker','?')}")
    return jsonify({"status": "ok"}), 200


@flask_app.route("/health", methods=["GET"])
def health():
    signals = load_signals()
    return jsonify({
        "status": "ok",
        "bot_active": get_bot_active(),
        "total_signals": len(signals),
        "last_signal": signals[-1]["received_at"] if signals else None,
    })


@flask_app.route("/signals", methods=["GET"])
def get_signals():
    signals = load_signals()
    limit = int(request.args.get("limit", 20))
    return jsonify({"signals": signals[-limit:][::-1], "total": len(signals)})


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_bot_active(True)
    signals = load_signals()
    webhook_url = f"https://{os.environ.get('REPLIT_DOMAINS', 'your-repl.replit.app').split(',')[0]}/webhook/tradingview"
    text = (
        "✅ <b>TradingView Signal Bot is ACTIVE</b>\n\n"
        f"📡 <b>Signals received:</b> {len(signals)}\n\n"
        f"🔗 <b>TradingView Webhook URL:</b>\n<code>{webhook_url}</code>\n\n"
        "<b>How to set up TradingView alerts:</b>\n"
        "1. Open an alert in TradingView\n"
        "2. Set <b>Notifications → Webhook URL</b> to the URL above\n"
        "3. In the <b>Message</b> field, use JSON:\n"
        '<code>{"action":"{{strategy.order.action}}","ticker":"{{ticker}}","price":"{{close}}","timeframe":"{{interval}}","strategy":"Your Strategy Name"}</code>\n\n'
        "<b>Commands:</b>\n"
        "/start — Activate & show webhook URL\n"
        "/stop — Pause signal forwarding\n"
        "/status — Show current status\n"
        "/history — Last 10 signals\n"
        "/clear — Clear signal history\n"
        "/help — Show this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_bot_active(False)
    await update.message.reply_text(
        "⏸ <b>Bot PAUSED</b>\n\nSignals will be received but not forwarded to this chat.\nUse /start to resume.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_bot_active()
    signals = load_signals()
    last = signals[-1] if signals else None
    status_emoji = "✅ ACTIVE" if active else "⏸ PAUSED"
    last_str = last["received_at"] if last else "No signals yet"
    if last:
        d = last["data"]
        last_signal = f"{d.get('action','?').upper()} {d.get('ticker', d.get('symbol','?')).upper()} @ {d.get('price', d.get('close','?'))}"
    else:
        last_signal = "—"

    text = (
        f"📊 <b>Bot Status: {status_emoji}</b>\n\n"
        f"📈 <b>Total signals logged:</b> {len(signals)}\n"
        f"🕐 <b>Last signal time:</b> {last_str}\n"
        f"📡 <b>Last signal:</b> {last_signal}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    signals = load_signals()
    if not signals:
        await update.message.reply_text("📭 No signals received yet.")
        return

    recent = signals[-10:][::-1]
    lines = ["📋 <b>Last 10 signals:</b>\n"]
    for s in recent:
        d = s["data"]
        action = d.get("action", "?").upper()
        ticker = d.get("ticker", d.get("symbol", "?")).upper()
        price = d.get("price", d.get("close", "?"))
        ts = s["received_at"][:16].replace("T", " ")
        if action in ("BUY", "LONG"):
            em = "🟢"
        elif action in ("SELL", "SHORT"):
            em = "🔴"
        else:
            em = "⚪"
        lines.append(f"{em} <code>{ts}</code> — <b>{action}</b> <code>{ticker}</code> @ {price}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SIGNALS_FILE.write_text("[]")
    await update.message.reply_text("🗑 Signal history cleared.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


def run_telegram_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("Starting Telegram bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


def run_flask():
    logger.info(f"Starting Flask webhook server on port {PORT}...")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    import threading

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    run_telegram_bot()
