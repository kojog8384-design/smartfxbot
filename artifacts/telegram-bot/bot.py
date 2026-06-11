import os
import json
import logging
import asyncio
import threading
from datetime import datetime, timezone
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
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
PORT      = int(os.environ.get("PORT", 6000))

DATA_DIR     = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SIGNALS_FILE = DATA_DIR / "signals.json"
STATE_FILE   = DATA_DIR / "state.json"
LEVELS_FILE  = DATA_DIR / "levels.json"
DAILY_FILE   = DATA_DIR / "daily.json"

flask_app    = Flask(__name__)
_main_loop: asyncio.AbstractEventLoop = None
_telegram_app: Application = None

AUTO_CALC = {"sl_pct": 2.0, "tp1_pct": 2.0, "tp2_pct": 3.5, "tp3_pct": 5.5}

TF_SWING_PCT = {
    "1": 0.4, "3": 0.6, "5": 0.8,
    "15": 1.2, "30": 1.8,
    "60": 2.5, "1H": 2.5, "2H": 3.5,
    "240": 4.5, "4H": 4.5,
    "D": 6.0, "1D": 6.0, "W": 10.0, "1W": 10.0,
}

FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]

DEFAULT_FILTER = {
    "daily_limit":         5,      # max signals forwarded per day
    "min_quality":         60,     # minimum quality score (0–100)
    "coin_cooldown_hours": 4,      # minimum hours between same-coin signals
    "max_per_coin":        2,      # max signals per coin per day
    "allowed_coins":       ["BTC","ETH","SOL","BNB","XRP"],
    "whitelist_mode":      True,   # if True, only allowed_coins pass
}


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

def load_signals() -> list:  return load_json(SIGNALS_FILE, [])
def load_state()   -> dict:  return load_json(STATE_FILE,   {"active": True, "filter": {}})
def load_levels()  -> dict:  return load_json(LEVELS_FILE,  {})

def save_signal(sig):
    sigs = load_signals(); sigs.append(sig)
    save_json(SIGNALS_FILE, sigs[-500:])

def save_state(s): save_json(STATE_FILE, s)
def save_levels(l): save_json(LEVELS_FILE, l)

def get_bot_active() -> bool:
    return load_state().get("active", True)

def set_bot_active(v: bool):
    s = load_state(); s["active"] = v; save_state(s)

def load_filter() -> dict:
    stored = load_state().get("filter", {})
    return {**DEFAULT_FILTER, **stored}

def save_filter(f: dict):
    s = load_state(); s["filter"] = f; save_state(s)


# ── Daily stats ────────────────────────────────────────────────────────────────

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def load_daily() -> dict:
    d = load_json(DAILY_FILE, {})
    if d.get("date") != _today_utc():
        d = {
            "date":         _today_utc(),
            "sent_count":   0,
            "rejected_count": 0,
            "coins_today":  {},          # ticker -> count sent today
            "last_sent_at": {},          # ticker -> ISO timestamp of last sent
            "sent_log":     [],          # [{time, ticker, action, score}]
        }
    return d

def save_daily(d: dict):
    save_json(DAILY_FILE, d)

def record_sent(ticker: str, action: str, score: int):
    d = load_daily()
    now = datetime.now(timezone.utc).isoformat()
    d["sent_count"] += 1
    d["coins_today"][ticker] = d["coins_today"].get(ticker, 0) + 1
    d["last_sent_at"][ticker] = now
    d["sent_log"].append({"time": now, "ticker": ticker, "action": action, "score": score})
    save_daily(d)

def record_rejected():
    d = load_daily()
    d["rejected_count"] += 1
    save_daily(d)


# ── Signal quality scoring ─────────────────────────────────────────────────────

def score_signal(data: dict) -> tuple[int, list[str]]:
    """
    Score a signal 0–100. Returns (score, reasons[]).
    Higher = better quality. Min ~60 required to pass by default.
    """
    score   = 0
    reasons = []

    # Override: TradingView can pass an explicit quality score
    manual_q = data.get("quality") or data.get("strength")
    if manual_q is not None:
        try:
            q = max(0, min(100, int(float(manual_q))))
            return q, [f"📊 Manual quality score: {q}"]
        except (ValueError, TypeError):
            pass

    action = data.get("action", "").upper()
    if action in ("BUY", "LONG", "SELL", "SHORT"):
        score += 25; reasons.append("✅ Clear directional signal")

    raw_price = data.get("price", data.get("close"))
    try:
        price = float(raw_price)
        score += 10; reasons.append("✅ Price provided")
    except (TypeError, ValueError):
        price = None

    if data.get("strategy") or data.get("strategy_name"):
        score += 15; reasons.append("✅ Strategy name provided")

    if data.get("comment") or data.get("message"):
        score += 8; reasons.append("✅ Confluence comment provided")

    if data.get("swing_high") and data.get("swing_low"):
        score += 12; reasons.append("✅ Swing high/low provided")
    elif data.get("high") and data.get("low"):
        score += 6; reasons.append("⚡ Bar high/low provided")

    if data.get("ob_high") or data.get("ob_low"):
        score += 10; reasons.append("✅ Order Block data provided")

    if data.get("fvg_high") or data.get("fvg_low"):
        score += 7; reasons.append("✅ FVG data provided")

    if data.get("exchange"):
        score += 3; reasons.append("✅ Exchange specified")

    if data.get("timeframe") or data.get("interval"):
        score += 5; reasons.append("✅ Timeframe specified")

    # R/R bonus: uses provided or auto-calc levels
    if price and price > 0 and action in ("BUY","LONG","SELL","SHORT"):
        is_long = action in ("BUY","LONG")
        try:
            sl_val  = float(data["sl"])  if "sl"  in data else _pct(price, AUTO_CALC["sl_pct"],  -1 if is_long else +1)
            tp3_val = float(data["tp3"]) if "tp3" in data else _pct(price, AUTO_CALC["tp3_pct"], +1 if is_long else -1)
            risk    = abs(price - sl_val)
            reward  = abs(tp3_val - price)
            if risk > 0:
                rr = reward / risk
                if rr >= 4:
                    score += 20; reasons.append(f"🏆 Exceptional R/R: {rr:.1f}R")
                elif rr >= 3:
                    score += 14; reasons.append(f"✅ Strong R/R: {rr:.1f}R")
                elif rr >= 2:
                    score += 8;  reasons.append(f"✅ Good R/R: {rr:.1f}R")
                elif rr >= 1.5:
                    score += 3;  reasons.append(f"⚡ Decent R/R: {rr:.1f}R")
        except (TypeError, ValueError, KeyError):
            pass

    return min(score, 100), reasons


def check_signal_filter(data: dict) -> tuple[bool, str, int]:
    """
    Returns (should_send, rejection_reason, quality_score).
    Checks: coin whitelist, daily limit, per-coin limit, cooldown, quality.
    """
    f       = load_filter()
    daily   = load_daily()
    ticker  = data.get("ticker", data.get("symbol", "")).upper()
    # Normalise: strip USDT/BUSD/PERP suffix for whitelist check
    base    = ticker.replace("USDT","").replace("BUSD","").replace("PERP","").replace("USD","")

    # 1. Coin whitelist
    if f["whitelist_mode"] and f["allowed_coins"]:
        allowed = [c.upper() for c in f["allowed_coins"]]
        if base not in allowed:
            return False, f"🚫 {ticker} not in watchlist ({', '.join(allowed)})", 0

    # 2. Daily total cap
    if daily["sent_count"] >= f["daily_limit"]:
        return False, f"📵 Daily limit reached ({f['daily_limit']} signals/day)", 0

    # 3. Per-coin daily cap
    coin_count = daily["coins_today"].get(ticker, 0)
    if coin_count >= f["max_per_coin"]:
        return False, f"📵 {ticker} already has {coin_count} signal(s) today (max {f['max_per_coin']})", 0

    # 4. Cooldown between same-coin signals
    last_sent_iso = daily["last_sent_at"].get(ticker)
    if last_sent_iso:
        try:
            last_dt  = datetime.fromisoformat(last_sent_iso)
            now_dt   = datetime.now(timezone.utc)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed_h = (now_dt - last_dt).total_seconds() / 3600
            if elapsed_h < f["coin_cooldown_hours"]:
                remaining = f["coin_cooldown_hours"] - elapsed_h
                return False, f"⏳ {ticker} cooldown: {remaining:.1f}h remaining", 0
        except Exception:
            pass

    # 5. Quality score
    score, _ = score_signal(data)
    if score < f["min_quality"]:
        return False, f"⚠️ Quality too low: {score}/100 (min {f['min_quality']})", score

    return True, "ok", score


# ── Number helpers ─────────────────────────────────────────────────────────────

def fmt(v: float) -> str:
    if v >= 1000:  return f"{v:,.2f}"
    elif v >= 1:   return f"{v:.4f}"
    else:          return f"{v:.6f}"

def pct_diff(entry: float, target: float) -> str:
    d = (target - entry) / entry * 100
    return f"{'+'if d>=0 else ''}{d:.1f}%"

def _pct(base: float, pct: float, direction: float) -> float:
    return base * (1 + direction * pct / 100)


# ── Fibonacci ──────────────────────────────────────────────────────────────────

def compute_fib_levels(high: float, low: float) -> dict:
    diff = high - low
    return {r: high - r * diff for r in FIB_RATIOS}

def swing_from_data(data: dict, price: float) -> tuple[float, float]:
    sh  = data.get("swing_high") or data.get("swingHigh")
    sl_ = data.get("swing_low")  or data.get("swingLow")
    if sh and sl_:
        try:
            return float(sh), float(sl_)
        except (ValueError, TypeError):
            pass
    bh = data.get("high"); bl = data.get("low")
    if bh and bl:
        try:
            h, l = float(bh), float(bl)
            if h > price and l < price:
                return h, l
        except (ValueError, TypeError):
            pass
    tf  = str(data.get("timeframe", data.get("interval", "60"))).upper()
    pct = TF_SWING_PCT.get(tf, 2.5)
    return price * (1 + pct / 100), price * (1 - pct / 100)


# ── SMC Analysis ───────────────────────────────────────────────────────────────

def compute_smc(action: str, price: float, high: float, low: float,
                fibs: dict, data: dict) -> dict:
    is_long  = action in ("BUY", "LONG")
    is_short = action in ("SELL", "SHORT")

    # Order Block
    if is_long:   ob_lo_key, ob_hi_key = 0.786, 0.618
    elif is_short: ob_lo_key, ob_hi_key = 0.236, 0.382
    else:          ob_lo_key, ob_hi_key = 0.618, 0.5

    ob_lo = data.get("ob_low"); ob_hi = data.get("ob_high"); ob_est = True
    if ob_lo and ob_hi:
        try:
            ob_lo, ob_hi, ob_est = float(ob_lo), float(ob_hi), False
        except (ValueError, TypeError):
            ob_lo, ob_hi = None, None
    if ob_lo is None or ob_hi is None:
        ob_lo, ob_hi = fibs[ob_lo_key], fibs[ob_hi_key]
        if ob_lo > ob_hi: ob_lo, ob_hi = ob_hi, ob_lo

    # FVG
    fvg_lo = data.get("fvg_low"); fvg_hi = data.get("fvg_high"); fvg_est = True
    if fvg_lo and fvg_hi:
        try:
            fvg_lo, fvg_hi, fvg_est = float(fvg_lo), float(fvg_hi), False
        except (ValueError, TypeError):
            fvg_lo, fvg_hi = None, None
    if fvg_lo is None or fvg_hi is None:
        fvg_lo, fvg_hi = min(fibs[0.5], fibs[0.618]), max(fibs[0.5], fibs[0.618])

    # Liquidity
    liq_h = data.get("liq_high"); liq_l = data.get("liq_low"); liq_est = True
    if liq_h and liq_l:
        try:
            liq_h, liq_l, liq_est = float(liq_h), float(liq_l), False
        except (ValueError, TypeError):
            liq_h, liq_l = None, None
    if liq_h is None or liq_l is None:
        liq_h, liq_l = high * 1.003, low * 0.997

    # BOS
    bos_raw = data.get("bos")
    bos     = str(bos_raw) if bos_raw else ("Bullish" if is_long else ("Bearish" if is_short else "Neutral"))
    bos_est = not bool(bos_raw)

    # CHoCH
    choch_level = None; choch_est = True
    choch_raw = data.get("choch")
    if choch_raw:
        try:
            choch_level = float(choch_raw); choch_est = False
        except (ValueError, TypeError):
            pass
    if choch_level is None:
        choch_level = low if is_long else high

    return {
        "ob":       (ob_lo, ob_hi, "Bullish" if is_long else "Bearish", ob_est),
        "fvg":      (fvg_lo, fvg_hi, fvg_est),
        "liq_high": (liq_h, liq_est),
        "liq_low":  (liq_l, liq_est),
        "bos":      (bos, bos_est),
        "choch":    (choch_level, choch_est),
    }


# ── Trade levels ───────────────────────────────────────────────────────────────

def compute_levels(action: str, price: float, ticker: str, data: dict) -> dict:
    stored  = load_levels().get(ticker.upper(), {})
    is_long = action in ("BUY", "LONG")
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

def format_signal_message(data: dict, score: int = 0) -> str:
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

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Quality bar  ░░░░░░░░░░ (10 blocks)
    filled = round(score / 10)
    q_bar  = "█" * filled + "░" * (10 - filled)
    stars  = "⭐" * (1 if score < 70 else (2 if score < 85 else 3))

    # ── Header ──
    lines = [
        f"{hdr_emoji} <b>{direction}</b>",
        f"📌 <b>Ticker:</b> <code>{ticker}</code>",
    ]
    meta = []
    if timeframe: meta.append(f"⏱ {timeframe}")
    if exchange:  meta.append(f"🏦 {exchange}")
    if meta: lines.append("  ".join(meta))
    if strategy: lines.append(f"🧠 <b>Strategy:</b> {strategy}")
    if score > 0:
        lines.append(f"⚡ <b>Quality:</b> {score}/100  {stars}  <code>{q_bar}</code>")
    lines.append("")

    if price is None:
        lines.append(f"🕐 {now}")
        return "\n".join(lines)

    show_levels = action in ("BUY", "LONG", "SELL", "SHORT")

    # ── Trade Levels ──
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
    lines.append(
        f"🔢 <b>FIBONACCI</b>  "
        f"<code>{fmt(swing_lo)}</code> ↔ <code>{fmt(swing_hi)}</code>"
        + (" <i>(est.)</i>" if swing_est else "")
    )
    fib_syms  = {0.0:"┣",0.236:"┣",0.382:"┣",0.5:"┣",0.618:"┣",0.786:"┣",1.0:"┗"}
    fib_emojis= {0.0:"⬜",0.236:"🔵",0.382:"🔵",0.5:"🟡",0.618:"🔑",0.786:"🟠",1.0:"⬜"}
    for r in FIB_RATIOS:
        level = fibs[r]
        tag   = "  ◀ <i>price</i>" if abs(level - price) / price < 0.005 else ""
        lines.append(f"{fib_syms[r]} {fib_emojis[r]} <code>{r:.3f}</code>  <code>{fmt(level)}</code>{tag}")
    lines.append("")

    # ── SMC ──
    if show_levels:
        smc = compute_smc(action, price, swing_hi, swing_lo, fibs, data)
        ob_lo, ob_hi, ob_dir, ob_est = smc["ob"]
        fvg_lo, fvg_hi, fvg_est      = smc["fvg"]
        liq_h, liq_h_est             = smc["liq_high"]
        liq_l, liq_l_est             = smc["liq_low"]
        bos_str, bos_est             = smc["bos"]
        choch_lvl, choch_est         = smc["choch"]
        bos_em = "✅" if "Bull" in bos_str else ("🔻" if "Bear" in bos_str else "➡️")
        ob_em  = "🟦" if ob_dir == "Bullish" else "🟥"
        lines += [
            "🧠 <b>SMART MONEY CONCEPTS</b>",
            f"┣ {ob_em} <b>Order Block</b>  <code>{fmt(ob_lo)}</code> – <code>{fmt(ob_hi)}</code>  <i>({ob_dir}){_e(ob_est)}</i>",
            f"┣ 🌊 <b>Fair Value Gap</b>  <code>{fmt(fvg_lo)}</code> – <code>{fmt(fvg_hi)}</code>{_e(fvg_est)}",
            f"┣ 💧 <b>Liq. High</b>  <code>{fmt(liq_h)}</code>  <i>(Buy-side){_e(liq_h_est)}</i>",
            f"┣ 💧 <b>Liq. Low</b>   <code>{fmt(liq_l)}</code>  <i>(Sell-side){_e(liq_l_est)}</i>",
            f"┣ {bos_em} <b>BOS</b>  {bos_str}{_e(bos_est)}",
            f"┗ ⚡ <b>CHoCH</b>  <code>{fmt(choch_lvl)}</code>  <i>(watch level){_e(choch_est)}</i>",
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
        if request.is_json:
            data = request.get_json(force=True)
        else:
            raw = request.data.decode("utf-8").strip()
            data = json.loads(raw) if raw else {"action": "SIGNAL"}
    except Exception as e:
        logger.error(f"Webhook parse error: {e}")
        return jsonify({"error": "invalid payload"}), 400

    if not data:
        return jsonify({"error": "empty payload"}), 400

    # Always save the raw signal for history
    save_signal({"received_at": datetime.now(timezone.utc).isoformat(), "data": data})

    # Run quality filter
    should_send, reason, score = check_signal_filter(data)
    ticker = data.get("ticker", data.get("symbol", "?")).upper()
    action = data.get("action", "?").upper()

    if not should_send:
        record_rejected()
        logger.info(f"Signal BLOCKED [{ticker} {action}] — {reason}")
        return jsonify({"status": "filtered", "reason": reason, "score": score}), 200

    # Send to Telegram
    try:
        send_telegram_message_sync(format_signal_message(data, score=score))
        record_sent(ticker, action, score)
        logger.info(f"Signal SENT [{ticker} {action}] score={score}")
        return jsonify({"status": "ok", "score": score}), 200
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return jsonify({"status": "error", "reason": str(e)}), 500


@flask_app.route("/health", methods=["GET"])
def health():
    sigs  = load_signals()
    daily = load_daily()
    f     = load_filter()
    return jsonify({
        "status":       "ok",
        "bot_active":   get_bot_active(),
        "total_signals": len(sigs),
        "today_sent":   daily["sent_count"],
        "daily_limit":  f["daily_limit"],
        "last_signal":  sigs[-1]["received_at"] if sigs else None,
    })

@flask_app.route("/signals", methods=["GET"])
def get_signals():
    sigs = load_signals()
    limit = int(request.args.get("limit", 20))
    return jsonify({"signals": sigs[-limit:][::-1], "total": len(sigs)})


# ── Telegram command handlers ──────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_bot_active(True)
    domain = os.environ.get("REPLIT_DOMAINS", "your-repl.replit.app").split(",")[0]
    f = load_filter()
    coins = ", ".join(f["allowed_coins"])
    text = (
        "✅ <b>TradingView Signal Bot — ACTIVE</b>\n\n"
        f"🔗 <b>Webhook URL:</b>\n"
        f"<code>https://{domain}/webhook/tradingview</code>\n\n"
        f"🎯 <b>Filter:</b> {f['daily_limit']} signals/day  |  min quality {f['min_quality']}/100\n"
        f"📌 <b>Watchlist:</b> {coins}\n\n"
        "<b>Alert payload template:</b>\n"
        '<code>{"action":"{{strategy.order.action}}","ticker":"{{ticker}}",'
        '"price":"{{close}}","timeframe":"{{interval}}","strategy":"Name",'
        '"comment":"{{strategy.order.comment}}"}</code>\n\n'
        "<b>Commands:</b>\n"
        "/status — Daily counter &amp; filter summary\n"
        "/filter — Show filter settings\n"
        "/setfilter — Change filter settings\n"
        "/history — Last 10 forwarded signals\n"
        "/setlevels TICKER entry sl tp1 tp2 tp3\n"
        "/clearlevels TICKER  |  /levels\n"
        "/stop — Pause  |  /clear — Clear history\n"
        "/help — Show this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_bot_active(False)
    await update.message.reply_text(
        "⏸ <b>Bot PAUSED</b>\n\nUse /start to resume.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_bot_active()
    sigs   = load_signals()
    daily  = load_daily()
    f      = load_filter()
    last   = sigs[-1] if sigs else None
    status = "✅ ACTIVE" if active else "⏸ PAUSED"

    # Daily progress bar
    sent  = daily["sent_count"]
    limit = f["daily_limit"]
    filled = round((sent / limit) * 10) if limit else 0
    d_bar  = "█" * filled + "░" * (10 - filled)

    # Coins breakdown
    coins_today = daily["coins_today"]
    coins_str = "  ".join(
        f"<code>{tk}</code>×{cnt}" for tk, cnt in coins_today.items()
    ) if coins_today else "none yet"

    # Last signal info
    if last:
        d = last["data"]
        sig_str = (
            f"{d.get('action','?').upper()} "
            f"{d.get('ticker',d.get('symbol','?')).upper()} "
            f"@ {d.get('price',d.get('close','?'))}"
        )
        last_t = last["received_at"][:16].replace("T", " ") + " UTC"
    else:
        sig_str, last_t = "—", "No signals yet"

    today = _today_utc()
    text = (
        f"📊 <b>Bot Status: {status}</b>  —  {today}\n\n"
        f"📅 <b>Today's signals:</b>  {sent}/{limit}  <code>{d_bar}</code>\n"
        f"🚫 <b>Rejected today:</b>  {daily['rejected_count']}\n"
        f"📌 <b>Coins today:</b>  {coins_str}\n\n"
        f"📈 <b>Total signals (all time):</b> {len(sigs)}\n"
        f"🕐 <b>Last signal:</b> {last_t}\n"
        f"📡 <b>Last:</b> {sig_str}\n\n"
        f"⚙️ <b>Filter:</b> quality ≥ {f['min_quality']}  |  cooldown {f['coin_cooldown_hours']}h  |  max {f['max_per_coin']}/coin\n"
        f"📌 <b>Watchlist:</b> {', '.join(f['allowed_coins'])}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = load_filter()
    mode = "✅ ON (only watchlist coins)" if f["whitelist_mode"] else "❌ OFF (all coins accepted)"
    coins = ", ".join(f["allowed_coins"]) if f["allowed_coins"] else "none"
    text = (
        "⚙️ <b>Current Filter Settings</b>\n\n"
        f"📅 <b>Daily limit:</b>       {f['daily_limit']} signals/day\n"
        f"⚡ <b>Min quality:</b>       {f['min_quality']}/100\n"
        f"⏳ <b>Coin cooldown:</b>     {f['coin_cooldown_hours']}h between same-coin signals\n"
        f"🔢 <b>Max per coin/day:</b>  {f['max_per_coin']}\n"
        f"📌 <b>Whitelist mode:</b>    {mode}\n"
        f"📌 <b>Watchlist:</b>         {coins}\n\n"
        "<b>To change settings use /setfilter:</b>\n"
        "<code>/setfilter daily_limit=5</code>\n"
        "<code>/setfilter min_quality=65</code>\n"
        "<code>/setfilter cooldown=4</code>\n"
        "<code>/setfilter max_per_coin=2</code>\n"
        "<code>/setfilter coins=BTC,ETH,SOL,BNB,XRP</code>\n"
        "<code>/setfilter whitelist=on</code> or <code>off</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_setfilter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setfilter daily_limit=5
    /setfilter min_quality=70
    /setfilter cooldown=3
    /setfilter max_per_coin=1
    /setfilter coins=BTC,ETH,SOL
    /setfilter whitelist=on|off
    """
    usage = (
        "⚙️ <b>Usage examples:</b>\n"
        "<code>/setfilter daily_limit=5</code>  — max signals per day\n"
        "<code>/setfilter min_quality=65</code>  — min quality score (0–100)\n"
        "<code>/setfilter cooldown=4</code>       — hours between same coin\n"
        "<code>/setfilter max_per_coin=2</code>   — max signals per coin/day\n"
        "<code>/setfilter coins=BTC,ETH,SOL,BNB,XRP</code>\n"
        "<code>/setfilter whitelist=on</code> or <code>off</code>"
    )
    args = context.args
    if not args:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML); return

    f = load_filter()
    changes = []
    errors  = []

    for arg in args:
        if "=" not in arg:
            errors.append(f"Invalid format: <code>{arg}</code> (use key=value)"); continue
        key, _, val = arg.partition("=")
        key = key.strip().lower(); val = val.strip()

        if key == "daily_limit":
            try:
                f["daily_limit"] = max(1, int(val)); changes.append(f"daily_limit → {f['daily_limit']}")
            except ValueError:
                errors.append(f"daily_limit must be a number")

        elif key == "min_quality":
            try:
                f["min_quality"] = max(0, min(100, int(val))); changes.append(f"min_quality → {f['min_quality']}")
            except ValueError:
                errors.append("min_quality must be 0–100")

        elif key == "cooldown":
            try:
                f["coin_cooldown_hours"] = max(0, float(val)); changes.append(f"cooldown → {f['coin_cooldown_hours']}h")
            except ValueError:
                errors.append("cooldown must be a number (hours)")

        elif key == "max_per_coin":
            try:
                f["max_per_coin"] = max(1, int(val)); changes.append(f"max_per_coin → {f['max_per_coin']}")
            except ValueError:
                errors.append("max_per_coin must be a number")

        elif key == "coins":
            coins = [c.strip().upper() for c in val.split(",") if c.strip()]
            f["allowed_coins"] = coins; changes.append(f"coins → {', '.join(coins)}")

        elif key == "whitelist":
            if val.lower() in ("on", "true", "yes", "1"):
                f["whitelist_mode"] = True;  changes.append("whitelist → ON")
            elif val.lower() in ("off", "false", "no", "0"):
                f["whitelist_mode"] = False; changes.append("whitelist → OFF")
            else:
                errors.append("whitelist must be on or off")
        else:
            errors.append(f"Unknown setting: <code>{key}</code>")

    if errors:
        err_text = "\n".join(f"❌ {e}" for e in errors)
        await update.message.reply_text(err_text + "\n\n" + usage, parse_mode=ParseMode.HTML)
        return

    save_filter(f)
    lines = ["✅ <b>Filter updated:</b>\n"] + [f"  • {c}" for c in changes]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_resetdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_daily({
        "date": _today_utc(),
        "sent_count": 0, "rejected_count": 0,
        "coins_today": {}, "last_sent_at": {}, "sent_log": [],
    })
    await update.message.reply_text("🔄 Daily signal counter reset to 0.")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sigs = load_signals()
    if not sigs:
        await update.message.reply_text("📭 No signals received yet.")
        return
    lines = ["📋 <b>Last 10 signals (received):</b>\n"]
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
        "⚙️ <b>Usage:</b> <code>/setlevels TICKER entry sl tp1 tp2 tp3</code>\n"
        "<b>Example:</b> <code>/setlevels BTCUSDT 67500 65000 69000 71000 75000</code>\n"
        "Use <code>-</code> for any value to keep auto-calculated."
    )
    args = context.args
    if not args or len(args) < 6:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML); return

    ticker = args[0].upper()
    keys   = ["entry", "sl", "tp1", "tp2", "tp3"]
    labels = {"entry": "🎯 Entry", "sl": "🛑 SL", "tp1": "💚 TP1", "tp2": "💛 TP2", "tp3": "🏆 TP3"}
    levels = load_levels()
    entry  = levels.get(ticker, {})

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
        lines.append(labels[key] + ": " + (f"<code>{fmt(val)}</code>" if val is not None else "<i>auto</i>"))
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_clearlevels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("⚙️ <b>Usage:</b> <code>/clearlevels TICKER</code>", parse_mode=ParseMode.HTML); return
    ticker = args[0].upper()
    levels = load_levels()
    if ticker in levels:
        del levels[ticker]; save_levels(levels)
        await update.message.reply_text(f"🗑 Manual levels cleared for <b>{ticker}</b>.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"ℹ️ No manual levels set for <b>{ticker}</b>.", parse_mode=ParseMode.HTML)


async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    levels = load_levels()
    if not levels:
        await update.message.reply_text("📭 No manual levels set.\nUse /setlevels to configure."); return
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
        ("start",       cmd_start),
        ("stop",        cmd_stop),
        ("status",      cmd_status),
        ("filter",      cmd_filter),
        ("setfilter",   cmd_setfilter),
        ("resetdaily",  cmd_resetdaily),
        ("history",     cmd_history),
        ("clear",       cmd_clear),
        ("help",        cmd_help),
        ("setlevels",   cmd_setlevels),
        ("clearlevels", cmd_clearlevels),
        ("levels",      cmd_levels),
    ]:
        _telegram_app.add_handler(CommandHandler(cmd, fn))

    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True, name="flask-webhook",
    ).start()
    logger.info(f"Flask webhook server started on port {PORT}")

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
