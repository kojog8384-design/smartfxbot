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

BOT_TOKEN = ["8910718380:AAF9h3mZr-2RAZWBXiLwGMeuyG2-l1KyVBM"]
CHAT_ID   = ["-1003971075145"]
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

# Per-instrument-type auto SL/TP percentages
# Crypto defaults are wider; forex pairs need pip-realistic tighter bands
INSTRUMENT_AUTO_CALC = {
    "crypto": {"sl_pct": 2.0,  "tp1_pct": 2.0,  "tp2_pct": 3.5,  "tp3_pct": 5.5},
    "gold":   {"sl_pct": 0.6,  "tp1_pct": 0.8,  "tp2_pct": 1.5,  "tp3_pct": 2.5},
    "forex":  {"sl_pct": 0.25, "tp1_pct": 0.3,  "tp2_pct": 0.55, "tp3_pct": 0.9},
}
AUTO_CALC = INSTRUMENT_AUTO_CALC["crypto"]   # legacy reference kept for safety

# Crypto timeframe → estimated swing range (%)
TF_SWING_PCT = {
    "1": 0.4, "3": 0.6, "5": 0.8,
    "15": 1.2, "30": 1.8,
    "60": 2.5, "1H": 2.5, "2H": 3.5,
    "240": 4.5, "4H": 4.5,
    "D": 6.0, "1D": 6.0, "W": 10.0, "1W": 10.0,
}
# Gold (XAUUSD) — more volatile than forex, less than crypto
TF_SWING_GOLD = {
    "1": 0.15, "3": 0.2, "5": 0.25,
    "15": 0.4, "30": 0.6,
    "60": 0.9, "1H": 0.9, "2H": 1.3,
    "240": 1.8, "4H": 1.8,
    "D": 2.5, "1D": 2.5, "W": 4.5, "1W": 4.5,
}
# Forex majors (EURUSD, GBPUSD, etc.) — very tight moves per timeframe
TF_SWING_FOREX = {
    "1": 0.04, "3": 0.06, "5": 0.08,
    "15": 0.15, "30": 0.22,
    "60": 0.35, "1H": 0.35, "2H": 0.5,
    "240": 0.7, "4H": 0.7,
    "D": 1.0, "1D": 1.0, "W": 2.0, "1W": 2.0,
}

# Known forex/commodities tickers (full pair names)
FOREX_PAIRS = {"EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","NZDUSD",
               "USDCAD","EURGBP","EURJPY","GBPJPY","EURCHF"}
GOLD_PAIRS  = {"XAUUSD","GOLD","XAUEUR"}
SILVER_PAIRS= {"XAGUSD","SILVER"}

def detect_instrument(ticker: str) -> str:
    """Return 'gold', 'forex', or 'crypto' based on ticker."""
    t = ticker.upper().replace(" ","")
    if t in GOLD_PAIRS or t in SILVER_PAIRS:
        return "gold"
    if t in FOREX_PAIRS:
        return "forex"
    # Catch patterns like XAU/USD or XAU-USD
    if "XAU" in t or "GOLD" in t:
        return "gold"
    if any(t.startswith(fx) or t.endswith(fx)
           for fx in ("EUR","GBP","JPY","CHF","AUD","NZD","CAD") if len(t) == 6):
        return "forex"
    return "crypto"

FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]

DEFAULT_FILTER = {
    "daily_limit":         5,      # max signals forwarded per day
    "min_quality":         60,     # minimum quality score (0–100)
    "coin_cooldown_hours": 4,      # minimum hours between same-coin signals
    "max_per_coin":        2,      # max signals per coin per day
    "allowed_coins":       ["BTC","ETH","SOL","BNB","XRP","XAUUSD","EURUSD","GBPUSD"],
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

def record_rejected(ticker: str, action: str, reason: str, score: int):
    d = load_daily()
    d["rejected_count"] += 1
    if "rejected_log" not in d:
        d["rejected_log"] = []
    d["rejected_log"].append({
        "time":   datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "action": action,
        "reason": reason,
        "score":  score,
    })
    d["rejected_log"] = d["rejected_log"][-50:]   # keep last 50 per day
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
    instrument = detect_instrument(ticker)
    # Normalise base for whitelist check:
    # - Forex/gold tickers are kept as-is (XAUUSD, EURUSD, GBPUSD)
    # - Crypto strips USDT/BUSD/PERP suffix
    if instrument in ("forex", "gold"):
        base = ticker
    else:
        base = ticker.replace("USDT","").replace("BUSD","").replace("PERP","").replace("USD","")

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

def fmt(v: float, instrument: str = "crypto") -> str:
    """Format a price with appropriate decimal precision per instrument type."""
    if v >= 1000:  return f"{v:,.2f}"           # BTC, XAUUSD
    elif v >= 10:  return f"{v:.2f}"             # SOL, BNB at $100+
    elif v >= 1:   return f"{v:.5f}"             # EURUSD 1.08456, GBPUSD 1.26750
    else:          return f"{v:.6f}"             # sub-dollar crypto

def pct_diff(entry: float, target: float) -> str:
    d = (target - entry) / entry * 100
    return f"{'+'if d>=0 else ''}{d:.1f}%"

def _pct(base: float, pct: float, direction: float) -> float:
    return base * (1 + direction * pct / 100)


# ── Fibonacci ──────────────────────────────────────────────────────────────────

def compute_fib_levels(high: float, low: float) -> dict:
    diff = high - low
    return {r: high - r * diff for r in FIB_RATIOS}

def swing_from_data(data: dict, price: float, instrument: str = "crypto") -> tuple[float, float]:
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
    tf = str(data.get("timeframe", data.get("interval", "60"))).upper()
    if instrument == "gold":
        pct = TF_SWING_GOLD.get(tf, 0.9)
    elif instrument == "forex":
        pct = TF_SWING_FOREX.get(tf, 0.35)
    else:
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
    stored     = load_levels().get(ticker.upper(), {})
    instrument = detect_instrument(ticker)
    ac         = INSTRUMENT_AUTO_CALC[instrument]
    is_long    = action in ("BUY", "LONG")
    sl_dir     = -1 if is_long else +1
    tp_dir     = +1 if is_long else -1
    auto = {
        "entry": price,
        "sl":  _pct(price, ac["sl_pct"],  sl_dir),
        "tp1": _pct(price, ac["tp1_pct"], tp_dir),
        "tp2": _pct(price, ac["tp2_pct"], tp_dir),
        "tp3": _pct(price, ac["tp3_pct"], tp_dir),
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
    action     = data.get("action", "").upper()
    ticker     = data.get("ticker", data.get("symbol", "UNKNOWN")).upper()
    timeframe  = data.get("timeframe", data.get("interval", ""))
    strategy   = data.get("strategy", data.get("strategy_name", ""))
    comment    = data.get("comment", data.get("message", ""))
    exchange   = data.get("exchange", "")
    volume     = data.get("volume", "")
    instrument = detect_instrument(ticker)

    # Instrument badge shown in header
    inst_badge = {"gold": "🥇 Gold", "forex": "💱 Forex", "crypto": "🪙 Crypto"}[instrument]

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

    # Quality bar (10 blocks)
    filled = round(score / 10)
    q_bar  = "█" * filled + "░" * (10 - filled)
    stars  = "⭐" * (1 if score < 70 else (2 if score < 85 else 3))

    # ── Header ──
    lines = [
        f"{hdr_emoji} <b>{direction}</b>",
        f"📌 <b>Ticker:</b> <code>{ticker}</code>  <i>{inst_badge}</i>",
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

    # shorthand formatter bound to this instrument
    F = lambda v: fmt(v, instrument)

    show_levels = action in ("BUY", "LONG", "SELL", "SHORT")

    # ── Trade Levels ──
    if show_levels:
        lvl = compute_levels(action, price, ticker, data)
        entry, sl, tp1, tp2, tp3 = lvl["entry"], lvl["sl"], lvl["tp1"], lvl["tp2"], lvl["tp3"]
        ac = INSTRUMENT_AUTO_CALC[instrument]
        lines += [
            f"📊 <b>TRADE LEVELS</b>  <i>(SL {ac['sl_pct']}% / TP {ac['tp1_pct']}–{ac['tp3_pct']}%)</i>",
            f"┣ 🎯 <b>Entry</b>      <code>{F(entry)}</code>",
            f"┣ 🛑 <b>Stop Loss</b>  <code>{F(sl)}</code>  <i>({pct_diff(entry, sl)})</i>",
            f"┣ 💚 <b>TP1</b>        <code>{F(tp1)}</code>  <i>({pct_diff(entry, tp1)})</i>",
            f"┣ 💛 <b>TP2</b>        <code>{F(tp2)}</code>  <i>({pct_diff(entry, tp2)})</i>",
            f"┗ 🏆 <b>TP3</b>        <code>{F(tp3)}</code>  <i>({pct_diff(entry, tp3)})</i>",
            "",
        ]
    else:
        lines.append(f"💰 <b>Price</b>  <code>{F(price)}</code>\n")

    # ── Fibonacci ──
    swing_hi, swing_lo = swing_from_data(data, price, instrument)
    fibs = compute_fib_levels(swing_hi,
