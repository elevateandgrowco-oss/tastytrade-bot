"""
Tastytrade MES Futures Bot
Strategy: Thomas Wade 4-Channel Price Action + TJR ICT Concepts
Instrument: MES (Micro E-mini S&P 500) — $5/point

First run: visit Railway URL, enter SMS codes when prompted (2 codes).
After that: trades automatically.
"""

import os, json, time, threading, requests, yfinance as yf
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

TT_SESSION_TOKEN_ENV = os.getenv("TT_SESSION_TOKEN", "")
TWELVE_DATA_KEY      = os.getenv("TWELVE_DATA_API_KEY", "")

# Strategy params
MAX_TRADES_PER_DAY = 3
MAX_CONTRACTS      = int(os.getenv("MAX_CONTRACTS", "3"))
RISK_PCT           = float(os.getenv("RISK_PCT", "1.0"))   # % of equity to risk per trade
EMA_PERIOD         = 20; RSI_PERIOD = 3; ATR_PERIOD = 14
ATR_STOP_MULT      = 1.5; MIN_STOP_POINTS = 2.0; MAX_STOP_POINTS = 10.0
EMA_PROXIMITY_PCT  = 1.5; VOLUME_MULT = 1.2; NEWS_BLOCK_MIN = 15
AVOID_OPEN_MINUTES = 15; AVOID_CLOSE_MINUTES = 30
MAX_DAILY_LOSS     = float(os.getenv("MAX_DAILY_LOSS", "150"))
MIN_ATR            = float(os.getenv("MIN_ATR", "3.0"))
TRAIL_POINTS       = float(os.getenv("TRAIL_POINTS", "3.0"))
NEWS_TIMES_ET      = [(8,30),(10,0),(14,0),(14,30)]

# ── Shared state (dashboard reads this) ───────────────────────────────────────
_state = {"price":None,"ema20":None,"rsi3":None,"atr":None,"vwap":None,
          "bias15m":None,"slope":None,"last_bar":None,"equity":None}

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

_bar_cache = {"5m":{"bars":[],"ts":0},"15m":{"bars":[],"ts":0}}

# ── Utilities ──────────────────────────────────────────────────────────────────
def et_offset():
    """DST-aware UTC→ET offset: -4 (EDT) or -5 (EST)."""
    now = datetime.now(timezone.utc)
    y = now.year
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

# ── Market data ────────────────────────────────────────────────────────────────
def fetch_bars_twelvedata(interval="5min", outputsize=100):
    if not TWELVE_DATA_KEY: return []
    try:
        r=requests.get("https://api.twelvedata.com/time_series",
            params={"symbol":"MES","exchange":"CME","interval":interval,
                    "outputsize":outputsize,"apikey":TWELVE_DATA_KEY,"order":"ASC"}, timeout=15)
        if r.status_code!=200: print(f"⚠️ TwelveData:{r.status_code}"); return []
        data=r.json()
        if data.get("status")=="error": print(f"⚠️ TwelveData:{data.get('message')}"); return []
        bars=[Bar(i["datetime"],float(i["open"]),float(i["high"]),float(i["low"]),
                  float(i["close"]),int(i.get("volume",0))) for i in data.get("values",[])]
        print(f"  📊 TwelveData({interval}): {len(bars)} bars"); return bars
    except Exception as e: print(f"⚠️ TwelveData:{e}"); return []

def fetch_bars(sym=None, interval="5m", period="2d"):
    key = "15m" if interval=="15m" else "5m"
    ttl = 270 if key=="5m" else 600
    cache = _bar_cache[key]
    if cache["bars"] and time.time()-cache["ts"]<ttl:
        return cache["bars"]
    td_interval = "15min" if interval=="15m" else "5min"
    td_size = 150 if interval=="15m" else 100
    bars = fetch_bars_twelvedata(td_interval, td_size)
    if not bars:
        try:
            df=yf.download("MES=F",period=period,interval=interval,progress=False,auto_adjust=True)
            if df.empty: df=yf.download("ES=F",period=period,interval=interval,progress=False,auto_adjust=True)
            if not df.empty:
                for ts,row in df.iterrows():
                    def _v(col):
                        v=row[col]; return float(v.iloc[0] if hasattr(v,"iloc") else v)
                    bars.append(Bar(str(ts),_v("Open"),_v("High"),_v("Low"),_v("Close"),int(_v("Volume"))))
        except Exception as e: print(f"⚠️ yfinance:{e}")
    if bars: _bar_cache[key]={"bars":bars,"ts":time.time()}
    elif cache["bars"]: print("  ↩️ Using cached bars"); return cache["bars"]
    return bars

# ── Indicators ─────────────────────────────────────────────────────────────────
def ema(c,p):
    if len(c)<p: return None
    m=2/(p+1); e=sum(c[:p])/p
    for x in c[p:]: e=x*m+e*(1-m)
    return e

def ema_slope(c,p,lookback=3):
    """Positive = EMA rising, negative = falling."""
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

def session_vwap(bars):
    """VWAP anchored to today's 9:30 AM ET — resets each day, DST-aware."""
    et_date = now_et().strftime("%Y-%m-%d")
    session_open = et_date + " 09:30"
    session_bars = [b for b in bars if str(b.date)[:16] >= session_open]
    if not session_bars: return None
    cv=cv2=0
    for b in session_bars: tp=(b.high+b.low+b.close)/3; cv+=tp*b.volume; cv2+=b.volume
    return cv/cv2 if cv2 else None

def avgvol(bars,p=20):
    v=[b.volume for b in bars[-p:] if b.volume>0]; return sum(v)/len(v) if v else 0

def bias15m():
    try:
        bars=fetch_bars(interval="15m",period="5d")
        if len(bars)<EMA_PERIOD: return None
        c=[b.close for b in bars]; e=ema(c,EMA_PERIOD)
        return "bull" if c[-1]>e else "bear"
    except: return None

def second_entry_confirmed(bars, e20, bull):
    """Thomas Wade second entry: prev candle must have touched the EMA zone."""
    if len(bars)<2 or e20 is None: return True  # soft pass if not enough data
    prev=bars[-2]
    zone=e20*0.003  # 0.3% of EMA counts as a touch
    if bull:   return prev.low  <= e20 + zone
    else:      return prev.high >= e20 - zone

# ── Market timing ──────────────────────────────────────────────────────────────
def mkt():
    m=datetime.now(timezone.utc); t=m.hour*60+m.minute
    return (13*60+35)<=t<=(19*60+55)

def avoid():
    m=datetime.now(timezone.utc); t=m.hour*60+m.minute
    return t<(13*60+30)+AVOID_OPEN_MINUTES or t>(20*60)-AVOID_CLOSE_MINUTES

def news():
    et=now_et(); t=et.hour*60+et.minute
    for h,mn in NEWS_TIMES_ET:
        if abs(t-(h*60+mn))<=NEWS_BLOCK_MIN: return True,f"{h:02d}:{mn:02d}ET"
    return False,None

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

# ── Tastytrade API ─────────────────────────────────────────────────────────────
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
    """Risk RISK_PCT% of account equity per trade, capped at MAX_CONTRACTS."""
    equity=get_account_equity(acct)
    if equity<=0 or stop_pts<=0: return 1
    risk_dollars=equity*(RISK_PCT/100)
    contracts=max(1,int(risk_dollars/(stop_pts*5)))
    c=min(contracts,MAX_CONTRACTS)
    print(f"  💰 Equity:${equity:.0f} Risk:${risk_dollars:.0f} → {c} contract(s)")
    return c

def get_mes_position(acct):
    r=requests.get(f"{TT_BASE_URL}/accounts/{acct}/positions",headers=get_headers()); r.raise_for_status()
    for p in r.json()["data"]["items"]:
        if "MES" in p.get("symbol",""):
            qty=int(p.get("quantity",0))
            return qty if p.get("quantity-direction","Long")=="Long" else -qty
    return 0

def get_mes_symbol():
    r=requests.get(f"{TT_BASE_URL}/instruments/futures",params={"product-code":"MES"},
                   headers=get_headers()); r.raise_for_status()
    items=r.json()["data"]["items"]; today=str(datetime.now(timezone.utc).date())
    active=[i for i in items if i.get("expiration-date","9999")>=today]
    if not active: active=items
    active.sort(key=lambda x:x.get("expiration-date","9999"))
    sym=active[0]["symbol"]; print(f"  MES:{sym} exp:{active[0].get('expiration-date')}"); return sym

def place_order(acct,sym,side,qty,cur_pos):
    action=("Buy to Close" if side=="Buy" else "Sell to Close") if cur_pos!=0 \
           else ("Buy to Open" if side=="Buy" else "Sell to Open")
    r=requests.post(f"{TT_BASE_URL}/accounts/{acct}/orders",
        json={"time-in-force":"Day","order-type":"Market",
              "legs":[{"instrument-type":"Future","symbol":sym,"quantity":qty,"action":action}]},
        headers=get_headers())
    if r.status_code not in (200,201): raise Exception(f"Order failed:{r.status_code} {r.text}")
    oid=r.json()["data"].get("order",{}).get("id","?")
    print(f"✅ {action} {qty} {sym} ID:{oid}"); return oid

# ── Auth ───────────────────────────────────────────────────────────────────────
def save_session(token):
    with open(SESSION_FILE,"w") as f: json.dump({"session_token":token},f)

def load_session():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f: return json.load(f).get("session_token")
    return None

def validate_session(token):
    """Returns True if the token is still valid, False if expired."""
    try:
        r=requests.get(f"{TT_BASE_URL}/customers/me",
            headers={"Content-Type":"application/json","Authorization":token}, timeout=10)
        return r.status_code==200
    except: return False

def try_auto_login():
    if not TT_USERNAME or not TT_PASSWORD: return None
    try:
        r=requests.post(f"{TT_BASE_URL}/sessions",
            json={"login":TT_USERNAME,"password":TT_PASSWORD,"remember-me":True},
            headers={"Content-Type":"application/json"}, timeout=15)
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

    print("📱 Device challenge — waiting via web UI...")
    auth["step"]="device_code"; auth["got_input"].clear()
    auth["got_input"].wait(timeout=600)
    device_code=auth["input"]; auth["input"]=None; auth["got_input"].clear()
    if not device_code: raise Exception("Timed out waiting for device code")

    r2=requests.post(f"{TT_BASE_URL}/device-challenge",json={"code":device_code},headers=challenge_headers)
    if r2.status_code not in (200,201): raise Exception(f"Device challenge failed:{r2.status_code} {r2.text}")
    print("✅ Device challenge accepted!")

    r2_data=r2.json().get("data",{})
    needs_otp="X-Tastyworks-OTP" in str(r2_data.get("redirect",{}).get("required-headers",[]))
    final_headers=dict(challenge_headers)
    if needs_otp:
        print("📱 2FA code required — waiting via web UI...")
        auth["step"]="otp_code"; auth["got_input"].clear()
        auth["got_input"].wait(timeout=300)
        otp=auth["input"]; auth["input"]=None; auth["got_input"].clear()
        if not otp: raise Exception("Timed out waiting for OTP")
        final_headers["X-Tastyworks-OTP"]=otp

    r3=requests.post(f"{TT_BASE_URL}/sessions",
        json={"login":TT_USERNAME,"password":TT_PASSWORD,"remember-me":True},headers=final_headers)
    if r3.status_code not in (200,201): raise Exception(f"Final login failed:{r3.status_code} {r3.text}")
    token=r3.json()["data"]["session-token"]
    save_session(token); auth["session_token"]=token; auth["step"]="done"
    print("✅ Fully authorized!"); auth["ready"].set()

# ── Web server + dashboard ─────────────────────────────────────────────────────
def dashboard_html(log):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades=[t for t in log.get("trades",[]) if t["timestamp"].startswith(today)]
    closed=[t for t in today_trades if t.get("closed")]
    ot=open_trade(log)
    pnl=sum(t.get("pnl_usd",0) for t in closed)
    wins=sum(1 for t in closed if t.get("pnl_usd",0)>0)
    losses=len(closed)-wins
    s=_state
    sc="#22c55e" if auth["step"]=="done" else "#ef4444"
    pc="#22c55e" if pnl>=0 else "#ef4444"
    rows=""
    for t in reversed(today_trades[-10:]):
        p=t.get("pnl_usd"); ep=t.get("price","?"); ex=t.get("exit_price","")
        pcol="#22c55e" if p and p>0 else ("#ef4444" if p and p<0 else "#94a3b8")
        pstr=f"<span style='color:{pcol}'>${p:+.2f}</span>" if p is not None else "<span style='color:#f59e0b'>Open</span>"
        rows+=f"<tr><td>{'L' if t['action']=='Buy' else 'S'}</td><td>{ep}</td><td>{ex or '—'}</td><td>{t.get('stop_pts','?')}/{t.get('target_pts','?')}</td><td>{pstr}</td><td>{t['timestamp'][11:16]}</td></tr>"

    open_card=""
    if ot:
        ep2=ot.get("price",0); upnl_est="—"
        if s["price"] and ep2:
            d=1 if ot["action"]=="Buy" else -1
            upnl_est=f"${(s['price']-ep2)*d*ot.get('qty',1)*5:+.2f}"
        open_card=f"""<div class="card" style="border:1px solid #f59e0b55">
        <div class="label">Open Trade</div>
        <div style="display:flex;justify-content:space-between;margin-top:10px">
          <span style="font-size:18px;font-weight:700">{'LONG' if ot['action']=='Buy' else 'SHORT'} {ot.get('qty',1)}ct @ {ep2}</span>
          <span style="font-size:18px;color:#f59e0b">{upnl_est}</span>
        </div>
        <div style="font-size:12px;color:#64748b;margin-top:4px">Stop: -{ot.get('stop_pts','?')}pts &nbsp; Target: +{ot.get('target_pts','?')}pts &nbsp; BE: {'Yes' if ot.get('breakeven_triggered') else 'No'}</div>
        </div>"""

    slope_arrow="" if s["slope"] is None else ("↑" if s["slope"]>0 else "↓")
    bias_color="#22c55e" if s["bias15m"]=="bull" else ("#ef4444" if s["bias15m"]=="bear" else "#94a3b8")

    return f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MES Bot</title><meta http-equiv="refresh" content="30">
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
h1{{font-size:18px;font-weight:700;margin-bottom:14px;color:#f8fafc}}</style></head>
<body>
<h1>📈 MES Bot</h1>
<div class="g2">
  <div class="card"><div class="label">Status</div>
    <div style="margin-top:8px"><span class="badge" style="background:{sc}22;color:{sc}">{'● Live' if auth['step']=='done' else '● Offline'}</span></div></div>
  <div class="card"><div class="label">Today P&L</div><div class="val" style="color:{pc}">${pnl:+.2f}</div></div>
  <div class="card"><div class="label">Trades</div><div class="val">{len(closed)} <span style="font-size:13px;color:#64748b">{wins}W / {losses}L</span></div></div>
  <div class="card"><div class="label">Equity</div><div class="val" style="font-size:18px">${f"{s['equity']:,.0f}" if s['equity'] else '—'}</div></div>
</div>
<div class="card">
  <div class="label" style="margin-bottom:12px">Market</div>
  <div class="g3">
    <div><div class="label">Price</div><div style="font-size:20px;font-weight:600">{f"{s['price']:.2f}" if s['price'] else '—'}</div></div>
    <div><div class="label">EMA(20) {slope_arrow}</div><div style="font-size:20px;font-weight:600">{f"{s['ema20']:.2f}" if s['ema20'] else '—'}</div></div>
    <div><div class="label">RSI(3)</div><div style="font-size:20px;font-weight:600">{f"{s['rsi3']:.1f}" if s['rsi3'] else '—'}</div></div>
    <div><div class="label">ATR(14)</div><div style="font-size:20px;font-weight:600">{f"{s['atr']:.2f}" if s['atr'] else '—'}</div></div>
    <div><div class="label">VWAP</div><div style="font-size:20px;font-weight:600">{f"{s['vwap']:.2f}" if s['vwap'] else '—'}</div></div>
    <div><div class="label">15m Bias</div><div style="font-size:20px;font-weight:600;color:{bias_color}">{(s['bias15m'] or '—').upper()}</div></div>
  </div>
</div>
{open_card}
<div class="card">
  <div class="label" style="margin-bottom:10px">Today's Trades</div>
  {'<table><thead><tr><th>Side</th><th>Entry</th><th>Exit</th><th>Stop/Tgt</th><th>P&L</th><th>Time</th></tr></thead><tbody>'+rows+'</tbody></table>' if today_trades else '<p style="color:#64748b;font-size:13px">No trades yet today</p>'}
</div>
<p style="text-align:center;color:#334155;font-size:11px;padding:8px">Refreshes every 30s · {now_et().strftime('%I:%M %p ET')}</p>
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
            log=load_log()
            html=dashboard_html(log)
        else:
            if step=="device_code":
                msg="<h2>Step 1 of 2: Device Verification</h2><p>Enter the SMS code Tastytrade sent to your phone:</p>"
            elif step=="otp_code":
                msg="<h2>Step 2 of 2: Two-Factor Auth</h2><p>Enter the 2FA code:</p>"
            else:
                msg="<h2>Tastytrade Bot</h2><p>Starting up...</p>"
            form=f"""<form method='POST' action='/code'>
                <input name='code' style='font-size:28px;width:160px;text-align:center' autofocus>
                <br><br><button type='submit' style='font-size:18px;padding:10px 30px'>Submit</button>
            </form>""" if step in ("device_code","otp_code") else ""
            html=f"<html><body style='font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center'>{msg}{form}</body></html>"

        self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        n=int(self.headers.get("Content-Length",0))
        body=self.rfile.read(n).decode()
        code=parse_qs(body).get("code",[""])[0].strip()
        if code: auth["input"]=code; auth["got_input"].set()
        self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers()
        self.wfile.write(b"<html><body style='font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center'><h2>Submitted!</h2><p><a href='/'>Check status</a></p></body></html>")

def start_web_server():
    s=HTTPServer(("0.0.0.0",PORT),Handler)
    threading.Thread(target=s.serve_forever,daemon=True).start()
    print(f"🌐 Web server on port {PORT}")

def start_session_refresh():
    """Proactively refresh TT session every 20h — before the ~24h expiry."""
    def _loop():
        while True:
            time.sleep(20*3600)
            if auth["step"]!="done": continue
            print("🔄 Proactive session refresh...")
            token=try_auto_login()
            if token: auth["session_token"]=token; print("✅ Session refreshed")
            else: print("⚠️ Proactive refresh failed")
    threading.Thread(target=_loop,daemon=True).start()

def start_eod_summary():
    """Send P&L summary SMS at 4:05 PM ET every trading day."""
    def _loop():
        while True:
            et=now_et()
            target=et.replace(hour=16,minute=5,second=0,microsecond=0)
            if et>=target: target+=timedelta(days=1)
            time.sleep((target-et).total_seconds())
            log=load_log()
            today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
            closed=[t for t in log["trades"] if t["timestamp"].startswith(today) and t.get("closed")]
            pnl=sum(t.get("pnl_usd",0) for t in closed)
            wins=sum(1 for t in closed if t.get("pnl_usd",0)>0)
            if closed:
                sms(f"📊 MES EOD: {wins}W/{len(closed)-wins}L | P&L: ${pnl:+.2f}")
            else:
                sms("📊 MES EOD: No trades today")
    threading.Thread(target=_loop,daemon=True).start()

# ── Core trading logic ─────────────────────────────────────────────────────────
def on_bar(bars, acct, sym):
    print(f"\n{'='*55}\n  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*55}")
    if not mkt(): print("🕐 Outside market hours"); return
    if avoid(): print("⏳ Open/close buffer"); return
    nn,nl=news()
    if nn: print(f"📰 News:{nl}"); return
    if len(bars)<ATR_PERIOD+EMA_PERIOD: print(f"⏳ Not enough bars ({len(bars)})"); return

    cl=[b.close for b in bars]; price=cl[-1]
    e20=ema(cl,EMA_PERIOD); r3=rsi(cl,RSI_PERIOD); at=atr(bars,ATR_PERIOD)
    sl=ema_slope(cl,EMA_PERIOD); vw=session_vwap(bars); b15=bias15m()
    av=avgvol(bars,20); lv=bars[-1].volume
    dp=abs((price-e20)/e20)*100 if e20 else 0
    sp=max(MIN_STOP_POINTS,min(MAX_STOP_POINTS,(at*ATR_STOP_MULT) if at else 4.0)); tp=sp*2

    # Update shared state for dashboard
    _state.update({"price":price,"ema20":e20,"rsi3":r3,"atr":at,"vwap":vw,
                   "bias15m":b15,"slope":sl,"last_bar":bars[-1].date})

    print(f"  {price:.2f} EMA:{e20:.2f}({dp:.1f}%,{'↑' if sl and sl>0 else '↓'}) RSI:{r3:.1f} ATR:{at:.2f}"
          if all([e20,r3,at]) else f"  Price:{price:.2f}")

    cur=get_mes_position(acct)
    if cur!=0:
        log=load_log(); ot=open_trade(log)
        ep=ot.get("price",price) if ot else price
        osp=ot.get("stop_pts",sp) if ot else sp
        otp=ot.get("target_pts",tp) if ot else tp
        be=ot.get("breakeven_triggered",False) if ot else False
        trail_high=ot.get("trail_high",ep) if ot else ep
        qty=ot.get("qty",abs(cur)) if ot else abs(cur)
        d=1 if cur>0 else -1; pts=(price-ep)*d; pnl=pts*qty*5
        print(f"  Open: {'L' if cur>0 else 'S'}{qty} @ {ep:.2f} P&L:${pnl:.2f}({pts:.2f}pts)")

        # Move stop up/down with trail once breakeven is triggered
        if TRAIL_POINTS>0 and be:
            new_best=max(trail_high,price) if cur>0 else min(trail_high,price)
            if new_best!=trail_high:
                if ot: ot["trail_high"]=new_best; save_log(log)
                trail_high=new_best
            trail_stop_pts=(trail_high-ep)*d-TRAIL_POINTS
            if trail_stop_pts>0: osp=trail_stop_pts

        partial_done=ot.get("partial_done",False) if ot else False

        # Partial take profit — close half at 1:1 R:R when qty > 1
        if not partial_done and qty>1 and pts>=osp:
            half=qty//2
            print(f"  🎯 Partial TP — closing {half}/{qty} at 1:1 (+{pts:.2f}pts)")
            try:
                place_order(acct,sym,"Sell" if cur>0 else "Buy",half,cur)
                partial_pnl=pts*half*5
                if ot:
                    ot["partial_done"]=True; ot["qty"]=qty-half
                    ot["breakeven_triggered"]=True; ot["trail_high"]=price
                    save_log(log)
                partial_done=True; be=True; qty=qty-half
                sms(f"🎯 MES PARTIAL +{pts:.1f}pts | Closed {half}ct @ {price:.2f} (${partial_pnl:+.2f}) | {qty}ct still open")
            except Exception as e: print(f"❌ Partial:{e}")

        if not be and pts>=otp/2:
            if ot: ot["breakeven_triggered"]=True; ot["trail_high"]=price; save_log(log)
            be=True; print("  🔒 Breakeven + trail active")

        eff=0.0 if be else osp; ex=None
        if cur>0:
            if e20 and price<e20: ex="Below EMA"
            elif pts<=-eff: ex=f"Stop({pts:.2f})"
            elif pts>=otp: ex=f"Target(+{pts:.2f})"
        else:
            if e20 and price>e20: ex="Above EMA"
            elif pts<=-eff: ex=f"Stop({pts:.2f})"
            elif pts>=otp: ex=f"Target(+{pts:.2f})"

        if ex:
            print(f"🔴 CLOSE — {ex}")
            try:
                place_order(acct,sym,"Sell" if cur>0 else "Buy",abs(cur),cur)
                if ot: ot.update({"closed":True,"exit_price":price,"exit_reason":ex,
                                   "pnl_usd":pnl,"exit_timestamp":datetime.now(timezone.utc).isoformat()})
                save_log(log)
                emoji="✅" if pnl>=0 else "🔴"
                sms(f"{emoji} MES CLOSED {ex} | {'L' if cur>0 else 'S'}{qty} @ {ep:.2f}→{price:.2f} | ${pnl:+.2f}")
            except Exception as e: print(f"❌ Close:{e}")
        else:
            trail_info=f" trail:{trail_high:.2f}-{TRAIL_POINTS}pts" if be and TRAIL_POINTS>0 else ""
            print(f"  ✅ Hold — stop:{'BE' if be else f'-{osp:.1f}'}{trail_info} target:+{otp:.1f}")
        return

    log=load_log()
    if todays_trades(log)>=MAX_TRADES_PER_DAY: print("🚫 Max trades"); return

    dpnl=todays_pnl(log)
    if dpnl<=-MAX_DAILY_LOSS:
        print(f"🛑 Daily loss limit (${dpnl:.2f})")
        sms(f"🛑 MES bot hit daily loss limit (${dpnl:.2f}). Done for today.")
        return

    if e20 is None or r3 is None: return

    if at and at<MIN_ATR:
        print(f"🚫 Chop: ATR {at:.2f} < {MIN_ATR}"); return

    bull=price>e20

    # EMA slope filter — only trade in direction EMA is pointing
    if sl is not None:
        slope_ok=(sl>0 and bull) or (sl<0 and not bull)
        if not slope_ok:
            print(f"🚫 EMA slope against trade direction ({sl:.3f})"); return

    # Second entry confirmation — prev candle must have touched EMA zone
    if not second_entry_confirmed(bars,e20,bull):
        print(f"🚫 No second entry: prev candle didn't touch EMA zone"); return

    checks=([(f"Above EMA",price>e20),(f"Within {EMA_PROXIMITY_PCT}%",dp<EMA_PROXIMITY_PCT),
              ("RSI<40",r3<40),("Above VWAP",vw is None or price>vw),
              ("15m bull",b15 is None or b15=="bull"),(f"Vol>={VOLUME_MULT}x",av==0 or lv>=av*VOLUME_MULT)]
            if bull else
            [(f"Below EMA",price<e20),(f"Within {EMA_PROXIMITY_PCT}%",dp<EMA_PROXIMITY_PCT),
              ("RSI>60",r3>60),("Below VWAP",vw is None or price<vw),
              ("15m bear",b15 is None or b15=="bear"),(f"Vol>={VOLUME_MULT}x",av==0 or lv>=av*VOLUME_MULT)])
    print(f"\n  {'LONG' if bull else 'SHORT'} checks:")
    ok=True
    for lb,ps in checks: print(f"  {'✅' if ps else '🚫'} {lb}"); ok=ok and ps
    if not ok: print("🚫 BLOCKED"); return

    side="Buy" if bull else "Sell"
    qty=calc_contracts(acct,sp)
    print(f"✅ ENTRY {side} {qty} MES @ ~{price:.2f}")
    try:
        oid=place_order(acct,sym,side,qty,0)
        log["trades"].append({"timestamp":datetime.now(timezone.utc).isoformat(),"action":side,
            "symbol":sym,"qty":qty,"price":price,"ema20":round(e20,2),"rsi3":round(r3,2),
            "atr":round(at,2) if at else None,"vwap":round(vw,2) if vw else None,"bias_15m":b15,
            "ema_slope":round(sl,4) if sl else None,"stop_pts":round(sp,2),"target_pts":round(tp,2),
            "breakeven_triggered":False,"trail_high":price,"closed":False,"order_id":oid,"order_placed":True})
        save_log(log)
        sms(f"{'📈' if side=='Buy' else '📉'} MES {'LONG' if side=='Buy' else 'SHORT'} {qty}ct @ {price:.2f} | Stop:-{sp:.1f}pts Target:+{tp:.1f}pts")
    except Exception as e: print(f"❌ Order:{e}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("="*55)
    print(f"  Tastytrade MES Bot — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Thomas Wade 4-Channel + TJR ICT | ATR Stops")
    print(f"  Risk:{RISK_PCT}%/trade  MaxLoss:${MAX_DAILY_LOSS}  MinATR:{MIN_ATR}  Trail:{TRAIL_POINTS}pts")
    print("="*55)
    start_web_server()
    start_session_refresh()
    start_eod_summary()

    saved=load_session() or TT_SESSION_TOKEN_ENV or None
    if saved:
        print("🔍 Validating saved session token...")
        if validate_session(saved):
            auth["session_token"]=saved; auth["step"]="done"; auth["ready"].set()
            print("✅ Session token valid")
        else:
            print("⚠️ Saved session expired — attempting auto re-login...")
            token=try_auto_login()
            if token:
                auth["session_token"]=token; auth["step"]="done"; auth["ready"].set()
            else:
                print(f"\n🔐 Visit: https://tastytrade-bot-production.up.railway.app")
                while True:
                    try: do_login(); break
                    except Exception as e:
                        print(f"❌ Login: {e} — retrying in 5 min")
                        time.sleep(300)
    else:
        token=try_auto_login()
        if token:
            auth["session_token"]=token; auth["step"]="done"; auth["ready"].set()
        else:
            print(f"\n🔐 Visit: https://tastytrade-bot-production.up.railway.app")
            while True:
                try: do_login(); break
                except Exception as e:
                    print(f"❌ Login: {e} — retrying in 5 min")
                    time.sleep(300)

    auth["ready"].wait()

    acct=None
    while not acct:
        try: acct=get_account(); print(f"✅ Account:{acct}")
        except Exception as e:
            print(f"❌ Account:{e} — retrying in 5 min"); time.sleep(300)

    sym=None
    while not sym:
        try: sym=get_mes_symbol()
        except Exception as e:
            print(f"❌ MES symbol:{e} — retrying in 5 min"); time.sleep(300)

    print(f"\n📡 Polling every 60s...\n")
    last=None

    while True:
        try:
            bars=fetch_bars(sym)
            if bars and len(bars)>=2:
                done=bars[:-1]; bt=done[-1].date
                # Stale data guard — don't trade off bars older than 2 candles (10 min)
                try:
                    bar_dt=datetime.fromisoformat(str(bt).replace(" ","T"))
                    if bar_dt.tzinfo is None: bar_dt=bar_dt.replace(tzinfo=timezone.utc)
                    age_min=(datetime.now(timezone.utc)-bar_dt).total_seconds()/60
                    if mkt() and age_min>10:
                        print(f"⚠️ Stale data: last bar {age_min:.0f}min old — skipping"); time.sleep(60); continue
                except Exception: pass
                if bt!=last: last=bt; on_bar(done,acct,sym)
                elif mkt(): print(f"⏳ {datetime.now().strftime('%H:%M:%S')} waiting...")
                else: print(f"🕐 {datetime.now().strftime('%H:%M:%S')} market closed")
            else: print("⚠️ No bar data")
        except Exception as e:
            print(f"❌ {e}")
            if "401" in str(e) or "invalid_session" in str(e).lower() or "unauthorized" in str(e).lower():
                print("🔄 Session expired — trying auto re-login...")
                token=try_auto_login()
                if token:
                    auth["session_token"]=token; auth["step"]="done"; auth["ready"].set()
                    sms("🔄 MES bot: session expired, auto re-login successful.")
                else:
                    auth["step"]="idle"; auth["ready"].clear()
                    sms(f"⚠️ MES bot: session expired. Visit https://tastytrade-bot-production.up.railway.app")
                    print("🔐 Auto re-login failed — visit web UI")
                    try: do_login()
                    except: time.sleep(300)
        time.sleep(60)

if __name__=="__main__":
    main()
