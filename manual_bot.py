# manual_bot.py — Coinbase • PAPER trading • €300 Sniper-Safe
# v3.1 (2025-08-10)
# - STRICT, SCORE≥97, Top-50 USD/USDT, 5% alloc, max 5 open
# - Real-time paper engine (TP/SL, multi-partials optional), PnL & journal
# - 13 modules ON by default (toggles via Telegram)
#
# Commands:
# /start /scan /paper /positions /pnl /stats [/stats 7d|all|open|perf <sym>]
# /setscore N /setmax N /setalloc P /setmode RELAXED|STRICT /paperreset
# Toggles: /edgeon/off /corron/off /regimeon/off /cooldownon/off /focuson/off
#          /trailon/off /newson/off /recoveron/off /mtp_on/off /sention/off /sessionon/off
# Extras:  /heatmap /journal /replay <symbol>

import os, time, math, random, logging, threading, http.server, socketserver, json
from datetime import datetime, timedelta, time as dtime
import pytz, requests, numpy as np
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue
from requests.exceptions import RequestException
from http.client import RemoteDisconnected

# ---------------- tiny HTTP server (Render keep-alive) ----------------
PORT = int(os.getenv("PORT", "10000"))
class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def _keepalive():
    with socketserver.TCPServer(("", PORT), _Handler) as httpd:
        httpd.serve_forever()
threading.Thread(target=_keepalive, daemon=True).start()

START_TS = time.time()

# ---------------- env & sniper defaults ----------------
TOKEN              = os.getenv("TELEGRAM_MANUAL_TOKEN")               # REQUIRED (set in Render → Environment)
TIMEZONE           = os.getenv("TIMEZONE", "Europe/Dublin")
DAILY_REPORT_HOUR  = int(os.getenv("DAILY_REPORT_HOUR", "20"))
FORCED_CHAT_ID     = os.getenv("CHAT_ID")
tz = pytz.timezone(TIMEZONE)

# cadence
SCAN_INTERVAL_SECONDS   = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
SYMBOLS_PER_SCAN        = int(os.getenv("SYMBOLS_PER_SCAN", "25"))   # smaller chunks = smoother
SIGNAL_COOLDOWN_MIN     = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "8"))

# universe cap (sniper mode = 50)
MAX_PAIRS               = int(os.getenv("MAX_PAIRS", "50"))

# mode & score (sniper = STRICT, 97)
SCORE_MIN               = int(os.getenv("SCORE_MIN", "97"))
MODE                    = os.getenv("MODE", "STRICT").strip().upper()  # RELAXED|STRICT

RELAXED = dict(BREAKOUT_LOOKBACK=3, VOLUME_MULTIPLIER=1.05, RSI_MIN=20, RSI_MAX=80,
               USE_EMA_CROSS=True,  EMA_TOL_PCT=0.012, USE_ADX_FILTER=True, ADX_MIN=10,
               USE_HTF_FILTER=False, HTF_TOL_PCT=0.005, USE_BTC_BIAS=False)
STRICT  = dict(BREAKOUT_LOOKBACK=6, VOLUME_MULTIPLIER=1.20, RSI_MIN=35, RSI_MAX=65,
               USE_EMA_CROSS=True,  EMA_TOL_PCT=0.008, USE_ADX_FILTER=True, ADX_MIN=18,
               USE_HTF_FILTER=True,  HTF_TOL_PCT=0.005, USE_BTC_BIAS=True)

# exits (R = ATR 5m)
R_TP1, R_TP2, R_SL      = float(os.getenv("R_TP1", "1.1")), float(os.getenv("R_TP2","2.2")), float(os.getenv("R_SL","1.1"))
TRAIL_AFTER_TP1         = os.getenv("TRAIL_AFTER_TP1", "1") == "1"
FEE_RATE                = float(os.getenv("FEE_RATE", "0.001"))     # 0.10%

# paper sizing — sniper: 5% per trade, max 5
POSITION_PCT            = float(os.getenv("POSITION_PCT", "0.05"))
MAX_OPEN_TRADES         = int(os.getenv("MAX_OPEN_TRADES", "5"))
MIN_USD_PER_TRADE       = float(os.getenv("MIN_USD_PER_TRADE", "10"))
PAPER_START_CASH        = float(os.getenv("PAPER_START_CASH", "300"))  # start with €300≈$300
PAPER_CHECK_SECONDS     = int(os.getenv("PAPER_CHECK_SECONDS", "15"))

# keepalive (your URL)
KEEPALIVE_URL           = os.getenv("KEEPALIVE_URL", "https://insidersignalsmanual-olo3.onrender.com").strip()
KEEPALIVE_SECONDS       = int(os.getenv("KEEPALIVE_SECONDS", "240"))

# historical validator (ON; sniper-safe)
HIST_LOOKBACK_BARS_5M   = int(os.getenv("HIST_LOOKBACK_BARS_5M", "900"))  # ~75h
HIST_MIN_OCCURRENCES    = int(os.getenv("HIST_MIN_OCCURRENCES", "10"))
HIST_MIN_SUCCESS        = float(os.getenv("HIST_MIN_SUCCESS", "0.55"))

# correlation thresholds
CORR_BLOCK              = float(os.getenv("CORR_BLOCK", "0.8"))
CORR_HALF               = float(os.getenv("CORR_HALF", "0.6"))

# streak guard
COOLDOWN_LOSSES         = int(os.getenv("COOLDOWN_LOSSES", "3"))
COOLDOWN_WINDOW_MIN     = int(os.getenv("COOLDOWN_WINDOW_MIN", "120"))
COOLDOWN_PAUSE_MIN      = int(os.getenv("COOLDOWN_PAUSE_MIN", "60"))

# session hours (Dublin) to tighten a bit
SESSION_QUIET_FROM      = int(os.getenv("SESSION_QUIET_FROM", "2"))
SESSION_QUIET_TO        = int(os.getenv("SESSION_QUIET_TO", "5"))

# retries / jitter
RETRY_MAX               = int(os.getenv("RETRY_MAX", "4"))
RETRY_BASE_DELAY        = float(os.getenv("RETRY_BASE_DELAY", "0.6"))
REQ_JITTER_MIN_MS       = int(os.getenv("REQ_JITTER_MIN_MS", "50"))
REQ_JITTER_MAX_MS       = int(os.getenv("REQ_JITTER_MAX_MS", "140"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------------- Coinbase API ----------------
CB_BASE = "https://api.exchange.coinbase.com"
GRAN_5M, GRAN_15M, GRAN_1H, GRAN_4H = 300, 900, 3600, 14400
session = requests.Session()
session.headers.update({"User-Agent": "insider-sniper/3.1"})

# ---------------- state & persistence ----------------
CONFIG_PATH = "config.json"
CONFIG = {
    "MODE": MODE, "SCORE_MIN": SCORE_MIN,
    "POSITION_PCT": POSITION_PCT, "MAX_OPEN_TRADES": MAX_OPEN_TRADES,
    # 13 feature flags — ON by default
    "EDGE": 1, "CORR": 1, "REGIME": 1, "COOLDOWN": 1, "FOCUS": 1,
    "TRAIL": 1, "NEWS": 1, "RECOVER": 1, "MTP": 1, "SENTI": 1, "SESSION": 1
}
signals = []
last_signal_time = {}
scan_cursor = 0
watchlist = []
NO_NATIVE_4H = set()
last_chat_id = None
BTC_STATE = {"adx1h": 0.0}
ETH_STATE = {"adx1h": 0.0}
STREAK = {"loss_times": [], "cooldown_until": 0}
PAPER = {"cash": PAPER_START_CASH, "equity": PAPER_START_CASH,
         "positions": {}, "closed": [], "seq": 1,
         "realized_pnl": 0.0, "fees_paid": 0.0, "wins": 0, "losses": 0}

def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path,"r") as f: return json.load(f)
    except Exception as e:
        logging.warning(f"load {path} error: {e}")
    return default
def _save_json(path, data):
    try:
        with open(path,"w") as f: json.dump(data,f)
    except Exception as e:
        logging.warning(f"save {path} error: {e}")
def load_config():
    global CONFIG
    data = _load_json(CONFIG_PATH, None)
    if data: CONFIG.update(data)
def save_config(): _save_json(CONFIG_PATH, CONFIG)
load_config()

def apply_mode(m):
    params = STRICT if (m or "STRICT").upper()=="STRICT" else RELAXED
    globals().update(params); CONFIG["MODE"]=(m or "STRICT").upper()
apply_mode(CONFIG["MODE"])

# ---------------- indicators ----------------
def ema(arr,p):
    arr=np.array(arr,float)
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

# ---------------- HTTP helpers ----------------
def _jitter(): time.sleep(random.randint(REQ_JITTER_MIN_MS, REQ_JITTER_MAX_MS)/1000.0)
def _get(url, params=None, timeout=15):
    delay=RETRY_BASE_DELAY
    for k in range(RETRY_MAX):
        try:
            r=session.get(url, params=params, timeout=timeout)
            if r.status_code in (429,502,503,504): raise RequestException(f"HTTP {r.status_code}")
            r.raise_for_status(); return r
        except (RequestException, RemoteDisconnected):
            if k==RETRY_MAX-1: raise
            time.sleep(delay*(1.6**k)+random.uniform(0.05,0.25))

# ---------------- market data ----------------
def get_pairs_top(limit=50):
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
    try:
        _jitter()
        r=_get(f"{CB_BASE}/products/{pid}/candles", params={"granularity": gran}, timeout=20)
        data=r.json()
        if not isinstance(data,list) or not data: return (None,)*5
        data.sort(key=lambda x:x[0])
        # dedupe timestamps
        clean=[]; last_t=0
        for d in data[-limit:]:
            if d[0]>last_t:
                clean.append(d); last_t=d[0]
        t, low, high, op, close, vol = zip(*[(d[0],d[1],d[2],d[3],d[4],d[5]) for d in clean])
        return list(op), list(high), list(low), list(close), list(vol)
    except Exception as e:
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

# ---------------- helper tools ----------------
def r_targets(entry, atr5, side):
    if side=="LONG":
        sl= entry - R_SL*atr5
        tp1=entry + R_TP1*atr5
        tp2=entry + R_TP2*atr5
    else:
        sl= entry + R_SL*atr5
        tp1=entry - R_TP1*atr5
        tp2=entry - R_TP2*atr5
    return sl,tp1,tp2

def near(price, ema_val, tol_pct):
    if ema_val is None or ema_val==0: return False
    return abs(price-ema_val)/ema_val <= tol_pct

def illiquid(h5,l5,c5):
    if not c5 or len(c5)<60: return True
    rng=(max(h5[-24:])-min(l5[-24:]))/max(1e-9,c5[-1])
    return rng<0.0005

# ---------------- regime / breadth / news-stub ----------------
def _update_regime():
    o1,h1,l1,c1,v1 = cb_candles("BTC-USD", GRAN_1H, 220)
    if c1:
        a=adx(h1,l1,c1,14)
        if a is not None and not math.isnan(a[-1]): BTC_STATE["adx1h"]=float(a[-1])
    o1e,h1e,l1e,c1e,v1e = cb_candles("ETH-USD", GRAN_1H, 220)
    if c1e:
        a=adx(h1e,l1e,c1e,14)
        if a is not None and not math.isnan(a[-1]): ETH_STATE["adx1h"]=float(a[-1])

def _market_breadth():
    syms=_ensure_universe()
    good=0; total=0
    for pid in syms[:40]:
        o1,h1,l1,c1,v1=cb_candles(pid,GRAN_1H,120)
        if not c1: continue
        e50=ema(c1,50)
        if e50 is None: continue
        total+=1
        if c1[-1]>e50[-1]: good+=1
    return (good/total) if total else 0.5

def _news_block():
    # conservative placeholder: block when BTC 5m move > ±1.2%
    o5,h5,l5,c5,v5=cb_candles("BTC-USD",GRAN_5M,10)
    if not c5 or len(c5)<2: return False
    chg=(c5[-1]-c5[-2])/max(1e-9,c5[-2])
    return abs(chg)>=0.012

# ---------------- historical quick validator ----------------
def hist_profitable(pid, o5,h5,l5,c5,v5):
    if not CONFIG.get("EDGE",1): return True
    try:
        if not c5 or len(c5)<200:  # not enough to judge
            return True
        BL=BREAKOUT_LOOKBACK
        wins=0; total=0
        ema20=ema(c5,20)
        for i in range(BL+30, min(len(c5)-1, HIST_LOOKBACK_BARS_5M), 6):
            hh=max(h5[i-BL:i]); last=c5[i]
            cross=(ema20 is not None and c5[i]>ema20[i] and c5[i-1]<=ema20[i-1])
            brk=(last>hh)
            if not (brk or (USE_EMA_CROSS and cross)): continue
            atr5=atr(h5[:i+1],l5[:i+1],c5[:i+1],14)
            if atr5 is None or math.isnan(atr5[-1]): continue
            _,_,tp2=r_targets(last,float(atr5[-1]),"LONG")
            hit=None
            for j in range(i+1, min(i+13, len(c5))):
                if c5[j]>=tp2: hit="TP2"; break
                if c5[j]<=last - R_SL*float(atr5[-1]): hit="SL"; break
            if hit:
                total+=1
                if hit=="TP2": wins+=1
        if total < HIST_MIN_OCCURRENCES: return True
        return (wins/total) >= HIST_MIN_SUCCESS
    except Exception as e:
        logging.warning(f"hist validator error {pid}: {e}")
        return True

# ---------------- correlation control ----------------
def _corr_block(pid, open_symbols):
    if not CONFIG.get("CORR",1) or not open_symbols: return 1.0
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

# ---------------- streak guard ----------------
def _cooldown_active():
    if not CONFIG.get("COOLDOWN",1): return False
    return time.time() < STREAK.get("cooldown_until", 0)
def _register_trade_result(pnl_usd):
    if not CONFIG.get("COOLDOWN",1): return
    now=time.time()
    window=now - COOLDOWN_WINDOW_MIN*60
    STREAK["loss_times"]=[t for t in STREAK.get("loss_times",[]) if t>=window]
    if pnl_usd<0:
        STREAK["loss_times"].append(now)
        if len(STREAK["loss_times"])>=COOLDOWN_LOSSES:
            STREAK["cooldown_until"]= now + COOLDOWN_PAUSE_MIN*60

# ---------------- universe focus ----------------
def focus_universe(base):
    if not CONFIG.get("FOCUS",1): return base
    vol_rank=[]; mom_rank=[]
    try:
        r=_get(f"{CB_BASE}/products", timeout=20); pro=r.json()
        for p in pro:
            if p.get("status")!="online" or p.get("trading_disabled"): continue
            if (p.get("quote_currency") or "").upper() not in ("USD","USDT"): continue
            vol=float(p.get("volume_24h") or 0.0); vol_rank.append((vol,p["id"]))
        vol_rank.sort(reverse=True); top_vol=[pid for _,pid in vol_rank[:40]]
    except Exception:
        top_vol=base[:40]
    for pid in base[:80]:
        o1,h1,l1,c1,v1=cb_candles(pid,GRAN_1H,30)
        if not c1 or len(c1)<2: continue
        ret=(c1[-1]-c1[-2])/max(1e-9,c1[-2])
        mom_rank.append((ret,pid))
    mom_rank.sort(reverse=True); top_mom=[pid for _,pid in mom_rank[:20]]
    seen=set(); out=[]
    for pid in top_vol+top_mom:
        if pid not in seen:
            out.append(pid); seen.add(pid)
        if len(out)>=MAX_PAIRS: break
    return out or base

# ---------------- sentiment & session ----------------
def senti_ok():
    if not CONFIG.get("SENTI",1): return True
    breadth=_market_breadth()
    return breadth >= 0.45
def session_tighten():
    if not CONFIG.get("SESSION",1): return 0
    now=datetime.now(tz).hour
    qf, qt = SESSION_QUIET_FROM, SESSION_QUIET_TO
    if qf<=qt: quiet = qf <= now <= qt
    else: quiet = not (qt < now < qf)
    return 2 if quiet else 0

# ---------------- scoring ----------------
def score_signal(price, ema50_now, atr5, vol_ok, rsi_val, adx_val):
    s=0
    if vol_ok: s+=25
    if adx_val is not None and not math.isnan(adx_val): s+=min(25,max(0,(adx_val-10)*2))
    if ema50_now:
        dist=abs(price-ema50_now)/ema50_now
        s+=max(0,25-100*dist)
    if rsi_val is not None and not math.isnan(rsi_val): s+=max(0,25-abs(50-rsi_val))
    return int(min(100,s))

# ---------------- signal builder ----------------
def build_signal(pid):
    if _cooldown_active(): return None
    if CONFIG.get("NEWS",1) and _news_block(): return None
    if not senti_ok(): return None

    o5,h5,l5,c5,v5 = cb_candles(pid,GRAN_5M,240)
    if not c5 or illiquid(h5,l5,c5): return None
    if not hist_profitable(pid,o5,h5,l5,c5,v5): return None

    o15,h15,l15,c15,v15 = cb_candles(pid,GRAN_15M,200)
    o1,h1,l1,c1,v1      = cb_candles(pid,GRAN_1H,220)

    c4h=None
    if USE_HTF_FILTER and pid not in NO_NATIVE_4H:
        o4h,h4h,l4h,c4h,v4h = cb_candles(pid,GRAN_4H,220)
        if not c4h: NO_NATIVE_4H.add(pid)
    if USE_HTF_FILTER and c4h is None and c1:
        _o,_h,_l,_c,_v = resample_1h_to_4h(o1,h1,l1,c1,v1)
        c4h = _c

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
    hh=max(h5[-(BL+1):-1]); last=c5[-1]
    vol_avg=sum(v5[-(BL+1):-1])/BL; last_vol=v5[-1]
    r=rsi(c15,14)
    if r is None or math.isnan(r[-1]): return None
    r_ok = (RSI_MIN <= r[-1] <= RSI_MAX)

    ema20_5=ema(c5,20)
    cross_up= ema20_5 is not None and c5[-1]>ema20_5[-1] and c5[-2]<=ema20_5[-2]
    brk_up  = last>hh
    long_trig = brk_up or (USE_EMA_CROSS and cross_up)

    trend_up = (last>ema50_now) or near(last, ema50_now, EMA_TOL_PCT)

    htf_up=True
    if USE_HTF_FILTER and ema200_4h is not None:
        htf_up = (c1[-1] >= (1-HTF_TOL_PCT)*ema200_1h[-1]) and (c4h[-1] >= (1-HTF_TOL_PCT)*ema200_4h[-1])

    vol_ok = last_vol >= VOLUME_MULTIPLIER*max(vol_avg,1e-9)

    atr5=atr(h5,l5,c5,14)
    if atr5 is None or math.isnan(atr5[-1]): return None
    a5=float(atr5[-1])

    ok = long_trig and vol_ok and r_ok and trend_up and htf_up and adx_ok
    if not ok: return None

    sc = score_signal(last, ema50_now, a5, vol_ok, r[-1], adx1h[-1] if adx1h is not None else None)

    if CONFIG.get("REGIME",1):
        _update_regime()
        if BTC_STATE["adx1h"] < 18 and ETH_STATE["adx1h"] < 18:  # choppy
            sc -= 2

    sc -= session_tighten()
    if sc < CONFIG["SCORE_MIN"]: return None

    sl,tp1,tp2 = r_targets(last,a5,"LONG")
    return {"symbol": pid, "side": "LONG", "entry": round(last,6),
            "tp1": round(tp1,6), "tp2": round(tp2,6), "sl": round(sl,6),
            "atr": round(a5,6), "score": sc}

# ---------------- telegram helpers ----------------
def target_chat_id(update: Update=None):
    global last_chat_id
    if FORCED_CHAT_ID: return int(FORCED_CHAT_ID)
    if update: last_chat_id = update.effective_chat.id
    return last_chat_id
def fmt(sig):
    return (f"🚀 <b>{sig['symbol']}</b> LONG (Intraday • {CONFIG['MODE']})\n"
            f"Entry: <b>{sig['entry']}</b>\nTP1: <b>{sig['tp1']}</b> | TP2: <b>{sig['tp2']}</b>\n"
            f"SL: <b>{sig['sl']}</b>\nATR(5m): {sig['atr']} | Score: <b>{sig['score']}</b>/100")

# ---------------- paper engine (MTP/trailing/recovery) ----------------
def _can_open(): return len(PAPER["positions"]) < CONFIG["MAX_OPEN_TRADES"]
def _free_usd():  return float(PAPER["cash"])

def _alloc_for(pid):
    open_syms = {p["symbol"] for p in PAPER["positions"].values()}
    mult = _corr_block(pid, open_syms) if open_syms else 1.0
    if mult == 0.0: return 0.0
    base = max(MIN_USD_PER_TRADE, PAPER["cash"]*float(CONFIG["POSITION_PCT"]))
    return base * mult

def _open_long(sig, ctx, chat_id):
    if not _can_open():
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: reached MAX_OPEN_TRADES"); return
    alloc=_alloc_for(sig["symbol"])
    if alloc <= 0.0:
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: correlation block"); return
    if PAPER["cash"] < MIN_USD_PER_TRADE:
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: insufficient USD"); return
    entry=sig["entry"]; qty=(alloc*(1-FEE_RATE))/entry
    fee_open=alloc*FEE_RATE
    PAPER["cash"]-=alloc
    pid=f"paper-{PAPER['seq']}"; PAPER["seq"]+=1

    pos={
        "id":pid,"symbol":sig["symbol"],"side":"LONG",
        "qty_total":qty,"entry_avg":entry,
        "fees_open_total":fee_open,"fees_close_total":0.0,
        "time":datetime.now(tz).isoformat(),
        "atr":sig["atr"], "scaled_in": False
    }

    # keep MTP ON (can /mtp_off if you want simpler)
    # 4 partials at 1.0R, 1.8R, 2.6R, 3.5R
    r1=entry + 1.0*sig["atr"]; r2=entry + 1.8*sig["atr"]; r3=entry + 2.6*sig["atr"]; r4=entry + 3.5*sig["atr"]
    pos.update({
        "tp_levels":[r1,r2,r3,r4],
        "tp_fills":[False,False,False,False],
        "tp_qty":[qty*0.25, qty*0.25, qty*0.25, qty*0.25],
        "sl":sig["sl"], "sl_dyn":sig["sl"]
    })

    PAPER["positions"][pid]=pos
    ctx.bot.send_message(chat_id=chat_id, text=f"🟢 PAPER BUY {sig['symbol']} ${alloc:.2f} | entry {entry} | qty {qty:.6f}")

def _mark_equity():
    eq=PAPER["cash"]
    for p in PAPER["positions"].values():
        px=ticker(p["symbol"])
        if not np.isfinite(px): px=p["entry_avg"]
        remaining = p["qty_total"] - sum([p["tp_qty"][i] if p["tp_fills"][i] else 0.0 for i in range(4)])
        eq += px*remaining*(1-FEE_RATE)
    PAPER["equity"]=eq

def _close_record(p, avg_exit, reason):
    total_fees = p["fees_open_total"] + p.get("fees_close_total",0.0)
    pnl = (avg_exit - p["entry_avg"]) * p["qty_total"] - total_fees
    pnl_pct = (avg_exit - p["entry_avg"]) / p["entry_avg"] * 100
    PAPER["realized_pnl"] += pnl
    if pnl>=0: PAPER["wins"]+=1
    else: PAPER["losses"]+=1
    # streak guard
    now=time.time()
    window=now - COOLDOWN_WINDOW_MIN*60
    STREAK["loss_times"]=[t for t in STREAK.get("loss_times",[]) if t>=window]
    if pnl<0:
        STREAK["loss_times"].append(now)
        if len(STREAK["loss_times"])>=COOLDOWN_LOSSES:
            STREAK["cooldown_until"]= now + COOLDOWN_PAUSE_MIN*60
    PAPER["closed"].append({
        "id": p["id"], "symbol": p["symbol"], "entry_avg": p["entry_avg"],
        "exit_avg": avg_exit, "qty_total": p["qty_total"],
        "pnl_usd": pnl, "pnl_pct": pnl_pct, "reason": reason,
        "closed_at": datetime.now(tz).isoformat()
    })

def _paper_exits(ctx):
    if not PAPER["positions"]: return
    rm=[]
    for pid,p in list(PAPER["positions"].items()):
        px=ticker(p["symbol"])
        if not np.isfinite(px): continue

        # single scale-in at entry - 0.5R if enabled
        if CONFIG.get("RECOVER",1) and (not p.get("scaled_in")) and px <= (p["entry_avg"] - 0.5*p["atr"]):
            add_val = p["entry_avg"]*p["qty_total"]*0.25
            if PAPER["cash"] >= add_val:
                add_qty = (add_val*(1-FEE_RATE))/px
                PAPER["cash"] -= add_val
                p["qty_total"] += add_qty
                p["entry_avg"] = (p["entry_avg"]*(p["qty_total"]-add_qty) + px*add_qty)/p["qty_total"]
                p["fees_open_total"] += add_val*FEE_RATE
                p["scaled_in"]=True

        # partial TPs (4 levels)
        for i in range(4):
            if (not p["tp_fills"][i]) and px >= p["tp_levels"][i]:
                val = p["tp_levels"][i] * p["tp_qty"][i]
                fee = val * FEE_RATE
                PAPER["cash"] += (val - fee)
                PAPER["fees_paid"] = PAPER.get("fees_paid",0.0) + fee
                p["fees_close_total"] = p.get("fees_close_total",0.0) + fee
                p["tp_fills"][i] = True
                # smarter trail
                if CONFIG.get("TRAIL",1):
                    trail_be = p["entry_avg"]
                    trail_swing = p["entry_avg"] - 0.5*p["atr"]
                    p["sl_dyn"] = max(p["sl_dyn"], trail_be, trail_swing)

        # stop loss after partials
        if px <= p["sl_dyn"]:
            remaining = p["qty_total"] - sum([p["tp_qty"][i] if p["tp_fills"][i] else 0.0 for i in range(4)])
            if remaining > 0:
                val = p["sl_dyn"] * remaining
                fee = val * FEE_RATE
                PAPER["cash"] += (val - fee)
                PAPER["fees_paid"] = PAPER.get("fees_paid",0.0) + fee
                p["fees_close_total"] = p.get("fees_close_total",0.0) + fee
            tp_part = sum([p["tp_levels"][i]*p["tp_qty"][i] for i in range(4) if p["tp_fills"][i]])
            total_qty = p["qty_total"]
            exit_avg = (tp_part + (p["sl_dyn"] * max(0.0, total_qty - sum([p["tp_qty"][i] for i in range(4) if p["tp_fills"][i]])))) / total_qty
            _close_record(p, exit_avg, "SL")
            rm.append(pid)
            ctx.bot.send_message(chat_id=target_chat_id(), text=f"🛑 SL {p['symbol']} avg {exit_avg:.6f}")
            continue

        # TP4 finished → close journal entry
        if all(p["tp_fills"]):
            tp_part = sum([p["tp_levels"][i]*p["tp_qty"][i] for i in range(4)])
            exit_avg = tp_part / p["qty_total"]
            _close_record(p, exit_avg, "TP4")
            rm.append(pid)
            ctx.bot.send_message(chat_id=target_chat_id(), text=f"🎯 TP4 {p['symbol']} all partials filled")

    for k in rm: PAPER["positions"].pop(k, None)
    _mark_equity()

# ---------------- scanning ----------------
def _ensure_universe():
    global watchlist
    if not watchlist: watchlist = get_pairs_top(limit=MAX_PAIRS or 50)
    if CONFIG.get("FOCUS",1): return focus_universe(watchlist)
    return watchlist

def _scan_chunk(ctx: CallbackContext, push_to=None):
    global scan_cursor
    chat = push_to or target_chat_id()
    if not chat: return
    syms=_ensure_universe()
    if not syms: return
    if CONFIG.get("REGIME",1): _update_regime()

    now=datetime.now(tz)
    start=scan_cursor; end=min(start+SYMBOLS_PER_SCAN, len(syms))
    chunk=syms[start:end]; scan_cursor = 0 if end>=len(syms) else end
    hits=0
    for pid in chunk:
        lt=last_signal_time.get(pid)
        if lt and (now-lt)<timedelta(minutes=SIGNAL_COOLDOWN_MIN): continue
        sig=build_signal(pid)
        if not sig: continue
        if sig["score"] < CONFIG["SCORE_MIN"]: continue
        last_signal_time[pid]=now
        signals.append({"time":now, **sig})
        ctx.bot.send_message(chat_id=chat, text=fmt(sig), parse_mode=ParseMode.HTML)
        _open_long(sig, ctx, chat)
        hits+=1; time.sleep(0.06)
    if hits==0 and push_to:
        ctx.bot.send_message(chat_id=chat, text=f"(No valid setups ≥ {CONFIG['SCORE_MIN']} — watching.)")

# ---------------- reporting & stats ----------------
def _fmt_dur(seconds):
    m, s = divmod(int(seconds), 60); h, m = divmod(m, 60); return f"{h}h{m:02d}m"
def _pnl_slice(days=None):
    now = datetime.now(tz)
    items = PAPER["closed"]
    if days is not None:
        cut = now - timedelta(days=days)
        items = [t for t in items if datetime.fromisoformat(t["closed_at"]) >= cut]
    pnl = sum(t["pnl_usd"] for t in items)
    wins = sum(1 for t in items if t["pnl_usd"] >= 0)
    losses = sum(1 for t in items if t["pnl_usd"] < 0)
    aw = np.mean([t["pnl_usd"] for t in items if t["pnl_usd"] >= 0]) if wins else 0.0
    al = np.mean([abs(t["pnl_usd"]) for t in items if t["pnl_usd"] < 0]) if losses else 0.0
    pf = (aw/ max(al,1e-9)) if (wins and losses) else (float('inf') if wins and not losses else 0.0)
    wr = (wins / max(wins+losses,1) * 100.0)
    return pnl, wins, losses, wr, pf

def cmd_stats(update: Update, ctx: CallbackContext):
    args = update.message.text.strip().split()
    mode = (args[1].lower() if len(args)>1 else "today")
    if mode not in ("today","7d","all","open","perf"): mode = "today"
    if mode == "open": return cmd_positions(update, ctx)
    if mode == "perf" and len(args)>=3:
        sym=args[2].upper()
        items=[t for t in PAPER["closed"] if t["symbol"]==sym]
        if not items: ctx.bot.send_message(chat_id=target_chat_id(update), text=f"No history for {sym}."); return
        pnl=sum(t["pnl_usd"] for t in items); wins=sum(1 for t in items if t["pnl_usd"]>=0); losses=len(items)-wins
        wr= wins/max(len(items),1)*100
        ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
                             text=(f"📈 <b>{sym} Performance</b>\nTrades {len(items)} | PnL ${pnl:.2f}\nWins {wins} | Losses {losses} | WinRate {wr:.1f}%"))
        return
    days = {"today":1,"7d":7,"all":None}[mode]
    pnl,wins,losses,wr,pf = _pnl_slice(days=None if mode=="all" else days)
    _mark_equity()
    uptime=_fmt_dur(time.time()-START_TS)
    flags = " ".join([k for k,v in (("EDGE",CONFIG["EDGE"]),("CORR",CONFIG["CORR"]),("REGIME",CONFIG["REGIME"]),
                                     ("COOLDOWN",CONFIG["COOLDOWN"]),("FOCUS",CONFIG["FOCUS"]),("TRAIL",CONFIG["TRAIL"]),
                                     ("NEWS",CONFIG["NEWS"]),("RECOVER",CONFIG["RECOVER"]),("MTP",CONFIG["MTP"]),
                                     ("SENTI",CONFIG["SENTI"]),("SESSION",CONFIG["SESSION"])) if v])
    msg = (f"📊 STATS — {datetime.now(tz).strftime('%H:%M')}\n"
           f"Equity ${PAPER['equity']:.2f} | Cash ${PAPER['cash']:.2f}\n"
           f"PnL {mode}: ${pnl:.2f} | W{wins}/L{losses} | WR {wr:.1f}% | PF {pf:.2f}\n"
           f"Open {len(PAPER['positions'])}/{CONFIG['MAX_OPEN_TRADES']} | Alloc {int(CONFIG['POSITION_PCT']*100)}%\n"
           f"Mode {CONFIG['MODE']} | Score≥{CONFIG['SCORE_MIN']} | Flags: {flags} | Uptime {uptime}")
    ctx.bot.send_message(chat_id=target_chat_id(update), text=msg)

def cmd_heatmap(update: Update, ctx: CallbackContext):
    syms=_ensure_universe()[:50]
    movers=[]
    for pid in syms:
        o1,h1,l1,c1,v1=cb_candles(pid,GRAN_1H,6)
        if not c1 or len(c1)<2: continue
        ch=(c1[-1]-c1[-2])/max(1e-9,c1[-2])
        movers.append((ch,pid))
    movers.sort(reverse=True)
    top = " | ".join([f"{pid} {ch*100:+.1f}%" for ch,pid in movers[:8]])
    bot = " | ".join([f"{pid} {ch*100:+.1f}%" for ch,pid in movers[-8:]])
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"🔥 Top 1h: {top}\n❄️ Bottom 1h: {bot}")

# ---------------- basics & toggles ----------------
def cmd_start(update: Update, ctx: CallbackContext):
    chat=target_chat_id(update)
    ctx.bot.send_message(chat_id=chat, parse_mode=ParseMode.HTML, text=(
        "👋 InsiderSignals (Coinbase • PAPER • Sniper)\n"
        f"Mode: <b>{CONFIG['MODE']}</b> | Score≥<b>{CONFIG['SCORE_MIN']}</b>\n"
        f"Max open: <b>{CONFIG['MAX_OPEN_TRADES']}</b> | Alloc: <b>{int(CONFIG['POSITION_PCT']*100)}%</b>\n"
        f"Universe: top-{MAX_PAIRS} USD/USDT | Keepalive: {'ON' if KEEPALIVE_URL else 'OFF'}\n\n"
        "/scan /paper /positions /pnl /stats /heatmap /journal /replay <sym>\n"
        "/setscore N /setmax N /setalloc P /setmode RELAXED|STRICT /paperreset\n"
        "Toggles: /edgeon/off /corron/off /regimeon/off /cooldownon/off /focuson/off\n"
        "         /trailon/off /newson/off /recoveron/off /mtp_on/off /sention/off /sessionon/off"
    ))

def cmd_scan(update, ctx):
    chat=target_chat_id(update)
    ctx.bot.send_message(chat_id=chat, text=f"🔍 Scan running (mode={CONFIG['MODE']}, score≥{CONFIG['SCORE_MIN']})…")
    _scan_chunk(ctx, push_to=chat)

def cmd_paper(update, ctx):
    _mark_equity()
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
                         text=(f"💼 <b>Paper</b>\nCash: ${PAPER['cash']:.2f}\nEquity: ${PAPER['equity']:.2f}\n"
                               f"Open: {len(PAPER['positions'])} | Closed: {len(PAPER['closed'])}\n"
                               f"Realized: ${PAPER['realized_pnl']:.2f} | Fees: ${PAPER['fees_paid']:.2f}"))

def cmd_positions(update, ctx):
    chat=target_chat_id(update)
    if not PAPER["positions"]:
        ctx.bot.send_message(chat_id=chat, text="No open paper positions."); return
    lines=["📊 <b>Open Positions</b>"]
    for p in PAPER["positions"].values():
        px=ticker(p["symbol"])
        if not np.isfinite(px): px=p["entry_avg"]
        pnl_usd=(px-p["entry_avg"])*p["qty_total"]-p["fees_open_total"]
        pnl_pct=(px-p["entry_avg"])/p["entry_avg"]*100
        lines.append(f"• {p['symbol']} qty {p['qty_total']:.6f} | now {px:.6f} | PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%)")
    ctx.bot.send_message(chat_id=chat, text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_pnl(update, ctx):
    w,l=PAPER["wins"],PAPER["losses"]; tot=w+l; wr=(w/tot*100) if tot else 0
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
                         text=(f"📈 <b>PnL</b>\nRealized: ${PAPER['realized_pnl']:.2f}\n"
                               f"Fees: ${PAPER['fees_paid']:.2f}\nWins: {w} | Losses: {l} | Win rate: {wr:.1f}%"))

def cmd_journal(update, ctx):
    if not PAPER["closed"]:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="No closed trades yet."); return
    lines=["📒 <b>Journal (last 20)</b>"]
    for t in PAPER["closed"][-20:]:
        lines.append(f"• {t['symbol']} {t['reason']} PnL ${t['pnl_usd']:.2f} ({t['pnl_pct']:.2f}%) @ {t['closed_at'][11:16]}")
    ctx.bot.send_message(chat_id=target_chat_id(update), text="\n".join(lines), parse_mode=ParseMode.HTML)

def cmd_replay(update, ctx):
    args=update.message.text.strip().split()
    if len(args)<2:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /replay <symbol>"); return
    sym=args[1].upper()
    items=[t for t in PAPER["closed"] if t["symbol"]==sym]
    if not items:
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"No history for {sym}."); return
    lines=[f"▶️ Replay {sym} ({len(items)} trades)"]
    for t in items[-10:]:
        lines.append(f"- {t['reason']} | entry {t['entry_avg']:.6f} → exit {t['exit_avg']:.6f} | PnL ${t['pnl_usd']:.2f}")
    ctx.bot.send_message(chat_id=target_chat_id(update), text="\n".join(lines))

# setters
def cmd_setscore(update, ctx):
    try:
        s=int(update.message.text.split()[1]); assert 1<=s<=100
        CONFIG["SCORE_MIN"]=s; save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ SCORE_MIN set to {s}.")
    except Exception: ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setscore 1..100")
def cmd_setmax(update, ctx):
    try:
        n=int(update.message.text.split()[1]); assert 1<=n<=20
        CONFIG["MAX_OPEN_TRADES"]=n; save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ MAX_OPEN_TRADES set to {n}.")
    except Exception: ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setmax 1..20")
def cmd_setalloc(update, ctx):
    try:
        p=float(update.message.text.split()[1]); assert 1<=p<=100
        CONFIG["POSITION_PCT"]=p/100.0; save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ POSITION_PCT set to {p}%.")
    except Exception: ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setalloc 1..100")
def cmd_setmode(update, ctx):
    try:
        m=update.message.text.split()[1].upper(); assert m in ("RELAXED","STRICT")
        apply_mode(m); save_config()
        ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ MODE set to {m}.")
    except Exception: ctx.bot.send_message(chat_id=target_chat_id(update), text="Usage: /setmode RELAXED|STRICT")

# toggles (13 modules)
def _toggle(flag, on, name, update, ctx):
    CONFIG[flag]=1 if on else 0; save_config()
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"{'✅' if on else '⛔️'} {name} {'ON' if on else 'OFF'}.")

def cmd_edge_on(u,c): _toggle("EDGE", True, "Historical edge", u,c)
def cmd_edge_off(u,c): _toggle("EDGE", False, "Historical edge", u,c)
def cmd_corr_on(u,c): _toggle("CORR", True, "Correlation control", u,c)
def cmd_corr_off(u,c): _toggle("CORR", False, "Correlation control", u,c)
def cmd_regime_on(u,c): _toggle("REGIME", True, "Regime switch", u,c)
def cmd_regime_off(u,c): _toggle("REGIME", False, "Regime switch", u,c)
def cmd_cool_on(u,c): _toggle("COOLDOWN", True, "Smart cooldown", u,c)
def cmd_cool_off(u,c): _toggle("COOLDOWN", False, "Smart cooldown", u,c)
def cmd_focus_on(u,c): _toggle("FOCUS", True, "Universe focus", u,c)
def cmd_focus_off(u,c): _toggle("FOCUS", False, "Universe focus", u,c)
def cmd_trail_on(u,c): _toggle("TRAIL", True, "Smarter trailing", u,c)
def cmd_trail_off(u,c): _toggle("TRAIL", False, "Smarter trailing", u,c)
def cmd_news_on(u,c): _toggle("NEWS", True, "News impact (stub)", u,c)
def cmd_news_off(u,c): _toggle("NEWS", False, "News impact (stub)", u,c)
def cmd_recov_on(u,c): _toggle("RECOVER", True, "Trade recovery", u,c)
def cmd_recov_off(u,c): _toggle("RECOVER", False, "Trade recovery", u,c)
def cmd_mtp_on(u,c): _toggle("MTP", True, "Multi-partial TP", u,c)
def cmd_mtp_off(u,c): _toggle("MTP", False, "Multi-partial TP", u,c)
def cmd_senti_on(u,c): _toggle("SENTI", True, "Sentiment proxy", u,c)
def cmd_senti_off(u,c): _toggle("SENTI", False, "Sentiment proxy", u,c)
def cmd_session_on(u,c): _toggle("SESSION", True, "Session-aware", u,c)
def cmd_session_off(u,c): _toggle("SESSION", False, "Session-aware", u,c)

# ---------------- jobs ----------------
def job_daily(ctx: CallbackContext):
    chat=target_chat_id()
    if not chat: return
    cutoff=datetime.now(tz)-timedelta(days=1)
    recent=[s for s in signals if s["time"]>=cutoff and s.get("score",0)>=CONFIG["SCORE_MIN"]]
    lines=[]
    if recent:
        lines.append("📊 <b>Daily Report</b>")
        for s in recent[-25:]:
            lines.append(f"• {s['time'].strftime('%H:%M')} {s['symbol']} LONG @ {s['entry']} | TP1 {s.get('tp1','-')} | TP2 {s.get('tp2','-')} | SL {s['sl']} | Score {s['score']}")
    else:
        lines.append("📊 Daily Report: No signals.")
    pnl,w,l,wr,pf=_pnl_slice(days=1)
    lines.append(f"— 24h: PnL ${pnl:.2f} | W{w}/L{l} | WR {wr:.1f}% | PF {pf:.2f}")
    ctx.bot.send_message(chat_id=chat, text="\n".join(lines), parse_mode=ParseMode.HTML)

def job_keepalive(ctx: CallbackContext):
    if not KEEPALIVE_URL: return
    try: session.get(KEEPALIVE_URL, timeout=10)
    except Exception: pass

def job_scan(ctx): _scan_chunk(ctx)
def job_paper(ctx): _paper_exits(ctx)

# ---------------- main ----------------
def main():
    if not TOKEN: raise RuntimeError("Missing TELEGRAM_MANUAL_TOKEN")
    up=Updater(TOKEN, use_context=True)
    up.bot.delete_webhook(drop_pending_updates=True)

    dp=up.dispatcher
    # core
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("scan", cmd_scan))
    dp.add_handler(CommandHandler("paper", cmd_paper))
    dp.add_handler(CommandHandler("positions", cmd_positions))
    dp.add_handler(CommandHandler("pnl", cmd_pnl))
    dp.add_handler(CommandHandler("stats", cmd_stats))
    dp.add_handler(CommandHandler("heatmap", cmd_heatmap))
    dp.add_handler(CommandHandler("journal", cmd_journal))
    dp.add_handler(CommandHandler("replay", cmd_replay))
    # setters
    dp.add_handler(CommandHandler("setscore", cmd_setscore))
    dp.add_handler(CommandHandler("setmax", cmd_setmax))
    dp.add_handler(CommandHandler("setalloc", cmd_setalloc))
    dp.add_handler(CommandHandler("setmode", cmd_setmode))
    dp.add_handler(CommandHandler("paperreset", lambda u,c: (PAPER.update({"cash":PAPER_START_CASH,"equity":PAPER_START_CASH,
                                                                           "positions":{}, "closed":[], "seq":1,
                                                                           "realized_pnl":0.0,"fees_paid":0.0,
                                                                           "wins":0,"losses":0}),
                                                             c.bot.send_message(chat_id=target_chat_id(u), text=f"🧹 Paper reset to ${PAPER_START_CASH:.2f}."))))
    # toggles
    dp.add_handler(CommandHandler("edgeon",   cmd_edge_on));   dp.add_handler(CommandHandler("edgeoff",  cmd_edge_off))
    dp.add_handler(CommandHandler("corron",   cmd_corr_on));   dp.add_handler(CommandHandler("corroff",  cmd_corr_off))
    dp.add_handler(CommandHandler("regimeon", cmd_regime_on)); dp.add_handler(CommandHandler("regimeoff",cmd_regime_off))
    dp.add_handler(CommandHandler("cooldownon", cmd_cool_on)); dp.add_handler(CommandHandler("cooldownoff", cmd_cool_off))
    dp.add_handler(CommandHandler("focuson", cmd_focus_on));   dp.add_handler(CommandHandler("focusoff", cmd_focus_off))
    dp.add_handler(CommandHandler("trailon", cmd_trail_on));   dp.add_handler(CommandHandler("trailoff", cmd_trail_off))
    dp.add_handler(CommandHandler("newson", cmd_news_on));     dp.add_handler(CommandHandler("newsoff", cmd_news_off))
    dp.add_handler(CommandHandler("recoveron", cmd_recov_on)); dp.add_handler(CommandHandler("recoveroff", cmd_recov_off))
    dp.add_handler(CommandHandler("mtp_on", cmd_mtp_on));      dp.add_handler(CommandHandler("mtp_off", cmd_mtp_off))
    dp.add_handler(CommandHandler("sention", cmd_senti_on));   dp.add_handler(CommandHandler("sentioff", cmd_senti_off))
    dp.add_handler(CommandHandler("sessionon", cmd_session_on)); dp.add_handler(CommandHandler("sessionoff", cmd_session_off))

    jq: JobQueue = up.job_queue
    jq.run_repeating(job_scan, interval=SCAN_INTERVAL_SECONDS, first=random.randint(6,12))
    jq.run_repeating(job_paper, interval=PAPER_CHECK_SECONDS, first=8)
    jq.run_daily(job_daily, time=dtime(hour=DAILY_REPORT_HOUR, minute=0, tzinfo=tz))
    if KEEPALIVE_URL: jq.run_repeating(job_keepalive, interval=KEEPALIVE_SECONDS, first=5)

    logging.info("Bot up. MODE=%s SCORE≥%s Top=%s Features=%s",
                 CONFIG["MODE"], CONFIG["SCORE_MIN"], MAX_PAIRS, [k for k,v in CONFIG.items() if isinstance(v,int) and v==1])
    up.start_polling(); up.idle()

if __name__=="__main__":
    main()
