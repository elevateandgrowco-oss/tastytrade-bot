"""
Tastytrade MES Futures Bot — WebSocket Edition
Real-time DXLink candle stream + limit order entries.
Strategy: Thomas Wade + PATs Trading + Day Trader Next Door + Trade Brigade
Instrument: MES (Micro E-mini S&P 500) — $5/point, tick=0.25pts

Execution improvements over polling version:
  - Bar close detected instantly via DXLink (was up to 60s late)
  - Limit orders at exact entry price (was market order with slippage)
  - Auto-cancel limit if price never returns (no stale orders)
  - Exits still use market orders for immediate fill
"""

import os, json, time, threading, asyncio, requests, websockets, yfinance as yf
import queue as _queue_mod
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

# ── Config ─────────────────────────────────────────────────────────────────────
TT_BASE_URL  = os.getenv("TT_BASE_URL", "https://api.tastytrade.com")
TT_USERNAME  = os.getenv("TT_USERNAME", "")
TT_PASSWORD  = os.getenv("TT_PASSWORD", "")
PORT         = int(os.getenv("PORT", "8080"))

TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.getenv("TWILIO_PHONE", "")
ALERT_TO     = os.getenv("ALERT_PHONE", os.getenv("OWNER_PHONE", "+14017716184"))

DATA_DIR     = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
SESSION_FILE = os.path.join(DATA_DIR, "session.json")
LOG_FILE     = os.path.join(DATA_DIR, "trades.json")

TT_SESSION_TOKEN_ENV  = os.getenv("TT_SESSION_TOKEN", "")
TWELVE_DATA_KEY       = os.getenv("TWELVE_DATA_API_KEY", "")
LIMIT_ORDER_TIMEOUT   = int(os.getenv("LIMIT_ORDER_TIMEOUT", "120"))  # secs before auto-cancel

# ── Strategy params ────────────────────────────────────────────────────────────
TICK_SIZE          = 0.25
SCALP_TICKS        = 8
MAX_BAR_TICKS      = 22
PDH_PDL_BUFFER     = 2.0
MIN_STOP_PTS       = 0.5
EMA_PERIOD         = 20
RSI_PERIOD         = 3
ATR_PERIOD         = 14
EMA_PROXIMITY_PCT  = 0.5
VOLUME_MULT        = 1.2
NEWS_BLOCK_MIN     = 15
AVOID_OPEN_MINUTES = 15
AVOID_CLOSE_MINUTES= 30
DEAD_ZONE_START    = (12, 0)
DEAD_ZONE_END      = (13, 30)
MAX_TRADES_PER_DAY = 3
MAX_CONTRACTS      = int(os.getenv("MAX_CONTRACTS", "3"))
RISK_PCT           = float(os.getenv("RISK_PCT", "1.0"))
MAX_DAILY_LOSS     = float(os.getenv("MAX_DAILY_LOSS", "150"))
MIN_ATR            = float(os.getenv("MIN_ATR", "3.0"))
TRAIL_POINTS       = float(os.getenv("TRAIL_POINTS", "2.0"))
NEWS_TIMES_ET      = [(8,30),(10,0),(14,0),(14,30)]

# ── Shared state ───────────────────────────────────────────────────────────────
_state = {"price":None,"ema20":None,"rsi3":None,"atr":None,
          "bias15m":None,"slope":None,"last_bar":None,"equity":None,
          "pdh":None,"pdl":None,"pdc":None,"stream":"connecting"}

auth = {
    "step": "idle",
    "session_token": None,
    "challenge_token": None,
    "challenge_headers": None,
    "input": None,
    "ready": threading.Event(),
    "got_input": threading.Event(),
}

class Bar:
    def __init__(self,date,o,h,l,c,v):
        self.date=date;self.open=o;self.high=h;self.low=l;self.close=c;self.volume=v

# Live bar buffers (bootstrapped from Twelve Data, then updated by DXLink)
_bars_5m   = []
_bars_15m  = []
_bars_1day = []
_bars_lock = threading.Lock()

# Queue: DXLink thread → strategy thread
_bar_queue = _queue_mod.Queue()

# Pending limit order (one at a time — we only enter one position)
_pending = {"order_id": None}
_pending_lock = threading.Lock()

# ── Utilities ──────────────────────────────────────────────────────────────────
def et_offset():
    now = datetime.now(timezone.utc); y = now.year
    mar1 = datetime(y,3,1,tzinfo=timezone.utc)
    dst_start = mar1 + timedelta(days=(6-mar1.weekday()+7)%7+7) + timedelta(hours=7)
    nov1 = datetime(y,11,1,tzinfo=timezone.utc)
    dst_end   = nov1 + timedelta(days=(6-nov1.weekday())%7)    + timedelta(hours=6)
    return -4 if dst_start <= now < dst_end else -5

def now_et():
    return datetime.now(timezone.utc) + timedelta(hours=et_offset())

def sms(msg):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, ALERT_TO]): return
    try:
        requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"From":TWILIO_FROM,"To":ALERT_TO,"Body":msg}, timeout=10)
    except Exception as e: print(f"⚠️ SMS: {e}")

# ── Historical bar bootstrap (indicator warmup) ────────────────────────────────
def _fetch_td(interval="5min", outputsize=100):
    if not TWELVE_DATA_KEY: return []
    try:
        r=requests.get("https://api.twelvedata.com/time_series",
            params={"symbol":"MES","exchange":"CME","interval":interval,
                    "outputsize":outputsize,"apikey":TWELVE_DATA_KEY,
                    "order":"ASC","timezone":"UTC"}, timeout=15)
        if r.status_code!=200:
            print(f"⚠️ TwelveData({interval}) HTTP {r.status_code}: {r.text[:200]}")
            return []
        d=r.json()
        if d.get("status")=="error":
            print(f"⚠️ TwelveData({interval}) error: {d.get('message','')}")
            return []
        return [Bar(i["datetime"],float(i["open"]),float(i["high"]),float(i["low"]),
                    float(i["close"]),int(i.get("volume",0))) for i in d.get("values",[])]
    except Exception as e: print(f"⚠️ TwelveData({interval}):{e}"); return []

def _fetch_yf(ticker, period, interval):
    try:
        df=yf.download(ticker,period=period,interval=interval,progress=False,auto_adjust=True)
        if df.empty: return []
        bars=[]
        for ts,row in df.iterrows():
            def _v(col):
                v=row[col]; return float(v.iloc[0] if hasattr(v,"iloc") else v)
            bars.append(Bar(str(ts),_v("Open"),_v("High"),_v("Low"),_v("Close"),int(_v("Volume"))))
        return bars
    except: return []

def bootstrap_bars():
    global _bars_5m, _bars_15m, _bars_1day
    print("📊 Loading historical bars...")
    b5  = _fetch_td("5min", 100)  or _fetch_yf("MES=F","2d","5m")  or _fetch_yf("ES=F","2d","5m")
    b15 = _fetch_td("15min",100)  or _fetch_yf("MES=F","5d","15m") or _fetch_yf("ES=F","5d","15m")
    b1d = _fetch_td("1day", 30)   or _fetch_yf("MES=F","60d","1d") or _fetch_yf("ES=F","60d","1d")
    with _bars_lock:
        _bars_5m   = b5[-150:]  if b5  else []
        _bars_15m  = b15[-150:] if b15 else []
        _bars_1day = b1d[-60:]  if b1d else []
    print(f"  5m:{len(_bars_5m)} 15m:{len(_bars_15m)} 1d:{len(_bars_1day)} bars loaded")

# ── Indicators ─────────────────────────────────────────────────────────────────
def ema(c,p):
    if len(c)<p: return None
    m=2/(p+1); e=sum(c[:p])/p
    for x in c[p:]: e=x*m+e*(1-m)
    return e

def ema_slope(c,p,lookback=3):
    if len(c)<p+lookback: return None
    e_now=ema(c,p); e_prev=ema(c[:-lookback],p)
    if e_now is None or e_prev is None: return None
    return e_now - e_prev

def rsi(c,p=3):
    if len(c)<p+1: return None
    g=l=0
    for i in range(len(c)-p,len(c)):
        d=c[i]-c[i-1]
        if d>0: g+=d
        else: l-=d
    ag,al=g/p,l/p
    return 100 if al==0 else 100-100/(1+ag/al)

def atr(bars,p=14):
    if len(bars)<p+1: return None
    trs=[max(bars[i].high-bars[i].low,abs(bars[i].high-bars[i-1].close),
             abs(bars[i].low-bars[i-1].close)) for i in range(1,len(bars))]
    return sum(trs[-p:])/p

def avgvol(bars,p=20):
    v=[b.volume for b in bars[-p:] if b.volume>0]; return sum(v)/len(v) if v else 0

def bias15m():
    with _bars_lock:
        bars=list(_bars_15m)
    if len(bars)<EMA_PERIOD: return None
    c=[b.close for b in bars]; e=ema(c,EMA_PERIOD)
    return "bull" if c[-1]>e else "bear"

# ── Thomas Wade strategy helpers ───────────────────────────────────────────────
def calc_entry_stop(setup_bar, price, bull):
    if bull: sp = price - (setup_bar.low  - TICK_SIZE)
    else:    sp = (setup_bar.high + TICK_SIZE) - price
    return max(MIN_STOP_PTS, round(sp, 2))

def signal_bar_too_large(bar):
    return (bar.high - bar.low) / TICK_SIZE > MAX_BAR_TICKS

def signal_bar_quality(bar, bull):
    r = bar.high - bar.low
    if r < TICK_SIZE * 2: return True
    if bull: return bar.close >= bar.low  + r * 0.67
    else:    return bar.close <= bar.high - r * 0.67

def two_bar_block(bars, bull):
    if len(bars)<2: return False
    prev,curr = bars[-2],bars[-1]
    if bull: return abs(prev.high-curr.high)<=0.50
    else:    return abs(prev.low -curr.low )<=0.50

def confirmation_candle(bars, bull):
    if len(bars)<2: return False, None
    setup,confirm = bars[-2],bars[-1]
    if bull:
        return confirm.high > setup.high, setup.high + TICK_SIZE
    else:
        return confirm.low  < setup.low,  setup.low  - TICK_SIZE

def second_entry_confirmed(bars, e20, bull):
    if len(bars)<2 or e20 is None: return True
    prev=bars[-2]; zone=e20*0.003
    return prev.low<=e20+zone if bull else prev.high>=e20-zone

def get_prev_day_levels():
    try:
        with _bars_lock: bars=list(_bars_1day)
        today=now_et().strftime("%Y-%m-%d")
        past=[b for b in bars if str(b.date)[:10]<today]
        if not past: return None,None,None
        y=past[-1]; return y.high,y.low,y.close
    except: return None,None,None

def get_session_extreme(bars, bull):
    utc_open = 13 if et_offset()==-4 else 14
    utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_open_utc = f"{utc_date} {utc_open:02d}:30"
    sb=[b for b in bars if str(b.date)[:16]>=session_open_utc]
    if not sb: return None
    return max(b.high for b in sb) if bull else min(b.low for b in sb)

def calc_runner_target(bars, price, bull, at):
    extreme=get_session_extreme(bars,bull)
    floor=(at*2) if at else 4.0
    if extreme is None: return floor
    if bull: pts=max(floor,(extreme+TICK_SIZE)-price)
    else:    pts=max(floor,price-(extreme-TICK_SIZE))
    return round(max(floor,pts),2)

# ── Market timing (DST-aware) ──────────────────────────────────────────────────
def mkt():
    et=now_et(); t=et.hour*60+et.minute
    return (9*60+35)<=t<=(15*60+55)

def avoid():
    et=now_et(); t=et.hour*60+et.minute
    return t<(9*60+30)+AVOID_OPEN_MINUTES or t>(16*60)-AVOID_CLOSE_MINUTES

def news():
    et=now_et(); t=et.hour*60+et.minute
    for h,mn in NEWS_TIMES_ET:
        if abs(t-(h*60+mn))<=NEWS_BLOCK_MIN: return True,f"{h:02d}:{mn:02d}ET"
    return False,None

def dead_zone():
    et=now_et(); t=et.hour*60+et.minute
    return (DEAD_ZONE_START[0]*60+DEAD_ZONE_START[1])<=t<=(DEAD_ZONE_END[0]*60+DEAD_ZONE_END[1])

# ── Persistence ────────────────────────────────────────────────────────────────
def load_log():
    if not os.path.exists(LOG_FILE): return {"trades":[]}
    with open(LOG_FILE) as f: return json.load(f)

def save_log(log):
    with open(LOG_FILE,"w") as f: json.dump(log,f,indent=2)

def todays_trades(log):
    t=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for x in log["trades"] if x["timestamp"].startswith(t) and x.get("order_placed"))

def todays_pnl(log):
    t=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(x.get("pnl_usd",0) for x in log["trades"] if x["timestamp"].startswith(t) and x.get("closed"))

def open_trade(log):
    for t in reversed(log["trades"]):
        if t.get("order_placed") and not t.get("closed"): return t
    return None

# ── Tastytrade REST API ────────────────────────────────────────────────────────
def get_headers():
    return {"Content-Type":"application/json","Authorization":auth["session_token"]}

def get_account():
    r=requests.get(f"{TT_BASE_URL}/customers/me/accounts",headers=get_headers()); r.raise_for_status()
    accounts=r.json()["data"]["items"]
    if not accounts: raise Exception("No accounts found")
    for a in accounts:
        if a["account"].get("margin-or-cash")=="Margin": return a["account"]["account-number"]
    return accounts[0]["account"]["account-number"]

def get_account_equity(acct):
    try:
        r=requests.get(f"{TT_BASE_URL}/accounts/{acct}/balances",headers=get_headers(),timeout=10)
        r.raise_for_status()
        data=r.json()["data"]
        eq=float(data.get("net-liquidating-value",data.get("equity-value",data.get("cash-balance",0))))
        _state["equity"]=eq; return eq
    except Exception as e: print(f"⚠️ Equity:{e}"); return 0

def calc_contracts(acct, stop_pts):
    equity=get_account_equity(acct)
    if equity<=0 or stop_pts<=0: return 1
    risk_dollars=equity*(RISK_PCT/100)
    c=min(max(1,int(risk_dollars/(stop_pts*5))),MAX_CONTRACTS)
    print(f"  💰 Equity:${equity:.0f} Risk:${risk_dollars:.0f} Stop:{stop_pts}pts → {c}ct")
    return c

def get_mes_position(acct):
    r=requests.get(f"{TT_BASE_URL}/accounts/{acct}/positions",headers=get_headers()); r.raise_for_status()
    for p in r.json()["data"]["items"]:
        if "MES" in p.get("symbol",""):
            qty=int(p.get("quantity",0))
            return qty if p.get("quantity-direction","Long")=="Long" else -qty
    return 0

def place_market_order(acct, sym, side, qty, cur_pos):
    action=("Buy to Close" if side=="Buy" else "Sell to Close") if cur_pos!=0 \
           else ("Buy to Open" if side=="Buy" else "Sell to Open")
    r=requests.post(f"{TT_BASE_URL}/accounts/{acct}/orders",
        json={"time-in-force":"Day","order-type":"Market",
              "legs":[{"instrument-type":"Future","symbol":sym,"quantity":qty,"action":action}]},
        headers=get_headers())
    if r.status_code not in (200,201): raise Exception(f"Order failed:{r.status_code} {r.text}")
    oid=r.json()["data"].get("order",{}).get("id","?")
    print(f"✅ MARKET {action} {qty} {sym} ID:{oid}"); return oid

def place_limit_order(acct, sym, side, qty, limit_price):
    """Limit order at exact entry price. Auto-cancels after LIMIT_ORDER_TIMEOUT seconds."""
    action = "Buy to Open" if side=="Buy" else "Sell to Open"
    lp = round(round(limit_price/TICK_SIZE)*TICK_SIZE, 2)
    r = requests.post(f"{TT_BASE_URL}/accounts/{acct}/orders",
        json={"time-in-force":"GTC",
              "order-type":"Limit",
              "price": str(lp),
              "price-effect": "Debit" if side=="Buy" else "Credit",
              "legs":[{"instrument-type":"Future","symbol":sym,"quantity":qty,"action":action}]},
        headers=get_headers())
    if r.status_code not in (200,201):
        raise Exception(f"Limit order failed:{r.status_code} {r.text}")
    oid = r.json()["data"]["order"]["id"]
    print(f"✅ LIMIT {action} {qty} {sym} @ {lp} ID:{oid} (cancel in {LIMIT_ORDER_TIMEOUT}s if unfilled)")

    def _auto_cancel():
        time.sleep(LIMIT_ORDER_TIMEOUT)
        with _pending_lock:
            if _pending["order_id"] != oid: return  # already filled/handled
        try:
            rc=requests.delete(f"{TT_BASE_URL}/accounts/{acct}/orders/{oid}",
                               headers=get_headers(), timeout=10)
            if rc.status_code in (200,201,204):
                print(f"⏰ Limit {oid} auto-cancelled (no fill in {LIMIT_ORDER_TIMEOUT}s)")
                sms(f"⏰ MES limit cancelled — price didn't reach {lp:.2f}")
                with _pending_lock: _pending["order_id"] = None
        except Exception as e: print(f"⚠️ Auto-cancel {oid}:{e}")
    threading.Thread(target=_auto_cancel, daemon=True).start()

    with _pending_lock: _pending["order_id"] = oid
    return oid

# ── DXLink streaming ───────────────────────────────────────────────────────────
def get_streamer_token():
    r=requests.get(f"{TT_BASE_URL}/quote-streamer-tokens",headers=get_headers(),timeout=10)
    r.raise_for_status()
    d=r.json()["data"]
    url = d.get("dxlink-url") or d.get("websocket-url") or d.get("streamer-url","")
    return d["token"], url

def get_mes_symbols():
    """Returns (order_symbol, streamer_symbol) for front-month MES."""
    r=requests.get(f"{TT_BASE_URL}/instruments/futures",params={"product-code":"MES"},
                   headers=get_headers(),timeout=10); r.raise_for_status()
    items=r.json()["data"]["items"]; today=str(datetime.now(timezone.utc).date())
    active=[i for i in items if i.get("expiration-date","9999")>=today]
    if not active: active=items
    active.sort(key=lambda x:x.get("expiration-date","9999"))
    front=active[0]
    order_sym   = front["symbol"]
    streamer_sym= front.get("streamer-symbol", order_sym)
    print(f"  MES order:{order_sym}  streamer:{streamer_sym}  exp:{front.get('expiration-date')}")
    return order_sym, streamer_sym

def _candle_sym(base, period):
    """Build DXFeed candle symbol: base{=5m,tho=true}"""
    dxf = {"5m":"5m","15m":"15m","1day":"1d"}[period]
    return f"{base}{{={dxf},tho=true}}"

async def dxlink_stream(streamer_sym):
    """DXLink WebSocket — subscribes to 5m/15m/1day candles, pushes closed bars to _bar_queue."""

    # Per-period candle state: track current candle to detect closes
    state = {p: {"time_ms": None, "candle": None} for p in ("5m","15m","1day")}

    def on_candle(evt):
        raw_sym = evt.get("eventSymbol","")
        t_ms    = evt.get("time")
        if t_ms is None: return
        try: t_ms = int(t_ms)
        except: return

        # Which period?
        period = None
        for p in ("5m","15m","1day"):
            if _candle_sym(streamer_sym, p) == raw_sym:
                period = p; break
        if period is None: return

        o=evt.get("open"); h=evt.get("high"); l=evt.get("low"); c=evt.get("close")
        v=evt.get("volume",0)
        # Skip NaN/None values (DXLink sends NaN for missing fields)
        if any(x is None for x in (o,h,l,c)): return
        try:
            o,h,l,c = float(o),float(h),float(l),float(c)
            if any(x!=x for x in (o,h,l,c)): return  # NaN check
        except: return

        s = state[period]
        if s["time_ms"] is None:
            s["time_ms"] = t_ms
            s["candle"]  = {"o":o,"h":h,"l":l,"c":c,"v":v}
            return

        if t_ms > s["time_ms"]:
            # New candle started → previous candle is COMPLETE
            prev = s["candle"]
            bar_dt = datetime.fromtimestamp(s["time_ms"]/1000, tz=timezone.utc)
            completed = Bar(bar_dt.isoformat(), prev["o"],prev["h"],prev["l"],prev["c"],int(prev["v"] or 0))
            _bar_queue.put((period, completed))
            print(f"  📊 [{period}] {bar_dt.strftime('%H:%M UTC')} "
                  f"O:{prev['o']:.2f} H:{prev['h']:.2f} L:{prev['l']:.2f} C:{prev['c']:.2f}")
            s["time_ms"] = t_ms
            s["candle"]  = {"o":o,"h":h,"l":l,"c":c,"v":v}
        else:
            # Update current in-progress candle
            s["candle"] = {"o":o,"h":h,"l":l,"c":c,"v":v}

    token, ws_url = get_streamer_token()
    if not ws_url:
        raise Exception("No WebSocket URL returned from quote-streamer-tokens")

    print(f"📡 DXLink → {ws_url}")
    field_map = {}

    async with websockets.connect(
        ws_url,
        additional_headers={"Authorization": f"Bearer {token}"},
        ping_interval=20, ping_timeout=30, close_timeout=5
    ) as ws:
        _state["stream"] = "connected"

        # ── Handshake ──────────────────────────────────────────────────────────
        # Some servers send SETUP first; others wait for client
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            first = json.loads(raw)
            if first.get("type") != "SETUP":
                print(f"  DXLink first msg: {first.get('type')}")
        except asyncio.TimeoutError:
            first = {}

        await ws.send(json.dumps({
            "type":"SETUP","channel":0,
            "keepaliveTimeout":60,"acceptKeepaliveTimeout":60,
            "version":"0.1-js/1.0.0"
        }))

        # Auth loop
        for _ in range(10):
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw)
            if msg.get("type") == "AUTH_STATE":
                if msg.get("state") == "AUTHORIZED": break
                await ws.send(json.dumps({"type":"AUTH","channel":0,"token":token}))
            elif msg.get("type") == "SETUP":
                await ws.send(json.dumps({"type":"AUTH","channel":0,"token":token}))

        print("✅ DXLink authorized")
        _state["stream"] = "authorized"

        # Open FEED channel
        await ws.send(json.dumps({
            "type":"CHANNEL_REQUEST","channel":1,
            "service":"FEED","parameters":{"contract":"AUTO"}
        }))
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type")=="CHANNEL_OPENED" and msg.get("channel")==1: break

        # Request full dict format (no compact ambiguity)
        await ws.send(json.dumps({
            "type":"FEED_SETUP","channel":1,
            "acceptAggregationPeriod":0,
            "acceptDataFormat":"FULL",
            "acceptEventFields":{
                "Candle":["eventType","eventSymbol","time","sequence","count",
                          "open","high","low","close","volume","vwap","openInterest"]
            }
        }))

        # Subscribe to 5m, 15m, 1day candles with fromTime so DXLink backfills history
        now_ms = int(time.time() * 1000)
        subs = [
            {"type":"Candle","symbol":_candle_sym(streamer_sym,"5m"),   "fromTime": now_ms - 150*5*60*1000},
            {"type":"Candle","symbol":_candle_sym(streamer_sym,"15m"),  "fromTime": now_ms - 150*15*60*1000},
            {"type":"Candle","symbol":_candle_sym(streamer_sym,"1day"), "fromTime": now_ms - 90*24*60*60*1000},
        ]
        await ws.send(json.dumps({"type":"FEED_SUBSCRIPTION","channel":1,"add":subs}))
        print(f"✅ Subscribed: {[s['symbol'] for s in subs]}")
        _state["stream"] = "live"

        # ── Event loop ─────────────────────────────────────────────────────────
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except: continue

            t = msg.get("type","")

            if t == "KEEPALIVE":
                await ws.send(json.dumps({"type":"KEEPALIVE","channel":0}))
                continue

            if t == "FEED_CONFIG":
                field_map.update(msg.get("eventFields",{}))
                continue

            if t == "ERROR":
                print(f"⚠️ DXLink error: {msg}")
                continue

            if t != "FEED_DATA":
                continue

            data = msg.get("data",[])
            if not data: continue

            # FULL format: list of event dicts
            if isinstance(data, list) and data and isinstance(data[0], dict):
                for evt in data:
                    if evt.get("eventType") == "Candle":
                        on_candle(evt)

            # COMPACT format: ["Candle", v1, v2, ..., "Candle", ...]
            elif isinstance(data, list) and data and isinstance(data[0], str):
                fields = field_map.get("Candle", [])
                if not fields: continue
                i = 0
                while i < len(data):
                    if data[i] == "Candle" and i+1+len(fields) <= len(data):
                        vals = data[i+1:i+1+len(fields)]
                        evt  = dict(zip(fields, vals))
                        evt["eventType"] = "Candle"
                        on_candle(evt)
                        i += 1 + len(fields)
                    else:
                        i += 1

async def stream_with_reconnect(streamer_sym):
    """Runs dxlink_stream forever with automatic reconnection."""
    while True:
        try:
            await dxlink_stream(streamer_sym)
        except Exception as e:
            _state["stream"] = "reconnecting"
            print(f"❌ DXLink disconnect: {e} — reconnecting in 5s")
            sms(f"⚠️ MES stream lost: {e}. Reconnecting...")
        await asyncio.sleep(5)

# ── Auth ───────────────────────────────────────────────────────────────────────
def save_session(token):
    with open(SESSION_FILE,"w") as f: json.dump({"session_token":token},f)

def load_session():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f: return json.load(f).get("session_token")
    return None

def validate_session(token):
    try:
        r=requests.get(f"{TT_BASE_URL}/customers/me",
            headers={"Content-Type":"application/json","Authorization":token},timeout=10)
        return r.status_code==200
    except: return False

def try_auto_login():
    if not TT_USERNAME or not TT_PASSWORD: return None
    try:
        r=requests.post(f"{TT_BASE_URL}/sessions",
            json={"login":TT_USERNAME,"password":TT_PASSWORD,"remember-me":True},
            headers={"Content-Type":"application/json"},timeout=15)
        if r.status_code in (200,201):
            token=r.json()["data"]["session-token"]
            save_session(token); print("✅ Auto re-login successful"); return token
    except Exception as e: print(f"⚠️ Auto re-login: {e}")
    return None

def do_login():
    headers={"Content-Type":"application/json"}
    r=requests.post(f"{TT_BASE_URL}/sessions",
        json={"login":TT_USERNAME,"password":TT_PASSWORD,"remember-me":True},headers=headers)
    if r.status_code in (200,201):
        token=r.json()["data"]["session-token"]
        save_session(token); auth["session_token"]=token; auth["step"]="done"
        print("✅ Logged in"); auth["ready"].set(); return
    if r.status_code!=403: raise Exception(f"Login failed:{r.status_code} {r.text}")
    err=r.json().get("error",{})
    if err.get("code")!="device_challenge_required": raise Exception(f"Login error:{r.text}")
    challenge_token=r.headers.get("X-Tastyworks-Challenge-Token","")
    challenge_headers={"Content-Type":"application/json","X-Tastyworks-Challenge-Token":challenge_token}
    auth["challenge_token"]=challenge_token; auth["challenge_headers"]=challenge_headers
    auth["step"]="device_code"; auth["got_input"].clear(); auth["got_input"].wait(timeout=600)
    device_code=auth["input"]; auth["input"]=None; auth["got_input"].clear()
    if not device_code: raise Exception("Timed out waiting for device code")
    r2=requests.post(f"{TT_BASE_URL}/device-challenge",json={"code":device_code},headers=challenge_headers)
    if r2.status_code not in (200,201): raise Exception(f"Device challenge failed:{r2.status_code} {r2.text}")
    r2_data=r2.json().get("data",{})
    needs_otp="X-Tastyworks-OTP" in str(r2_data.get("redirect",{}).get("required-headers",[]))
    final_headers=dict(challenge_headers)
    if needs_otp:
        auth["step"]="otp_code"; auth["got_input"].clear(); auth["got_input"].wait(timeout=300)
        otp=auth["input"]; auth["input"]=None; auth["got_input"].clear()
        if not otp: raise Exception("Timed out waiting for OTP")
        final_headers["X-Tastyworks-OTP"]=otp
    r3=requests.post(f"{TT_BASE_URL}/sessions",
        json={"login":TT_USERNAME,"password":TT_PASSWORD,"remember-me":True},headers=final_headers)
    if r3.status_code not in (200,201): raise Exception(f"Final login failed:{r3.status_code} {r3.text}")
    token=r3.json()["data"]["session-token"]
    save_session(token); auth["session_token"]=token; auth["step"]="done"
    print("✅ Fully authorized!"); auth["ready"].set()

# ── Web dashboard ──────────────────────────────────────────────────────────────
def dashboard_html(log):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades=[t for t in log.get("trades",[]) if t["timestamp"].startswith(today)]
    closed=[t for t in today_trades if t.get("closed")]
    ot=open_trade(log)
    pnl=sum(t.get("pnl_usd",0) for t in closed)
    wins=sum(1 for t in closed if t.get("pnl_usd",0)>0)
    s=_state
    sc="#22c55e" if auth["step"]=="done" else "#ef4444"
    stream_col={"live":"#22c55e","authorized":"#f59e0b","connected":"#f59e0b"}.get(s["stream"],"#ef4444")
    pc="#22c55e" if pnl>=0 else "#ef4444"
    rows=""
    for t in reversed(today_trades[-10:]):
        p=t.get("pnl_usd"); ep=t.get("price","?"); ex=t.get("exit_price","")
        pcol="#22c55e" if p and p>0 else ("#ef4444" if p and p<0 else "#94a3b8")
        pstr=f"<span style='color:{pcol}'>${p:+.2f}</span>" if p is not None else "<span style='color:#f59e0b'>Open</span>"
        rows+=f"<tr><td>{'L' if t['action']=='Buy' else 'S'}</td><td>{ep}</td><td>{ex or '—'}</td><td>{t.get('stop_pts','?')}</td><td>{pstr}</td><td>{t['timestamp'][11:16]}</td></tr>"
    open_card=""
    if ot:
        ep2=ot.get("price",0); upnl="—"
        if s["price"] and ep2:
            d=1 if ot["action"]=="Buy" else -1
            upnl=f"${(s['price']-ep2)*d*ot.get('qty',1)*5:+.2f}"
        open_card=f"""<div class="card" style="border:1px solid #f59e0b55">
        <div class="label">Open Trade</div>
        <div style="display:flex;justify-content:space-between;margin-top:10px">
          <span style="font-size:18px;font-weight:700">{'LONG' if ot['action']=='Buy' else 'SHORT'} {ot.get('qty',1)}ct @ {ep2}</span>
          <span style="font-size:18px;color:#f59e0b">{upnl}</span>
        </div>
        <div style="font-size:12px;color:#64748b;margin-top:4px">Stop:-{ot.get('stop_pts','?')}pts &nbsp; Target:+{ot.get('target_pts','?')}pts &nbsp; BE:{'Yes' if ot.get('breakeven_triggered') else 'No'} &nbsp; Partial:{'Yes' if ot.get('partial_done') else 'No'}</div>
        </div>"""
    slope_arrow="" if s["slope"] is None else ("↑" if s["slope"]>0 else "↓")
    bc="#22c55e" if s["bias15m"]=="bull" else ("#ef4444" if s["bias15m"]=="bear" else "#94a3b8")
    pdh_str=f"{s['pdh']:.2f}" if s['pdh'] else "—"
    pdl_str=f"{s['pdl']:.2f}" if s['pdl'] else "—"
    pdc_str=f"{s['pdc']:.2f}" if s['pdc'] else "—"
    return f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MES Bot</title><meta http-equiv="refresh" content="15">
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;padding:16px;max-width:600px;margin:0 auto}}
.card{{background:#1e293b;border-radius:12px;padding:16px;margin-bottom:12px}}
.g2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.g3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}}
.label{{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.05em}}
.val{{font-size:24px;font-weight:700;margin-top:4px}}
.badge{{display:inline-block;padding:4px 12px;border-radius:99px;font-size:12px;font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;padding:8px 6px;color:#64748b;border-bottom:1px solid #334155;font-size:11px;text-transform:uppercase}}
td{{padding:8px 6px;border-bottom:1px solid #1e293b}}
h1{{font-size:18px;font-weight:700;margin-bottom:14px}}</style></head><body>
<h1>📈 MES Bot — WS Edition</h1>
<div class="g2">
  <div class="card"><div class="label">Auth</div>
    <div style="margin-top:8px"><span class="badge" style="background:{sc}22;color:{sc}">{'● Live' if auth['step']=='done' else '● Offline'}</span></div></div>
  <div class="card"><div class="label">Stream</div>
    <div style="margin-top:8px"><span class="badge" style="background:{stream_col}22;color:{stream_col}">● {s['stream'].upper()}</span></div></div>
  <div class="card"><div class="label">Today P&L</div><div class="val" style="color:{pc}">${pnl:+.2f}</div></div>
  <div class="card"><div class="label">Trades</div><div class="val">{len(closed)} <span style="font-size:13px;color:#64748b">{wins}W/{len(closed)-wins}L</span></div></div>
</div>
<div class="card">
  <div class="label" style="margin-bottom:12px">Market</div>
  <div class="g3">
    <div><div class="label">Price</div><div style="font-size:20px;font-weight:600">{f"{s['price']:.2f}" if s['price'] else '—'}</div></div>
    <div><div class="label">EMA(20) {slope_arrow}</div><div style="font-size:20px;font-weight:600">{f"{s['ema20']:.2f}" if s['ema20'] else '—'}</div></div>
    <div><div class="label">RSI(3)</div><div style="font-size:20px;font-weight:600">{f"{s['rsi3']:.1f}" if s['rsi3'] else '—'}</div></div>
    <div><div class="label">ATR(14)</div><div style="font-size:20px;font-weight:600">{f"{s['atr']:.2f}" if s['atr'] else '—'}</div></div>
    <div><div class="label">15m Bias</div><div style="font-size:20px;font-weight:600;color:{bc}">{(s['bias15m'] or '—').upper()}</div></div>
    <div><div class="label">PDH/PDL</div><div style="font-size:14px;font-weight:600;margin-top:4px;color:#f59e0b">{pdh_str} / {pdl_str}</div></div>
  </div>
  <div style="font-size:11px;color:#475569;margin-top:10px">PDC:{pdc_str} &nbsp; Equity:${f"{s['equity']:,.0f}" if s['equity'] else '—'}</div>
</div>
{open_card}
<div class="card">
  <div class="label" style="margin-bottom:10px">Today's Trades</div>
  {'<table><thead><tr><th>Side</th><th>Entry</th><th>Exit</th><th>Stop</th><th>P&L</th><th>Time</th></tr></thead><tbody>'+rows+'</tbody></table>' if today_trades else '<p style="color:#64748b;font-size:13px">No trades yet today</p>'}
</div>
<p style="text-align:center;color:#334155;font-size:11px;padding:8px">Refreshes every 15s · {now_et().strftime('%I:%M %p ET')}</p>
</body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        if self.path=="/token":
            tok=auth.get("session_token") or ""
            self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers()
            self.wfile.write(tok.encode()); return
        step=auth["step"]
        if step=="done":
            html=dashboard_html(load_log())
        else:
            msg={"device_code":"<h2>Step 1 of 2: Device Verification</h2><p>Enter the SMS code Tastytrade sent:</p>",
                 "otp_code":"<h2>Step 2 of 2: Two-Factor Auth</h2><p>Enter the 2FA code:</p>"}.get(step,"<h2>MES Bot — Starting up...</h2>")
            form=f"""<form method='POST' action='/code'><input name='code' style='font-size:28px;width:160px;text-align:center' autofocus>
                <br><br><button type='submit' style='font-size:18px;padding:10px 30px'>Submit</button></form>""" if step in ("device_code","otp_code") else ""
            html=f"<html><body style='font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center'>{msg}{form}</body></html>"
        self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers()
        self.wfile.write(html.encode())
    def do_POST(self):
        n=int(self.headers.get("Content-Length",0)); body=self.rfile.read(n).decode()
        code=parse_qs(body).get("code",[""])[0].strip()
        if code: auth["input"]=code; auth["got_input"].set()
        self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers()
        self.wfile.write(b"<html><body style='font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center'><h2>Submitted!</h2><p><a href='/'>Back</a></p></body></html>")

def start_web_server():
    s=HTTPServer(("0.0.0.0",PORT),Handler)
    threading.Thread(target=s.serve_forever,daemon=True).start()
    print(f"🌐 Web server on port {PORT}")

def start_session_refresh():
    def _loop():
        while True:
            time.sleep(20*3600)
            if auth["step"]!="done": continue
            token=try_auto_login()
            if token: auth["session_token"]=token; print("✅ Session refreshed")
    threading.Thread(target=_loop,daemon=True).start()

def start_eod_summary():
    def _loop():
        while True:
            et=now_et()
            target=et.replace(hour=16,minute=5,second=0,microsecond=0)
            if et>=target: target+=timedelta(days=1)
            time.sleep((target-et).total_seconds())
            log=load_log(); today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
            closed=[t for t in log["trades"] if t["timestamp"].startswith(today) and t.get("closed")]
            pnl=sum(t.get("pnl_usd",0) for t in closed)
            wins=sum(1 for t in closed if t.get("pnl_usd",0)>0)
            sms(f"📊 MES EOD: {wins}W/{len(closed)-wins}L | P&L: ${pnl:+.2f}" if closed else "📊 MES EOD: No trades today")
    threading.Thread(target=_loop,daemon=True).start()

# ── Core trading logic ─────────────────────────────────────────────────────────
def on_bar(bars, acct, sym):
    """Called on every completed 5-minute bar. Entry via limit order, exits via market."""
    print(f"\n{'='*55}\n  BAR CLOSE {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*55}")
    if not mkt(): print("🕐 Outside market hours"); return
    if avoid(): print("⏳ Open/close buffer"); return
    nn,nl=news()
    if nn: print(f"📰 News:{nl}"); return
    if dead_zone(): print(f"😴 Dead zone (12:00–1:30 PM ET)"); return
    if len(bars)<ATR_PERIOD+EMA_PERIOD: print(f"⏳ Warming up ({len(bars)} bars)"); return

    cl=[b.close for b in bars]; price=cl[-1]
    e20=ema(cl,EMA_PERIOD); at=atr(bars,ATR_PERIOD)
    sl=ema_slope(cl,EMA_PERIOD); b15=bias15m()
    r3=rsi(cl[:-1],RSI_PERIOD) if len(cl)>1 else rsi(cl,RSI_PERIOD)  # RSI at setup bar
    av=avgvol(bars,20); lv=bars[-1].volume
    dp=abs((price-e20)/e20)*100 if e20 else 0
    pdh,pdl,pdc=get_prev_day_levels()

    _state.update({"price":price,"ema20":e20,"rsi3":r3,"atr":at,
                   "bias15m":b15,"slope":sl,"last_bar":bars[-1].date,
                   "pdh":pdh,"pdl":pdl,"pdc":pdc})

    print(f"  {price:.2f} EMA:{e20:.2f}({dp:.1f}%,{'↑' if sl and sl>0 else '↓'}) RSI:{r3:.1f} ATR:{at:.2f}"
          if all([e20,r3,at]) else f"  Price:{price:.2f}")
    if pdh: print(f"  PDH:{pdh:.2f} PDL:{pdl:.2f} PDC:{pdc:.2f}")

    cur=get_mes_position(acct)
    if cur!=0:
        log=load_log(); ot=open_trade(log)
        ep   = ot.get("price",price)   if ot else price
        osp  = ot.get("stop_pts",2.0)  if ot else 2.0
        otp  = ot.get("target_pts",4.0)if ot else 4.0
        be   = ot.get("breakeven_triggered",False) if ot else False
        partial_done = ot.get("partial_done",False) if ot else False
        trail_high   = ot.get("trail_high",ep)      if ot else ep
        qty  = ot.get("qty",abs(cur)) if ot else abs(cur)
        scalp_pts = SCALP_TICKS * TICK_SIZE  # 2.0 pts
        d=1 if cur>0 else -1; pts=(price-ep)*d; pnl=pts*qty*5
        print(f"  Open: {'L' if cur>0 else 'S'}{qty} @ {ep:.2f}  P&L:${pnl:.2f} ({pts:.2f}pts)")

        # Partial exit at 8 ticks — lock scalp, let runner go
        if not partial_done and qty>1 and pts>=scalp_pts:
            half=qty//2
            print(f"  🎯 Scalp partial: close {half}ct at +{pts:.2f}pts")
            try:
                place_market_order(acct,sym,"Sell" if cur>0 else "Buy",half,cur)
                partial_pnl=pts*half*5
                if ot:
                    ot["partial_done"]=True; ot["qty"]=qty-half
                    ot["breakeven_triggered"]=True; ot["trail_high"]=price
                    save_log(log)
                partial_done=True; be=True; qty=qty-half
                sms(f"🎯 MES SCALP +{pts:.1f}pts | {half}ct @ {price:.2f} (${partial_pnl:+.2f}) | {qty}ct running")
            except Exception as e: print(f"❌ Partial:{e}")

        # Update trailing high
        if TRAIL_POINTS>0 and be:
            new_best = max(trail_high,price) if cur>0 else min(trail_high,price)
            if new_best!=trail_high:
                if ot: ot["trail_high"]=new_best; save_log(log)
                trail_high=new_best

        # Early breakeven at 1 tick
        if not be and pts>=scalp_pts/2:
            if ot: ot["breakeven_triggered"]=True; ot["trail_high"]=price; save_log(log)
            be=True; print("  🔒 Breakeven active")

        # Effective stop level
        if be and TRAIL_POINTS>0:
            tspts=(trail_high-ep)*d-TRAIL_POINTS
            min_pts=max(0.0,tspts)
        else:
            min_pts=-osp

        ex=None
        if cur>0:
            if e20 and price<e20: ex="Below EMA"
            elif pts<min_pts: ex=f"{'TrailStop' if be else 'Stop'}({pts:.2f})"
            elif pts>=otp: ex=f"Target(+{pts:.2f})"
        else:
            if e20 and price>e20: ex="Above EMA"
            elif pts<min_pts: ex=f"{'TrailStop' if be else 'Stop'}({pts:.2f})"
            elif pts>=otp: ex=f"Target(+{pts:.2f})"

        if ex:
            print(f"🔴 CLOSE — {ex}")
            try:
                place_market_order(acct,sym,"Sell" if cur>0 else "Buy",abs(cur),cur)
                if ot: ot.update({"closed":True,"exit_price":price,"exit_reason":ex,
                                   "pnl_usd":pnl,"exit_timestamp":datetime.now(timezone.utc).isoformat()})
                save_log(log)
                emoji="✅" if pnl>=0 else "🔴"
                sms(f"{emoji} MES {ex} | {'L' if cur>0 else 'S'}{qty} @ {ep:.2f}→{price:.2f} | ${pnl:+.2f}")
            except Exception as e: print(f"❌ Close:{e}")
        else:
            stop_label=f"trail@{min_pts:.2f}pts" if be else f"-{osp:.2f}pts"
            print(f"  ✅ Hold — stop:{stop_label}  target:+{otp:.2f}  trail_high:{trail_high:.2f}")
        return

    # ── Entry checks ───────────────────────────────────────────────────────────
    # Skip if a pending limit order is open
    with _pending_lock:
        if _pending["order_id"] is not None:
            print(f"⏳ Limit order {_pending['order_id']} pending — skip"); return

    log=load_log()
    if todays_trades(log)>=MAX_TRADES_PER_DAY: print("🚫 Max trades"); return

    dpnl=todays_pnl(log)
    if dpnl<=-MAX_DAILY_LOSS:
        print(f"🛑 Daily loss limit (${dpnl:.2f})")
        sms(f"🛑 MES daily loss limit (${dpnl:.2f}). Done for today."); return

    if e20 is None or r3 is None: return
    if at and at<MIN_ATR: print(f"🚫 Chop ATR:{at:.2f}<{MIN_ATR}"); return

    bull = price > e20

    # Setup bar checks (bars[-2])
    setup_bar = bars[-2] if len(bars)>=2 else bars[-1]
    if signal_bar_too_large(setup_bar):
        print(f"🚫 Setup bar too large ({(setup_bar.high-setup_bar.low)/TICK_SIZE:.0f} ticks)"); return
    if not signal_bar_quality(setup_bar, bull):
        print(f"🚫 Poor setup bar quality"); return

    # Confirmation candle: bars[-1] broke above/below setup bar
    confirmed, entry_price = confirmation_candle(bars, bull)
    if not confirmed:
        print(f"🚫 No confirmation — {'high' if bull else 'low'} {setup_bar.high if bull else setup_bar.low:.2f} not broken"); return

    # No chasing — skip if we're already >4 ticks past entry
    chase_pts = 4 * TICK_SIZE
    if bull  and price > entry_price + chase_pts:
        print(f"🚫 Chase: price {price:.2f} is {(price-entry_price)/TICK_SIZE:.0f} ticks past entry"); return
    if not bull and price < entry_price - chase_pts:
        print(f"🚫 Chase: price {price:.2f} is {(entry_price-price)/TICK_SIZE:.0f} ticks past entry"); return

    # Two-bar matching highs/lows
    if two_bar_block(bars, bull):
        print(f"🚫 Two-bar {'highs' if bull else 'lows'} block"); return

    # EMA slope
    if sl is not None and not ((sl>0 and bull) or (sl<0 and not bull)):
        print(f"🚫 EMA slope against trade ({sl:.3f})"); return

    # Second entry (setup bar touched EMA)
    if not second_entry_confirmed(bars, e20, bull):
        print(f"🚫 Setup bar didn't touch EMA zone"); return

    # PDH/PDL avoidance
    if pdh and bull  and entry_price >= pdh - PDH_PDL_BUFFER:
        print(f"🚫 Long blocked — near PDH {pdh:.2f}"); return
    if pdl and not bull and entry_price <= pdl + PDH_PDL_BUFFER:
        print(f"🚫 Short blocked — near PDL {pdl:.2f}"); return

    # Core conditions
    ep_dist = abs((entry_price-e20)/e20)*100 if e20 else 0
    checks = ([(f"Above EMA", entry_price>e20),
               (f"Within {EMA_PROXIMITY_PCT}%", ep_dist<EMA_PROXIMITY_PCT),
               ("RSI<40", r3<40),
               ("15m bull", b15 is None or b15=="bull"),
               (f"Vol>={VOLUME_MULT}x", av==0 or lv>=av*VOLUME_MULT)]
              if bull else
              [(f"Below EMA", entry_price<e20),
               (f"Within {EMA_PROXIMITY_PCT}%", ep_dist<EMA_PROXIMITY_PCT),
               ("RSI>60", r3>60),
               ("15m bear", b15 is None or b15=="bear"),
               (f"Vol>={VOLUME_MULT}x", av==0 or lv>=av*VOLUME_MULT)])
    print(f"\n  {'LONG' if bull else 'SHORT'} checks:")
    ok=True
    for lb,ps in checks: print(f"  {'✅' if ps else '🚫'} {lb}"); ok=ok and ps
    if not ok: print("🚫 BLOCKED"); return

    # Stops + targets
    sp  = calc_entry_stop(setup_bar, entry_price, bull)
    tp  = calc_runner_target(bars, entry_price, bull, at)
    qty = calc_contracts(acct, sp)

    print(f"\n✅ {'LONG' if bull else 'SHORT'} {qty}ct — LIMIT @ {entry_price:.2f}")
    print(f"   Stop: -{sp:.2f}pts (1 tick beyond setup bar {'low' if bull else 'high'} {setup_bar.low if bull else setup_bar.high:.2f})")
    print(f"   Scalp: {SCALP_TICKS} ticks ({SCALP_TICKS*TICK_SIZE:.2f}pts)  Runner: {tp:.2f}pts")
    print(f"   Limit auto-cancels in {LIMIT_ORDER_TIMEOUT}s if unfilled")
    side = "Buy" if bull else "Sell"
    try:
        oid = place_limit_order(acct, sym, side, qty, entry_price)
        log["trades"].append({"timestamp":datetime.now(timezone.utc).isoformat(),"action":side,
            "symbol":sym,"qty":qty,"price":entry_price,"ema20":round(e20,2),"rsi3":round(r3,2),
            "atr":round(at,2) if at else None,"bias_15m":b15,
            "ema_slope":round(sl,4) if sl else None,"stop_pts":round(sp,2),"target_pts":round(tp,2),
            "breakeven_triggered":False,"partial_done":False,"trail_high":entry_price,
            "closed":False,"order_id":oid,"order_placed":True,"order_type":"limit"})
        save_log(log)
        sms(f"{'📈' if side=='Buy' else '📉'} MES {'LONG' if bull else 'SHORT'} LIMIT {qty}ct @ {entry_price:.2f} | Stop:{sp:.1f}pts Scalp:{SCALP_TICKS}t Runner:{tp:.1f}pts")
    except Exception as e: print(f"❌ Limit order:{e}")

# ── Strategy processor thread ──────────────────────────────────────────────────
def strategy_loop(order_sym, acct):
    """Reads completed bars from queue and runs on_bar() for 5m bars."""
    print("🧠 Strategy processor started")
    while True:
        try:
            period, bar = _bar_queue.get(timeout=2)
        except _queue_mod.Empty:
            continue
        with _bars_lock:
            if period == "5m":
                _bars_5m.append(bar)
                if len(_bars_5m)>200: _bars_5m.pop(0)
                snapshot = list(_bars_5m)
            elif period == "15m":
                _bars_15m.append(bar)
                if len(_bars_15m)>200: _bars_15m.pop(0)
                continue  # 15m bars update bias, don't trigger strategy
            elif period == "1day":
                _bars_1day.append(bar)
                if len(_bars_1day)>60: _bars_1day.pop(0)
                continue
            else:
                continue
        # Skip strategy on historical warmup bars (older than 2 bar periods)
        try:
            bar_ts = datetime.fromisoformat(bar.date.replace("Z","+00:00"))
            if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
        except Exception:
            bar_ts = datetime.now(timezone.utc)
        if bar_ts < datetime.now(timezone.utc) - timedelta(minutes=11):
            continue  # historical bar — accumulated for warmup, strategy skipped
        try:
            on_bar(snapshot, acct, order_sym)
        except Exception as e:
            print(f"❌ Strategy error: {e}")
            if "401" in str(e) or "unauthorized" in str(e).lower():
                token=try_auto_login()
                if token:
                    auth["session_token"]=token; auth["step"]="done"
                    sms("🔄 MES: session refreshed after 401")

# ── Main ───────────────────────────────────────────────────────────────────────
def do_auth():
    # Try env var token first (most up-to-date), then saved file
    for candidate in [TT_SESSION_TOKEN_ENV, load_session()]:
        if not candidate: continue
        print("🔍 Validating session...")
        if validate_session(candidate):
            auth["session_token"]=candidate; auth["step"]="done"; auth["ready"].set()
            print("✅ Session valid"); return
    print("⚠️ No valid session — auto re-login...")
    token=try_auto_login()
    if token:
        auth["session_token"]=token; auth["step"]="done"; auth["ready"].set(); return
    print(f"\n🔐 Visit: https://tastytrade-bot-production.up.railway.app")
    while True:
        try: do_login(); return
        except Exception as e: print(f"❌ Login:{e}"); time.sleep(300)

def main():
    print("="*55)
    print(f"  MES Bot — WebSocket Edition")
    print(f"  Strategy: Thomas Wade + PATs + DTND + Trade Brigade")
    print(f"  Entries: LIMIT orders  |  Exits: MARKET orders")
    print(f"  Risk:{RISK_PCT}%  MaxLoss:${MAX_DAILY_LOSS}  LimitTimeout:{LIMIT_ORDER_TIMEOUT}s")
    print("="*55)

    start_web_server()
    start_session_refresh()
    start_eod_summary()

    do_auth()
    auth["ready"].wait()

    acct=None
    while not acct:
        try: acct=get_account(); print(f"✅ Account:{acct}")
        except Exception as e: print(f"❌ Account:{e}"); time.sleep(30)

    order_sym=None; streamer_sym=None
    while not order_sym:
        try: order_sym, streamer_sym = get_mes_symbols()
        except Exception as e: print(f"❌ Symbol:{e}"); time.sleep(30)

    bootstrap_bars()

    # Start strategy processor thread
    t=threading.Thread(target=strategy_loop, args=(order_sym,acct), daemon=True)
    t.start()

    # Run DXLink websocket in asyncio (main thread)
    print(f"\n📡 Starting DXLink stream for {streamer_sym}...\n")
    try:
        asyncio.run(stream_with_reconnect(streamer_sym))
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")

if __name__=="__main__":
    main()
