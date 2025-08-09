# manual_bot.py
# InsiderSignals_Manual — Intraday signals (5m/15m/1h) for top-50 USDT pairs
# Uses Binance public API (no API key). Runs as a Web Service on Render.

import os, time, math, json, logging, threading, http.server, socketserver
from datetime import datetime, timedelta, time as dtime
import pytz
import requests
import numpy as np

from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue

# ---------- Keep-alive tiny web server for Render free plan ----------
PORT = int(os.getenv("PORT", "10000"))
class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def _start_http():
    with socketserver.TCPServer(("", PORT), _Handler) as httpd:
        httpd.serve_forever()
threading.Thread(target=_start_http, daemon=True).start()
# ---------------------------------------------------------------------

# ---------- Settings ----------
TOKEN = os.getenv("TELEGRAM_MANUAL_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Dublin")
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "20"))
FORCED_CHAT_ID = os.getenv("CHAT_ID")  # optional, numeric ID or channel id
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))  # scan every 2 min
SIGNAL_COOLDOWN_MIN = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "20"))   # min minutes between signals per symbol

tz = pytz.timezone(TIMEZONE)
logging.basicConfig(level=logging.INFO)
BINANCE = "https://api.binance.com"  # public endpoints

# ---------- State ----------
signals = []                 # for the daily report
last_signal_time = {}        # symbol -> datetime
watchlist_symbols = []       # top 50 USDT pairs (filled on startup)
last_chat_id = None          # where to reply if CHAT_ID not set

# ---------- Helpers: indicators ----------
def ema(arr, period):
    arr = np.array(arr, dtype=float)
    if len(arr) < period: return None
    alpha = 2 / (period + 1)
    e = [arr[0]]
    for x in arr[1:]:
        e.append(alpha * x + (1 - alpha) * e[-1])
    return np.array(e)

def rsi(arr, period=14):
    arr = np.array(arr, dtype=float)
    if len(arr) <= period: return None
    diff = np.diff(arr)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    # Wilder’s smoothing
    avg_gain = [np.mean(gain[:period])]
    avg_loss = [np.mean(loss[:period])]
    for i in range(period, len(diff)):
        avg_gain.append((avg_gain[-1]*(period-1) + gain[i]) / period)
        avg_loss.append((avg_loss[-1]*(period-1) + loss[i]) / period)
    avg_gain = np.array(avg_gain); avg_loss = np.array(avg_loss)
    rs = np.where(avg_loss == 0, 0, avg_gain / avg_loss)
    rsi_vals = 100 - (100 / (1 + rs))
    # pad to align length with price array
    pad = len(arr) - len(rsi_vals) - 1
    return np.concatenate([np.array([np.nan]*pad), rsi_vals])

def atr(highs, lows, closes, period=14):
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    closes = np.array(closes, dtype=float)
    if len(closes) <= period: return None
    prev_close = np.concatenate([[closes[0]], closes[:-1]])
    tr = np.maximum(highs - lows, np.maximum(abs(highs - prev_close), abs(lows - prev_close)))
    # Wilder’s smoothing
    atr_vals = [np.mean(tr[1:period+1])]
    for i in range(period+1, len(tr)):
        atr_vals.append((atr_vals[-1]*(period-1) + tr[i]) / period)
    pad = len(tr) - len(atr_vals)
    return np.concatenate([np.array([np.nan]*pad), np.array(atr_vals)])

# ---------- Binance data ----------
def get_top50_usdt_symbols():
    """Return top-50 USDT symbols by 24h quote volume (spot)."""
    try:
        r = requests.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        # filter USDT spot pairs, ignore leveraged/UP/DOWN/.*BULL/BEAR tokens
        filtered = [
            d for d in data
            if d.get("symbol","").endswith("USDT")
            and "UP" not in d["symbol"] and "DOWN" not in d["symbol"]
            and "BULL" not in d["symbol"] and "BEAR" not in d["symbol"]
        ]
        # sort by quoteVolume (as float) desc
        filtered.sort(key=lambda x: float(x.get("quoteVolume","0")), reverse=True)
        syms = [d["symbol"] for d in filtered[:50]]
        return syms
    except Exception as e:
        logging.error(f"get_top50_usdt_symbols error: {e}")
        return ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","XLMUSDT"]  # fallback

def get_klines(symbol, interval, limit=200):
    """Return klines arrays: opens, highs, lows, closes, volumes"""
    try:
        r = requests.get(
            f"{BINANCE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        arr = r.json()
        opens = [float(x[1]) for x in arr]
        highs = [float(x[2]) for x in arr]
        lows  = [float(x[3]) for x in arr]
        closes= [float(x[4]) for x in arr]
        vols  = [float(x[5]) for x in arr]
        return opens, highs, lows, closes, vols
    except Exception as e:
        logging.error(f"get_klines error {symbol} {interval}: {e}")
        return None, None, None, None, None

# ---------- Strategy (intraday 5m/15m/1h) ----------
def intraday_signal(symbol):
    """
    Return dict signal or None.
    Long: 5m close breaks 20-bar high & 5m vol >= 2x avg20,
          AND 15m RSI rising from 40–55 area,
          AND 1h close > EMA50.
    Short: mirror.
    """
    o5,h5,l5,c5,v5 = get_klines(symbol, "5m", 200)
    if not c5: return None
    o15,h15,l15,c15,v15 = get_klines(symbol, "15m", 200)
    o1,h1,l1,c1,v1 = get_klines(symbol, "1h", 200)

    # Indicators
    ema50_1h = ema(c1, 50)
    if ema50_1h is None: return None
    last_ema50_1h = ema50_1h[-1]
    last_close_1h = c1[-1]

    # 5m breakout
    hh20 = max(h5[-21:-1])  # highest high of previous 20 bars (exclude current)
    vol_avg20 = sum(v5[-21:-1]) / 20.0
    last_close_5m = c5[-1]
    last_vol_5m = v5[-1]

    # 15m RSI
    rsi15 = rsi(c15, 14)
    if rsi15 is None: return None
    last_rsi15 = rsi15[-1]
    prev_rsi15 = rsi15[-2] if not math.isnan(rsi15[-2]) else last_rsi15

    # 5m ATR for stops/targets
    atr5 = atr(h5, l5, c5, 14)
    if atr5 is None or math.isnan(atr5[-1]): return None
    last_atr5 = float(atr5[-1])

    # LONG conditions
    long_ok = (
        last_close_5m > hh20 and
        last_vol_5m >= 2.0 * vol_avg20 and
        40 <= last_rsi15 <= 70 and last_rsi15 > prev_rsi15 and
        last_close_1h > last_ema50_1h
    )

    # SHORT conditions
    ll20 = min(l5[-21:-1])
    short_ok = (
        last_close_5m < ll20 and
        last_vol_5m >= 2.0 * vol_avg20 and
        30 <= last_rsi15 <= 60 and last_rsi15 < prev_rsi15 and
        last_close_1h < last_ema50_1h
    )

    if not (long_ok or short_ok):
        return None

    side = "LONG" if long_ok else "SHORT"
    entry = last_close_5m
    if long_ok:
        sl = entry - 1.5 * last_atr5
        tp1 = entry + 1.0 * last_atr5
        tp2 = entry + 2.0 * last_atr5
    else:
        sl = entry + 1.5 * last_atr5
        tp1 = entry - 1.0 * last_atr5
        tp2 = entry - 2.0 * last_atr5

    return {
        "symbol": symbol,
        "side": side,
        "entry": round(entry, 6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "sl": round(sl, 6),
        "atr": round(last_atr5, 6),
    }

# ---------- Telegram helpers ----------
def target_chat_id(update: Update = None) -> int:
    global last_chat_id
    if FORCED_CHAT_ID: return int(FORCED_CHAT_ID)
    if update:
        last_chat_id = update.effective_chat.id
        return last_chat_id
    return last_chat_id

def format_signal_msg(sig) -> str:
    return (
        f"🚀 <b>{sig['symbol']}</b> {sig['side']} (Intraday)\n"
        f"Entry: <b>{sig['entry']}</b>\n"
        f"TP1: <b>{sig['tp1']}</b> | TP2: <b>{sig['tp2']}</b>\n"
        f"SL: <b>{sig['sl']}</b>\n"
        f"ATR(5m): {sig['atr']}\n"
        f"Rules: 5m breakout + vol≥2x, 15m RSI rising, 1h EMA50 trend."
    )

# ---------- Commands ----------
def start(update: Update, context: CallbackContext):
    chat_id = target_chat_id(update)
    context.bot.send_message(
        chat_id=chat_id,
        text=("👋 InsiderSignals_Manual (Intraday)\n"
              "You’ll receive real signals for top 50 USDT pairs.\n\n"
              "Commands:\n"
              "/signals – show last signals\n"
              "/ping – health check"),
        parse_mode=ParseMode.HTML
    )

def ping(update: Update, context: CallbackContext):
    chat_id = target_chat_id(update)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    context.bot.send_message(chat_id=chat_id, text=f"✅ Bot alive: {now} {TIMEZONE}")

def list_signals(update: Update, context: CallbackContext):
    chat_id = target_chat_id(update)
    if not signals:
        context.bot.send_message(chat_id=chat_id, text="No signals in the last 24h yet.")
        return
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff]
    if not recent:
        context.bot.send_message(chat_id=chat_id, text="No signals in the last 24h.")
        return
    lines = ["📋 <b>Recent Signals (24h)</b>"]
    for s in recent[-10:]:
        lines.append(f"• {s['symbol']} {s['side']} @ {s['entry']} | TP1 {s['tp1']} | TP2 {s['tp2']} | SL {s['sl']}")
    context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)

# ---------- Jobs ----------
def scan_top50(context: CallbackContext):
    """Scan market and emit signals."""
    chat_id = target_chat_id()
    if not chat_id:  # nobody pressed /start and no FORCED_CHAT_ID
        return
    global watchlist_symbols
    now = datetime.now(tz)
    try:
        if not watchlist_symbols:
            watchlist_symbols = get_top50_usdt_symbols()
            logging.info(f"Loaded top50: {watchlist_symbols[:10]} ...")
        # refresh top50 once per hour
        if now.minute == 0:
            watchlist_symbols[:] = get_top50_usdt_symbols()

        for sym in watchlist_symbols:
            # throttle per symbol
            last_t = last_signal_time.get(sym)
            if last_t and (now - last_t) < timedelta(minutes=SIGNAL_COOLDOWN_MIN):
                continue
            sig = intraday_signal(sym)
            if not sig: 
                continue
            last_signal_time[sym] = now
            # record for report
            record = {"time": now, **sig}
            signals.append(record)
            context.bot.send_message(chat_id=chat_id, text=format_signal_msg(sig), parse_mode=ParseMode.HTML)
            time.sleep(0.2)  # gentle rate limit
    except Exception as e:
        logging.error(f"scan_top50 error: {e}")

def send_daily_report(context: CallbackContext):
    chat_id = target_chat_id()
    if not chat_id: return
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff]
    if not recent:
        msg = "📊 Daily Report (last 24h): No signals."
    else:
        lines = ["📊 <b>Daily Report</b> (last 24h)"]
        for s in recent:
            t = s["time"].strftime("%H:%M")
            lines.append(f"• {t} {s['symbol']} {s['side']} @ {s['entry']} | TP1 {s['tp1']} | TP2 {s['tp2']} | SL {s['sl']}")
        msg = "\n".join(lines)
    context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

# ---------- Main ----------
def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_MANUAL_TOKEN")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("ping", ping))
    dp.add_handler(CommandHandler("signals", list_signals))

    jq: JobQueue = updater.job_queue
    # start scanning right away, then every SCAN_INTERVAL_SECONDS
    jq.run_repeating(scan_top50, interval=SCAN_INTERVAL_SECONDS, first=10)

    # daily report at 20:00 local time
    send_time = dtime(hour=DAILY_REPORT_HOUR, minute=0, tzinfo=tz)
    jq.run_daily(send_daily_report, time=send_time)

    logging.info("InsiderSignals_Manual (intraday) started.")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
