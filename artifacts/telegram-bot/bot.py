import os
import json
import logging
import asyncio
import threading
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
LEVELS_FILE = DATA_DIR / "levels.json"

flask_app = Flask(__name__)
_main_loop: asyncio.AbstractEventLoop = None
_telegram_app: Application = None

# Default auto-calc percentages (can be tuned here)
AUTO_CALC = {
    "sl_pct":  2.0,
    "tp1_pct": 2.0,
    "tp2_pct": 3.5,
    "tp3_pct": 5.5,
}


# ── Persistence helpers ────────────────────────────────────────────────────────

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


def load_levels() -> dict:
    """Returns dict of ticker -> {entry, sl, tp1, tp2, tp3}."""
    if LEVELS_FILE.exists():
        try:
            return json.loads(LEVELS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_levels(levels: dict):
    LEVELS_FILE.write_text(json.dumps(levels, indent=2))


# ── Level calculation ──────────────────────────────────────────────────────────

def _pct_change(base: float, pct: float, direction: float) -> float:
    """direction: +1 for up, -1 for down."""
    return base * (1 + direction * pct / 100)


def _fmt_price(value: float) -> str:
    """Format a price value cleanly."""
    if value >= 1000:
        return f"{value:,.2f}"
    elif value >= 1:
        return f"{value:.4f}"
    else:
        return f"{value:.6f}"


def _pct_diff(entry: float, target: float) -> str:
    diff = (target - entry) / entry * 100
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.1f}%"


def compute_levels(action: str, price: float, ticker: str, override_data: dict) -> dict:
    """
    Return a dict with entry, sl, tp1, tp2, tp3.
    Priority: webhook payload fields > stored manual levels > auto-calc.
    """
    stored = load_levels().get(ticker.upper(), {})

    # Start with auto-calc
    is_long = action in ("BUY", "LONG")
    is_short = action in ("SELL", "SHORT")

    if is_long:
        sl_dir, tp_dir = -1, +1
    elif is_short:
        sl_dir, tp_dir = +1, -1
    else:
        sl_dir, tp_dir = -1, +1  # neutral default

    auto = {
        "entry": price,
        "sl":    _pct_change(price, AUTO_CALC["sl_pct"],  sl_dir),
        "tp1":   _pct_change(price, AUTO_CALC["tp1_pct"], tp_dir),
        "tp2":   _pct_change(price, AUTO_CALC["tp2_pct"], tp_dir),
        "tp3":   _pct_change(price, AUTO_CALC["tp3_pct"], tp_dir),
    }

    # Merge: stored manual levels override auto
    merged = {**auto, **{k: float(v) for k, v in stored.items() if v is not None}}

    # Webhook payload fields take highest priority
    for key in ("entry", "sl", "tp1", "tp2", "tp3"):
        if key in override_data:
            try:
                merged[key] = float(override_data[key])
            except (ValueError, TypeError):
                pass

    return merged


# ── Message formatting ─────────────────────────────────────────────────────────

def format_signal_message(data: dict) -> str:
    action = data.get("action", "").upper()
    ticker = data.get("ticker", data.get("symbol", "UNKNOWN")).upper()
    timeframe = data.get("timeframe", data.get("interval", ""))
    strategy = data.get("strategy", data.get("strategy_name", ""))
    comment = data.get("comment", data.get("message", ""))
    exchange = data.get("exchange", "")
    volume = data.get("volume", "")

    # Parse current price
    raw_price = data.get("price", data.get("close", None))
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        price = None

    if action in ("BUY", "LONG"):
        header_emoji, direction = "🟢", "BUY / LONG"
    elif action in ("SELL", "SHORT"):
        header_emoji, direction = "🔴", "SELL / SHORT"
    elif action in ("CLOSE", "EXIT"):
        header_emoji, direction = "⚪", "CLOSE / EXIT"
    else:
        header_emoji, direction = "📊", action or "SIGNAL"

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"{header_emoji} <b>{direction}</b>",
        f"📌 <b>Ticker:</b> <code>{ticker}</code>",
    ]

    if exchange:
        lines.append(f"🏦 <b>Exchange:</b> {exchange}")
    if timeframe:
        lines.append(f"⏱ <b>Timeframe:</b> {timeframe}")
    if strategy:
        lines.append(f"🧠 <b>Strategy:</b> {strategy}")

    lines.append("")

    # ── Levels block ──
    if price is not None and action in ("BUY", "LONG", "SELL", "SHORT"):
        lvl = compute_levels(action, price, ticker, data)

        entry = lvl["entry"]
        sl    = lvl["sl"]
        tp1   = lvl["tp1"]
        tp2   = lvl["tp2"]
        tp3   = lvl["tp3"]

        lines += [
            "📊 <b>LEVELS</b>",
            f"┣ 🎯 <b>Entry:</b>     <code>{_fmt_price(entry)}</code>",
            f"┣ 🛑 <b>Stop Loss:</b>  <code>{_fmt_price(sl)}</code>  <i>({_pct_diff(entry, sl)})</i>",
            f"┣ 💚 <b>TP1:</b>        <code>{_fmt_price(tp1)}</code>  <i>({_pct_diff(entry, tp1)})</i>",
            f"┣ 💛 <b>TP2:</b>        <code>{_fmt_price(tp2)}</code>  <i>({_pct_diff(entry, tp2)})</i>",
            f"┗ 🏆 <b>TP3:</b>        <code>{_fmt_price(tp3)}</code>  <i>({_pct_diff(entry, tp3)})</i>",
            "",
        ]
    elif price is not None:
        lines.append(f"💰 <b>Price:</b> <code>{_fmt_price(price)}</code>\n")

    if volume:
        lines.append(f"📦 <b>Volume:</b> <code>{volume}</code>")
    if comment:
        lines.append(f"💬 <b>Note:</b> {comment}")

    lines.append(f"\n🕐 {now}")
    return "\n".join(lines)


# ── Thread-safe Telegram send ──────────────────────────────────────────────────

async def _send_message_async(text: str):
    await _telegram_app.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


def send_telegram_message_sync(text: str):
    if _main_loop is None or not _main_loop.is_running():
        raise RuntimeError("Main event loop is not running")
    future = asyncio.run_coroutine_threadsafe(_send_message_async(text), _main_loop)
    future.result(timeout=15)


# ── Flask routes ───────────────────────────────────────────────────────────────

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

    signal = {"received_at": datetime.utcnow().isoformat(), "data": data}
    save_signal(signal)

    try:
        message = format_signal_message(data)
        send_telegram_message_sync(message)
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return jsonify({"status": "error", "reason": str(e)}), 500

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


# ── Telegram command handlers ──────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_bot_active(True)
    signals = load_signals()
    domain = os.environ.get("REPLIT_DOMAINS", "your-repl.replit.app").split(",")[0]
    webhook_url = f"https://{domain}/webhook/tradingview"
    text = (
        "✅ <b>TradingView Signal Bot is ACTIVE</b>\n\n"
        f"📡 <b>Signals received:</b> {len(signals)}\n\n"
        f"🔗 <b>Webhook URL:</b>\n<code>{webhook_url}</code>\n\n"
        "<b>TradingView alert message template:</b>\n"
        '<code>{"action":"{{strategy.order.action}}","ticker":"{{ticker}}",'
        '"price":"{{close}}","timeframe":"{{interval}}","strategy":"My Strategy",'
        '"comment":"{{strategy.order.comment}}"}</code>\n\n'
        "<b>You can also pass levels directly from TradingView:</b>\n"
        '<code>{"action":"BUY","ticker":"BTCUSDT","price":"{{close}}",'
        '"sl":"{{plot_0}}","tp1":"{{plot_1}}","tp2":"{{plot_2}}","tp3":"{{plot_3}}"}</code>\n\n'
        "<b>Commands:</b>\n"
        "/start — Activate &amp; show webhook URL\n"
        "/stop — Pause signal forwarding\n"
        "/status — Show current status\n"
        "/history — Last 10 signals\n"
        "/setlevels — Set manual levels for a ticker\n"
        "/clearlevels — Remove manual levels for a ticker\n"
        "/levels — Show all configured manual levels\n"
        "/clear — Clear signal history\n"
        "/help — Show this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_bot_active(False)
    await update.message.reply_text(
        "⏸ <b>Bot PAUSED</b>\n\nSignals will be received but not forwarded.\nUse /start to resume.",
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
        last_signal = (
            f"{d.get('action','?').upper()} "
            f"{d.get('ticker', d.get('symbol','?')).upper()} "
            f"@ {d.get('price', d.get('close','?'))}"
        )
    else:
        last_signal = "—"

    await update.message.reply_text(
        f"📊 <b>Bot Status: {status_emoji}</b>\n\n"
        f"📈 <b>Total signals logged:</b> {len(signals)}\n"
        f"🕐 <b>Last signal time:</b> {last_str}\n"
        f"📡 <b>Last signal:</b> {last_signal}",
        parse_mode=ParseMode.HTML,
    )


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
        em = "🟢" if action in ("BUY", "LONG") else ("🔴" if action in ("SELL", "SHORT") else "⚪")
        lines.append(f"{em} <code>{ts}</code> — <b>{action}</b> <code>{ticker}</code> @ {price}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SIGNALS_FILE.write_text("[]")
    await update.message.reply_text("🗑 Signal history cleared.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_setlevels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /setlevels BTCUSDT 67500 65000 69000 71000 75000
           (ticker entry sl tp1 tp2 tp3)
    """
    usage = (
        "⚙️ <b>Usage:</b>\n"
        "<code>/setlevels TICKER entry sl tp1 tp2 tp3</code>\n\n"
        "<b>Example:</b>\n"
        "<code>/setlevels BTCUSDT 67500 65000 69000 71000 75000</code>\n\n"
        "Set any value to <code>0</code> or <code>-</code> to keep auto-calculated for that field.\n"
        "Use /clearlevels TICKER to remove all manual levels."
    )

    args = context.args
    if not args or len(args) < 6:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    ticker = args[0].upper()
    keys = ["entry", "sl", "tp1", "tp2", "tp3"]
    levels = load_levels()
    entry = levels.get(ticker, {})

    for i, key in enumerate(keys):
        raw = args[i + 1]
        if raw in ("0", "-", "auto"):
            entry.pop(key, None)  # remove so auto-calc is used
        else:
            try:
                entry[key] = float(raw)
            except ValueError:
                await update.message.reply_text(
                    f"❌ Invalid value for <b>{key}</b>: <code>{raw}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

    levels[ticker] = entry
    save_levels(levels)

    lines = [f"✅ <b>Manual levels saved for {ticker}:</b>\n"]
    label_map = {"entry": "🎯 Entry", "sl": "🛑 Stop Loss", "tp1": "💚 TP1", "tp2": "💛 TP2", "tp3": "🏆 TP3"}
    for key in keys:
        val = entry.get(key)
        label = label_map[key]
        if val is not None:
            lines.append(f"{label}: <code>{_fmt_price(val)}</code>")
        else:
            lines.append(f"{label}: <i>auto-calculated</i>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_clearlevels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /clearlevels BTCUSDT"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚙️ <b>Usage:</b> <code>/clearlevels TICKER</code>\n"
            "Example: <code>/clearlevels BTCUSDT</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    ticker = args[0].upper()
    levels = load_levels()
    if ticker in levels:
        del levels[ticker]
        save_levels(levels)
        await update.message.reply_text(
            f"🗑 Manual levels cleared for <b>{ticker}</b>. Levels will now be auto-calculated.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"ℹ️ No manual levels were set for <b>{ticker}</b>.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all stored manual levels."""
    levels = load_levels()
    if not levels:
        await update.message.reply_text(
            "📭 No manual levels configured.\n\n"
            "All tickers use auto-calculated levels.\n"
            "Use /setlevels to configure a ticker.",
        )
        return

    label_map = {"entry": "🎯 Entry", "sl": "🛑 SL", "tp1": "💚 TP1", "tp2": "💛 TP2", "tp3": "🏆 TP3"}
    lines = ["📋 <b>Configured Manual Levels:</b>\n"]
    for ticker, vals in levels.items():
        lines.append(f"<b>{ticker}</b>")
        for key in ["entry", "sl", "tp1", "tp2", "tp3"]:
            if key in vals:
                lines.append(f"  {label_map[key]}: <code>{_fmt_price(vals[key])}</code>")
        lines.append("")

    lines.append("<i>Use /clearlevels TICKER to reset to auto-calc.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Main async entry point ─────────────────────────────────────────────────────

async def main():
    global _main_loop, _telegram_app

    _main_loop = asyncio.get_running_loop()

    _telegram_app = Application.builder().token(BOT_TOKEN).build()
    _telegram_app.add_handler(CommandHandler("start", cmd_start))
    _telegram_app.add_handler(CommandHandler("stop", cmd_stop))
    _telegram_app.add_handler(CommandHandler("status", cmd_status))
    _telegram_app.add_handler(CommandHandler("history", cmd_history))
    _telegram_app.add_handler(CommandHandler("clear", cmd_clear))
    _telegram_app.add_handler(CommandHandler("help", cmd_help))
    _telegram_app.add_handler(CommandHandler("setlevels", cmd_setlevels))
    _telegram_app.add_handler(CommandHandler("clearlevels", cmd_clearlevels))
    _telegram_app.add_handler(CommandHandler("levels", cmd_levels))

    flask_thread = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True,
        name="flask-webhook",
    )
    flask_thread.start()
    logger.info(f"Flask webhook server started on port {PORT}")

    logger.info("Starting Telegram bot polling...")
    async with _telegram_app:
        await _telegram_app.initialize()
        await _telegram_app.start()
        await _telegram_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Bot is live — polling for Telegram updates")

        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await _telegram_app.updater.stop()
            await _telegram_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
