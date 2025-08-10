# manual_bot.py — Coinbase • PAPER trading • €300 Sniper-Safe
# v3.2 (2025-08-10)
# - STRICT, SCORE≥97, Top-50 USD/USDT, 5% alloc, max 5 open
# - Real-time paper engine (TP1 partial, BE trail, TP2 full, gap-safe)
# - Historical “quick validator”, correlation, regime, session-quiet guard
# - 1h→4h resample fallback if native 4h missing
# - Token read ONLY from env: TELEGRAM_MANUAL_TOKEN

import os, time, math, random, logging, threading, http.server, socketserver, json
from datetime import datetime, timedelta, time as dtime

import pytz, requests, numpy as np
from requests.exceptions import RequestException
from http.client import RemoteDisconnected

from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue

# ---------------- tiny HTTP server (Render keep-alive) ----------------
PORT = int(os.getenv("PORT", "10000"))
class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def _keepalive():
    with socketserver.TCPServer(("", PORT), _Handler) as httpd:
        httpd.serve_forever()
threading.Thread(target=_keepalive, daemon=True).start()

# ---------------- env & sniper defaults ----------------
TOKEN = os.getenv("TELEGRAM_MANUAL_TOKEN")
if not TOKEN or not TOKEN.strip():
    raise RuntimeError("Missing TELEGRAM_MANUAL_TOKEN in Environment. Set it in Render → Environment tab.")

TIMEZONE           = os.getenv("TIMEZONE", "Europe/Dublin")
DAILY_REPORT_HOUR  = int(os.getenv("DAILY_REPORT_HOUR", "20"))
FORCED_CHAT_ID     = os.getenv("CHAT_ID")
tz = pytz.timezone(TIMEZONE)

# cadence
SCAN_INTERVAL_SECONDS   = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
SYMBOLS_PER_SCAN        = int(os.getenv("SYMBOLS_PER_SCAN", "25"))
SIGNAL_COOLDOWN_MIN     = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "8"))

# universe cap (sniper = 50)
MAX_PAIRS               = int(os.getenv("MAX_PAIRS", "50"))

# mode & score (sniper = STRICT, 97)
SCORE_MIN               = int(os.getenv("SCORE_MIN", "97"))
MODE                    = os.getenv("MODE", "STRICT").strip().upper()

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
PAPER_START_CASH        = float(os.getenv("PAPER_START_CASH", "300"))
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
session.headers.update({"User-Agent": "insider-sniper/3.2"})

# ---------------- state & persistence ----------------
CONFIG_PATH = "config.json"
CONFIG = {
    "MODE": MODE, "SCORE_MIN": SCORE_MIN,
    "POSITION_PCT": POSITION_PCT, "MAX_OPEN_TRADES": MAX_OPEN_TRADES,
    # Feature flags — ON by default
    "EDGE": 1, "CORR": 1, "REGIME": 1, "COOLDOWN": 1, "FOCUS": 1,
    "TRAIL": 1, "NEWS": 1, "MTP": 1, "SENTI": 1, "SESSION": 1
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
    if arr is None: return None
    arr=np.array(arr,float)
    if len(arr)<p: return None
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
        # de-dupe timestamps
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

# ---------------- helpers ----------------
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

# ---------------- regime/sentiment/news stubs ----------------
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
        if not c5 or len(c5)<200:
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

# ---------------- cooldown guard ----------------
def _cooldown_active():
    if not CONFIG.get("COOLDOWN",1): return False
    return time.time() < STREAK.get("cooldown_until", 0)

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

# ---------------- scoring & signal ----------------
def score_signal(price, ema50_now, atr5, vol_ok, rsi_val, adx_val):
    s=0
    if vol_ok: s+=25
    if adx_val is not None and not math.isnan(adx_val): s+=min(25,max(0,(adx_val-10)*2))
    if ema50_now:
        dist=abs(price-ema50_now)/ema50_now
        s+=max(0,25-100*dist)
    if rsi_val is not None and not math.isnan(rsi_val): s+=max(0,25-abs(50-rsi_val))
    return int(min(100,s))

def build_signal(pid):
    if _cooldown_active(): return None
    if CONFIG.get("NEWS",1) and _news_block(): return None

    # sentiment / breadth
    breadth=_market_breadth() if CONFIG.get("SENTI",1) else 0.6
    if breadth < 0.45: return None

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

    # regime + session tweaks
    if CONFIG.get("REGIME",1):
        _update_regime()
        if BTC_STATE["adx1h"] < 18 and ETH_STATE["adx1h"] < 18:
            sc -= 2
    now_hour = datetime.now(tz).hour
    qf, qt = SESSION_QUIET_FROM, SESSION_QUIET_TO
    quiet = (qf <= now_hour <= qt) if qf <= qt else not (qt < now_hour < qf)
    if CONFIG.get("SESSION",1) and quiet:
        sc -= 2

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

# ---------------- paper engine ----------------
def _can_open_more_trades(): return len(PAPER["positions"]) < CONFIG["MAX_OPEN_TRADES"]
def _free_usd_balance(): return float(PAPER["cash"])

def _execute_long(sig, ctx: CallbackContext, chat_id):
    if not _can_open_more_trades():
        ctx.bot.send_message(chat_id=chat_id, text="⚪ Skipped: MAX_OPEN_TRADES reached"); return
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
        "qty_total": qty, "qty_tp1": qty * 0.5, "qty_runner": qty * 0.5,
        "entry_avg": entry, "tp1": sig["tp1"], "tp2": sig["tp2"],
        "sl": sig["sl"], "sl_dyn": sig["sl"], "tp1_done": False,
        "time": datetime.now(tz).isoformat(), "fees_open_total": fee_open, "fees_close_total": 0.0
    }
    PAPER["positions"][pos_id] = pos
    _save_json("paper.json", PAPER)
    ctx.bot.send_message(chat_id=chat_id, text=f"🟢 PAPER BUY {sig['symbol']} ${alloc:.2f} | entry {entry} | qty {qty:.6f}")

def _paper_check_exits(ctx: CallbackContext):
    if not PAPER["positions"]: return
    to_remove=[]
    for pid, p in list(PAPER["positions"].items()):
        cur = ticker(p["symbol"])
        if not np.isfinite(cur): continue

        # TP2 → close everything (gap-safe)
        if cur >= p["tp2"]:
            exit_price = p["tp2"]
            proceeds = exit_price * p["qty_total"]
            fee_close = proceeds * FEE_RATE
            PAPER["cash"] += (proceeds - fee_close)
            PAPER["fees_paid"] += fee_close
            total_fees = p["fees_open_total"] + fee_close
            pnl_usd = (exit_price - p["entry_avg"]) * p["qty_total"] - total_fees
            pnl_pct = (exit_price - p["entry_avg"]) / p["entry_avg"] * 100 if p["entry_avg"] else 0
            PAPER["realized_pnl"] += pnl_usd; PAPER["wins"] += 1 if pnl_usd>=0 else 0; PAPER["losses"] += 1 if pnl_usd<0 else 0
            PAPER["closed"].append({
                "id": pid, "symbol": p["symbol"], "entry_avg": p["entry_avg"],
                "exit_avg": exit_price, "qty_total": p["qty_total"],
                "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "reason": "TP2",
                "closed_at": datetime.now(tz).isoformat()
            })
            to_remove.append(pid)
            ctx.bot.send_message(chat_id=target_chat_id(), text=f"🎯 <b>TP2 hit</b> {p['symbol']} @ {exit_price:.6f} | PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%)", parse_mode=ParseMode.HTML)
            continue

        # TP1 partial → trail to BE if enabled
        if (not p["tp1_done"]) and cur >= p["tp1"]:
            exit_value = p["tp1"] * p["qty_tp1"]
            fee_close = exit_value * FEE_RATE
            PAPER["cash"] += (exit_value - fee_close)
            PAPER["fees_paid"] += fee_close
            p["fees_close_total"] += fee_close
            p["tp1_done"] = True
            if TRAIL_AFTER_TP1: p["sl_dyn"] = max(p["sl_dyn"], p["entry_avg"])
            ctx.bot.send_message(chat_id=target_chat_id(), text=f"🥳 <b>TP1 partial</b> {p['symbol']} sold {p['qty_tp1']:.6f} @ {p['tp1']:.6f}", parse_mode=ParseMode.HTML)

        # SL for the runner
        if cur <= p["sl_dyn"]:
            runner_val = p["sl_dyn"] * p["qty_runner"]
            fee_close = runner_val * FEE_RATE
            PAPER["cash"] += (runner_val - fee_close)
            PAPER["fees_paid"] += fee_close
            total_fees = p["fees_open_total"] + p["fees_close_total"] + fee_close
            tp1_part = (p["tp1"] * p["qty_tp1"]) if p["tp1_done"] else 0.0
            avg_exit = (tp1_part + p["sl_dyn"] * p["qty_runner"]) / p["qty_total"]
            pnl_usd = (avg_exit - p["entry_avg"]) * p["qty_total"] - total_fees
            pnl_pct = (avg_exit - p["entry_avg"]) / p["entry_avg"] * 100 if p["entry_avg"] else 0
            PAPER["realized_pnl"] += pnl_usd; PAPER["wins"] += 1 if pnl_usd>=0 else 0; PAPER["losses"] += 1 if pnl_usd<0 else 0
            PAPER["closed"].append({
                "id": pid, "symbol": p["symbol"], "entry_avg": p["entry_avg"],
                "exit_avg": avg_exit, "qty_total": p["qty_total"],
                "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "reason": "SL",
                "closed_at": datetime.now(tz).isoformat()
            })
            to_remove.append(pid)
            ctx.bot.send_message(chat_id=target_chat_id(), text=f"🛑 <b>SL</b> {p['symbol']} avg exit {avg_exit:.6f} | PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%)", parse_mode=ParseMode.HTML)

    for pid in to_remove:
        PAPER["positions"].pop(pid, None)
    if to_remove: _save_json("paper.json", PAPER)

def _update_equity_mark():
    eq = PAPER["cash"]
    for p in PAPER["positions"].values():
        cur = ticker(p["symbol"])
        if np.isfinite(cur):
            remaining = p["qty_runner"] + (0 if p["tp1_done"] else p["qty_tp1"])
            eq += cur * remaining * (1 - FEE_RATE)
    PAPER["equity"] = eq
    _save_json("paper.json", PAPER)

# ---------------- scan loop ----------------
def _ensure_universe():
    global watchlist
    if not watchlist:
        base = get_pairs_top(limit=200)    # load many
        watchlist = focus_universe(base)   # focus to MAX_PAIRS
        if len(watchlist) > MAX_PAIRS:
            watchlist = watchlist[:MAX_PAIRS]
        logging.info(f"Watchlist: {len(watchlist)} pairs.")
    return watchlist

def _scan_chunk(ctx: CallbackContext, push_to=None):
    global scan_cursor
    chat_id = push_to or target_chat_id()
    if not chat_id: return
    now = datetime.now(tz)
    syms = _ensure_universe()
    if not syms: return

    start = scan_cursor
    end = min(start + SYMBOLS_PER_SCAN, len(syms))
    chunk = syms[start:end]
    scan_cursor = 0 if end >= len(syms) else end

    hits = 0
    open_syms = [p["symbol"] for p in PAPER["positions"].values()]
    for pid in chunk:
        lt = last_signal_time.get(pid)
        if lt and (now - lt) < timedelta(minutes=SIGNAL_COOLDOWN_MIN): continue

        # correlation guard
        corr_factor = _corr_block(pid, set(open_syms))
        if corr_factor == 0.0: continue

        sig = build_signal(pid)
        if not sig: 
            time.sleep(0.05); continue

        last_signal_time[pid] = now
        signals.append({"time": now, **sig})

        # score threshold (already filtered) + send
        ctx.bot.send_message(chat_id=chat_id, text=fmt(sig), parse_mode=ParseMode.HTML)

        # Paper execution with correlation scaling
        if corr_factor < 1.0:
            # temporarily scale alloc by factor (simple)
            old = CONFIG["POSITION_PCT"]
            CONFIG["POSITION_PCT"] = max(0.01, old * corr_factor)
            _execute_long(sig, ctx, chat_id)
            CONFIG["POSITION_PCT"] = old
        else:
            _execute_long(sig, ctx, chat_id)

        hits += 1
        time.sleep(0.1)

    if hits == 0 and push_to:
        ctx.bot.send_message(chat_id=push_to, text=f"(No valid setups ≥ {CONFIG['SCORE_MIN']} right now — watching.)")

# ---------------- commands ----------------
def cmd_start(update: Update, ctx: CallbackContext):
    me = ctx.bot.get_me()
    ctx.bot.send_message(
        chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
        text=(f"👋 <b>InsiderSignals_Manual</b> @{me.username}\n"
              f"Mode: <b>{CONFIG['MODE']}</b> | SCORE_MIN <b>{CONFIG['SCORE_MIN']}</b>\n"
              f"Alloc: <b>{int(CONFIG['POSITION_PCT']*100)}%</b> | Max open: <b>{CONFIG['MAX_OPEN_TRADES']}</b>\n"
              f"Paper cash: ${PAPER['cash']:.2f}\n"
              "Commands: /ping /scan /paper /positions /closed /pnl /paperreset\n"
              "/setscore N /setmode RELAXED|STRICT /setmax N /setalloc P")
    )

def cmd_ping(update: Update, ctx: CallbackContext):
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"✅ Bot alive: {now} {TIMEZONE}")

def cmd_scan(update: Update, ctx: CallbackContext):
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"🔍 Scan (mode={CONFIG['MODE']}, score≥{CONFIG['SCORE_MIN']})…")
    _scan_chunk(ctx, push_to=target_chat_id(update))

def cmd_paper(update: Update, ctx: CallbackContext):
    _update_equity_mark()
    realized = PAPER["realized_pnl"]; fees = PAPER["fees_paid"]
    ctx.bot.send_message(chat_id=target_chat_id(update), parse_mode=ParseMode.HTML,
        text=(f"💼 <b>Paper</b>\nCash: ${PAPER['cash']:.2f}\nEquity: ${PAPER['equity']:.2f}\n"
              f"Open: {len(PAPER['positions'])} | Closed: {len(PAPER['closed'])}\n"
              f"Realized: ${realized:.2f} | Fees: ${fees:.2f}"))

def cmd_positions(update: Update, ctx: CallbackContext):
    if not PAPER["positions"]:
        ctx.bot.send_message(chat_id=target_chat_id(update), text="No open paper positions."); return
    lines = ["📊 <b>Open Positions</b>"]
    for pid, p in PAPER["positions"].items():
        cur = ticker(p["symbol"])
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
    _save_json("paper.json", PAPER)
    ctx.bot.send_message(chat_id=target_chat_id(update), text=f"🧹 Paper reset to ${PAPER_START_CASH:.2f}.")

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

# ---------------- jobs ----------------
def job_scan(ctx: CallbackContext): _scan_chunk(ctx)
def job_paper(ctx: CallbackContext):
    _paper_check_exits(ctx); _update_equity_mark()

def job_daily(ctx: CallbackContext):
    chat_id = target_chat_id()
    if not chat_id: return
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff and s.get("score",0) >= CONFIG["SCORE_MIN"]]
    msg = ("📊 Daily Report (24h): No signals." if not recent else
           "📊 <b>Daily Report</b>\n" + "\n".join(
               f"• {s['time'].strftime('%H:%M')} {s['symbol']} {s['side']} @ {s['entry']} | TP1 {s['tp1']} | TP2 {s['tp2']} | SL {s['sl']} | Score {s.get('score','-')}"
               for s in recent[-20:]))
    ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

def job_keepalive(ctx: CallbackContext):
    if not KEEPALIVE_URL: return
    try: session.get(KEEPALIVE_URL, timeout=10)
    except Exception as e: logging.warning(f"Keep-alive ping failed: {e}")

# ---------------- main ----------------
def main():
    updater = Updater(TOKEN, use_context=True)
    updater.bot.delete_webhook(drop_pending_updates=True)
    me = updater.bot.get_me()
    logging.info(f"Bot logged in as @{me.username} (id={me.id})")

    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("scan", cmd_scan))
    dp.add_handler(CommandHandler("paper", cmd_paper))
    dp.add_handler(CommandHandler("positions", cmd_positions))
    dp.add_handler(CommandHandler("closed", cmd_closed))
    dp.add_handler(CommandHandler("pnl", cmd_pnl := lambda u,c: c.bot.send_message(
        chat_id=target_chat_id(u),
        text=f"📈 Realized: ${PAPER['realized_pnl']:.2f} | Fees: ${PAPER['fees_paid']:.2f} | Wins: {PAPER['wins']} | Losses: {PAPER['losses']}",
        parse_mode=ParseMode.HTML
    )))
    dp.add_handler(CommandHandler("paperreset", cmd_paperreset))
    dp.add_handler(CommandHandler("setscore", cmd_setscore))
    dp.add_handler(CommandHandler("setmode", cmd_setmode))
    dp.add_handler(CommandHandler("setmax", cmd_setmax))
    dp.add_handler(CommandHandler("setalloc", cmd_setalloc))

    jq: JobQueue = updater.job_queue
    jq.run_repeating(job_scan, interval=SCAN_INTERVAL_SECONDS, first=random.randint(6,12))
    jq.run_repeating(job_paper, interval=PAPER_CHECK_SECONDS, first=8)
    jq.run_daily(job_daily, time=dtime(hour=DAILY_REPORT_HOUR, minute=0, tzinfo=tz))
    if KEEPALIVE_URL:
        jq.run_repeating(job_keepalive, interval=KEEPALIVE_SECONDS, first=5)

    updater.start_polling()
    logging.info("Bot polling started.")
    updater.idle()

if __name__ == "__main__":
    main()

