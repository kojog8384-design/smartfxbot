import os
import json
import logging
import asyncio
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from telegram import Update
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
SIGNALS_FILE  = DATA_DIR / "signals.json"
STATE_FILE    = DATA_DIR / "state.json"
LEVELS_FILE   = DATA_DIR / "levels.json"

flask_app = Flask(__name__)
_main_loop: asyncio.AbstractEventLoop = None
_telegram_app: Application = None

# Default auto-calc percentages for SL/TP
AUTO_CALC = {"sl_pct": 2.0, "tp1_pct": 2.0, "tp2_pct": 3.5, "tp3_pct": 5.5}

# Timeframe → estimated swing range (%)
TF_SWING_PCT = {
    "1": 0.4, "3": 0.6, "5": 0.8,
    "15": 1.2, "30": 1.8,
    "60": 2.5, "1H": 2.5, "2H": 3.5,
    "240": 4.5, "4H": 4.5,
    "D": 6.0, "1D": 6.0, "W": 10.0, "1W": 10.0,
}

FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


# ── Persistence ────────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default

def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))

def load_signals() -> list:   return load_json(SIGNALS_FILE, [])
def load_state() -> dict:     return load_json(STATE_FILE, {"active": True})
def load_levels() -> dict:    return load_json(LEVELS_FILE, {})

def save_signal(sig):
    sigs = load_signals(); sigs.append(sig); save_json(SIGNALS_FILE, sigs[-500:])
def save_state(s):  save_json(STATE_FILE, s)
def save_levels(l): save_json(LEVELS_FILE, l)

def get_bot_active() -> bool: return load_state().get("active", True)
def set_bot_active(v: bool):  save_state({"active": v})


# ── Number helpers ─────────────────────────────────────────────────────────────

def fmt(v: float) -> str:
    if v >= 1000:   return f"{v:,.2f}"
    elif v >= 1:    return f"{v:.4f}"
    else:           return f"{v:.6f}"

def pct_diff(entry: float, target: float) -> str:
    d = (target - entry) / entry * 100
    return f"{'+'if d>=0 else ''}{d:.1f}%"

def _pct(base: float, pct: float, direction: float) -> float:
    return base * (1 + direction * pct / 100)


# ── Fibonacci ──────────────────────────────────────────────────────────────────

def compute_fib_levels(high: float, low: float) -> dict[float, float]:
    diff = high - low
    return {r: high - r * diff for r in FIB_RATIOS}

def nearest_fib(price: float, fibs: dict[float, float]) -> tuple[float, float]:
    """Return (ratio, level) of the fib level closest to price."""
    return min(fibs.items(), key=lambda kv: abs(kv[1] - price))

def swing_from_data(data: dict, price: float) -> tuple[float, float]:
    """
    Determine swing_high and swing_low from the payload.
    Priority: explicit swing_high/swing_low > bar high/low > timeframe estimate.
    """
    sh = data.get("swing_high") or data.get("swingHigh")
    sl_ = data.get("swing_low") or data.get("swingLow")

    if sh and sl_:
        try:
            return float(sh), float(sl_)
        except (ValueError, TypeError):
            pass

    bh = data.get("high")
    bl = data.get("low")
    if bh and bl:
        try:
            h, l = float(bh), float(bl)
            if h > price and l < price:
                return h, l
        except (ValueError, TypeError):
            pass

    tf = str(data.get("timeframe", data.get("interval", "60"))).upper()
    pct = TF_SWING_PCT.get(tf, 2.5)
    return price * (1 + pct / 100), price * (1 - pct / 100)


# ── SMC Analysis ───────────────────────────────────────────────────────────────

def compute_smc(action: str, price: float, high: float, low: float,
                fibs: dict, data: dict) -> dict:
    """
    Estimate SMC zones from fib levels + payload overrides.
    All estimates are marked with is_estimated=True.
    """
    is_long  = action in ("BUY", "LONG")
    is_short = action in ("SELL", "SHORT")

    # ── Order Block ──
    # Bullish OB = golden pocket zone (0.618–0.786 fib from above)
    # Bearish OB = 0.236–0.382 zone (supply area)
    if is_long:
        ob_lo_key, ob_hi_key = 0.786, 0.618
    elif is_short:
        ob_lo_key, ob_hi_key = 0.236, 0.382
    else:
        ob_lo_key, ob_hi_key = 0.618, 0.5

    ob_lo = data.get("ob_low")
    ob_hi = data.get("ob_high")
    ob_est = True
    if ob_lo and ob_hi:
        try:
            ob_lo, ob_hi, ob_est = float(ob_lo), float(ob_hi), False
        except (ValueError, TypeError):
            ob_lo, ob_hi = None, None

    if ob_lo is None or ob_hi is None:
        ob_lo = fibs[ob_lo_key]
        ob_hi = fibs[ob_hi_key]
        if ob_lo > ob_hi:
            ob_lo, ob_hi = ob_hi, ob_lo

    # ── Fair Value Gap ──
    # Imbalance zone between 0.5 and 0.618 (price moves through fast, leaving a gap)
    fvg_lo = data.get("fvg_low")
    fvg_hi = data.get("fvg_high")
    fvg_est = True
    if fvg_lo and fvg_hi:
        try:
            fvg_lo, fvg_hi, fvg_est = float(fvg_lo), float(fvg_hi), False
        except (ValueError, TypeError):
            fvg_lo, fvg_hi = None, None

    if fvg_lo is None or fvg_hi is None:
        fvg_lo = min(fibs[0.5], fibs[0.618])
        fvg_hi = max(fibs[0.5], fibs[0.618])

    # ── Liquidity Zones ──
    liq_high = data.get("liq_high")
    liq_low  = data.get("liq_low")
    liq_est  = True
    if liq_high and liq_low:
        try:
            liq_high, liq_low, liq_est = float(liq_high), float(liq_low), False
        except (ValueError, TypeError):
            liq_high, liq_low = None, None

    if liq_high is None or liq_low is None:
        # Liquidity sits just beyond the swing extremes (stop clusters)
        liq_high = high * 1.003
        liq_low  = low  * 0.997

    # ── BOS (Break of Structure) ──
    bos_raw = data.get("bos")
    if bos_raw:
        bos = str(bos_raw)
        bos_est = False
    else:
        bos = "Bullish" if is_long else ("Bearish" if is_short else "Neutral")
        bos_est = True

    # ── CHoCH (Change of Character) ──
    choch_raw   = data.get("choch")
    choch_level = None
    choch_est   = True
    if choch_raw:
        try:
            choch_level = float(choch_raw)
            choch_est = False
        except (ValueError, TypeError):
            choch_level = None

    if choch_level is None:
        # CHoCH is typically at the key structure level opposite to current move
        choch_level = low if is_long else high

    return {
        "ob":         (ob_lo, ob_hi, "Bullish" if is_long else "Bearish", ob_est),
        "fvg":        (fvg_lo, fvg_hi, fvg_est),
        "liq_high":   (liq_high, liq_est),
        "liq_low":    (liq_low, liq_est),
        "bos":        (bos, bos_est),
        "choch":      (choch_level, choch_est),
    }


# ── Trade levels (entry / SL / TP) ────────────────────────────────────────────

def compute_levels(action: str, price: float, ticker: str, data: dict) -> dict:
    stored  = load_levels().get(ticker.upper(), {})
    is_long = action in ("BUY", "LONG")
    is_short= action in ("SELL", "SHORT")
    sl_dir  = -1 if is_long else +1
    tp_dir  = +1 if is_long else -1

    auto = {
        "entry": price,
        "sl":  _pct(price, AUTO_CALC["sl_pct"],  sl_dir),
        "tp1": _pct(price, AUTO_CALC["tp1_pct"], tp_dir),
        "tp2": _pct(price, AUTO_CALC["tp2_pct"], tp_dir),
        "tp3": _pct(price, AUTO_CALC["tp3_pct"], tp_dir),
    }
    merged = {**auto, **{k: float(v) for k, v in stored.items() if v is not None}}
    for key in ("entry", "sl", "tp1", "tp2", "tp3"):
        if key in data:
            try:
                merged[key] = float(data[key])
            except (ValueError, TypeError):
                pass
    return merged


# ── Signal message formatter ───────────────────────────────────────────────────

def _e(is_estimated: bool) -> str:
    return " <i>~</i>" if is_estimated else ""

def format_signal_message(data: dict) -> str:
    action    = data.get("action", "").upper()
    ticker    = data.get("ticker", data.get("symbol", "UNKNOWN")).upper()
    timeframe = data.get("timeframe", data.get("interval", ""))
    strategy  = data.get("strategy", data.get("strategy_name", ""))
    comment   = data.get("comment", data.get("message", ""))
    exchange  = data.get("exchange", "")
    volume    = data.get("volume", "")

    raw_price = data.get("price", data.get("close", None))
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        price = None

    if action in ("BUY", "LONG"):
        hdr_emoji, direction = "🟢", "BUY / LONG"
    elif action in ("SELL", "SHORT"):
        hdr_emoji, direction = "🔴", "SELL / SHORT"
    elif action in ("CLOSE", "EXIT"):
        hdr_emoji, direction = "⚪", "CLOSE / EXIT"
    else:
        hdr_emoji, direction = "📊", action or "SIGNAL"

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ── Header ──
    lines = [
        f"{hdr_emoji} <b>{direction}</b>",
        f"📌 <b>Ticker:</b> <code>{ticker}</code>",
    ]
    meta = []
    if timeframe: meta.append(f"⏱ {timeframe}")
    if exchange:  meta.append(f"🏦 {exchange}")
    if meta:      lines.append("  ".join(meta))
    if strategy:  lines.append(f"🧠 <b>Strategy:</b> {strategy}")
    lines.append("")

    if price is None:
        lines.append(f"🕐 {now}")
        return "\n".join(lines)

    # ── Trade Levels ──
    show_levels = action in ("BUY", "LONG", "SELL", "SHORT")
    if show_levels:
        lvl = compute_levels(action, price, ticker, data)
        entry, sl, tp1, tp2, tp3 = lvl["entry"], lvl["sl"], lvl["tp1"], lvl["tp2"], lvl["tp3"]
        lines += [
            "📊 <b>TRADE LEVELS</b>",
            f"┣ 🎯 <b>Entry</b>      <code>{fmt(entry)}</code>",
            f"┣ 🛑 <b>Stop Loss</b>  <code>{fmt(sl)}</code>  <i>({pct_diff(entry, sl)})</i>",
            f"┣ 💚 <b>TP1</b>        <code>{fmt(tp1)}</code>  <i>({pct_diff(entry, tp1)})</i>",
            f"┣ 💛 <b>TP2</b>        <code>{fmt(tp2)}</code>  <i>({pct_diff(entry, tp2)})</i>",
            f"┗ 🏆 <b>TP3</b>        <code>{fmt(tp3)}</code>  <i>({pct_diff(entry, tp3)})</i>",
            "",
        ]
    else:
        lines.append(f"💰 <b>Price</b>  <code>{fmt(price)}</code>\n")

    # ── Fibonacci ──
    swing_hi, swing_lo = swing_from_data(data, price)
    fibs = compute_fib_levels(swing_hi, swing_lo)
    swing_est = not (data.get("swing_high") or data.get("swingHigh") or
                     (data.get("high") and data.get("low")))

    near_ratio, near_level = nearest_fib(price, fibs)

    fib_label = (
        f"🔢 <b>FIBONACCI</b>"
        f"  <code>{fmt(swing_lo)}</code> ↔ <code>{fmt(swing_hi)}</code>"
        + (" <i>(est.)</i>" if swing_est else "")
    )
    lines.append(fib_label)

    fib_symbols = {
        0.0: "┣", 0.236: "┣", 0.382: "┣",
        0.5: "┣", 0.618: "┣", 0.786: "┣", 1.0: "┗",
    }
    fib_emojis = {
        0.0: "⬜", 0.236: "🔵", 0.382: "🔵",
        0.5: "🟡", 0.618: "🔑", 0.786: "🟠", 1.0: "⬜",
    }
    for r in FIB_RATIOS:
        level = fibs[r]
        sym   = fib_symbols[r]
        em    = fib_emojis[r]
        tag   = "  ◀ <i>price</i>" if abs(level - price) / price < 0.005 else ""
        lines.append(f"{sym} {em} <code>{r:.3f}</code>  <code>{fmt(level)}</code>{tag}")
    lines.append("")

    # ── Smart Money Concepts ──
    if show_levels:
        smc = compute_smc(action, price, swing_hi, swing_lo, fibs, data)

        ob_lo, ob_hi, ob_dir, ob_est = smc["ob"]
        fvg_lo, fvg_hi, fvg_est      = smc["fvg"]
        liq_h, liq_h_est             = smc["liq_high"]
        liq_l, liq_l_est             = smc["liq_low"]
        bos_str, bos_est             = smc["bos"]
        choch_lvl, choch_est         = smc["choch"]

        bos_em  = "✅" if "Bull" in bos_str else ("🔻" if "Bear" in bos_str else "➡️")
        ob_em   = "🟦" if ob_dir == "Bullish" else "🟥"

        lines += [
            "🧠 <b>SMART MONEY CONCEPTS</b>",
            f"┣ {ob_em} <b>Order Block</b>  "
            f"<code>{fmt(ob_lo)}</code> – <code>{fmt(ob_hi)}</code>"
            f"  <i>({ob_dir}){_e(ob_est)}</i>",

            f"┣ 🌊 <b>Fair Value Gap</b>  "
            f"<code>{fmt(fvg_lo)}</code> – <code>{fmt(fvg_hi)}</code>"
            f"{_e(fvg_est)}",

            f"┣ 💧 <b>Liq. High</b>  <code>{fmt(liq_h)}</code>"
            f"  <i>(Buy-side){_e(liq_h_est)}</i>",

            f"┣ 💧 <b>Liq. Low</b>   <code>{fmt(liq_l)}</code>"
            f"  <i>(Sell-side){_e(liq_l_est)}</i>",

            f"┣ {bos_em} <b>BOS</b>  {bos_str}"
            f"{_e(bos_est)}",

            f"┗ ⚡ <b>CHoCH</b>  <code>{fmt(choch_lvl)}</code>"
            f"  <i>(watch level){_e(choch_est)}</i>",
            "",
        ]

    # ── Footer ──
    if volume:  lines.append(f"📦 <b>Volume</b>  <code>{volume}</code>")
    if comment: lines.append(f"💬 <b>Note</b>  {comment}")
    lines.append(f"🕐 {now}")
    return "\n".join(lines)


# ── Thread-safe Telegram send ──────────────────────────────────────────────────

async def _send_message_async(text: str):
    await _telegram_app.bot.send_message(
        chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML,
    )

def send_telegram_message_sync(text: str):
    if _main_loop is None or not _main_loop.is_running():
        raise RuntimeError("Main event loop is not running")
    asyncio.run_coroutine_threadsafe(_send_message_async(text), _main_loop).result(timeout=15)


# ── Flask routes ───────────────────────────────────────────────────────────────

@flask_app.route("/webhook/tradingview", methods=["POST"])
def tradingview_webhook():
    if not get_bot_active():
        return jsonify({"status": "ignored", "reason": "bot is paused"}), 200

    try:
        data = request.get_json(force=True) if request.is_json else (
            lambda r: json.loads(r) if r else {"action": "SIGNAL"}
        )(request.data.decode("utf-8").strip())
    except Exception as e:
        logger.error(f"Webhook parse error: {e}")
        return jsonify({"error": "invalid payload"}), 400

    if not data:
        return jsonify({"error": "empty payload"}), 400

    save_signal({"received_at": datetime.utcnow().isoformat(), "data": data})

    try:
        send_telegram_message_sync(format_signal_message(data))
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return jsonify({"status": "error", "reason": str(e)}), 500

    logger.info(f"Signal forwarded: {data.get('action','?')} {data.get('ticker','?')}")
    return jsonify({"status": "ok"}), 200


@flask_app.route("/health", methods=["GET"])
def health():
    sigs = load_signals()
    return jsonify({
        "status": "ok", "bot_active": get_bot_active(),
        "total_signals": len(sigs),
        "last_signal": sigs[-1]["received_at"] if sigs else None,
    })


@flask_app.route("/signals", methods=["GET"])
def get_signals():
    sigs = load_signals()
    limit = int(request.args.get("limit", 20))
    return jsonify({"signals": sigs[-limit:][::-1], "total": len(sigs)})


# ── Telegram command handlers ──────────────────────────────────────────────────

HELP_TEXT = (
    "✅ <b>TradingView Signal Bot</b>\n\n"
    "🔗 <b>Webhook URL:</b>\n"
    "<code>https://{domain}/webhook/tradingview</code>\n\n"
    "<b>Basic alert payload:</b>\n"
    '<code>{{"action":"{{{{strategy.order.action}}}}","ticker":"{{{{ticker}}}}",'
    '"price":"{{{{close}}}}","timeframe":"{{{{interval}}}}","strategy":"Name",'
    '"comment":"{{{{strategy.order.comment}}}}"}}</code>\n\n'
    "<b>With Fibonacci swings:</b> add <code>\"swing_high\":\"...\",\"swing_low\":\"...\"</code>\n"
    "<b>With SMC from indicators:</b> add any of:\n"
    "<code>ob_high, ob_low, fvg_high, fvg_low, liq_high, liq_low, bos, choch</code>\n\n"
    "<b>Commands:</b>\n"
    "/start — Activate &amp; show this message\n"
    "/stop — Pause signal forwarding\n"
    "/status — Bot status &amp; last signal\n"
    "/history — Last 10 signals\n"
    "/setlevels TICKER entry sl tp1 tp2 tp3\n"
    "/clearlevels TICKER — Reset to auto-calc\n"
    "/levels — Show all manual level configs\n"
    "/clear — Clear signal history\n"
    "/help — Show this message\n\n"
    "<i>~ values are estimated from price structure</i>"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_bot_active(True)
    domain = os.environ.get("REPLIT_DOMAINS", "your-repl.replit.app").split(",")[0]
    await update.message.reply_text(
        HELP_TEXT.format(domain=domain), parse_mode=ParseMode.HTML,
    )

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_bot_active(False)
    await update.message.reply_text(
        "⏸ <b>Bot PAUSED</b>\n\nUse /start to resume.",
        parse_mode=ParseMode.HTML,
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_bot_active()
    sigs   = load_signals()
    last   = sigs[-1] if sigs else None
    status = "✅ ACTIVE" if active else "⏸ PAUSED"
    last_t = last["received_at"] if last else "No signals yet"
    if last:
        d = last["data"]
        sig_str = f"{d.get('action','?').upper()} {d.get('ticker',d.get('symbol','?')).upper()} @ {d.get('price',d.get('close','?'))}"
    else:
        sig_str = "—"
    await update.message.reply_text(
        f"📊 <b>Status: {status}</b>\n\n"
        f"📈 <b>Total signals:</b> {len(sigs)}\n"
        f"🕐 <b>Last signal:</b> {last_t}\n"
        f"📡 <b>Last:</b> {sig_str}",
        parse_mode=ParseMode.HTML,
    )

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sigs = load_signals()
    if not sigs:
        await update.message.reply_text("📭 No signals received yet.")
        return
    lines = ["📋 <b>Last 10 signals:</b>\n"]
    for s in sigs[-10:][::-1]:
        d  = s["data"]
        ac = d.get("action", "?").upper()
        tk = d.get("ticker", d.get("symbol", "?")).upper()
        pr = d.get("price", d.get("close", "?"))
        ts = s["received_at"][:16].replace("T", " ")
        em = "🟢" if ac in ("BUY","LONG") else ("🔴" if ac in ("SELL","SHORT") else "⚪")
        lines.append(f"{em} <code>{ts}</code> — <b>{ac}</b> <code>{tk}</code> @ {pr}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SIGNALS_FILE.write_text("[]")
    await update.message.reply_text("🗑 Signal history cleared.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def cmd_setlevels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usage = (
        "⚙️ <b>Usage:</b>\n"
        "<code>/setlevels TICKER entry sl tp1 tp2 tp3</code>\n\n"
        "<b>Example:</b>\n"
        "<code>/setlevels BTCUSDT 67500 65000 69000 71000 75000</code>\n\n"
        "Use <code>-</code> for any value to keep auto-calculated."
    )
    args = context.args
    if not args or len(args) < 6:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML); return

    ticker  = args[0].upper()
    keys    = ["entry", "sl", "tp1", "tp2", "tp3"]
    levels  = load_levels()
    entry   = levels.get(ticker, {})
    labels  = {"entry": "🎯 Entry", "sl": "🛑 Stop Loss", "tp1": "💚 TP1", "tp2": "💛 TP2", "tp3": "🏆 TP3"}

    for i, key in enumerate(keys):
        raw = args[i + 1]
        if raw in ("0", "-", "auto"):
            entry.pop(key, None)
        else:
            try:
                entry[key] = float(raw)
            except ValueError:
                await update.message.reply_text(
                    f"❌ Invalid value for <b>{key}</b>: <code>{raw}</code>",
                    parse_mode=ParseMode.HTML,
                ); return

    levels[ticker] = entry
    save_levels(levels)
    lines = [f"✅ <b>Manual levels saved for {ticker}:</b>\n"]
    for key in keys:
        val = entry.get(key)
        lines.append(f"{labels[key]}: " + (f"<code>{fmt(val)}</code>" if val else "<i>auto</i>"))
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_clearlevels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚙️ <b>Usage:</b> <code>/clearlevels TICKER</code>",
            parse_mode=ParseMode.HTML,
        ); return
    ticker = args[0].upper()
    levels = load_levels()
    if ticker in levels:
        del levels[ticker]; save_levels(levels)
        await update.message.reply_text(
            f"🗑 Manual levels cleared for <b>{ticker}</b>. Back to auto-calc.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"ℹ️ No manual levels configured for <b>{ticker}</b>.",
            parse_mode=ParseMode.HTML,
        )

async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    levels = load_levels()
    if not levels:
        await update.message.reply_text(
            "📭 No manual levels set. All tickers use auto-calculated levels.\n"
            "Use /setlevels to configure a ticker."
        ); return
    labels = {"entry": "🎯", "sl": "🛑", "tp1": "💚", "tp2": "💛", "tp3": "🏆"}
    lines = ["📋 <b>Configured Manual Levels:</b>\n"]
    for ticker, vals in levels.items():
        lines.append(f"<b>{ticker}</b>")
        for key in ["entry", "sl", "tp1", "tp2", "tp3"]:
            if key in vals:
                lines.append(f"  {labels[key]} {key.upper()}: <code>{fmt(vals[key])}</code>")
        lines.append("")
    lines.append("<i>Use /clearlevels TICKER to reset to auto-calc.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    global _main_loop, _telegram_app
    _main_loop = asyncio.get_running_loop()

    _telegram_app = Application.builder().token(BOT_TOKEN).build()
    for cmd, fn in [
        ("start", cmd_start), ("stop", cmd_stop), ("status", cmd_status),
        ("history", cmd_history), ("clear", cmd_clear), ("help", cmd_help),
        ("setlevels", cmd_setlevels), ("clearlevels", cmd_clearlevels),
        ("levels", cmd_levels),
    ]:
        _telegram_app.add_handler(CommandHandler(cmd, fn))

    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True, name="flask-webhook",
    ).start()
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
