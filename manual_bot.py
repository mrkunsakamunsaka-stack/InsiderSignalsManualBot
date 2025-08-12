# manual_bot.py — Coinbase • PAPER trading • €300 Sniper-Safe
# v3.6 (2025-08-12)
# - LONG & SHORT support (paper), side-aware exits, alerts, and stats
# - SL glitch guard: two-tick confirm, micro band, post-TP hold, sanity clamps
# - 3-TP ladder (configurable split) with ratcheting SL (BE → TP1 → TP2)
# - STRICT/RELAXED, SCORE≥97, prioritized scan, adaptive pacing, watchdog
# - EDGE/CORR/REGIME/COOLDOWN/SESSION filters, candle cache, 1h→4h fallback
# - Day loss guard, trade journaling, compact commands

import os, time, math, random, logging, threading, json, traceback, tempfile
from datetime import datetime, timedelta, time as dtime, date
import pytz, requests, numpy as np
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue
from requests.exceptions import RequestException
from http.client import RemoteDisconnected

# ---------- ENV ----------
TOKEN = os.getenv("TELEGRAM_MANUAL_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_MANUAL_TOKEN in environment variables")

TIMEZONE           = os.getenv("TIMEZONE", "Europe/Dublin")
DAILY_REPORT_HOUR  = int(os.getenv("DAILY_REPORT_HOUR", "20"))
FORCED_CHAT_ID     = os.getenv("CHAT_ID")
tz = pytz.timezone(TIMEZONE)

# cadence / universe
SCAN_INTERVAL_SECONDS   = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
SYMBOLS_PER_SCAN        = int(os.getenv("SYMBOLS_PER_SCAN", "20"))
SIGNAL_COOLDOWN_MIN     = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "8"))
MAX_PAIRS               = int(os.getenv("MAX_PAIRS", "60"))

# mode & score
SCORE_MIN               = int(os.getenv("SCORE_MIN", "97"))
MODE                    = os.getenv("MODE", "STRICT").strip().upper()

RELAXED = dict(BREAKOUT_LOOKBACK=3, VOLUME_MULTIPLIER=1.05, RSI_MIN=20, RSI_MAX=80,
               USE_EMA_CROSS=True,  EMA_TOL_PCT=0.012, USE_ADX_FILTER=True, ADX_MIN=10,
               USE_HTF_FILTER=False, HTF_TOL_PCT=0.005)
STRICT  = dict(BREAKOUT_LOOKBACK=6, VOLUME_MULTIPLIER=1.20, RSI_MIN=35, RSI_MAX=65,
               USE_EMA_CROSS=True,  EMA_TOL_PCT=0.008, USE_ADX_FILTER=True, ADX_MIN=18,
               USE_HTF_FILTER=True,  HTF_TOL_PCT=0.005)

# exits (R = ATR 5m) and 3-TP ladder
R_TP1, R_TP2, R_TP3     = float(os.getenv("R_TP1", "1.0")), float(os.getenv("R_TP2","2.0")), float(os.getenv("R_TP3","3.0"))
R_SL                    = float(os.getenv("R_SL","1.1"))

# TP split config: e.g. "50,30,20" (percent); TP levels: 2 or 3
TP_SPLIT                = os.getenv("TP_SPLIT", "30,30,40").replace(" ", "")
TP_LEVELS               = int(os.getenv("TP_LEVELS", "3"))

# Paper fees
FEE_RATE                = float(os.getenv("FEE_RATE", "0.001"))  # 0.10%

# SHORT enable (paper)
ENABLE_SHORTS           = os.getenv("ENABLE_SHORTS", "0") == "1"

# SL glitch-prevention knobs (Update 1 core)
SL_BAND_PCT             = float(os.getenv("SL_BAND_PCT", "0.0002"))   # 0.02% micro-band
POST_TP_HOLD_MS         = int(os.getenv("POST_TP_HOLD_MS", "1500"))   # block SL right after TP fill
MAX_STOP_PCT            = float(os.getenv("MAX_STOP_PCT", "0.20"))    # hard stop <= 20% from entry
MAX_GAP_PCT             = float(os.getenv("MAX_GAP_PCT", "0.08"))     # 8% gap detector (log only)

# paper sizing
POSITION_PCT            = float(os.getenv("POSITION_PCT", "0.05"))
MAX_OPEN_TRADES         = int(os.getenv("MAX_OPEN_TRADES", "10"))
MIN_USD_PER_TRADE       = float(os.getenv("MIN_USD_PER_TRADE", "10"))
PAPER_START_CASH        = float(os.getenv("PAPER_START_CASH", "300"))
PAPER_CHECK_SECONDS     = int(os.getenv("PAPER_CHECK_SECONDS", "5"))

# feature flags (1/0)
FLAGS = {
    "EDGE": int(os.getenv("EDGE", "1")),
    "CORR": int(os.getenv("CORR", "1")),
    "REGIME": int(os.getenv("REGIME", "1")),
    "COOLDOWN": int(os.getenv("COOLDOWN", "1")),
    "SESSION": int(os.getenv("SESSION", "1")),
}

# historical validator thresholds
HIST_LOOKBACK_BARS_5M   = int(os.getenv("HIST_LOOKBACK_BARS_5M", "900"))
HIST_MIN_OCCURRENCES    = int(os.getenv("HIST_MIN_OCCURRENCES", "10"))
HIST_MIN_SUCCESS        = float(os.getenv("HIST_MIN_SUCCESS", "0.55"))

# correlation thresholds
CORR_BLOCK              = float(os.getenv("CORR_BLOCK", "0.8"))
CORR_HALF               = float(os.getenv("CORR_HALF", "0.6"))

# streak/day guard
COOLDOWN_LOSSES         = int(os.getenv("COOLDOWN_LOSSES", "3"))
COOLDOWN_WINDOW_MIN     = int(os.getenv("COOLDOWN_WINDOW_MIN", "120"))
COOLDOWN_PAUSE_MIN      = int(os.getenv("COOLDOWN_PAUSE_MIN", "60"))
DAY_MAX_DD_PCT          = float(os.getenv("DAY_MAX_DD_PCT", "3.0"))
DAY_LOCK_MIN            = int(os.getenv("DAY_LOCK_MIN", "180"))

# retries / jitter
RETRY_MAX               = int(os.getenv("RETRY_MAX", "4"))
RETRY_BASE_DELAY        = float(os.getenv("RETRY_BASE_DELAY", "0.6"))
REQ_JITTER_MIN_MS       = int(os.getenv("REQ_JITTER_MIN_MS", "50"))
REQ_JITTER_MAX_MS       = int(os.getenv("REQ_JITTER_MAX_MS", "140"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- Coinbase API ----------
CB_BASE = "https://api.exchange.coinbase.com"
GRAN_5M, GRAN_15M, GRAN_1H, GRAN_4H = 300, 900, 3600, 14400
session = requests.Session()
session.headers.update({"User-Agent": "insider-sniper/3.6"})

# ---------- state & persistence ----------
CONFIG_PATH = "config.json"
PAPER_PATH  = "paper.json"
JOURNAL_PATH= "journal.jsonl"

CONFIG = {
    "MODE": MODE, "SCORE_MIN": SCORE_MIN,
    "POSITION_PCT": POSITION_PCT, "MAX_OPEN_TRADES": MAX_OPEN_TRADES,
    **FLAGS
}
signals = []
last_signal_time = {}  # key f"{pid}:{side}"
scan_cursor = 0
watchlist = []
NO_4H_NATIVE = set()
last_chat_id = None

BTC_STATE = {"adx1h": 0.0}
ETH_STATE = {"adx1h": 0.0}
STREAK = {"loss_times": [], "cooldown_until": 0}

PAPER = {"cash": PAPER_START_CASH, "equity": PAPER_START_CASH,
         "positions": {}, "closed": [], "seq": 1,
         "realized_pnl": 0.0, "fees_paid": 0.0, "wins": 0, "losses": 0}

START_OF_DAY = date.today()
EQUITY_OPEN  = PAPER_START_CASH
DAY_LOCK_UNTIL = 0

LAST_SCAN_TS = time.time()
SCAN_LOCK = threading.Lock()
CHUNK_SIZE = SYMBOLS_PER_SCAN

# ---------- atomic JSON helpers ----------
def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path,"r") as f: return json.load(f)
    except Exception as e:
        logging.warning(f"load {path} error: {e}")
    return default

def _save_json_atomic(path, data):
    try:
        d = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        dirn = os.path.dirname(path) or "."
        with tempfile.NamedTemporaryFile("w", dir=dirn, delete=False) as tf:
            tmp = tf.name
            tf.write(d)
        os.replace(tmp, path)
    except Exception as e:
        logging.warning(f"atomic save {path} error: {e}")

def save_config(): _save_json_atomic(CONFIG_PATH, CONFIG)
def load_config():
    global CONFIG
    d = _load_json(CONFIG_PATH, None)
    if d: CONFIG.update(d)

def save_paper(): _save_json_atomic(PAPER_PATH, PAPER)
def load_paper():
    global PAPER, EQUITY_OPEN, START_OF_DAY
    d = _load_json(PAPER_PATH, None)
    if d: PAPER.update(d)
    START_OF_DAY = date.today()
    EQUITY_OPEN  = PAPER["equity"]

def _append_journal(obj):
    try:
        with open(JOURNAL_PATH, "a") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception:
        pass

load_config(); load_paper()

def apply_mode(m):
    params = STRICT if (m or "STRICT").upper()=="STRICT" else RELAXED
    globals().update(params); CONFIG["MODE"]=(m or "STRICT").upper()
apply_mode(CONFIG["MODE"])

# ---------- indicators ----------
def ema(arr,p):
    arr=np.array(arr,float) if arr is not None else None
    if arr is None or len(arr)<p: return None
    a=2/(p+1); out=[arr[0]]
    for x in arr[1:]: out.append(a*x+(1-a)*out[-1])
    return np.array(out)

def rsi(arr,p=14):
    arr=np.array(arr,float)
    if len(arr)<=p: return None
    d=np.diff(arr); g=np.where(d>0,d,0.0); l=np.where(d<0,-d,0.0)
    ag=[np.mean(g[:p])]; al=[np.mean(l[:p])]
    for i in range(p,len(d)):
        ag.append((ag[-1]*(p-1)+g[i])/p)
        al.append((al[-1]*(p-1)+l[i])/p)
    rs=np.where(np.array(al)==0,0,np.array(ag)/np.array(al))
    r=100-(100/(1+rs)); pad=len(arr)-len(r)-1
    return np.concatenate([np.full(pad,np.nan),r])

def atr(h,l,c,p=14):
    h=np.array(h,float); l=np.array(l,float); c=np.array(c,float)
    if len(c)<=p: return None
    pc=np.concatenate([[c[0]],c[:-1]])
    tr=np.maximum(h-l, np.maximum(abs(h-pc), abs(l-pc)))
    a=[np.mean(tr[1:p+1])]
    for i in range(p+1,len(tr)):
        a.append((a[-1]*(p-1)+tr[i])/p)
    pad=len(tr)-len(a)
    return np.concatenate([np.full(pad,np.nan),np.array(a)])

def adx(h,l,c,p=14):
    h=np.array(h,float); l=np.array(l,float); c=np.array(c,float)
    if len(c)<=p+1: return None
    up=h[1:]-h[:-1]; dn=l[:-1]-l[1:]
    plus=np.where((up>dn)&(up>0),up,0.0); minus=np.where((dn>up)&(dn>0),dn,0.0)
    tr=np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))
    a=2/(p+1)
    def _ema(x):
        o=[x[0]]
        for v in x[1:]: o.append(a*v+(1-a)*o[-1])
        return np.array(o)
    atr14=_ema(tr); pdi=100*_ema(plus)/np.maximum(atr14,1e-9); mdi=100*_ema(minus)/np.maximum(atr14,1e-9)
    dx=100*np.abs(pdi-mdi)/np.maximum(pdi+mdi,1e-9); adxv=_ema(dx)
    pad=len(c)-len(adxv)-1
    return np.concatenate([np.full(pad,np.nan),adxv])

# ---------- HTTP helpers ----------
def _jitter(): time.sleep(random.randint(REQ_JITTER_MIN_MS, REQ_JITTER_MAX_MS)/1000.0)
def _get(url, params=None, timeout=15):
    delay=RETRY_BASE_DELAY
    for k in range(RETRY_MAX):
        try:
            r=session.get(url, params=params, timeout=timeout)
            if r.status_code in (429,502,503,504): raise RequestException(f"HTTP {r.status_code}")
            if r.status_code==400: r.raise_for_status()
            r.raise_for_status(); return r
        except (RequestException, RemoteDisconnected):
            if k==RETRY_MAX-1: raise
            time.sleep(delay*(1.6**k)+random.uniform(0.05,0.25))

# ---------- small candle cache ----------
CANDLE_CACHE = {}  # (pid,gran) -> (expires,(o,h,l,c,v))
CACHE_TTL = 3.0
def _cache_get(pid, gran):
    v=CANDLE_CACHE.get((pid,gran))
    return v[1] if v and v[0]>time.time() else None
def _cache_put(pid, gran, data):
    CANDLE_CACHE[(pid,gran)] = (time.time()+CACHE_TTL, data)

# ---------- market data ----------
def get_pairs_top(limit=60):
    try:
        r=_get(f"{CB_BASE}/products", timeout=25); products=r.json()
        rows=[]
        for p in products:
            if p.get("status")!="online" or p.get("trading_disabled"): continue
            if (p.get("quote_currency") or "").upper() not in ("USD","USDT"): continue
            vol=float(p.get("volume_24h") or p.get("volume") or 0.0)
            rows.append((vol, p["id"]))
        rows.sort(reverse=True)
        out=[pid for _,pid in rows[:max(1,limit)]]
        if not out: out=["BTC-USD","ETH-USD","SOL-USD","XRP-USD","ADA-USD"]
        logging.info(f"Universe loaded: {len(out)} pairs.")
        return out
    except Exception as e:
        logging.error(f"get_pairs_top error: {e}")
        return ["BTC-USD","ETH-USD","SOL-USD","XRP-USD","ADA-USD"]

def cb_candles(pid, gran, limit=300):
    if gran==GRAN_4H and pid in NO_4H_NATIVE:
        return (None,)*5
    cached=_cache_get(pid,gran)
    if cached: return cached
    try:
        _jitter()
        r=_get(f"{CB_BASE}/products/{pid}/candles", params={"granularity": gran}, timeout=20)
        data=r.json()
        if not isinstance(data,list) or not data: 
            return (None,)*5
        data.sort(key=lambda x:x[0])
        clean=[]; last_t=0
        for d in data[-limit:]:
            if d[0]>last_t:
                clean.append(d); last_t=d[0]
        t, low, high, op, close, vol = zip(*[(d[0],d[1],d[2],d[3],d[4],d[5]) for d in clean])
        out=(list(op), list(high), list(low), list(close), list(vol))
        _cache_put(pid,gran,out)
        return out
    except Exception as e:
        if gran==GRAN_4H:
            NO_4H_NATIVE.add(pid)
        logging.error(f"candles error {pid} g{gran}: {e}")
        return (None,)*5

def resample_1h_to_4h(o1,h1,l1,c1,v1):
    if not c1 or len(c1)<8: return (None,)*5
    n=len(c1)//4
    O=[];H=[];L=[];C=[];V=[]
    for i in range(n):
        s=i*4; e=s+4
        O.append(o1[s]); C.append(c1[e-1])
        H.append(max(h1[s:e])); L.append(min(l1[s:e]))
        V.append(sum(v1[s:e]))
    return O,H,L,C,V

def ticker(pid):
    try:
        _jitter()
        r=_get(f"{CB_BASE}/products/{pid}/ticker", timeout=10)
        j=r.json(); p=j.get("price") or j.get("last") or j.get("price_24h")
        return float(p) if p else float("nan")
    except Exception:
        return float("nan")

# ---------- helpers ----------
def r_targets(entry, atr5, side):
    if side=="LONG":
        sl  = entry - R_SL*atr5
        tp1 = entry + R_TP1*atr5
        tp2 = entry + R_TP2*atr5
        tp3 = entry + R_TP3*atr5
    else:  # SHORT
        sl  = entry + R_SL*atr5
        tp1 = entry - R_TP1*atr5
        tp2 = entry - R_TP2*atr5
        tp3 = entry - R_TP3*atr5
    return sl,tp1,tp2,tp3

def near(price, ema_val, tol_pct):
    if ema_val is None or ema_val==0: return False
    return abs(price-ema_val)/ema_val <= tol_pct

def illiquid(h5,l5,c5):
    if not c5 or len(c5)<60: return True
    rng=(max(h5[-24:])-min(l5[-24:]))/max(1e-9,c5[-1])
    return rng<0.0005

# ---------- regime / session ----------
def _update_regime():
    if not FLAGS.get("REGIME",1): 
        BTC_STATE["adx1h"]=ETH_STATE["adx1h"]=25.0
        return
    o1,h1,l1,c1,v1 = cb_candles("BTC-USD", GRAN_1H, 220)
    if c1:
        a=adx(h1,l1,c1,14)
        if a is not None and not math.isnan(a[-1]): BTC_STATE["adx1h"]=float(a[-1])
    o1e,h1e,l1e,c1e,v1e = cb_candles("ETH-USD", GRAN_1H, 220)
    if c1e:
        a=adx(h1e,l1e,c1e,14)
        if a is not None and not math.isnan(a[-1]): ETH_STATE["adx1h"]=float(a[-1])

def _session_quiet_now():
    if not FLAGS.get("SESSION",1): return False
    qf = int(os.getenv("SESSION_QUIET_FROM","2"))
    qt = int(os.getenv("SESSION_QUIET_TO","5"))
    h = datetime.now(tz).hour
    return (qf <= h <= qt) if qf <= qt else not (qt < h < qf)

# ---------- historical quick validator ----------
def hist_profitable(pid, o5,h5,l5,c5,v5):
    if not FLAGS.get("EDGE",1): return True
    try:
        if not c5 or len(c5)<200:
            return True
        BL=BREAKOUT_LOOKBACK
        wins=0; total=0
        ema20=ema(c5,20)
        for i in range(BL+30, min(len(c5)-1, HIST_LOOKBACK_BARS_5M), 6):
            hh=max(h5[i-BL:i]); ll=min(l5[i-BL:i]); last=c5[i]
            cross_up=(ema20 is not None and c5[i]>ema20[i] and c5[i-1]<=ema20[i-1])
            cross_dn=(ema20 is not None and c5[i]<ema20[i] and c5[i-1]>=ema20[i-1])
            brk_up=(last>hh); brk_dn=(last<ll)
            trig = brk_up or brk_dn or (USE_EMA_CROSS and (cross_up or cross_dn))
            if not trig: continue
            atr5=atr(h5[:i+1],l5[:i+1],c5[:i+1],14)
            if atr5 is None or math.isnan(atr5[-1]): continue
            _sl,_tp1,_tp2,_tp3=r_targets(last,float(atr5[-1]),"LONG")
            hit=None
            for j in range(i+1, min(i+13, len(c5))):
                if c5[j]>=_tp2: hit="TP2"; break
                if c5[j]<=_sl: hit="SL"; break
            if hit:
                total+=1
                if hit=="TP2": wins+=1
        if total < HIST_MIN_OCCURRENCES: return True
        return (wins/total) >= HIST_MIN_SUCCESS
    except Exception as e:
        logging.warning(f"hist validator error {pid}: {e}")
        return True

# ---------- correlation control ----------
def corr_multiplier(pid, open_symbols):
    if not FLAGS.get("CORR",1) or not open_symbols: return 1.0
    try:
        o1,h1,l1,c1,v1=cb_candles(pid,GRAN_1H,120)
        if not c1: return 1.0
        r1=np.diff(np.array(c1[-60:],float))/np.array(c1[-60:-1],float)
        max_rho = 0.0
        for sym in list(open_symbols)[:5]:
            o,h,l,c,v=cb_candles(sym,GRAN_1H,120)
            if not c: continue
            r2=np.diff(np.array(c[-60:],float))/np.array(c[-60:-1],float)
            if len(r1)!=len(r2): continue
            rho = float(np.corrcoef(r1,r2)[0,1])
            max_rho = max(max_rho, abs(rho))
        if max_rho >= CORR_BLOCK: return 0.0
        if max_rho >= CORR_HALF:  return 0.5
        return 1.0
    except Exception:
        return 1.0

# ---------- streak/day guard ----------
def cooldown_active():
    if time.time() < DAY_LOCK_UNTIL: return True
    if not FLAGS.get("COOLDOWN",1): return False
    return time.time() < STREAK.get("cooldown_until", 0)

def add_result_winloss(pnl_usd):
    global DAY_LOCK_UNTIL
    if pnl_usd >= 0: PAPER["wins"] += 1
    else:
        PAPER["losses"] += 1
        now = time.time()
        STREAK["loss_times"] = [t for t in STREAK["loss_times"] if now - t < COOLDOWN_WINDOW_MIN*60]
        STREAK["loss_times"].append(now)
        if len(STREAK["loss_times"]) >= COOLDOWN_LOSSES:
            STREAK["cooldown_until"] = now + COOLDOWN_PAUSE_MIN*60
    dd_pct = max(0.0, (EQUITY_OPEN - PAPER["equity"]) / max(1e-9, EQUITY_OPEN) * 100)
    if dd_pct >= DAY_MAX_DD_PCT:
        DAY_LOCK_UNTIL = time.time() + DAY_LOCK_MIN*60

# ---------- scoring & signals ----------
def score_signal(price, ema50_now, a5, vol_ok, rsi_val, adx_val):
    s=0
    if vol_ok: s+=25
    if adx_val is not None and not math.isnan(adx_val): s+=min(25,max(0,(adx_val-10)*2))
    if ema50_now:
        dist=abs(price-ema50_now)/ema50_now
        s+=max(0,25-100*dist)
    if rsi_val is not None and not math.isnan(rsi_val): s+=max(0,25-abs(50-rsi_val))
    if FLAGS.get("REGIME",1) and BTC_STATE["adx1h"] < 18 and ETH_STATE["adx1h"] < 18: s -= 2
    if FLAGS.get("SESSION",1) and _session_quiet_now(): s -= 2
    return int(min(100,max(0,s)))

def _build_signal_side(pid, side):
    o5,h5,l5,c5,v5 = cb_candles(pid,GRAN_5M,240)
    if not c5 or illiquid(h5,l5,c5): return None
    if not hist_profitable(pid,o5,h5,l5,c5,v5): return None

    o15,h15,l15,c15,v15 = cb_candles(pid,GRAN_15M,200)
    o1,h1,l1,c1,v1      = cb_candles(pid,GRAN_1H,220)

    c4h=None
    if USE_HTF_FILTER:
        o4h,h4h,l4h,c4h,v4h = cb_candles(pid,GRAN_4H,220)
        if not c4h and c1:
            _o,_h,_l,_c,_v = resample_1h_to_4h(o1,h1,l1,c1,v1)
            c4h=_c

    ema50_1h=ema(c1,50) if c1 else None
    ema200_1h=ema(c1,200) if c1 else None
    if ema50_1h is None or ema200_1h is None: return None
    ema50_now=float(ema50_1h[-1])
    ema200_4h=ema(c4h,200) if c4h else None

    adx1h=adx(h1,l1,c1,14) if c1 else None
    adx_ok = True
    if USE_ADX_FILTER:
        adx_ok = (adx1h is not None and not math.isnan(adx1h[-1]) and adx1h[-1]>=ADX_MIN)

    BL=BREAKOUT_LOOKBACK
    last=c5[-1]
    vol_avg=sum(v5[-(BL+1):-1])/BL; last_vol=v5[-1]
    r=rsi(c15,14)
    if r is None or math.isnan(r[-1]): return None
    r_ok = (RSI_MIN <= r[-1] <= RSI_MAX)

    ema20_5=ema(c5,20)
    cross_up= ema20_5 is not None and c5[-1]>ema20_5[-1] and c5[-2]<=ema20_5[-2]
    cross_dn= ema20_5 is not None and c5[-1]<ema20_5[-1] and c5[-2]>=ema20_5[-2]
    brk_up  = last>max(h5[-(BL+1):-1])
    brk_dn  = last<min(l5[-(BL+1):-1])

    if side=="LONG":
        trig = brk_up or (USE_EMA_CROSS and cross_up)
        trend = (last>ema50_now) or near(last, ema50_now, EMA_TOL_PCT)
    else:
        trig = brk_dn or (USE_EMA_CROSS and cross_dn)
        trend = (last<ema50_now) or near(last, ema50_now, EMA_TOL_PCT)

    htf_ok=True
    if USE_HTF_FILTER and ema200_4h is not None:
        if side=="LONG":
            htf_ok = (c1[-1] >= (1-HTF_TOL_PCT)*ema200_1h[-1]) and (c4h[-1] >= (1-HTF_TOL_PCT)*ema200_4h[-1])
        else:
            htf_ok = (c1[-1] <= (1+HTF_TOL_PCT)*ema200_1h[-1]) and (c4h[-1] <= (1+HTF_TOL_PCT)*ema200_4h[-1])

    vol_ok = last_vol >= VOLUME_MULTIPLIER*max(vol_avg,1e-9)

    atr5=atr(h5,l5,c5,14)
    if atr5 is None or math.isnan(atr5[-1]): return None
    a5=float(atr5[-1])

    ok = trig and vol_ok and r_ok and trend and htf_ok and adx_ok
    if not ok: return None

    _update_regime()
    sc = score_signal(last, ema50_now, a5, vol_ok, r[-1], adx1h[-1] if adx1h is not None else None)
    if sc < CONFIG["SCORE_MIN"]: return None

    sl,tp1,tp2,tp3 = r_targets(last,a5,side)

    # sanity clamp SL distance
    if side=="LONG":
        sl = max(sl, last*(1-MAX_STOP_PCT))
    else:
        sl = min(sl, last*(1+MAX_STOP_PCT))

    out = {"symbol": pid, "side": side, "entry": round(last,6),
           "tp1": round(tp1,6), "tp2": round(tp2,6), "tp3": round(tp3,6),
           "sl": round(sl,6), "atr": round(a5,6), "score": sc}
    return out

def build_signals(pid):
    sigs=[]
    long_sig = _build_signal_side(pid,"LONG")
    if long_sig: sigs.append(long_sig)
    if ENABLE_SHORTS:
        short_sig = _build_signal_side(pid,"SHORT")
        if short_sig: sigs.append(short_sig)
    return sigs

# ---------- Telegram helpers ----------
def target_chat_id(update: Update=None):
    global last_chat_id
    if FORCED_CHAT_ID: return int(FORCED_CHAT_ID)
    if update: last_chat_id = update.effective_chat.id
    return last_chat_id

def fmt(sig):
    ladder = f"TP1 {sig['tp1']} | TP2 {sig['tp2']}" + (f" | TP3 {sig['tp3']}" if TP_LEVELS>=3 else "")
    icon = "🚀" if sig["side"]=="LONG" else "🔻"
    return (f"{icon} <b>{sig['symbol']}</b> {sig['side']} (Intraday • {CONFIG['MODE']})\n"
            f"Entry: <b>{sig['entry']}</b>\n{ladder}\n"
            f"SL: <b>{sig['sl']}</b>\nATR(5m): {sig['atr']} | Score: <b>{sig['score']}</b>/100")

# ---------- Paper engine (3 TPs + ratchet + SL guard) ----------
def _parse_split(total_qty):
    try:
        parts=[int(x) for x in TP_SPLIT.split(",") if x!=""]
    except:
        parts=[30,30,40]
    if TP_LEVELS < 3: parts = parts[:2] or [60,40]
    s=sum(parts); parts=[max(0,p)/max(1,s) for p in parts]
    if TP_LEVELS >= 3:
        q1=total_qty*parts[0]; q2=total_qty*parts[1]; qr=total_qty - q1 - q2
        return q1,q2,max(0.0,qr)
    else:
        q1=total_qty*parts[0]; qr=total_qty - q1
        return q1,0.0,max(0.0,qr)

def can_open_more(): return len(PAPER["positions"]) < CONFIG["MAX_OPEN_TRADES"]
def free_usd(): return float(PAPER["cash"])

def exec_open(sig, ctx: CallbackContext, chat_id):
    if cooldown_active():
        ctx.bot.send_message(chat_id=chat_id, text="⏸ Trading paused (cooldown/day lock)."); return
    if not can_open_more():
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: reached MAX_OPEN_TRADES"); return

    bal = free_usd()
    base_alloc = max(MIN_USD_PER_TRADE, bal * float(CONFIG["POSITION_PCT"]))
    if bal < MIN_USD_PER_TRADE:
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: insufficient USD balance"); return

    open_syms = [PAPER["positions"][i]["symbol"] for i in PAPER["positions"]]
    alloc_mult = corr_multiplier(sig["symbol"], open_syms)
    if alloc_mult == 0.0:
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: highly correlated with open positions"); return
    alloc = base_alloc * alloc_mult

    entry = sig["entry"]
    qty = (alloc * (1 - FEE_RATE)) / entry
    fee_open = alloc * FEE_RATE
    PAPER["cash"] -= alloc

    q1,q2,qr = _parse_split(qty)
    pos_id = f"paper-{PAPER['seq']}"; PAPER["seq"] += 1
    pos = {
        "id": pos_id, "symbol": sig["symbol"], "side": sig["side"],
        "qty_total": qty, "qty_tp1": q1, "qty_tp2": q2, "qty_runner": qr,
        "entry_avg": entry,
        "tp1": sig["tp1"], "tp2": sig["tp2"], "tp3": sig.get("tp3", sig["tp2"]),
        "sl": sig["sl"], "sl_dyn": sig["sl"],
        "tp1_done": False, "tp2_done": False,
        "sl_pending": False, "sl_pending_price": None, "last_tp_ts": 0,
        "time": datetime.now(tz).isoformat(),
        "fees_open_total": fee_open, "fees_close_total": 0.0
    }
    PAPER["positions"][pos_id] = pos
    save_paper()
    _append_journal({"type":"OPEN","pos":pos})
    ctx.bot.send_message(
        chat_id=chat_id,
        text=("🟢" if sig["side"]=="LONG" else "🔴") + f" PAPER {sig['side']} {sig['symbol']} ${alloc:.2f} | entry {entry} | qty {qty:.6f}"
    )

def _close_block(p, qty, price):
    gross = price * qty
    fee  = gross * FEE_RATE
    PAPER["cash"] += (gross - fee)
    PAPER["fees_paid"] += fee
    p["fees_close_total"] += fee
    return fee

def _pnl_signed(entry, exit_avg, qty, side):
    if side=="LONG":
        return (exit_avg - entry) * qty
    else:
        return (entry - exit_avg) * qty

def _finalize_close(pid, p, reason, exit_avg):
    pnl_gross = _pnl_signed(p["entry_avg"], exit_avg, p["qty_total"], p["side"])
    pnl_usd = pnl_gross - (p["fees_open_total"] + p["fees_close_total"])
    pnl_pct = (pnl_gross / max(1e-9, p["entry_avg"] * p["qty_total"])) * 100
    PAPER["realized_pnl"] += pnl_usd
    add_result_winloss(pnl_usd)
    closed = {"id": pid, "symbol": p["symbol"], "side": p["side"], "entry_avg": p["entry_avg"],
              "exit_avg": exit_avg, "qty_total": p["qty_total"], "pnl_usd": pnl_usd,
              "pnl_pct": pnl_pct, "reason": reason, "closed_at": datetime.now(tz).isoformat()}
    PAPER["closed"].append(closed)
    _append_journal({"type":"CLOSE","trade":closed})
    PAPER["positions"].pop(pid, None)
    save_paper()
    return pnl_usd, pnl_pct

def _sl_effective(sl_dyn, side):
    # micro band to reduce false triggers from tiny crosses
    if side=="LONG":
        return sl_dyn * (1 - SL_BAND_PCT)
    else:
        return sl_dyn * (1 + SL_BAND_PCT)

def paper_check_exits(ctx: CallbackContext):
    if not PAPER["positions"]: return
    to_announce=[]
    now_ms = int(time.time()*1000)

    for pid, p in list(PAPER["positions"].items()):
        cur = ticker(p["symbol"])
        if not np.isfinite(cur): continue

        # Gap logger
        gap_pct = abs(cur - p["entry_avg"]) / max(1e-9, p["entry_avg"])
        if gap_pct > MAX_GAP_PCT:
            logging.warning(f"GAP {p['symbol']} {gap_pct*100:.2f}% vs entry")

        # ---- TP3 dominates (if enabled) ----
        if TP_LEVELS >= 3:
            if (p["side"]=="LONG" and cur >= p["tp3"]) or (p["side"]=="SHORT" and cur <= p["tp3"]):
                rem = p["qty_runner"] + (0 if p["tp2_done"] else p["qty_tp2"]) + (0 if p["tp1_done"] else p["qty_tp1"])
                if rem > 0:
                    _close_block(p, rem, p["tp3"])
                    tp1_part = (p["tp1"]*p["qty_tp1"]) if p["tp1_done"] else 0.0
                    tp2_part = (p["tp2"]*p["qty_tp2"]) if p["tp2_done"] else 0.0
                    exit_avg = (tp1_part + tp2_part + p["tp3"]*rem) / p["qty_total"]
                    pnl_usd, pnl_pct = _finalize_close(pid, p, "TP3", exit_avg)
                    to_announce.append(f"🎯 <b>TP3</b> {p['side']} {p['symbol']} avg exit {exit_avg:.6f} | PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%)")
                continue

        # ---- TP2 partial ----
        if (not p["tp2_done"]) and p["qty_tp2"] > 0:
            hit2 = (p["side"]=="LONG" and cur >= p["tp2"]) or (p["side"]=="SHORT" and cur <= p["tp2"])
            if hit2:
                _close_block(p, p["qty_tp2"], p["tp2"])
                p["tp2_done"] = True
                p["sl_dyn"] = max(p["sl_dyn"], p["tp1"]) if p["side"]=="LONG" else min(p["sl_dyn"], p["tp1"])
                p["last_tp_ts"] = now_ms
                to_announce.append(f"🥳 <b>TP2</b> {p['side']} {p['symbol']} sold {p['qty_tp2']:.6f} @ {p['tp2']:.6f}")

        # ---- TP1 partial ----
        if (not p["tp1_done"]) and p["qty_tp1"] > 0:
            hit1 = (p["side"]=="LONG" and cur >= p["tp1"]) or (p["side"]=="SHORT" and cur <= p["tp1"])
            if hit1:
                _close_block(p, p["qty_tp1"], p["tp1"])
                p["tp1_done"] = True
                # ratchet SL to BE after TP1
                if p["side"]=="LONG":
                    p["sl_dyn"] = max(p["sl_dyn"], p["entry_avg"])
                else:
                    p["sl_dyn"] = min(p["sl_dyn"], p["entry_avg"])
                p["last_tp_ts"] = now_ms
                to_announce.append(f"✅ <b>TP1</b> {p['side']} {p['symbol']} sold {p['qty_tp1']:.6f} @ {p['tp1']:.6f}")

        # ---- SL two-tick confirm + post-TP hold ----
        hold_active = (now_ms - p["last_tp_ts"]) < POST_TP_HOLD_MS
        eff_sl = _sl_effective(p["sl_dyn"], p["side"])
        breach = ((p["side"]=="LONG" and cur <= eff_sl) or
                  (p["side"]=="SHORT" and cur >= eff_sl))

        if breach and not hold_active:
            if p["sl_pending"]:
                # confirm on second observation
                rem_runner = p["qty_runner"]
                if rem_runner > 0:
                    _close_block(p, rem_runner, p["sl_dyn"])
                    tp1_part = (p["tp1"] * p["qty_tp1"]) if p["tp1_done"] else 0.0
                    tp2_part = (p["tp2"] * p["qty_tp2"]) if p["tp2_done"] else 0.0
                    exit_avg = (tp1_part + tp2_part + p["sl_dyn"]*rem_runner) / p["qty_total"]
                    pnl_usd, pnl_pct = _finalize_close(pid, p, "SL", exit_avg)
                    to_announce.append(f"🛑 <b>SL</b> {p['side']} {p['symbol']} avg exit {exit_avg:.6f} | PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%)")
                continue
            else:
                p["sl_pending"] = True
                p["sl_pending_price"] = cur
        else:
            p["sl_pending"] = False
            p["sl_pending_price"] = None

    for msg in to_announce:
        chat_id = target_chat_id()
        if chat_id:
            ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
    if to_announce: save_paper()

def update_equity_mark():
    # mark-to-market: equity = cash + sum(unrealized pnl)
    eq = PAPER["cash"]
    for p in PAPER["positions"].values():
        cur = ticker(p["symbol"])
        if not np.isfinite(cur): continue
        rem = (0 if p["tp1_done"] else p["qty_tp1"]) + (0 if p["tp2_done"] else p["qty_tp2"]) + p["qty_runner"]
        # unrealized PnL on remaining
        if p["side"]=="LONG":
            upnl = (cur - p["entry_avg"]) * rem
        else:
            upnl = (p["entry_avg"] - cur) * rem
        eq += upnl
    PAPER["equity"] = eq
    save_paper()

# ---------- scan engine ----------
def ensure_universe():
    global watchlist
    if not watchlist:
        base = get_pairs_top(limit=max(60, MAX_PAIRS))
        mom=[]
        for pid in base[:80]:
            o1,h1,l1,c1,v1=cb_candles(pid,GRAN_1H,30)
            if not c1 or len(c1)<2: continue
            ret=(c1[-1]-c1[-2])/max(1e-9,c1[-2])
            mom.append((ret,pid))
        mom.sort(reverse=True)
        merged=[]; seen=set()
        for _,pid in mom + [(0,p) for p in base]:
            if pid not in seen:
                merged.append(pid); seen.add(pid)
            if len(merged)>=MAX_PAIRS: break
        watchlist = merged or base
        logging.info(f"Watchlist prepared: {len(watchlist)} symbols")
    return watchlist

def scan_chunk(ctx: CallbackContext, push_to=None):
    global scan_cursor, CHUNK_SIZE, LAST_SCAN_TS
    if not SCAN_LOCK.acquire(blocking=False):
        return
    try:
        LAST_SCAN_TS = time.time()
        chat_id = push_to or target_chat_id()
        now = datetime.now(tz)
        syms = ensure_universe()
        if not syms: return

        start = scan_cursor; end = min(start + CHUNK_SIZE, len(syms))
        chunk = syms[start:end]; scan_cursor = 0 if end >= len(syms) else end

        hits = 0
        _update_regime()

        for pid in chunk:
            for side in (["LONG","SHORT"] if ENABLE_SHORTS else ["LONG"]):
                key=f"{pid}:{side}"
                lt = last_signal_time.get(key)
                if lt and (now - lt) < timedelta(minutes=SIGNAL_COOLDOWN_MIN): 
                    continue

                try:
                    for sig in build_signals(pid):
                        if sig["side"] != side: continue
                        if sig.get("score", 0) < CONFIG["SCORE_MIN"]: 
                            continue
                        last_signal_time[key] = now
                        signals.append({"time": now, **sig})
                        if chat_id:
                            ctx.bot.send_message(chat_id=chat_id, text=fmt(sig), parse_mode=ParseMode.HTML)
                        exec_open(sig, ctx, chat_id)
                        hits += 1
                        time.sleep(0.08)
                except Exception as e:
                    logging.error(f"build_signal crash {pid}:{side}: {e}\n{traceback.format_exc()}")
                    continue

            time.sleep(0.03)

        if hits == 0 and CHUNK_SIZE > 12: CHUNK_SIZE -= 1
        elif hits >= 2 and CHUNK_SIZE < 30: CHUNK_SIZE += 1

    finally:
        SCAN_LOCK.release()

def watchdog_scan(ctx: CallbackContext):
    if time.time() - LAST_SCAN_TS > 3*SCAN_INTERVAL_SECONDS:
        logging.warning("Scan watchdog: poking scan_chunk")
        scan_chunk(ctx)

# ---------- jobs ----------
def job_scan(ctx: CallbackContext): scan_chunk(ctx)
def job_paper(ctx: CallbackContext):
    paper_check_exits(ctx); update_equity_mark()

def job_daily(ctx: CallbackContext):
    global START_OF_DAY, EQUITY_OPEN
    chat_id = target_chat_id()
    if not chat_id: return
    if date.today() != START_OF_DAY:
        START_OF_DAY = date.today()
        EQUITY_OPEN = PAPER["equity"]
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff and s.get("score", 0) >= CONFIG["SCORE_MIN"]]
    def _lad(s): 
        return f"TP1 {s['tp1']} | TP2 {s['tp2']}" + (f" | TP3 {s['tp3']}" if TP_LEVELS>=3 else "")
    msg = ("📊 Daily Report (24h): No signals." if not recent else
           "📊 <b>Daily Report</b>\n" + "\n".join(
               f"• {s['time'].strftime('%H:%M')} {s['symbol']} {s['side']} @ {s['entry']} | {_lad(s)} | SL {s['sl']} | Score {s.get('score','-')}"
               for s in recent))
    ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

# ---------- commands ----------
def cmd_start(update: Update, ctx: CallbackContext):
    qf = int(os.getenv("SESSION_QUIET_FROM","2")); qt=int(os.getenv("SESSION_QUIET_TO","5"))
    short_txt = "ON" if ENABLE_SHORTS else "OFF"
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML, text=(
        "👋 InsiderSignals_Manual\n"
        f"Mode: <b>{CONFIG['MODE']}</b> | SCORE_MIN <b>{CONFIG['SCORE_MIN']}</b>\n"
        f"Max open: <b>{CONFIG['MAX_OPEN_TRADES']}</b> | Alloc: <b>{int(CONFIG['POSITION_PCT']*100)}%</b>\n"
        f"TPs: <b>{TP_LEVELS}</b> levels | Split <b>{TP_SPLIT}</b> | Ratchet SL (BE→TP1→TP2)\n"
        f"SHORTS: <b>{short_txt}</b>\n"
        f"Flags: EDGE={FLAGS['EDGE']} CORR={FLAGS['CORR']} REGIME={FLAGS['REGIME']} COOLDOWN={FLAGS['COOLDOWN']} SESSION={FLAGS['SESSION']} (quiet {qf}-{qt}h)\n"
        f"SL guard: band {SL_BAND_PCT*100:.3f}% | hold {POST_TP_HOLD_MS}ms | max_stop {MAX_STOP_PCT*100:.1f}% | gap_log {MAX_GAP_PCT*100:.1f}%\n"
        f"Paper cash: ${PAPER['cash']:.2f}\n\n"
        "Commands:\n"
        "/scan /paper /positions /closed /pnl /lasttrades\n"
        "/setscore S /setmode RELAXED|STRICT /setflags EDGE|CORR|REGIME|COOLDOWN|SESSION 0|1\n"
        "/status /ping"
    ))

def cmd_paper(update: Update, ctx: CallbackContext):
    update_equity_mark()
    realized = PAPER["realized_pnl"]; fees = PAPER["fees_paid"]
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
        text=(f"💼 <b>Paper</b>\nCash: ${PAPER['cash']:.2f}\nEquity: ${PAPER['equity']:.2f}\n"
              f"Open: {len(PAPER['positions'])} | Closed: {len(PAPER['closed'])}\n"
              f"Realized PnL: ${realized:.2f} | Fees: ${fees:.2f}"))

def cmd_pnl(update: Update, ctx: CallbackContext):
    wins = PAPER["wins"]; losses = PAPER["losses"]; total = wins + losses
    wr = (wins/total*100) if total else 0
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
        text=(f"📈 <b>PnL</b>\nRealized: ${PAPER['realized_pnl']:.2f}\n"
              f"Fees: ${PAPER['fees_paid']:.2f}\nWins: {wins} | Losses: {losses} | Win rate: {wr:.1f}%"))

def cmd_positions(update: Update, ctx: CallbackContext):
    if not PAPER["positions"]:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="No open paper positions."); return
    lines = ["📊 <b>Open Positions</b>"]
    for pid, p in PAPER["positions"].items():
        cur = ticker(p["symbol"])
        if not np.isfinite(cur): cur = p["entry_avg"]
        top_tp = p["tp3"] if TP_LEVELS>=3 else p["tp2"]
        up_span = (top_tp - p["entry_avg"]) if p["side"]=="LONG" else (p["entry_avg"] - top_tp)
        dn_span = (p["entry_avg"] - p["sl_dyn"]) if p["side"]=="LONG" else (p["sl_dyn"] - p["entry_avg"])
        if up_span <= 0: prog = 0.0
        elif (p["side"]=="LONG" and cur >= p["entry_avg"]):
            prog = min(100.0, (cur - p["entry_avg"]) / up_span * 100)
        elif (p["side"]=="SHORT" and cur <= p["entry_avg"]):
            prog = min(100.0, (p["entry_avg"] - cur) / up_span * 100)
        else:
            down = (p["entry_avg"] - cur) if p["side"]=="LONG" else (cur - p["entry_avg"])
            prog = -min(100.0, down / max(1e-9, dn_span) * 100)
        # side-aware unrealized
        rem_qty = p["qty_runner"] + (0 if p["tp1_done"] else p["qty_tp1"]) + (0 if p["tp2_done"] else p["qty_tp2"])
        upnl = (cur - p["entry_avg"]) * rem_qty if p["side"]=="LONG" else (p["entry_avg"] - cur) * rem_qty
        pnl_usd = upnl - p["fees_open_total"]
        pnl_pct = (upnl / max(1e-9, p["entry_avg"]*p["qty_total"])) * 100
        lines.append(
            f"• {p['symbol']} {p['side']} qty {p['qty_total']:.6f} | entry {p['entry_avg']:.6f} | now {cur:.6f} | "
            f"PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%) | progress {prog:.1f}%"
        )
    ctx.bot.send_message(chat_id=target_chat_id(update), text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_closed(update: Update, ctx: CallbackContext):
    if not PAPER["closed"]:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="No closed paper trades yet."); return
    lines = ["✅ <b>Closed Trades (last 10)</b>"]
    for t in PAPER["closed"][-10:]:
        lines.append(f"• {t['symbol']} {t['side']} @ {t['entry_avg']:.6f} → {t['exit_avg']:.6f} | "
                     f"Qty {t['qty_total']:.6f} | PnL ${t['pnl_usd']:.2f} ({t['pnl_pct']:.2f}%) [{t['reason']}]")
    ctx.bot.send_message(chat_id=target_chat_id(update), text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_lasttrades(update: Update, ctx: CallbackContext):
    if not os.path.exists(JOURNAL_PATH):
        ctx.bot.send_message(chat_id=target_chat_id(update), text="No trades yet."); return
    try:
        lines=[]
        with open(JOURNAL_PATH,"r") as f:
            for row in f.readlines()[-12:]:
                j=json.loads(row)
                if j.get("type")=="OPEN":
                    p=j["pos"]; lines.append(f"• OPEN {p['symbol']} {p['side']} qty {p['qty_total']:.6f} @ {p['entry_avg']}")
                elif j.get("type")=="CLOSE":
                    t=j["trade"]; lines.append(f"• CLOSE {t['symbol']} {t['side']} pnl ${t['pnl_usd']:.2f} ({t['pnl_pct']:.2f}%) [{t['reason']}]")
        if not lines: lines=["No trades yet."]
        ctx.bot.send_message(chat_id=target_chat_id(update), text="\n".join(lines))
    except Exception as e:
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"Journal read error: {e}")

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

def cmd_setflags(update: Update, ctx: CallbackContext):
    try:
        k, val = update.message.text.split()[1].upper(), int(update.message.text.split()[2])
        assert k in FLAGS and val in (0,1)
        FLAGS[k] = val; CONFIG[k]=val; save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ {k} set to {val}.")
    except Exception:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setflags EDGE|CORR|REGIME|COOLDOWN|SESSION 0|1")

def cmd_scan(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"🔍 Scan running (mode={CONFIG['MODE']}, score≥{CONFIG['SCORE_MIN']}, shorts={'ON' if ENABLE_SHORTS else 'OFF'})…")
    scan_chunk(ctx, push_to=target_chat_id(update))

def cmd_status(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(chat_id=target_chat_id(update),
        text=(f"OK | MODE={CONFIG['MODE']} SCORE≥{CONFIG['SCORE_MIN']} | chunk={CHUNK_SIZE} "
              f"| open={len(PAPER['positions'])} | cash=${PAPER['cash']:.2f} eq=${PAPER['equity']:.2f} "
              f"| cooldown={'YES' if cooldown_active() else 'NO'} | shorts={'ON' if ENABLE_SHORTS else 'OFF'}"))

def cmd_ping(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(chat_id=target_chat_id(update), text="pong")

# ---------- main ----------
def main():
    updater = Updater(TOKEN, use_context=True)
    updater.bot.delete_webhook(drop_pending_updates=True)

    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("paper", cmd_paper))
    dp.add_handler(CommandHandler("pnl", cmd_pnl))
    dp.add_handler(CommandHandler("positions", cmd_positions))
    dp.add_handler(CommandHandler("closed", cmd_closed))
    dp.add_handler(CommandHandler("lasttrades", cmd_lasttrades))
    dp.add_handler(CommandHandler("setscore", cmd_setscore))
    dp.add_handler(CommandHandler("setmode", cmd_setmode))
    dp.add_handler(CommandHandler("setflags", cmd_setflags))
    dp.add_handler(CommandHandler("scan", cmd_scan))
    dp.add_handler(CommandHandler("status", cmd_status))
    dp.add_handler(CommandHandler("ping", cmd_ping))

    jq: JobQueue = updater.job_queue
    jq.run_repeating(job_scan, interval=SCAN_INTERVAL_SECONDS, first=random.randint(6,12), name="job_scan")
    jq.run_repeating(job_paper, interval=PAPER_CHECK_SECONDS, first=8, name="job_paper")
    jq.run_repeating(watchdog_scan, interval=max(20, SCAN_INTERVAL_SECONDS), first=20, name="watchdog")
    jq.run_daily(job_daily, time=dtime(hour=DAILY_REPORT_HOUR, minute=0, tzinfo=tz))

    logging.info("Manual bot v3.6 started (shorts=%s)", "ON" if ENABLE_SHORTS else "OFF")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
