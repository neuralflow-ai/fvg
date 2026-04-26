# ============================================================
# GoldPulse AI — XAUUSD SMC Signal Bot
# No MT5 required — runs on any platform (Render, VPS, PC)
# Signals sent to Discord — execute manually on MT5 mobile
# ============================================================

# ── Section 1: Imports ───────────────────────────────────────
import logging
import os
import threading
import time
from datetime import datetime

import pytz
import requests
import schedule
import yfinance as yf
from flask import Flask


# ── Section 2: Constants ─────────────────────────────────────
DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1497958569478328520/htlj5ENbEvMe_t3nY8w0TuD1g6iapL7Muzgb7J-S5B-O-Lb4U79sXgzHngtcpQ5m7wDc"

SYMBOL      = "XAUUSD"
YF_SYMBOL   = "GC=F"        # Gold futures — OHLC structure data
COINBASE_URL = "https://api.coinbase.com/v2/prices/XAU-USD/spot"  # real spot price
RISK_AMOUNT = 500           # $500 = 1% of $50,000
SL_PIPS     = 8
PIP_SIZE    = 0.10          # 1 pip = $0.10 for XAUUSD
SL_DISTANCE = SL_PIPS * PIP_SIZE   # $0.80

PKT = pytz.timezone("Asia/Karachi")
SEP = "━━━━━━━━━━━━━━━━━━━━"


# ── Section 3: Global State ──────────────────────────────────
state = {
    # Bias
    "bias":             "NEUTRAL",
    "bias_swing_highs": [],
    "bias_swing_lows":  [],

    # Session
    "session_active":  False,
    "current_session": None,
    "session_start":   None,

    # Sweep
    "sweep_detected":  False,
    "sweep_time":      None,
    "sweep_direction": None,
    "sweep_price":     None,

    # FVG
    "fvg":         None,
    "signal_sent": False,   # prevent duplicate signals per FVG

    # Timing
    "last_30min_update": None,

    # Daily stats
    "daily_stats": {
        "signals": 0,
        "sweeps_detected": 0,
        "fvgs_detected": 0,
    },

    "_lock": threading.Lock(),
}


# ── Section 4: Logging ───────────────────────────────────────
log = logging.getLogger("GoldPulseAI")
log.setLevel(logging.DEBUG)

_fh = logging.FileHandler("goldpulse.log", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
))

log.addHandler(_fh)
log.addHandler(_ch)


# ── Section 5: Price Data (yfinance) ─────────────────────────

def _fetch_candles(interval: str, period: str) -> list:
    for attempt in range(3):
        try:
            ticker = yf.Ticker(YF_SYMBOL)
            df = ticker.history(period=period, interval=interval)
            if df is None or df.empty:
                log.warning("yfinance empty — interval=%s period=%s attempt=%d",
                            interval, period, attempt + 1)
                time.sleep(5)
                continue
            candles = []
            for ts, row in df.iterrows():
                candles.append({
                    "time":  int(ts.timestamp()),
                    "open":  float(row["Open"]),
                    "high":  float(row["High"]),
                    "low":   float(row["Low"]),
                    "close": float(row["Close"]),
                })
            return candles
        except Exception as exc:
            log.error("yfinance fetch error (attempt %d): %s", attempt + 1, exc)
            time.sleep(5)
    return None


def get_candles_1h(count: int = 10) -> list:
    candles = _fetch_candles("1h", "5d")
    return candles[-count:] if candles and len(candles) >= count else candles


def get_candles_15m(count: int = 20) -> list:
    candles = _fetch_candles("15m", "2d")
    return candles[-count:] if candles and len(candles) >= count else candles


def get_candles_1w(count: int = 2) -> list:
    candles = _fetch_candles("1wk", "3mo")
    return candles[-count:] if candles and len(candles) >= count else candles


def get_live_price() -> float:
    # Coinbase public API — real XAUUSD spot price, no API key needed
    for attempt in range(3):
        try:
            r = requests.get(COINBASE_URL, timeout=10)
            if r.status_code == 200:
                price = float(r.json()["data"]["amount"])
                if price > 0:
                    return price
        except Exception as exc:
            log.error("Coinbase price error (attempt %d): %s", attempt + 1, exc)
            time.sleep(3)
    # fallback: last GC=F candle close
    candles = _fetch_candles("15m", "1d")
    if candles:
        log.warning("Using GC=F candle close as price fallback")
        return candles[-1]["close"]
    return 0.0


def now_pkt() -> datetime:
    return datetime.now(PKT)


# ── Section 6: Discord Utilities ─────────────────────────────

def send_discord(message: str) -> bool:
    payload = {"content": message}
    for attempt in range(3):
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
            if r.status_code in (200, 204):
                return True
            log.warning("Discord HTTP %d: %s", r.status_code, r.text[:200])
        except Exception as exc:
            log.warning("Discord send attempt %d failed: %s", attempt + 1, exc)
        time.sleep(3)
    return False


# ── Format Functions ─────────────────────────────────────────

def fmt_morning_brief(bias: str, sh: list, sl: list, price: float) -> str:
    bias_icon = "🟢 BULLISH" if bias == "BULLISH" else ("🔴 BEARISH" if bias == "BEARISH" else "⚪ NEUTRAL")
    plan = "BUY only" if bias == "BULLISH" else ("SELL only" if bias == "BEARISH" else "No trade today")
    sh_str = " → ".join(f"${x:.2f}" for x in sh[:3]) if sh else "N/A"
    sl_str = " → ".join(f"${x:.2f}" for x in sl[:3]) if sl else "N/A"
    now = now_pkt()
    return (
        f"{SEP}\n"
        f"🌅 **GOLDPULSE — MORNING BRIEF**\n"
        f"📅 {now.strftime('%A, %d %b %Y')} | 🕐 {now.strftime('%I:%M %p')} PKT\n"
        f"{SEP}\n"
        f"**BIAS: {bias_icon}**\n"
        f"Price: ${price:.2f}\n"
        f"{SEP}\n"
        f"KEY LEVELS:\n"
        f"Last 3 Highs: {sh_str}\n"
        f"Last 3 Lows:  {sl_str}\n"
        f"{SEP}\n"
        f"Plan: {plan}\n"
        f"London: 2:00 PM PKT | NY: 7:00 PM PKT\n"
        f"{SEP}\n"
        f"⚠️ Educational only."
    )


def fmt_bias_alert(bias: str, sh: list, sl: list, price: float, session: str) -> str:
    bias_icon = "🟢 BULLISH" if bias == "BULLISH" else ("🔴 BEARISH" if bias == "BEARISH" else "⚪ NEUTRAL")
    plan = "BUY only" if bias == "BULLISH" else ("SELL only" if bias == "BEARISH" else "No trade")
    sh_str = " → ".join(f"${x:.2f}" for x in sh[:3]) if sh else "N/A"
    sl_str = " → ".join(f"${x:.2f}" for x in sl[:3]) if sl else "N/A"
    return (
        f"{SEP}\n"
        f"📊 **STEP 1 — 1H BIAS ({session.upper()})**\n"
        f"{SEP}\n"
        f"Bias: **{bias_icon}**\n"
        f"Price: ${price:.2f}\n"
        f"Last 3 Highs: {sh_str}\n"
        f"Last 3 Lows:  {sl_str}\n"
        f"Plan: {plan}\n"
        f"{SEP}\n"
        f"⏳ Watching for sweep..."
    )


def fmt_sweep_alert(direction: str, swept: float, wick: float, price: float) -> str:
    dir_icon = "🟢" if direction == "BULLISH" else "🔴"
    return (
        f"{SEP}\n"
        f"🌊 **STEP 2 — SWEEP DETECTED**\n"
        f"{SEP}\n"
        f"Type: **{direction}** {dir_icon}\n"
        f"Swept Level: ${swept:.2f}\n"
        f"Sweep Wick:  ${wick:.2f}\n"
        f"Price Now:   ${price:.2f}\n"
        f"Bias Match: ✅\n"
        f"{SEP}\n"
        f"⚡ Scanning for FVG..."
    )


def fmt_no_sweep(session: str, bias: str, price: float, elapsed_min: int) -> str:
    bias_icon = "🟢" if bias == "BULLISH" else "🔴"
    now = now_pkt()
    return (
        f"⏳ **STEP 2 — MONITORING ({session})** | {now.strftime('%I:%M %p')} PKT\n"
        f"Bias: {bias} {bias_icon} | Price: ${price:.2f}\n"
        f"Elapsed: {elapsed_min} min | No sweep yet..."
    )


def fmt_fvg_alert(fvg_type: str, high: float, low: float,
                  size_pips: float, price: float) -> str:
    midpoint = (high + low) / 2
    dir_icon = "🟢" if fvg_type == "BULLISH" else "🔴"
    distance = round(abs(price - midpoint) / PIP_SIZE, 1)
    return (
        f"{SEP}\n"
        f"⚡ **STEP 3 — FVG FOUND**\n"
        f"{SEP}\n"
        f"Type: **{fvg_type}** {dir_icon}\n"
        f"FVG High:  ${high:.2f}\n"
        f"FVG Low:   ${low:.2f}\n"
        f"Midpoint:  ${midpoint:.2f}\n"
        f"Size: {size_pips:.1f} pips\n"
        f"{SEP}\n"
        f"🎯 Waiting for price to enter zone...\n"
        f"Distance: {distance:.1f} pips away"
    )


def fmt_trade_signal(direction: str, entry: float, sl: float,
                     tp1: float, tp2: float, tp3: float,
                     rr1: float, rr2: float, rr3: float,
                     session: str, fvg_high: float, fvg_low: float) -> str:
    dir_icon = "🟢" if direction == "BUY" else "🔴"
    lots = calculate_lot_size()
    now = now_pkt()
    return (
        f"{SEP}\n"
        f"🚀 **TRADE SIGNAL — {SYMBOL}**\n"
        f"🕐 {now.strftime('%I:%M %p')} PKT | Session: {session}\n"
        f"{SEP}\n"
        f"Direction: **{direction} {dir_icon}**\n"
        f"FVG Zone: ${fvg_low:.2f} — ${fvg_high:.2f}\n"
        f"\n"
        f"**ENTRY:** ${entry:.2f} (market order)\n"
        f"**SL:** ${sl:.2f} ({SL_PIPS} pips)\n"
        f"\n"
        f"**SMC TARGETS:**\n"
        f"TP1: ${tp1:.2f} — 15M liquidity (1:{rr1}) → close 50%\n"
        f"TP2: ${tp2:.2f} — 1H liquidity (1:{rr2}) → close 30% ⭐\n"
        f"TP3: ${tp3:.2f} — Weekly liquidity (1:{rr3}) → close 20%\n"
        f"\n"
        f"**RISK:**\n"
        f"Account: $50,000 | Risk: 1% = ${RISK_AMOUNT}\n"
        f"Lot Size: {lots:.2f} lots\n"
        f"{SEP}\n"
        f"⚡ **Open MT5 and execute NOW!**"
    )


def fmt_daily_summary(stats: dict) -> str:
    bias = state["bias"]
    bias_icon = "🟢 BULLISH" if bias == "BULLISH" else ("🔴 BEARISH" if bias == "BEARISH" else "⚪ NEUTRAL")
    now = now_pkt()
    return (
        f"{SEP}\n"
        f"📋 **DAILY SUMMARY — {now.strftime('%d %b %Y')}**\n"
        f"{SEP}\n"
        f"Bias: {bias_icon}\n"
        f"Sweeps: {stats['sweeps_detected']} | FVGs: {stats['fvgs_detected']}\n"
        f"Signals Sent: {stats['signals']}\n"
        f"{SEP}"
    )


# ── Section 7: SMC Analysis ──────────────────────────────────

def detect_swing_highs(candles: list, lookback: int = 1, max_results: int = 3) -> list:
    results = []
    n = len(candles)
    for i in range(n - 2, lookback - 1, -1):
        if i < lookback or i > n - 2:
            continue
        is_swing = True
        for j in range(1, lookback + 1):
            if i - j < 0 or i + j >= n:
                is_swing = False
                break
            if candles[i]["high"] <= candles[i - j]["high"]:
                is_swing = False
                break
            if candles[i]["high"] <= candles[i + j]["high"]:
                is_swing = False
                break
        if is_swing:
            results.append(candles[i]["high"])
            if len(results) >= max_results:
                break
    return results


def detect_swing_lows(candles: list, lookback: int = 1, max_results: int = 3) -> list:
    results = []
    n = len(candles)
    for i in range(n - 2, lookback - 1, -1):
        if i < lookback or i > n - 2:
            continue
        is_swing = True
        for j in range(1, lookback + 1):
            if i - j < 0 or i + j >= n:
                is_swing = False
                break
            if candles[i]["low"] >= candles[i - j]["low"]:
                is_swing = False
                break
            if candles[i]["low"] >= candles[i + j]["low"]:
                is_swing = False
                break
        if is_swing:
            results.append(candles[i]["low"])
            if len(results) >= max_results:
                break
    return results


def detect_1h_bias(candles_1h: list):
    sh = detect_swing_highs(candles_1h, lookback=1, max_results=3)
    sl = detect_swing_lows(candles_1h, lookback=1, max_results=3)

    if len(sh) < 2 or len(sl) < 2:
        log.warning("Insufficient swing points — sh=%d sl=%d → NEUTRAL", len(sh), len(sl))
        return "NEUTRAL", sh, sl

    if sh[0] > sh[1] and sl[0] > sl[1]:
        bias = "BULLISH"
    elif sh[0] < sh[1] and sl[0] < sl[1]:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    log.info("1H Bias: %s | SH: %s | SL: %s", bias, sh[:2], sl[:2])
    return bias, sh, sl


def detect_sweep(candles_15m: list, bias: str, sh: list, sl: list):
    if not candles_15m or len(candles_15m) < 3 or not sh or not sl:
        return False, None

    candle = candles_15m[-2]  # last completed candle

    if bias == "BULLISH":
        level = sl[0]
        if candle["low"] < level and candle["close"] > level:
            return True, {
                "swept_level": level,
                "wick":        candle["low"],
                "candle_time": candle["time"],
            }

    elif bias == "BEARISH":
        level = sh[0]
        if candle["high"] > level and candle["close"] < level:
            return True, {
                "swept_level": level,
                "wick":        candle["high"],
                "candle_time": candle["time"],
            }

    return False, None


def detect_fvg(candles_15m: list, sweep_time: int, bias: str):
    if not candles_15m or len(candles_15m) < 3:
        return None

    sweep_idx = None
    for i, c in enumerate(candles_15m):
        if c["time"] == sweep_time:
            sweep_idx = i
            break

    if sweep_idx is None:
        log.debug("Sweep candle not found in 15M window")
        return None

    for n in range(sweep_idx + 2, len(candles_15m) - 1):
        c_prev2 = candles_15m[n - 2]
        c_curr  = candles_15m[n]

        if bias == "BULLISH" and c_prev2["high"] < c_curr["low"]:
            size = round((c_curr["low"] - c_prev2["high"]) / PIP_SIZE, 1)
            return {
                "type":        "BULLISH",
                "high":        c_curr["low"],
                "low":         c_prev2["high"],
                "formed_time": c_curr["time"],
                "size_pips":   size,
            }

        if bias == "BEARISH" and c_prev2["low"] > c_curr["high"]:
            size = round((c_prev2["low"] - c_curr["high"]) / PIP_SIZE, 1)
            return {
                "type":        "BEARISH",
                "high":        c_prev2["low"],
                "low":         c_curr["high"],
                "formed_time": c_curr["time"],
                "size_pips":   size,
            }

    return None


def find_tp(swing_levels: list, entry: float, direction: str) -> float:
    if direction == "BUY":
        above = [s for s in swing_levels if s > entry]
        return min(above) if above else 0.0
    else:
        below = [s for s in swing_levels if s < entry]
        return max(below) if below else 0.0


def calculate_tps(bias: str, entry: float, candles_15m: list, candles_1h: list):
    direction = "BUY" if bias == "BULLISH" else "SELL"

    # TP1 — 15M swing
    levels_15m = (detect_swing_highs(candles_15m, lookback=2, max_results=5)
                  if direction == "BUY"
                  else detect_swing_lows(candles_15m, lookback=2, max_results=5))
    tp1 = find_tp(levels_15m, entry, direction)

    # TP2 — 1H swing
    levels_1h = (detect_swing_highs(candles_1h, lookback=1, max_results=5)
                 if direction == "BUY"
                 else detect_swing_lows(candles_1h, lookback=1, max_results=5))
    tp2 = find_tp(levels_1h, entry, direction)

    # TP3 — Weekly
    candles_w1 = get_candles_1w(2)
    tp3 = 0.0
    if candles_w1:
        tp3 = max(c["high"] for c in candles_w1) if direction == "BUY" \
              else min(c["low"] for c in candles_w1)

    # Fallbacks if levels not found or invalid (0.0 = not found)
    if direction == "BUY":
        if tp1 <= entry or tp1 <= 0: tp1 = round(entry + SL_DISTANCE * 2, 2)
        if tp2 <= tp1  or tp2 <= 0:  tp2 = round(tp1   + SL_DISTANCE * 3, 2)
        if tp3 <= tp2  or tp3 <= 0:  tp3 = round(tp2   + SL_DISTANCE * 5, 2)
    else:
        if tp1 >= entry or tp1 <= 0: tp1 = round(entry - SL_DISTANCE * 2, 2)
        if tp2 >= tp1  or tp2 <= 0:  tp2 = round(tp1   - SL_DISTANCE * 3, 2)
        if tp3 >= tp2  or tp3 <= 0:  tp3 = round(tp2   - SL_DISTANCE * 5, 2)

    return round(tp1, 2), round(tp2, 2), round(tp3, 2)


def calculate_lot_size() -> float:
    return round(min(5.0, max(0.01, RISK_AMOUNT / (SL_PIPS * 10))), 2)


def calculate_rr(entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    if risk == 0:
        return 0.0
    return round(abs(tp - entry) / risk, 1)


def price_in_fvg(price: float, fvg: dict) -> bool:
    return fvg["low"] <= price <= fvg["high"]


# ── Section 8: Trade Signal ───────────────────────────────────

def send_trade_signal():
    with state["_lock"]:
        if state["signal_sent"]:
            return
        fvg     = state["fvg"]
        bias    = state["bias"]
        session = state["current_session"] or ""

    if fvg is None or bias == "NEUTRAL":
        return

    price = get_live_price()
    if price == 0.0:
        return

    if not price_in_fvg(price, fvg):
        return

    log.info("Price $%.2f entered FVG [%.2f–%.2f] — sending signal",
             price, fvg["low"], fvg["high"])

    direction = "BUY" if bias == "BULLISH" else "SELL"
    entry = price
    sl = round(entry - SL_DISTANCE if direction == "BUY" else entry + SL_DISTANCE, 2)

    candles_15m = get_candles_15m(20)
    candles_1h  = get_candles_1h(10)
    if not candles_15m or not candles_1h:
        log.error("Cannot fetch candles for TP calculation")
        return

    tp1, tp2, tp3 = calculate_tps(bias, entry, candles_15m, candles_1h)
    rr1 = calculate_rr(entry, sl, tp1)
    rr2 = calculate_rr(entry, sl, tp2)
    rr3 = calculate_rr(entry, sl, tp3)

    msg = fmt_trade_signal(direction, entry, sl, tp1, tp2, tp3,
                           rr1, rr2, rr3, session, fvg["high"], fvg["low"])
    send_discord(msg)

    with state["_lock"]:
        state["signal_sent"] = True
        state["daily_stats"]["signals"] += 1

    log.info("Trade signal sent — %s entry=%.2f sl=%.2f tp1=%.2f tp2=%.2f tp3=%.2f",
             direction, entry, sl, tp1, tp2, tp3)


# ── Section 9: Scheduled Jobs + Session Loop + Main ─────────

def _refresh_bias(session_label: str):
    candles_1h = get_candles_1h(10)
    if not candles_1h or len(candles_1h) < 4:
        log.warning("Not enough 1H candles for bias refresh")
        send_discord(f"⚠️ **GoldPulse** — Could not fetch price data for {session_label} bias")
        return

    bias, sh, sl = detect_1h_bias(candles_1h)
    price = get_live_price()

    with state["_lock"]:
        state["bias"]             = bias
        state["bias_swing_highs"] = sh
        state["bias_swing_lows"]  = sl
        if state["signal_sent"] is False:   # only reset sweep/fvg if no active signal
            state["sweep_detected"]  = False
            state["sweep_time"]      = None
            state["sweep_direction"] = None
            state["sweep_price"]     = None
            state["fvg"]             = None
            state["signal_sent"]     = False
        state["last_30min_update"] = now_pkt()

    send_discord(fmt_bias_alert(bias, sh, sl, price, session_label))
    log.info("Bias refreshed for %s: %s", session_label, bias)


def job_morning_brief():
    log.info("Running morning brief")
    candles_1h = get_candles_1h(10)
    if not candles_1h or len(candles_1h) < 4:
        send_discord("⚠️ **GoldPulse Morning Brief** — Could not fetch price data")
        return

    bias, sh, sl = detect_1h_bias(candles_1h)
    price = get_live_price()

    with state["_lock"]:
        state["bias"]             = bias
        state["bias_swing_highs"] = sh
        state["bias_swing_lows"]  = sl

    send_discord(fmt_morning_brief(bias, sh, sl, price))


def job_london_open():
    log.info("London session opening")
    _refresh_bias("London")
    with state["_lock"]:
        state["session_active"]  = True
        state["current_session"] = "London"
        state["session_start"]   = now_pkt()
        state["last_30min_update"] = now_pkt()


def job_london_close():
    log.info("London session closing")
    with state["_lock"]:
        state["session_active"]  = False
        state["current_session"] = None


def job_ny_open():
    log.info("NY session opening")
    _refresh_bias("NY")
    with state["_lock"]:
        state["session_active"]  = True
        state["current_session"] = "NY"
        state["session_start"]   = now_pkt()
        state["last_30min_update"] = now_pkt()


def job_ny_close():
    log.info("NY session closing")
    with state["_lock"]:
        state["session_active"]  = False
        state["current_session"] = None
    job_daily_summary()


def job_daily_summary():
    log.info("Sending daily summary")
    with state["_lock"]:
        stats = dict(state["daily_stats"])

    send_discord(fmt_daily_summary(stats))

    with state["_lock"]:
        state["daily_stats"] = {
            "signals": 0,
            "sweeps_detected": 0,
            "fvgs_detected": 0,
        }
        # Full reset for new day
        state["sweep_detected"]  = False
        state["sweep_time"]      = None
        state["fvg"]             = None
        state["signal_sent"]     = False


def session_sweep_loop():
    log.info("Sweep monitoring thread started")

    while True:
        with state["_lock"]:
            active = state["session_active"]

        if not active:
            time.sleep(30)
            continue

        iter_start = time.monotonic()

        try:
            with state["_lock"]:
                bias         = state["bias"]
                sweep_done   = state["sweep_detected"]
                fvg          = state["fvg"]
                signal_sent  = state["signal_sent"]
                sh           = state["bias_swing_highs"][:]
                sl_levels    = state["bias_swing_lows"][:]
                session      = state["current_session"] or ""
                last_update  = state["last_30min_update"]

            if bias == "NEUTRAL":
                time.sleep(300)
                continue

            if not sweep_done:
                # Step 2 — sweep check
                candles_15m = get_candles_15m(20)
                if candles_15m and len(candles_15m) >= 3:
                    found, sweep_info = detect_sweep(candles_15m, bias, sh, sl_levels)
                    if found:
                        log.info("Sweep detected — level=%.2f wick=%.2f",
                                 sweep_info["swept_level"], sweep_info["wick"])
                        price = get_live_price()
                        send_discord(fmt_sweep_alert(
                            bias,
                            sweep_info["swept_level"],
                            sweep_info["wick"],
                            price,
                        ))
                        with state["_lock"]:
                            state["sweep_detected"]  = True
                            state["sweep_time"]      = sweep_info["candle_time"]
                            state["sweep_direction"] = bias
                            state["sweep_price"]     = sweep_info["swept_level"]
                            state["daily_stats"]["sweeps_detected"] += 1
                            state["last_30min_update"] = now_pkt()

                # 30-min no-sweep status update
                if last_update is not None:
                    elapsed_sec = (now_pkt() - last_update).total_seconds()
                    if elapsed_sec >= 1800:
                        price = get_live_price()
                        elapsed_min = int(elapsed_sec / 60)
                        send_discord(fmt_no_sweep(session, bias, price, elapsed_min))
                        with state["_lock"]:
                            state["last_30min_update"] = now_pkt()

            elif fvg is None:
                # Step 3 — FVG detection
                candles_15m = get_candles_15m(20)
                with state["_lock"]:
                    sweep_time = state["sweep_time"]

                if candles_15m and sweep_time is not None:
                    new_fvg = detect_fvg(candles_15m, sweep_time, bias)
                    if new_fvg:
                        log.info("FVG detected — %s high=%.2f low=%.2f",
                                 new_fvg["type"], new_fvg["high"], new_fvg["low"])
                        price = get_live_price()
                        send_discord(fmt_fvg_alert(
                            new_fvg["type"],
                            new_fvg["high"],
                            new_fvg["low"],
                            new_fvg["size_pips"],
                            price,
                        ))
                        with state["_lock"]:
                            state["fvg"] = new_fvg
                            state["daily_stats"]["fvgs_detected"] += 1

            elif not signal_sent:
                # Step 4 — monitor for FVG entry and send trade signal
                send_trade_signal()

        except Exception as exc:
            log.error("session_sweep_loop exception: %s", exc)

        elapsed = time.monotonic() - iter_start
        time.sleep(max(5, 300 - elapsed))


def setup_schedule():
    # UTC times (Render and most cloud hosts run UTC)
    # PKT = UTC+5
    schedule.every().day.at("01:45").do(job_morning_brief)  # 06:45 PKT
    schedule.every().day.at("09:00").do(job_london_open)    # 14:00 PKT
    schedule.every().day.at("13:00").do(job_london_close)   # 18:00 PKT
    schedule.every().day.at("14:00").do(job_ny_open)        # 19:00 PKT
    schedule.every().day.at("18:00").do(job_ny_close)       # 23:00 PKT

    log.info("Schedule set (UTC) — Morning 01:45 | London 09:00-13:00 | NY 14:00-18:00")


# ── Flask Health Server ──────────────────────────────────────

_flask_app = Flask(__name__)

@_flask_app.route("/")
def health():
    price = state.get("last_price", 0.0)
    bias  = state.get("bias", "NEUTRAL")
    return {"status": "ok", "bot": "GoldPulse AI", "bias": bias, "price": price}, 200


# ── Bot Startup (runs on module import — works with gunicorn) ─

_bot_started = False
_bot_lock    = threading.Lock()


def _schedule_loop():
    while True:
        try:
            schedule.run_pending()
        except Exception as exc:
            log.error("Schedule loop error: %s", exc)
        time.sleep(30)


def _start_bot():
    global _bot_started
    with _bot_lock:
        if _bot_started:
            return
        _bot_started = True

    log.info("=" * 60)
    log.info("GoldPulse AI starting — Signal Bot (no MT5 required)")
    log.info("Data: Yahoo Finance (%s) | Signals: Discord", YF_SYMBOL)
    log.info("=" * 60)

    price = get_live_price()
    state["last_price"] = price
    if price > 0:
        log.info("Price data OK — XAUUSD: $%.2f", price)
    else:
        log.warning("Could not fetch live price at startup — will retry during sessions")

    send_discord(
        f"{SEP}\n"
        f"🟢 **GoldPulse AI — STARTED**\n"
        f"{SEP}\n"
        f"Symbol: {SYMBOL} | Risk: ${RISK_AMOUNT}/trade\n"
        f"SL: {SL_PIPS} pips | Lots: {calculate_lot_size()}\n"
        f"Price now: ${price:.2f}\n"
        f"{SEP}\n"
        f"📱 Signals sent here — execute on **MT5 mobile app**\n"
        f"London: 2 PM PKT | NY: 7 PM PKT\n"
        f"{SEP}"
    )

    setup_schedule()

    threading.Thread(target=session_sweep_loop, daemon=True, name="SweepLoop").start()
    threading.Thread(target=_schedule_loop,     daemon=True, name="SchedLoop").start()

    log.info("Bot running — waiting for sessions...")


# Start bot when module is imported (gunicorn imports module, then serves Flask)
_start_bot()


if __name__ == "__main__":
    # Local dev: run Flask dev server directly
    port = int(os.environ.get("PORT", 8080))
    _flask_app.run(host="0.0.0.0", port=port, use_reloader=False)
