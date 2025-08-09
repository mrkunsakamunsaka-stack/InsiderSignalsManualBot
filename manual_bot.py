# manual_bot.py
# InsiderSignals_Manual — Intraday signals (5m/15m/1h) for top-50 USDT pairs
# Uses Binance public API (no key). Works on Render free (web service with keep-alive HTTP).

import os, time, math, logging, threading, http.server, socketserver
from datetime import datetime, timedelta, time as dtime
import pytz, requests, numpy as np
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue

# ---------- keep-alive HTTP server so Render Web Service stays up ----------
PORT = int(os.getenv("PORT", "10000"))
class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def _keepalive():
    with socketserver.TCPServer(("", PORT), _Handler) as httpd:
        httpd.serve_forever()
threading.Thread(target=_keepalive, daemon=True).start()
# ---------------------------------------------------------------------------

# -------------------- settings (override in Render Environment) ------------
TOKEN = os.getenv("TELEGRAM_MANUAL_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Dublin")
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "20"))
FORCED_CHAT_ID = os.getenv("CHAT_ID")  # optional numeric id

# cadence / throttles
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))     # scan every 1 min
SIGNAL_COOLDOWN_MIN = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "10"))     # per-symbol cooldown
SYMBOLS_PER_SCAN = int(os.getenv("SYMBOLS_PER_SCAN", "10"))               # rotate 10 symbols per scan

# strategy thresholds (relaxed but sane). Tweak from Render without changing code.
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "10"))             # bars for breakout check (5m)
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", "1.2"))          # 5m vol vs avg
RSI_MIN = float(os.getenv("RSI_MIN", "35"))
RSI_MAX = float(os.getenv("RSI_MAX", "65"))
USE_EMA_CROSS = os.getenv("USE_EMA_CROSS", "1") == "1"                    # allow 5m EMA20 cross entries

# ---------------------------------------------------------------------------
tz = pytz.timezone(TIMEZONE)
logging.basicConfig(level=logging.INFO)
BINANCE = "https://api.binance.com"

# -------------------- state --------------------
signals = []                 # for /signals + daily report
last_signal_time = {}        # symbol -> datetime
watchlist_symbols = []       # top 50 list
scan_cursor = 0              # rotating window index
last_chat_id = None

# -------------------- indicators --------------------
def ema(arr, period):
    arr = np.array(arr, dtype=float)
    if len(arr) < period: return None
    alpha = 2 / (period + 1)
    out = [arr[0]]
    for x in arr[1:]:
        out.append(alpha * x + (1 - alpha) * out[-1])
    return np.array(out)

def rsi(arr, period=14):
    arr = np.array(arr, dtype=float)
    if len(arr) <= period: return None
    diff = np.diff(arr)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    ag = [np.mean(gain[:period])]
    al = [np.mean(loss[:period])]
    for i in range(period, len(diff)):
        ag.append((ag[-1]*(period-1) + gain[i]) / period)
        al.append((al[-1]*(period-1) + loss[i]) / period)
    rs = np.where(np.array(al) == 0, 0, np.array(ag)/np.array(al))
    r = 100 - (100/(1+rs))
    pad = len(arr) - len(r) - 1
    return np.concatenate([np.full(pad, np.nan), r])

def atr(h, l, c, period=14):
    h = np.array(h, float); l = np.array(l, float); c = np.array(c, float)
    if len(c) <= period: return None
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    a = [np.mean(tr[1:period+1])]
    for i in range(period+1, len(tr)):
        a.append((a[-1]*(period-1) + tr[i]) / period)
    pad = len(tr) - len(a)
    return np.concatenate([np.full(pad, np.nan), np.array(a)])

# -------------------- binance helpers --------------------
def get_top50_usdt_symbols():
    """Top-50 USDT spot pairs by 24h quote volume (exclude UP/DOWN/BULL/BEAR)."""
    try:
        data = requests.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10).json()
        filt = [d for d in data
                if d.get("symbol","").endswith("USDT")
                and all(x not in d["symbol"] for x in ["UP","DOWN","BULL","BEAR"])]
        filt.sort(key=lambda x: float(x.get("quoteVolume","0")), reverse=True)
        return [d["symbol"] for d in filt[:50]]
    except Exception as e:
        logging.error(f"top50 error: {e}")
        return ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","XLMUSDT"]

def get_klines(symbol, interval, limit=200, retries=3):
    """Return (opens, highs, lows, closes, volumes) or all None on failure."""
    for attempt in range(retries):
        try:
            r = requests.get(
                f"{BINANCE}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                headers={"User-Agent": "insider-bot/1.0"},
                timeout=10,
            )
            data = r.json()

            # Binance may return an error object; detect and raise
            if isinstance(data, dict):
                raise Exception(f"Binance error: {data.get('msg','unknown')}")

            opens  = [float(x[1]) for x in data]
            highs  = [float(x[2]) for x in data]
            lows   = [float(x[3]) for x in data]
            closes = [float(x[4]) for x in data]
            vols   = [float(x[5]) for x in data]
            return opens, highs, lows, closes, vols

        except Exception as e:
            logging.error(f"get_klines error {symbol} {interval}: {e}")
            time.sleep(0.6 + 0.6*attempt)  # gentle backoff

    return None, None, None, None, None

# -------------------- strategy (intraday 5m/15m/1h) --------------------
def intraday_signal(symbol):
    """Return dict signal or None."""
    o5,h5,l5,c5,v5 = get_klines(symbol, "5m", 200)
    if not c5: return None
    o15,h15,l15,c15,v15 = get_klines(symbol, "15m", 200)
    o1,h1,l1,c1,v1 = get_klines(symbol, "1h", 200)

    ema50_1h = ema(c1, 50)
    if ema50_1h is None: return None
    trend_up = c1[-1] > ema50_1h[-1]
    trend_dn = c1[-1] < ema50_1h[-1]

    # breakout on 5m with shorter lookback
    hh = max(h5[-(BREAKOUT_LOOKBACK+1):-1])
    ll = min(l5[-(BREAKOUT_LOOKBACK+1):-1])
    vol_avg = sum(v5[-(BREAKOUT_LOOKBACK+1):-1]) / BREAKOUT_LOOKBACK
    last_close = c5[-1]; last_vol = v5[-1]

    # RSI on 15m (wider gate)
    r = rsi(c15, 14)
    if r is None or math.isnan(r[-1]): return None
    prev = r[-2] if not math.isnan(r[-2]) else r[-1]
    r_up = (RSI_MIN <= r[-1] <= RSI_MAX) and (r[-1] > prev)
    r_dn = (RSI_MIN <= r[-1] <= RSI_MAX) and (r[-1] < prev)

    # optional entry: EMA20 cross on 5m
    ema20_5 = ema(c5, 20)
    cross_up = ema20_5 is not None and c5[-1] > ema20_5[-1] and c5[-2] <= ema20_5[-2]
    cross_dn = ema20_5 is not None and c5[-1] < ema20_5[-1] and c5[-2] >= ema20_5[-2]

    atr5 = atr(h5, l5, c5, 14)
    if atr5 is None or math.isnan(atr5[-1]): return None
    a5 = float(atr5[-1])

    long_ok = (
        ((last_close > hh) or (USE_EMA_CROSS and cross_up)) and
        last_vol >= VOLUME_MULTIPLIER * vol_avg and
        r_up and trend_up
    )
    short_ok = (
        ((last_close < ll) or (USE_EMA_CROSS and cross_dn)) and
        last_vol >= VOLUME_MULTIPLIER * vol_avg and
        r_dn and trend_dn
    )
    if not (long_ok or short_ok): return None

    side = "LONG" if long_ok else "SHORT"
    entry = last_close
    if long_ok:
        sl = entry - 1.5*a5; tp1 = entry + 1.0*a5; tp2 = entry + 2.0*a5
    else:
        sl = entry + 1.5*a5; tp1 = entry - 1.0*a5; tp2 = entry - 2.0*a5

    return {
        "symbol": symbol, "side": side,
        "entry": round(entry, 6), "tp1": round(tp1, 6),
        "tp2": round(tp2, 6), "sl": round(sl, 6),
        "atr": round(a5, 6)
    }

# -------------------- telegram helpers --------------------
def target_chat_id(update: Update = None):
    global last_chat_id
    if FORCED_CHAT_ID: return int(FORCED_CHAT_ID)
    if update:
        last_chat_id = update.effective_chat.id
        return last_chat_id
    return last_chat_id

def format_signal_msg(sig):
    return (
        f"🚀 <b>{sig['symbol']}</b> {sig['side']} (Intraday)\n"
        f"Entry: <b>{sig['entry']}</b>\n"
        f"TP1: <b>{sig['tp1']}</b> | TP2: <b>{sig['tp2']}</b>\n"
        f"SL: <b>{sig['sl']}</b>\n"
        f"ATR(5m): {sig['atr']}\n"
        f"Rules: breakout/EMA20 + vol≥{VOLUME_MULTIPLIER}×, RSI {RSI_MIN}-{RSI_MAX}, 1h EMA50 trend."
    )

# -------------------- commands --------------------
def cmd_start(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(
        chat_id=target_chat_id(update),
        text=("👋 InsiderSignals_Manual (Intraday)\n"
              "Real signals for top-50 USDT pairs.\n\n"
              "Commands:\n"
              "/ping – bot health\n"
              "/signals – recent signals (24h)\n"
              "/scan – scan now\n"
              "/testsignal – sample alert"),
        parse_mode=ParseMode.HTML
    )

def cmd_ping(update: Update, ctx: CallbackContext):
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ Bot alive: {now} {TIMEZONE}")

def cmd_signals(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff]
    if not recent:
        ctx.bot.send_message(chat_id=chat_id, text="No signals in the last 24h (yet).")
        return
    lines = ["📋 <b>Recent Signals (24h)</b>"]
    for s in recent[-12:]:
        lines.append(f"• {s['symbol']} {s['side']} @ {s['entry']} | TP1 {s['tp1']} | TP2 {s['tp2']} | SL {s['sl']}")
    ctx.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_testsignal(update: Update, ctx: CallbackContext):
    now = datetime.now(tz)
    demo = {"symbol":"XRPUSDT","side":"LONG","entry":0.55,"tp1":0.60,"tp2":0.65,"sl":0.52,"atr":0.01,"time":now}
    signals.append(demo)
    ctx.bot.send_message(chat_id=target_chat_id(update), text=format_signal_msg(demo), parse_mode=ParseMode.HTML)

def cmd_scan(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    ctx.bot.send_message(chat_id=chat_id, text="🔍 Scanning top-50 now…")
    _scan_chunk(ctx, push_to=chat_id)

# -------------------- jobs --------------------
def _ensure_top50():
    global watchlist_symbols
    if not watchlist_symbols:
        watchlist_symbols = get_top50_usdt_symbols()
        logging.info(f"Loaded top50: {watchlist_symbols[:10]} ...")
    return watchlist_symbols

def _scan_chunk(ctx: CallbackContext, push_to=None):
    """Rotate through top-50 in chunks to respect rate limits."""
    global scan_cursor
    chat_id = push_to or target_chat_id()
    if not chat_id:
        return
    now = datetime.now(tz)
    syms = _ensure_top50()
    if not syms:
        return

    start = scan_cursor
    end = min(start + SYMBOLS_PER_SCAN, len(syms))
    chunk = syms[start:end]
    scan_cursor = 0 if end >= len(syms) else end

    hits = 0
    for sym in chunk:
        last_t = last_signal_time.get(sym)
        if last_t and (now - last_t) < timedelta(minutes=SIGNAL_COOLDOWN_MIN):
            continue
        sig = intraday_signal(sym)  # internally retries API calls
        if not sig:
            time.sleep(0.1)
            continue
        last_signal_time[sym] = now
        rec = {"time": now, **sig}
        signals.append(rec)
        ctx.bot.send_message(chat_id=chat_id, text=format_signal_msg(sig), parse_mode=ParseMode.HTML)
        hits += 1
        time.sleep(0.2)

    if hits == 0 and push_to:
        ctx.bot.send_message(chat_id=push_to, text="(No valid setups this minute — still watching.)")

def job_scan(ctx: CallbackContext):
    _scan_chunk(ctx)

def job_daily(ctx: CallbackContext):
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
    ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

# -------------------- main --------------------
def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_MANUAL_TOKEN")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("signals", cmd_signals))
    dp.add_handler(CommandHandler("scan", cmd_scan))
    dp.add_handler(CommandHandler("testsignal", cmd_testsignal))

    jq: JobQueue = updater.job_queue
    jq.run_repeating(job_scan, interval=SCAN_INTERVAL_SECONDS, first=10)
    send_time = dtime(hour=DAILY_REPORT_HOUR, minute=0, tzinfo=tz)
    jq.run_daily(job_daily, time=send_time)

    logging.info("InsiderSignals_Manual (intraday, hardened) started.")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
