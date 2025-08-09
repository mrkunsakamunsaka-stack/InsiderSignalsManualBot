# manual_bot.py — Coinbase • PAPER trading with real-time PnL + alerts + gap-safe exits
# 2025-08-09 (fix: RSI calc, re-enable SHORT signals, enforce SCORE_MIN, scheduler max_instances=2)

import os, time, math, random, logging, threading, http.server, socketserver, json
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

# ------------------------------- config/env ----------------------------------
TOKEN = os.getenv("TELEGRAM_MANUAL_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Dublin")
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "20"))
FORCED_CHAT_ID = os.getenv("CHAT_ID")

# scan cadence
SCAN_INTERVAL_SECONDS   = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
SYMBOLS_PER_SCAN        = int(os.getenv("SYMBOLS_PER_SCAN", "30"))
SIGNAL_COOLDOWN_MIN     = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "8"))
MAX_PAIRS               = int(os.getenv("MAX_PAIRS", "0"))  # 0 = all USD/USDT

# score & mode defaults
ENV_SCORE_MIN           = int(os.getenv("SCORE_MIN", "90"))
ENV_MODE                = os.getenv("MODE", "RELAXED").strip().upper()

# relaxed vs strict baselines
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

# exits (R = ATR(5m))
R_TP1 = float(os.getenv("R_TP1", "1.1"))
R_TP2 = float(os.getenv("R_TP2", "2.2"))
R_SL  = float(os.getenv("R_SL", "1.1"))
TRAIL_AFTER_TP1 = os.getenv("TRAIL_AFTER_TP1", "1") == "1"

# keep-alive
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "").strip()
KEEPALIVE_SECONDS = int(os.getenv("KEEPALIVE_SECONDS", "240"))

# retries / jitter
RETRY_MAX = int(os.getenv("RETRY_MAX", "4"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "0.6"))
REQ_JITTER_MIN_MS = int(os.getenv("REQ_JITTER_MIN_MS", "50"))
REQ_JITTER_MAX_MS = int(os.getenv("REQ_JITTER_MAX_MS", "140"))

# trading mode
TRADE_MODE = os.getenv("TRADE_MODE", "PAPER").upper()

# sizing/limits
ENV_POSITION_PCT    = float(os.getenv("POSITION_PCT", "0.05"))  # 5% of cash
ENV_MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "10"))
MIN_USD_PER_TRADE   = float(os.getenv("MIN_USD_PER_TRADE", "10"))
FEE_RATE            = float(os.getenv("FEE_RATE", "0.001"))     # 0.10% per side

# paper account
PAPER_START_CASH = float(os.getenv("PAPER_START_CASH", "200"))
PAPER_PATH = "paper.json"
PAPER_CHECK_SECONDS = int(os.getenv("PAPER_CHECK_SECONDS", "15"))

# alerts default (overridden by persisted config later)
ALERTS = os.getenv("ALERTS", "1") == "1"

tz = pytz.timezone(TIMEZONE)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ----------------------------- Coinbase API ----------------------------------
CB_BASE = "https://api.exchange.coinbase.com"
GRAN_5M, GRAN_15M, GRAN_1H, GRAN_4H = 300, 900, 3600, 4*3600

session = requests.Session()
session.headers.update({"User-Agent": "insider-paper-rt/1.5"})

# ------------------------------- state ---------------------------------------
signals = []
last_signal_time = {}
watchlist_symbols = []
scan_cursor = 0
last_chat_id = None
BTC_STATE = {"bias": 0, "ts": 0}
NO_4H = set()

CONFIG_PATH = "config.json"
CONFIG = {
    "MODE": ENV_MODE,
    "SCORE_MIN": ENV_SCORE_MIN,
    "POSITION_PCT": ENV_POSITION_PCT,
    "MAX_OPEN_TRADES": ENV_MAX_OPEN_TRADES,
    # ALERTS persisted below after load
}

PAPER = {
    "cash": PAPER_START_CASH,
    "equity": PAPER_START_CASH,
    "positions": {},     # id -> pos
    "closed": [],        # list
    "seq": 1,
    "realized_pnl": 0.0,
    "fees_paid": 0.0,
    "wins": 0,
    "losses": 0
}

# ------------------------- persistence helpers -------------------------
def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f: return json.load(f)
    except Exception as e:
        logging.warning(f"load {path} error: {e}")
    return default

def _save_json(path, data):
    try:
        with open(path, "w") as f: json.dump(data, f)
    except Exception as e:
        logging.warning(f"save {path} error: {e}")

def load_config():
    global CONFIG
    CONFIG.update(_load_json(CONFIG_PATH, CONFIG))

def save_config():
    _save_json(CONFIG_PATH, CONFIG)

def load_paper():
    global PAPER
    data = _load_json(PAPER_PATH, None)
    if data: PAPER.update(data)
    else:
        PAPER.update({"cash": PAPER_START_CASH, "equity": PAPER_START_CASH,
                      "positions": {}, "closed": [], "seq": 1,
                      "realized_pnl": 0.0, "fees_paid": 0.0, "wins": 0, "losses": 0})

def save_paper():
    _save_json(PAPER_PATH, PAPER)

load_config(); load_paper()
# persist/read alerts in CONFIG
try:
    CONFIG["ALERTS"] = int(CONFIG.get("ALERTS", 1 if ALERTS else 0))
    ALERTS = bool(CONFIG["ALERTS"])
except Exception:
    CONFIG["ALERTS"] = 1 if ALERTS else 0
    ALERTS = bool(CONFIG["ALERTS"])

# --- mode mapping
def apply_mode(mode: str):
    mode = (mode or "RELAXED").upper()
    params = RELAXED_DEFAULTS if mode == "RELAXED" else STRICT_DEFAULTS
    globals().update(params); CONFIG["MODE"] = mode
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
    # FIX: correct loss calculation (was flipped)
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
def jitter_sleep(): time.sleep(random.randint(REQ_JITTER_MIN_MS, REQ_JITTER_MAX_MS) / 1000.0)

def _get_with_retry(url, params=None, timeout=15):
    delay = RETRY_BASE_DELAY
    for attempt in range(RETRY_MAX):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 502, 503, 504): raise RequestException(f"HTTP {r.status_code}")
            r.raise_for_status(); return r
        except (RequestException, RemoteDisconnected) as e:
            if attempt == RETRY_MAX - 1: raise
            time.sleep(delay * (1.6 ** attempt) + random.uniform(0.05, 0.25))
    raise RequestException("unreachable")

def get_coinbase_pairs(limit=0):
    try:
        r = _get_with_retry(f"{CB_BASE}/products", timeout=20)
        products = r.json(); out = []
        for p in products:
            if p.get("status") != "online": continue
            if p.get("trading_disabled"): continue
            if (p.get("quote_currency") or "").upper() not in ("USD","USDT"): continue
            pid = p.get("id"); 
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
        r = _get_with_retry(f"{CB_BASE}/products/{product_id}/candles",
                            params={"granularity": granularity}, timeout=20)
        data = r.json()
        if not isinstance(data, list) or not data: return (None,)*5
        data.sort(key=lambda x: x[0])
        t, low, high, op, close, vol = zip(*[(d[0],d[1],d[2],d[3],d[4],d[5]) for d in data[-limit:]])
        return list(op), list(high), list(low), list(close), list(vol)
    except Exception as e:
        logging.error(f"coinbase candles error {product_id} g{granularity}: {e}")
        return (None,)*5

def cb_ticker_price(product_id):
    try:
        jitter_sleep()
        r = _get_with_retry(f"{CB_BASE}/products/{product_id}/ticker", timeout=10)
        data = r.json()
        price = data.get("price") or data.get("last") or data.get("price_24h")
        return float(price) if price else float("nan")
    except Exception as e:
        logging.warning(f"ticker error {product_id}: {e}")
        return float("nan")

# ------------------------------ signal logic ---------------------------------
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

def ema_arr(arr, p): 
    if arr is None: return None
    return ema(arr, p)

def adx_arr(h,l,c, p=14):
    if c is None: return None
    return adx(h,l,c,p)

def update_btc_bias():
    if not globals().get("USE_BTC_BIAS", False):
        BTC_STATE.update({"bias": 0, "ts": time.time()}); return
    now = time.time()
    if now - BTC_STATE["ts"] < 50: return
    o5,h5,l5,c5,v5 = cb_candles("BTC-USD", GRAN_5M, 120)
    o1,h1,l1,c1,v1 = cb_candles("BTC-USD", GRAN_1H, 200)
    if not c5 or not c1: BTC_STATE.update({"bias": 0, "ts": now}); return
    ema50_1h = ema_arr(c1, 50); r5 = rsi(c5, 14)
    bias = 0
    if ema50_1h is not None and r5 is not None and not math.isnan(r5[-1]):
        if c1[-1] < ema50_1h[-1] and r5[-1] < 35: bias = -1
        if c1[-1] > ema50_1h[-1] and r5[-1] > 65: bias = +1
    BTC_STATE.update({"bias": bias, "ts": now})

def score_signal(side, price, ema50_now, atr5, vol_ok, rsi_val, adx_val):
    score = 0
    if vol_ok: score += 25
    if adx_val is not None and not math.isnan(adx_val):
        score += min(25, max(0, (adx_val - 10) * 2))
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

    # HTF only when enabled and available
    c4h = None
    if globals().get("USE_HTF_FILTER", False) and pid not in NO_4H:
        o4h,h4h,l4h,c4h,v4h = cb_candles(pid, GRAN_4H, 200)
        if not c4h: NO_4H.add(pid)

    ema50_1h  = ema_arr(c1, 50)
    ema200_1h = ema_arr(c1, 200)
    if ema50_1h is None or ema200_1h is None: return None
    ema50_now = ema50_1h[-1]
    ema200_4h = ema_arr(c4h, 200) if c4h else None

    adx1h = adx_arr(h1, l1, c1, 14) if c1 else None
    adx_ok = True
    if globals().get("USE_ADX_FILTER", True):
        adx_ok = (adx1h is not None and not math.isnan(adx1h[-1]) and adx1h[-1] >= globals().get("ADX_MIN", 10))

    BL = globals().get("BREAKOUT_LOOKBACK",3)
    hh = max(h5[-(BL+1):-1]); ll = min(l5[-(BL+1):-1])
    vol_avg = sum(v5[-(BL+1):-1]) / BL
    last_close = c5[-1]; last_vol = v5[-1]

    r = rsi(c15, 14)
    if r is None or math.isnan(r[-1]): return None
    r_ok = (globals().get("RSI_MIN",20) <= r[-1] <= globals().get("RSI_MAX",80))

    ema20_5 = ema_arr(c5, 20)
    use_ema_cross = globals().get("USE_EMA_CROSS", True)
    cross_up = ema20_5 is not None and c5[-1] > ema20_5[-1] and c5[-2] <= ema20_5[-2]
    cross_dn = ema20_5 is not None and c5[-1] < ema20_5[-1] and c5[-2] >= ema20_5[-2]
    brk_up = last_close > hh; brk_dn = last_close < ll

    long_trigger  = brk_up or (use_ema_cross and cross_up)
    short_trigger = brk_dn or (use_ema_cross and cross_dn)

    EMA_TOL_PCT = globals().get("EMA_TOL_PCT", 0.012)
    trend_up = (last_close > ema50_now) or near_trend_ok(last_close, ema50_now, EMA_TOL_PCT)
    trend_dn = (last_close < ema50_now) or near_trend_ok(last_close, ema50_now, EMA_TOL_PCT)

    # HTF filter — both directions
    htf_up = True; htf_dn = True
    if globals().get("USE_HTF_FILTER", False) and ema200_4h is not None:
        HTF_TOL_PCT = globals().get("HTF_TOL_PCT", 0.005)
        htf_up = (c1[-1] >= (1-HTF_TOL_PCT)*ema200_1h[-1]) and (c4h[-1] >= (1-HTF_TOL_PCT)*ema200_4h[-1])
        htf_dn = (c1[-1] <= (1+HTF_TOL_PCT)*ema200_1h[-1]) and (c4h[-1] <= (1+HTF_TOL_PCT)*ema200_4h[-1])

    vol_ok = last_vol >= globals().get("VOLUME_MULTIPLIER",1.05) * max(vol_avg, 1e-9)

    # BTC bias can veto
    if globals().get("USE_BTC_BIAS", False):
        if BTC_STATE["bias"] == -1 and long_trigger:  long_trigger = False
        if BTC_STATE["bias"] == +1 and short_trigger: short_trigger = False

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
        "entry": round(last_close,6),
        "tp1": round(tp1,6), "tp2": round(tp2,6),
        "sl": round(sl,6),
        "atr": round(a5,6),
        "score": score_signal(side, last_close, ema50_now, a5, vol_ok, r[-1], adx1h[-1] if adx1h is not None else None)
    }

# ----------------------------- telegram helpers ------------------------------
def target_chat_id(update: Update = None):
    global last_chat_id
    if FORCED_CHAT_ID: return int(FORCED_CHAT_ID)
    if update:
        last_chat_id = update.effective_chat.id
        return last_chat_id
    return last_chat_id

def _set_alerts(val: bool):
    global ALERTS
    ALERTS = bool(val)
    CONFIG["ALERTS"] = 1 if ALERTS else 0
    save_config()

def _alert(ctx: CallbackContext, msg: str):
    if not ALERTS: return
    chat_id = target_chat_id()
    if chat_id:
        ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def format_signal_msg(sig):
    tag = CONFIG["MODE"]
    return (f"🚀 <b>{sig['symbol']}</b> {sig['side']} (Intraday • {tag})\n"
            f"Entry: <b>{sig['entry']}</b>\nTP1: <b>{sig['tp1']}</b> | TP2: <b>{sig['tp2']}</b>\n"
            f"SL: <b>{sig['sl']}</b>\nATR(5m): {sig['atr']} | Score: <b>{sig['score']}</b>/100")

# ------------------------------ commands -------------------------------------
def cmd_start(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML, text=(
        "👋 InsiderSignals_Manual (Coinbase • PAPER RT)\n"
        f"Mode: <b>{CONFIG['MODE']}</b> | SCORE_MIN <b>{CONFIG['SCORE_MIN']}</b>\n"
        f"Max open: <b>{CONFIG['MAX_OPEN_TRADES']}</b> | Alloc: <b>{int(CONFIG['POSITION_PCT']*100)}%</b>\n"
        f"TRADE_MODE: <b>{TRADE_MODE}</b> | Alerts: <b>{'ON' if ALERTS else 'OFF'}</b>\n"
        f"Paper cash: ${PAPER['cash']:.2f}\n\n"
        "Commands:\n"
        "/paper /positions /closed /pnl /paperreset /setpaper N\n"
        "/setmax N /setalloc P /setscore S /setmode RELAXED|STRICT\n"
        "/alertson /alertsoff\n"
        "/scan – force scan"
    ))

def cmd_paper(update: Update, ctx: CallbackContext):
    _update_equity_mark()
    realized = PAPER["realized_pnl"]; fees = PAPER["fees_paid"]
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
        text=(f"💼 <b>Paper Account</b>\nCash: ${PAPER['cash']:.2f}\nEquity: ${PAPER['equity']:.2f}\n"
              f"Open: {len(PAPER['positions'])} | Closed: {len(PAPER['closed'])}\n"
              f"Realized PnL: ${realized:.2f} | Fees paid: ${fees:.2f}"))

def cmd_pnl(update: Update, ctx: CallbackContext):
    wins = PAPER["wins"]; losses = PAPER["losses"]; total = wins + losses
    wr = (wins/total*100) if total else 0
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
        text=(f"📈 <b>PnL Totals</b>\nRealized: ${PAPER['realized_pnl']:.2f}\n"
              f"Fees: ${PAPER['fees_paid']:.2f}\nWins: {wins} | Losses: {losses} | Win rate: {wr:.1f}%"))

def cmd_positions(update: Update, ctx: CallbackContext):
    if not PAPER["positions"]:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="No open paper positions."); return
    lines = ["📊 <b>Open Positions</b>"]
    for pid, p in PAPER["positions"].items():
        cur = cb_ticker_price(p["symbol"])
        if not np.isfinite(cur): cur = p["entry_avg"]
        up_span = p["tp2"] - p["entry_avg"]; dn_span = p["entry_avg"] - p["sl_dyn"]
        if up_span <= 0: prog = 0.0
        elif cur >= p["entry_avg"]: prog = min(100.0, (cur - p["entry_avg"]) / up_span * 100)
        else: prog = -min(100.0, (p["entry_avg"] - cur) / max(1e-9, dn_span) * 100)
        pnl_usd = (cur - p["entry_avg"]) * p["qty_total"] - p["fees_open_total"]
        pnl_pct = (cur - p["entry_avg"]) / p["entry_avg"] * 100 if p["entry_avg"] else 0
        lines.append(
            f"• {p['symbol']} qty {p['qty_total']:.6f} | entry {p['entry_avg']:.6f} | now {cur:.6f} | "
            f"PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%) | progress {prog:.1f}%"
        )
    ctx.bot.send_message(chat_id=target_chat_id(update), text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_closed(update: Update, ctx: CallbackContext):
    if not PAPER["closed"]:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="No closed paper trades yet."); return
    lines = ["✅ <b>Closed Trades (last 10)</b>"]
    for t in PAPER["closed"][-10:]:
        lines.append(f"• {t['symbol']} @ {t['entry_avg']:.6f} → {t['exit_avg']:.6f} | "
                     f"Qty {t['qty_total']:.6f} | PnL ${t['pnl_usd']:.2f} ({t['pnl_pct']:.2f}%) [{t['reason']}]")
    ctx.bot.send_message(chat_id=target_chat_id(update), text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_paperreset(update: Update, ctx: CallbackContext):
    PAPER.update({"cash": PAPER_START_CASH, "equity": PAPER_START_CASH,
                  "positions": {}, "closed": [], "seq": 1,
                  "realized_pnl": 0.0, "fees_paid": 0.0, "wins": 0, "losses": 0})
    save_paper()
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"🧹 Paper reset to ${PAPER_START_CASH:.2f}.")

def cmd_setpaper(update: Update, ctx: CallbackContext):
    try:
        n = float(update.message.text.split()[1]); assert n > 0
        PAPER.update({"cash": n, "equity": n, "positions": {}, "closed": [], "seq": 1,
                      "realized_pnl": 0.0, "fees_paid": 0.0, "wins": 0, "losses": 0})
        save_paper()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ Paper start cash set to ${n:.2f} (reset).")
    except Exception:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setpaper <amount>")

def cmd_setmax(update: Update, ctx: CallbackContext):
    try:
        n = int(update.message.text.split()[1]); assert 1 <= n <= 20
        CONFIG["MAX_OPEN_TRADES"] = n; save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ MAX_OPEN_TRADES set to {n}.")
    except Exception:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setmax 1..20")

def cmd_setalloc(update: Update, ctx: CallbackContext):
    try:
        p = float(update.message.text.split()[1]); assert 1 <= p <= 100
        CONFIG["POSITION_PCT"] = p/100.0; save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ POSITION_PCT set to {p}%.")
    except Exception:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setalloc 1..100")

def cmd_setscore(update: Update, ctx: CallbackContext):
    try:
        s = int(update.message.text.split()[1]); assert 1 <= s <= 100
        CONFIG["SCORE_MIN"] = s; save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ SCORE_MIN set to {s}.")
    except Exception:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setscore 1..100")

def cmd_setmode(update: Update, ctx: CallbackContext):
    try:
        m = update.message.text.split()[1].upper(); assert m in ("RELAXED","STRICT")
        apply_mode(m); save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ MODE set to {m}.")
    except Exception:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setmode RELAXED|STRICT")

def cmd_alertson(update: Update, ctx: CallbackContext):
    _set_alerts(True)
    ctx.bot.send_message(chat_id=target_chat_id(update), text="🔔 Alerts ON.")

def cmd_alertsoff(update: Update, ctx: CallbackContext):
    _set_alerts(False)
    ctx.bot.send_message(chat_id=target_chat_id(update), text="🔕 Alerts OFF.")

def cmd_scan(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"🔍 Scan running (mode={CONFIG['MODE']}, score≥{CONFIG['SCORE_MIN']})…")
    _scan_chunk(ctx, push_to=target_chat_id(update))

# ------------------------------- enter trades ---------------------------------
def _can_open_more_trades():
    return len(PAPER["positions"]) < CONFIG["MAX_OPEN_TRADES"]

def _free_usd_balance():
    return float(PAPER["cash"])

def _execute_long_trade(sig, ctx: CallbackContext, chat_id):
    if not _can_open_more_trades():
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: reached MAX_OPEN_TRADES"); return
    free_usd = _free_usd_balance()
    alloc = max(MIN_USD_PER_TRADE, free_usd * float(CONFIG["POSITION_PCT"]))
    if free_usd < MIN_USD_PER_TRADE:
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: insufficient USD balance"); return

    entry = sig["entry"]
    qty = (alloc * (1 - FEE_RATE)) / entry
    fee_open = alloc * FEE_RATE
    PAPER["cash"] -= alloc
    pos_id = f"paper-{PAPER['seq']}"; PAPER["seq"] += 1
    pos = {
        "id": pos_id, "symbol": sig["symbol"], "side": "LONG",
        "qty_total": qty,
        "qty_tp1": qty * 0.5,
        "qty_runner": qty * 0.5,
        "entry_avg": entry,
        "tp1": sig["tp1"], "tp2": sig["tp2"],
        "sl": sig["sl"], "sl_dyn": sig["sl"],
        "tp1_done": False, "time": datetime.now(tz).isoformat(),
        "fees_open_total": fee_open, "fees_close_total": 0.0
    }
    PAPER["positions"][pos_id] = pos
    save_paper()
    ctx.bot.send_message(chat_id=chat_id, text=f"🟢 PAPER BUY {sig['symbol']} ${alloc:.2f} | entry {entry} | qty {qty:.6f}")

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

    start = scan_cursor; end = min(start + SYMBOLS_PER_SCAN, len(syms))
    chunk = syms[start:end]; scan_cursor = 0 if end >= len(syms) else end

    hits = 0
    for pid in chunk:
        lt = last_signal_time.get(pid)
        if lt and (now - lt) < timedelta(minutes=SIGNAL_COOLDOWN_MIN): continue
        sig = intraday_signal(pid)
        if not sig or sig.get("score", 0) < CONFIG["SCORE_MIN"]:
            time.sleep(0.05); continue
        last_signal_time[pid] = now
        signals.append({"time": now, **sig})
        ctx.bot.send_message(chat_id=chat_id, text=format_signal_msg(sig), parse_mode=ParseMode.HTML)
        # Paper: only auto-open on LONG (keep behavior), but you can add SHORT execution if you want to simulate shorts
        if sig["side"] == "LONG":
            _execute_long_trade(sig, ctx, chat_id)
        hits += 1; time.sleep(0.1)
    if hits == 0 and push_to:
        ctx.bot.send_message(chat_id=push_to, text=f"(No valid setups ≥ {CONFIG['SCORE_MIN']} this minute — still watching.)")

# ----------------------------- PAPER monitor (real-time + alerts) ------------
def _paper_check_exits(ctx: CallbackContext):
    if not PAPER["positions"]: return
    to_remove = []
    for pid, p in list(PAPER["positions"].items()):
        cur = cb_ticker_price(p["symbol"])
        if not np.isfinite(cur): continue

        # ----- GAP-SAFE: TP2 dominates everything -----
        if cur >= p["tp2"]:
            exit_price = p["tp2"]
            proceeds = exit_price * p["qty_total"]
            fee_close = proceeds * FEE_RATE
            PAPER["cash"] += (proceeds - fee_close)
            PAPER["fees_paid"] += fee_close
            total_fees = p["fees_open_total"] + fee_close
            gross = (exit_price - p["entry_avg"]) * p["qty_total"]
            pnl_usd = gross - total_fees
            pnl_pct = (exit_price - p["entry_avg"]) / p["entry_avg"] * 100 if p["entry_avg"] else 0
            PAPER["realized_pnl"] += pnl_usd
            if pnl_usd >= 0: PAPER["wins"] += 1
            else: PAPER["losses"] += 1
            PAPER["closed"].append({
                "id": pid, "symbol": p["symbol"], "entry_avg": p["entry_avg"],
                "exit_avg": exit_price, "qty_total": p["qty_total"],
                "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
                "reason": "TP2", "closed_at": datetime.now(tz).isoformat()
            })
            to_remove.append(pid)
            _alert(ctx, f"🎯 <b>TP2 hit</b> {p['symbol']} @ {exit_price:.6f} | PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%)")
            continue

        # ----- TP1 partial (only once), then trail SL to BE -----
        if (not p["tp1_done"]) and cur >= p["tp1"]:
            exit_value = p["tp1"] * p["qty_tp1"]
            fee_close = exit_value * FEE_RATE
            PAPER["cash"] += (exit_value - fee_close)
            PAPER["fees_paid"] += fee_close
            p["fees_close_total"] += fee_close
            p["tp1_done"] = True
            if TRAIL_AFTER_TP1: p["sl_dyn"] = max(p["sl_dyn"], p["entry_avg"])
            _alert(ctx, f"🥳 <b>TP1 partial</b> {p['symbol']} sold {p['qty_tp1']:.6f} @ {p['tp1']:.6f}")

        # ----- Final exits on runner via SL (TP2 case handled above) -----
        exit_reason = None; exit_price = None
        if cur <= p["sl_dyn"]:
            exit_reason, exit_price = "SL", p["sl_dyn"]

        if exit_price is not None:
            runner_val = exit_price * p["qty_runner"]
            fee_close = runner_val * FEE_RATE
            PAPER["cash"] += (runner_val - fee_close)
            PAPER["fees_paid"] += fee_close
            total_fees = p["fees_open_total"] + p["fees_close_total"] + fee_close
            tp1_part = (p["tp1"] * p["qty_tp1"]) if p["tp1_done"] else 0.0
            avg_exit = (tp1_part + exit_price * p["qty_runner"]) / p["qty_total"]
            gross = (avg_exit - p["entry_avg"]) * p["qty_total"]
            pnl_usd = gross - total_fees
            pnl_pct = (avg_exit - p["entry_avg"]) / p["entry_avg"] * 100 if p["entry_avg"] else 0
            PAPER["realized_pnl"] += pnl_usd
            if pnl_usd >= 0: PAPER["wins"] += 1
            else: PAPER["losses"] += 1
            PAPER["closed"].append({
                "id": pid, "symbol": p["symbol"], "entry_avg": p["entry_avg"],
                "exit_avg": avg_exit, "qty_total": p["qty_total"],
                "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
                "reason": exit_reason, "closed_at": datetime.now(tz).isoformat()
            })
            to_remove.append(pid)
            _alert(ctx, f"🛑 <b>{exit_reason}</b> {p['symbol']} avg exit {avg_exit:.6f} | PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%)")

    for pid in to_remove:
        PAPER["positions"].pop(pid, None)
    if to_remove: save_paper()

def _update_equity_mark():
    eq = PAPER["cash"]
    for p in PAPER["positions"].values():
        cur = cb_ticker_price(p["symbol"])
        if np.isfinite(cur):
            remaining = p["qty_runner"] + (0 if p["tp1_done"] else p["qty_tp1"])
            eq += cur * remaining * (1 - FEE_RATE)
    PAPER["equity"] = eq
    save_paper()

def job_paper_watch(ctx: CallbackContext):
    _paper_check_exits(ctx)
    _update_equity_mark()

# ------------------------------- daily/keepalive ------------------------------
def job_daily(ctx: CallbackContext):
    chat_id = target_chat_id()
    if not chat_id: return
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff and s.get("score", 0) >= CONFIG["SCORE_MIN"]]
    msg = ("📊 Daily Report (24h): No signals." if not recent else
           "📊 <b>Daily Report</b>\n" + "\n".join(
               f"• {s['time'].strftime('%H:%M')} {s['symbol']} {s['side']} @ {s['entry']} | TP1 {s['tp1']} | TP2 {s['tp2']} | SL {s['sl']} | Score {s.get('score','-')}"
               for s in recent))
    ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def job_keepalive(ctx: CallbackContext):
    if not KEEPALIVE_URL: return
    try: session.get(KEEPALIVE_URL, timeout=10)
    except Exception as e: logging.warning(f"Keep-alive ping failed: {e}")

# -------------------------------- main ---------------------------------------
def main():
    if not TOKEN: raise RuntimeError("Missing TELEGRAM_MANUAL_TOKEN")
    updater = Updater(TOKEN, use_context=True)
    updater.bot.delete_webhook(drop_pending_updates=True)

    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("paper", cmd_paper))
    dp.add_handler(CommandHandler("pnl", cmd_pnl))
    dp.add_handler(CommandHandler("positions", cmd_positions))
    dp.add_handler(CommandHandler("closed", cmd_closed))
    dp.add_handler(CommandHandler("paperreset", cmd_paperreset))
    dp.add_handler(CommandHandler("setpaper", cmd_setpaper))
    dp.add_handler(CommandHandler("setmax", cmd_setmax))
    dp.add_handler(CommandHandler("setalloc", cmd_setalloc))
    dp.add_handler(CommandHandler("setscore", cmd_setscore))
    dp.add_handler(CommandHandler("setmode", cmd_setmode))
    dp.add_handler(CommandHandler("alertson", cmd_alertson))
    dp.add_handler(CommandHandler("alertsoff", cmd_alertsoff))
    dp.add_handler(CommandHandler("scan", cmd_scan))

    jq: JobQueue = updater.job_queue
    jq.run_repeating(lambda c: _scan_chunk(c),
                     interval=SCAN_INTERVAL_SECONDS,
                     first=random.randint(6, 12),
                     name="job_scan",
                     max_instances=2)  # allow one overlap to reduce skipped runs
    jq.run_repeating(job_paper_watch, interval=PAPER_CHECK_SECONDS, first=8)
    jq.run_daily(job_daily, time=dtime(hour=DAILY_REPORT_HOUR, minute=0, tzinfo=tz))
    if KEEPALIVE_URL: jq.run_repeating(job_keepalive, interval=KEEPALIVE_SECONDS, first=5)

    logging.info(f"InsiderSignals_Manual started. TRADE_MODE={TRADE_MODE} PAPER_CASH=${PAPER['cash']:.2f} ALERTS={ALERTS}")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
