# manual_bot.py — Coinbase • Auto-Trading + Live Config Commands
# 2025-08-09
# Features:
# - Coinbase USD/USDT scanner (5m/15m/1h; 4h only if enabled and available)
# - Telegram alerts with score, entry, TP/SL
# - Auto-trading (Coinbase Advanced Trade): PAPER (default) or LIVE (LONG-only on spot)
# - Live config from Telegram: /setmax /setalloc /setscore /setmode /getconfig
# - Persistent config in config.json, survives restarts
# - Score filter (skip weak setups), cooldown per pair, max open orders, balance checks
# - Graceful skips when no balance; no spammy 4h errors

import os, time, math, random, logging, threading, http.server, socketserver, json, hmac, hashlib, base64, uuid
from datetime import datetime, timedelta, time as dtime
import pytz, requests, numpy as np
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue
from requests.exceptions import RequestException
from http.client import RemoteDisconnected

# ---------------- tiny HTTP server for Render keep-alive ----------------
PORT = int(os.getenv("PORT", "10000"))
class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def _keepalive():
    with socketserver.TCPServer(("", PORT), _Handler) as httpd:
        httpd.serve_forever()
threading.Thread(target=_keepalive, daemon=True).start()

# ------------------------------- defaults from env ----------------------
TOKEN = os.getenv("TELEGRAM_MANUAL_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Dublin")
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "20"))
FORCED_CHAT_ID = os.getenv("CHAT_ID")  # optional numeric id

# scanner cadence
SCAN_INTERVAL_SECONDS   = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
SYMBOLS_PER_SCAN        = int(os.getenv("SYMBOLS_PER_SCAN", "30"))
SIGNAL_COOLDOWN_MIN     = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "8"))
MAX_PAIRS               = int(os.getenv("MAX_PAIRS", "0"))  # 0 = ALL USD/USDT

# score & mode defaults (can be overridden at runtime)
ENV_SCORE_MIN           = int(os.getenv("SCORE_MIN", "90"))
ENV_MODE                = os.getenv("MODE", "RELAXED").strip().upper()  # RELAXED/STRICT

# relaxed/strict baseline gates
RELAXED_DEFAULTS = dict(
    BREAKOUT_LOOKBACK=3, VOLUME_MULTIPLIER=1.05, RSI_MIN=20, RSI_MAX=80,
    USE_EMA_CROSS=True, EMA_TOL_PCT=0.012, USE_ADX_FILTER=True, ADX_MIN=10,
    USE_HTF_FILTER=False, HTF_TOL_PCT=0.005, USE_BTC_BIAS=False
)
STRICT_DEFAULTS = dict(
    BREAKOUT_LOOKBACK=6, VOLUME_MULTIPLIER=1.2, RSI_MIN=35, RSI_MAX=65,
    USE_EMA_CROSS=True, EMA_TOL_PCT=0.008, USE_ADX_FILTER=True, ADX_MIN=18,
    USE_HTF_FILTER=True, HTF_TOL_PCT=0.005, USE_BTC_BIAS=True
)

# risk / exits (R = ATR(5m))
R_TP1              = float(os.getenv("R_TP1", "1.1"))
R_TP2              = float(os.getenv("R_TP2", "2.2"))
R_SL               = float(os.getenv("R_SL", "1.1"))
TRAIL_AFTER_TP1    = os.getenv("TRAIL_AFTER_TP1", "1") == "1"

# keep-alive
KEEPALIVE_URL      = os.getenv("KEEPALIVE_URL", "").strip()
KEEPALIVE_SECONDS  = int(os.getenv("KEEPALIVE_SECONDS", "240"))

# retries / jitter
RETRY_MAX          = int(os.getenv("RETRY_MAX", "4"))
RETRY_BASE_DELAY   = float(os.getenv("RETRY_BASE_DELAY", "0.6"))
REQ_JITTER_MIN_MS  = int(os.getenv("REQ_JITTER_MIN_MS", "50"))
REQ_JITTER_MAX_MS  = int(os.getenv("REQ_JITTER_MAX_MS", "140"))

# Trading (Coinbase Advanced Trade)
TRADE_MODE         = os.getenv("TRADE_MODE", "PAPER").upper()   # PAPER or LIVE
COINBASE_API_KEY   = os.getenv("COINBASE_API_KEY", "").strip()
COINBASE_API_SECRET= os.getenv("COINBASE_API_SECRET", "").strip()  # base64
ENV_POSITION_PCT   = float(os.getenv("POSITION_PCT", "0.05"))   # 5% default
ENV_MAX_OPEN_TRADES= int(os.getenv("MAX_OPEN_TRADES", "10"))
MIN_USD_PER_TRADE  = float(os.getenv("MIN_USD_PER_TRADE", "10"))

tz = pytz.timezone(TIMEZONE)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ----------------------------- Coinbase API ----------------------------------
CB_BASE = "https://api.exchange.coinbase.com"  # market data
CB_AT   = "https://api.coinbase.com"           # Advanced Trade v3

GRAN_5M, GRAN_15M, GRAN_1H, GRAN_4H = 300, 900, 3600, 4*3600
session = requests.Session()
session.headers.update({"User-Agent": "insider-autotrade-bot/1.2"})

# ------------------------------- state ---------------------------------------
signals = []
last_signal_time = {}
watchlist_symbols = []
scan_cursor = 0
last_chat_id = None
BTC_STATE = {"bias": 0, "ts": 0}
NO_4H = set()
OPEN_TRADES = {}
CONFIG_PATH = "config.json"

# ------------------------- runtime config (load/save) -------------------------
CONFIG = {
    "MODE": ENV_MODE,                 # RELAXED or STRICT
    "SCORE_MIN": ENV_SCORE_MIN,       # 1..100
    "POSITION_PCT": ENV_POSITION_PCT, # 0.01..1.0
    "MAX_OPEN_TRADES": ENV_MAX_OPEN_TRADES
}
def load_config():
    global CONFIG
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            CONFIG.update(data)
            logging.info(f"Loaded config: {CONFIG}")
    except Exception as e:
        logging.warning(f"config load error: {e}")
def save_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(CONFIG, f)
    except Exception as e:
        logging.warning(f"config save error: {e}")

load_config()

# --- apply mode to globals ---
def apply_mode(mode: str):
    mode = (mode or "RELAXED").upper()
    params = RELAXED_DEFAULTS if mode == "RELAXED" else STRICT_DEFAULTS
    globals().update(params)
    CONFIG["MODE"] = mode
apply_mode(CONFIG["MODE"])

# ---------------------------- indicators -------------------------------------
def ema(arr, period):
    arr = np.array(arr, dtype=float)
    if len(arr) < period: return None
    alpha = 2/(period+1); out=[arr[0]]
    for x in arr[1:]: out.append(alpha*x + (1-alpha)*out[-1])
    return np.array(out)

def rsi(arr, period=14):
    arr = np.array(arr, dtype=float)
    if len(arr) <= period: return None
    diff = np.diff(arr)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    ag = [np.mean(gain[:period])]; al = [np.mean(loss[:period])]
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

def adx(high, low, close, period=14):
    high = np.array(high, float); low = np.array(low, float); close = np.array(close, float)
    if len(close) <= period+1: return None
    up = high[1:] - high[:-1]
    dn = low[:-1] - low[1:]
    plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(high[1:] - low[1:], np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])))
    def _ema(x, p):
        a=2/(p+1); out=[x[0]]
        for v in x[1:]: out.append(a*v + (1-a)*out[-1])
        return np.array(out)
    atr14 = _ema(tr, period)
    pdi = 100 * _ema(plus_dm, period) / np.maximum(atr14, 1e-9)
    mdi = 100 * _ema(minus_dm, period) / np.maximum(atr14, 1e-9)
    dx = 100 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-9)
    adxv = _ema(dx, period)
    pad = len(close) - len(adxv) - 1
    return np.concatenate([np.full(pad, np.nan), adxv])

# --------------------------- data helpers ------------------------------------
def jitter_sleep():
    time.sleep(random.randint(REQ_JITTER_MIN_MS, REQ_JITTER_MAX_MS) / 1000.0)

def _get_with_retry(url, params=None, timeout=15):
    delay = RETRY_BASE_DELAY
    for attempt in range(RETRY_MAX):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 502, 503, 504):
                raise RequestException(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (RequestException, RemoteDisconnected) as e:
            if attempt == RETRY_MAX - 1: raise
            sleep_for = delay * (1.6 ** attempt) + random.uniform(0.05, 0.25)
            logging.warning(f"Retry {attempt+1}/{RETRY_MAX} for {url} ({e}); sleeping {sleep_for:.2f}s")
            time.sleep(sleep_for)
    raise RequestException("unreachable")

def get_coinbase_pairs(limit=0):
    try:
        r = _get_with_retry(f"{CB_BASE}/products", timeout=20)
        products = r.json()
        out = []
        for p in products:
            if p.get("status") != "online": continue
            if p.get("trading_disabled") is True: continue
            if (p.get("quote_currency") or "").upper() not in ("USD","USDT"): continue
            pid = p.get("id")
            if pid: out.append(pid)
        if limit and limit > 0: out = out[:limit]
        logging.info(f"Loaded {len(out)} USD/USDT pairs (Coinbase).")
        return out
    except Exception as e:
        logging.error(f"get_coinbase_pairs error: {e}")
        return ["BTC-USD","ETH-USD","SOL-USD","XRP-USD","ADA-USD"]

def cb_candles(product_id, granularity, limit=300):
    try:
        jitter_sleep()
        r = _get_with_retry(
            f"{CB_BASE}/products/{product_id}/candles",
            params={"granularity": granularity},
            timeout=20
        )
        data = r.json()
        if not isinstance(data, list) or not data: return (None,)*5
        data.sort(key=lambda x: x[0])
        t, low, high, op, close, vol = zip(*[(d[0],d[1],d[2],d[3],d[4],d[5]) for d in data[-limit:]])
        return list(op), list(high), list(low), list(close), list(vol)
    except Exception as e:
        logging.error(f"coinbase candles error {product_id} g{granularity}: {e}")
        return (None,)*5

# -------------------------- Advanced Trade auth/helpers -----------------------
def _cb_sign(timestamp, method, request_path, body=""):
    secret = base64.b64decode(COINBASE_API_SECRET) if COINBASE_API_SECRET else b""
    what = f"{timestamp}{method}{request_path}{body}".encode()
    sig = hmac.new(secret, what, hashlib.sha256).digest()
    return base64.b64encode(sig).decode()

def _cb_headers(method, path, body=""):
    ts = str(int(time.time()))
    return {
        "CB-ACCESS-KEY": COINBASE_API_KEY,
        "CB-ACCESS-SIGN": _cb_sign(ts, method, path, body),
        "CB-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json"
    }

def cb_get_accounts():
    if TRADE_MODE != "LIVE": return {"USD": {"available_balance": {"value": "10000"}}}
    path = "/api/v3/brokerage/accounts"
    r = session.get(CB_AT + path, headers=_cb_headers("GET", path), timeout=20)
    r.raise_for_status()
    out = {}
    for acc in r.json().get("accounts", []):
        out[acc["currency"]] = acc
    return out

def cb_place_market_buy(product_id, usd_amount):
    if TRADE_MODE != "LIVE":
        return {"paper": True, "success": True, "order_id": str(uuid.uuid4())}
    path = "/api/v3/brokerage/orders"
    body = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {
            "market_market_ioc": {
                "quote_size": f"{usd_amount:.2f}"
            }
        }
    }
    body_str = json.dumps(body, separators=(",", ":"))
    r = session.post(CB_AT + path, headers=_cb_headers("POST", path, body_str), data=body_str, timeout=20)
    r.raise_for_status()
    return r.json()

# ------------------------------ helpers/filters -------------------------------
def r_targets(entry, atr5, side, r1=1.1, r2=2.2, sl_mult=1.1):
    if side == "LONG":
        sl  = entry - sl_mult*atr5
        tp1 = entry + r1*atr5
        tp2 = entry + r2*atr5
    else:
        sl  = entry + sl_mult*atr5
        tp1 = entry - r1*atr5
        tp2 = entry - r2*atr5
    return sl, tp1, tp2

def near_trend_ok(price, ema_val, tol_pct):
    if ema_val is None or ema_val == 0: return False
    return abs(price - ema_val) / ema_val <= tol_pct

def illiquid(o5,h5,l5,c5):
    if not c5 or len(c5) < 50: return True
    rng = (max(h5[-20:]) - min(l5[-20:])) / max(1e-9, c5[-1])
    return rng < 0.0005

def update_btc_bias():
    if not globals().get("USE_BTC_BIAS", False):
        BTC_STATE.update({"bias": 0, "ts": time.time()}); return
    now = time.time()
    if now - BTC_STATE["ts"] < 50: return
    o5,h5,l5,c5,v5 = cb_candles("BTC-USD", GRAN_5M, 120)
    o1,h1,l1,c1,v1 = cb_candles("BTC-USD", GRAN_1H, 200)
    if not c5 or not c1:
        BTC_STATE.update({"bias": 0, "ts": now}); return
    ema50_1h = ema(c1, 50); r5 = rsi(c5, 14)
    bias = 0
    if ema50_1h is not None and r5 is not None and not math.isnan(r5[-1]):
        if c1[-1] < ema50_1h[-1] and r5[-1] < 35: bias = -1
        if c1[-1] > ema50_1h[-1] and r5[-1] > 65: bias = +1
    BTC_STATE.update({"bias": bias, "ts": now})

# ------------------------------ strategy -------------------------------------
def score_signal(side, price, ema50_now, atr5, vol_ok, rsi_val, adx_val):
    score = 0
    if vol_ok: score += 25
    if adx_val is not None and not math.isnan(adx_val):
        score += min(25, max(0, (adx_val - 10) * 2))  # base for relaxed; STRICT raises ADX_MIN upstream
    if ema50_now:
        dist = abs(price - ema50_now)/ema50_now
        score += max(0, 25 - 100*dist)
    if rsi_val is not None and not math.isnan(rsi_val):
        score += max(0, 25 - abs(50 - rsi_val))
    return int(min(100, score))

def intraday_signal(pid):
    o5,h5,l5,c5,v5   = cb_candles(pid, GRAN_5M, 200)
    if not c5: return None
    if illiquid(o5,h5,l5,c5): return None

    o15,h15,l15,c15,v15 = cb_candles(pid, GRAN_15M, 200)
    o1,h1,l1,c1,v1      = cb_candles(pid, GRAN_1H, 200)

    # 4h only if STRICT or USE_HTF_FILTER True and not flagged
    c4h = None
    if globals().get("USE_HTF_FILTER", False) and pid not in NO_4H:
        o4h,h4h,l4h,c4h,v4h = cb_candles(pid, GRAN_4H, 200)
        if not c4h:
            NO_4H.add(pid)
            logging.info(f"{pid} has no 4h granularity — skipping 4h checks hereafter.")

    ema50_1h  = ema(c1, 50)
    ema200_1h = ema(c1, 200)
    if ema50_1h is None or ema200_1h is None: return None
    ema50_now = ema50_1h[-1]
    ema200_4h = ema(c4h, 200) if c4h else None

    adx1h = adx(h1, l1, c1, 14) if c1 else None
    adx_ok = True
    if globals().get("USE_ADX_FILTER", True):
        adx_ok = (adx1h is not None and not math.isnan(adx1h[-1]) and adx1h[-1] >= globals().get("ADX_MIN", 10))

    BL = globals().get("BREAKOUT_LOOKBACK",3)
    hh = max(h5[-(BL+1):-1])
    ll = min(l5[-(BL+1):-1])
    vol_avg = sum(v5[-(BL+1):-1]) / BL
    last_close = c5[-1]; last_vol = v5[-1]

    r = rsi(c15, 14)
    if r is None or math.isnan(r[-1]): return None
    r_ok = (globals().get("RSI_MIN",20) <= r[-1] <= globals().get("RSI_MAX",80))

    ema20_5 = ema(c5, 20)
    use_ema_cross = globals().get("USE_EMA_CROSS", True)
    cross_up = ema20_5 is not None and c5[-1] > ema20_5[-1] and c5[-2] <= ema20_5[-2]
    cross_dn = ema20_5 is not None and c5[-1] < ema20_5[-1] and c5[-2] >= ema20_5[-2]
    brk_up = last_close > hh
    brk_dn = last_close < ll
    long_trigger  = brk_up or (use_ema_cross and cross_up)
    short_trigger = brk_dn or (use_ema_cross and cross_dn)

    EMA_TOL_PCT = globals().get("EMA_TOL_PCT", 0.012)
    trend_up = (last_close > ema50_now) or near_trend_ok(last_close, ema50_now, EMA_TOL_PCT)
    trend_dn = (last_close < ema50_now) or near_trend_ok(last_close, ema50_now, EMA_TOL_PCT)

    # HTF filter — only if enabled AND 4h exists
    htf_up = True; htf_dn = True
    if globals().get("USE_HTF_FILTER", False):
        if ema200_4h is not None:
            HTF_TOL_PCT = globals().get("HTF_TOL_PCT", 0.005)
            htf_up = (c1[-1] >= (1-HTF_TOL_PCT)*ema200_1h[-1]) and (c4h[-1] >= (1-HTF_TOL_PCT)*ema200_4h[-1])
            htf_dn = (c1[-1] <= (1+HTF_TOL_PCT)*ema200_1h[-1]) and (c4h[-1] <= (1+HTF_TOL_PCT)*ema200_4h[-1])

    vol_ok = last_vol >= globals().get("VOLUME_MULTIPLIER",1.05) * max(vol_avg, 1e-9)

    if globals().get("USE_BTC_BIAS", False):
        if BTC_STATE["bias"] == -1 and long_trigger:  return None
        if BTC_STATE["bias"] == +1 and short_trigger: return None

    a5_arr = atr(h5, l5, c5, 14)
    if a5_arr is None or math.isnan(a5_arr[-1]): return None
    a5 = float(a5_arr[-1])

    long_ok  = long_trigger  and vol_ok and r_ok and trend_up and htf_up and adx_ok
    short_ok = short_trigger and vol_ok and r_ok and trend_dn and htf_dn and adx_ok
    if not (long_ok or short_ok): return None

    side = "LONG" if long_ok else "SHORT"
    sl, tp1, tp2 = r_targets(last_close, a5, side, r1=R_TP1, r2=R_TP2, sl_mult=R_SL)

    return {
        "symbol": pid, "side": side,
        "entry": round(last_close, 6),
        "tp1": round(tp1, 6), "tp2": round(tp2, 6),
        "sl": round(sl, 6),
        "atr": round(a5, 6),
        "score": score_signal(side, last_close, ema50_now, a5, vol_ok, r[-1], adx1h[-1] if adx1h is not None else None)
    }

# ----------------------------- telegram --------------------------------------
def target_chat_id(update: Update = None):
    global last_chat_id
    if FORCED_CHAT_ID: return int(FORCED_CHAT_ID)
    if update:
        last_chat_id = update.effective_chat.id
        return last_chat_id
    return last_chat_id

def format_signal_msg(sig):
    tag = CONFIG["MODE"]
    return (
        f"🚀 <b>{sig['symbol']}</b> {sig['side']} (Intraday • {tag})\n"
        f"Entry: <b>{sig['entry']}</b>\n"
        f"TP1: <b>{sig['tp1']}</b> | TP2: <b>{sig['tp2']}</b>\n"
        f"SL: <b>{sig['sl']}</b>\n"
        f"ATR(5m): {sig['atr']} | Score: <b>{sig['score']}</b>/100\n"
        f"Rules: breakout({globals().get('BREAKOUT_LOOKBACK')})/EMA20, "
        f"vol≥{globals().get('VOLUME_MULTIPLIER')}×, RSI {globals().get('RSI_MIN')}-{globals().get('RSI_MAX')}, "
        f"EMA50±{int(globals().get('EMA_TOL_PCT')*100)}%."
    )

def format_trade_open(tr):
    return (f"🟢 Executed BUY {tr['product_id']} ~${tr['usd_spent']:.2f}\n"
            f"PosID: {tr['id']} | Mode: {TRADE_MODE}")

def format_config():
    pct = int(round(CONFIG['POSITION_PCT']*100))
    return (f"⚙️ <b>Config</b>\n"
            f"Mode: <b>{CONFIG['MODE']}</b>\n"
            f"SCORE_MIN: <b>{CONFIG['SCORE_MIN']}</b>\n"
            f"MAX_OPEN_TRADES: <b>{CONFIG['MAX_OPEN_TRADES']}</b>\n"
            f"POSITION_PCT: <b>{pct}%</b>\n"
            f"TRADE_MODE: <b>{TRADE_MODE}</b>")

# ------------------------------ commands -------------------------------------
def cmd_start(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(
        chat_id=target_chat_id(update),
        text=(f"👋 InsiderSignals_Manual (Coinbase)\n"
              f"{format_config()}\n\n"
              "Commands:\n"
              "/getconfig – show settings\n"
              "/setmax N (1..20)\n"
              "/setalloc P (1..100%)\n"
              "/setscore S (1..100)\n"
              "/setmode RELAXED|STRICT\n"
              "/signals – recent signals (24h)\n"
              "/status – open trades\n"
              "/scan – scan now\n"
              "/ping – bot health"),
        parse_mode=ParseMode.HTML
    )

def cmd_getconfig(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(chat_id=target_chat_id(update), text=format_config(), parse_mode=ParseMode.HTML)

def cmd_setmax(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    try:
        n = int(update.message.text.split()[1])
        if not (1 <= n <= 20): raise ValueError
        CONFIG["MAX_OPEN_TRADES"] = n
        save_config()
        ctx.bot.send_message(chat_id=chat_id, text=f"✅ MAX_OPEN_TRADES set to {n}.")
    except Exception:
        ctx.bot.send_message(chat_id=chat_id, text="Usage: /setmax 1..20")

def cmd_setalloc(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    try:
        p = float(update.message.text.split()[1])
        if not (1 <= p <= 100): raise ValueError
        CONFIG["POSITION_PCT"] = p/100.0
        save_config()
        ctx.bot.send_message(chat_id=chat_id, text=f"✅ POSITION_PCT set to {p}%.")
    except Exception:
        ctx.bot.send_message(chat_id=chat_id, text="Usage: /setalloc 1..100 (percent)")

def cmd_setscore(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    try:
        s = int(update.message.text.split()[1])
        if not (1 <= s <= 100): raise ValueError
        CONFIG["SCORE_MIN"] = s
        save_config()
        ctx.bot.send_message(chat_id=chat_id, text=f"✅ SCORE_MIN set to {s}.")
    except Exception:
        ctx.bot.send_message(chat_id=chat_id, text="Usage: /setscore 1..100")

def cmd_setmode(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    try:
        m = update.message.text.split()[1].upper()
        if m not in ("RELAXED","STRICT"): raise ValueError
        apply_mode(m)
        save_config()
        ctx.bot.send_message(chat_id=chat_id, text=f"✅ MODE set to {m}. Filters updated immediately.")
    except Exception:
        ctx.bot.send_message(chat_id=chat_id, text="Usage: /setmode RELAXED|STRICT")

def cmd_ping(update: Update, ctx: CallbackContext):
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    cfg = CONFIG
    ctx.bot.send_message(chat_id=target_chat_id(update),
        text=f"✅ Bot alive: {now} {TIMEZONE} | {cfg['MODE']} | SCORE_MIN={cfg['SCORE_MIN']} | MAX_OPEN_TRADES={cfg['MAX_OPEN_TRADES']}")

def cmd_signals(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff and s.get("score", 0) >= CONFIG["SCORE_MIN"]]
    if not recent:
        ctx.bot.send_message(chat_id=chat_id, text="No signals in the last 24h (above threshold)."); return
    lines = [f"📋 <b>Recent Signals (24h, score≥{CONFIG['SCORE_MIN']})</b>"]
    for s in recent[-16:]:
        lines.append(f"• {s['symbol']} {s['side']} @ {s['entry']} | TP1 {s['tp1']} | TP2 {s['tp2']} | SL {s['sl']} | Score {s.get('score','-')}")
    ctx.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_status(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    if not OPEN_TRADES:
        ctx.bot.send_message(chat_id=chat_id, text="No open trades."); return
    lines = ["📊 <b>Open Trades</b>"]
    for oid, tr in OPEN_TRADES.items():
        lines.append(f"• {tr['product_id']} ~${tr['usd_spent']:.2f} | id={oid} | opened={tr['time'].strftime('%H:%M')}")
    ctx.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_scan(update: Update, ctx: CallbackContext):
    chat_id = target_chat_id(update)
    ctx.bot.send_message(chat_id=chat_id, text=f"🔍 Scan running (mode={CONFIG['MODE']}, score≥{CONFIG['SCORE_MIN']})…")
    _scan_chunk(ctx, push_to=chat_id)

# ------------------------------- trading helpers ------------------------------
def _can_open_more_trades():
    return len(OPEN_TRADES) < CONFIG["MAX_OPEN_TRADES"]

def _free_usd_balance():
    try:
        accts = cb_get_accounts()
        usd_acc = accts.get("USD")
        if not usd_acc: return 0.0
        return float(usd_acc["available_balance"]["value"])
    except Exception as e:
        logging.warning(f"balance check failed: {e}")
        return 1e9 if TRADE_MODE != "LIVE" else 0.0

def _execute_long_trade(sig, ctx: CallbackContext, chat_id):
    if TRADE_MODE not in ("LIVE", "PAPER"): return
    if not _can_open_more_trades():
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: reached MAX_OPEN_TRADES"); return

    free_usd = _free_usd_balance()
    alloc = max(MIN_USD_PER_TRADE, free_usd * float(CONFIG["POSITION_PCT"]))
    if free_usd < MIN_USD_PER_TRADE:
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: insufficient USD balance"); return

    try:
        res = cb_place_market_buy(sig["symbol"], alloc)
        oid = res.get("order_id", str(uuid.uuid4()))
        OPEN_TRADES[oid] = {
            "id": oid,
            "product_id": sig["symbol"],
            "usd_spent": alloc,
            "time": datetime.now(tz),
            "entry_hint": sig["entry"],
        }
        ctx.bot.send_message(chat_id=chat_id, text=(
            f"🟢 Executed BUY {sig['symbol']} ~${alloc:.2f}\n"
            f"id={oid} | Mode: {TRADE_MODE}"
        ))
    except Exception as e:
        ctx.bot.send_message(chat_id=chat_id, text=f"⚪ Skipped: order error: {e}")

# ------------------------------- scan loop ------------------------------------
def _ensure_pairs():
    global watchlist_symbols
    if not watchlist_symbols:
        watchlist_symbols = get_coinbase_pairs(limit=MAX_PAIRS)
        logging.info(f"Watchlist size: {len(watchlist_symbols)}")
    return watchlist_symbols

def _scan_chunk(ctx: CallbackContext, push_to=None):
    global scan_cursor
    chat_id = push_to or target_chat_id()
    if not chat_id: return
    update_btc_bias()
    now = datetime.now(tz)
    syms = _ensure_pairs()
    if not syms: return

    start = scan_cursor
    end = min(start + SYMBOLS_PER_SCAN, len(syms))
    chunk = syms[start:end]
    scan_cursor = 0 if end >= len(syms) else end

    hits = 0
    for pid in chunk:
        lt = last_signal_time.get(pid)
        if lt and (now - lt) < timedelta(minutes=SIGNAL_COOLDOWN_MIN): continue

        sig = intraday_signal(pid)
        if not sig or sig.get("score", 0) < CONFIG["SCORE_MIN"]:
            time.sleep(0.05); continue

        last_signal_time[pid] = now
        record = {"time": now, **sig}
        signals.append(record)
        ctx.bot.send_message(chat_id=chat_id, text=format_signal_msg(sig), parse_mode=ParseMode.HTML)

        if sig["side"] == "LONG":
            _execute_long_trade(sig, ctx, chat_id)
        else:
            if TRADE_MODE == "LIVE":
                ctx.bot.send_message(chat_id=chat_id, text="(SHORT ignored on spot; derivatives not enabled.)")

        hits += 1
        time.sleep(0.1)

    if hits == 0 and push_to:
        ctx.bot.send_message(chat_id=push_to, text=f"(No valid setups ≥ {CONFIG['SCORE_MIN']} this minute — still watching.)")

# ------------------------------- jobs ----------------------------------------
def job_scan(ctx: CallbackContext): _scan_chunk(ctx)

def job_daily(ctx: CallbackContext):
    chat_id = target_chat_id()
    if not chat_id: return
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff and s.get("score", 0) >= CONFIG["SCORE_MIN"]]
    if not recent:
        msg = f"📊 Daily Report (24h): No signals with score ≥ {CONFIG['SCORE_MIN']}."
    else:
        lines = [f"📊 <b>Daily Report</b> (last 24h, score≥{CONFIG['SCORE_MIN']})"]
        for s in recent:
            t = s["time"].strftime("%H:%M")
            lines.append(f"• {t} {s['symbol']} {s['side']} @ {s['entry']} | TP1 {s['tp1']} | TP2 {s['tp2']} | SL {s['sl']} | Score {s.get('score','-')}")
        msg = "\n".join(lines)
    ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def job_keepalive(ctx: CallbackContext):
    if not KEEPALIVE_URL: return
    try:
        session.get(KEEPALIVE_URL, timeout=10)
        logging.info("Keep-alive ping OK")
    except Exception as e:
        logging.warning(f"Keep-alive ping failed: {e}")

# -------------------------------- main ---------------------------------------
def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_MANUAL_TOKEN")

    updater = Updater(TOKEN, use_context=True)
    updater.bot.delete_webhook(drop_pending_updates=True)

    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("getconfig", cmd_getconfig))
    dp.add_handler(CommandHandler("setmax", cmd_setmax))
    dp.add_handler(CommandHandler("setalloc", cmd_setalloc))
    dp.add_handler(CommandHandler("setscore", cmd_setscore))
    dp.add_handler(CommandHandler("setmode", cmd_setmode))
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("signals", cmd_signals))
    dp.add_handler(CommandHandler("status", cmd_status))
    dp.add_handler(CommandHandler("scan", cmd_scan))

    jq: JobQueue = updater.job_queue
    jq.run_repeating(job_scan, interval=SCAN_INTERVAL_SECONDS, first=random.randint(6, 12))
    send_time = dtime(hour=DAILY_REPORT_HOUR, minute=0, tzinfo=tz)
    jq.run_daily(job_daily, time=send_time)
    if KEEPALIVE_URL:
        jq.run_repeating(job_keepalive, interval=KEEPALIVE_SECONDS, first=5)

    logging.info(f"InsiderSignals_Manual started. Mode={CONFIG['MODE']} SCORE_MIN={CONFIG['SCORE_MIN']} MAX_OPEN_TRADES={CONFIG['MAX_OPEN_TRADES']} POSITION_PCT={CONFIG['POSITION_PCT']}")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
