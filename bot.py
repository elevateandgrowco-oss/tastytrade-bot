"""
Tastytrade MES Futures Bot
Strategy: Thomas Wade 4-Channel Price Action + TJR ICT Concepts
Instrument: MES (Micro E-mini S&P 500) — $5/point

First run: visit Railway URL, enter SMS codes when prompted (2 codes).
After that: trades automatically. Re-auth only needed if session expires.
"""

import os, sys, json, time, threading, requests, yfinance as yf
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

TT_BASE_URL   = os.getenv("TT_BASE_URL", "https://api.tastytrade.com")
TT_USERNAME   = os.getenv("TT_USERNAME", "")
TT_PASSWORD   = os.getenv("TT_PASSWORD", "")
PORT          = int(os.getenv("PORT", "8080"))

TWILIO_SID    = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM   = os.getenv("TWILIO_PHONE", "")
ALERT_TO      = os.getenv("ALERT_PHONE", os.getenv("OWNER_PHONE", "+14017716184"))

# DATA_DIR — persists trades and session to Railway volume
DATA_DIR      = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
SESSION_FILE  = os.path.join(DATA_DIR, "session.json")
LOG_FILE      = os.path.join(DATA_DIR, "trades.json")

# Env-var fallback so session survives Railway restarts
TT_SESSION_TOKEN_ENV = os.getenv("TT_SESSION_TOKEN", "")

MAX_TRADES_PER_DAY=3; CONTRACTS=1; EMA_PERIOD=20; RSI_PERIOD=3
ATR_PERIOD=14; ATR_STOP_MULT=1.5; MIN_STOP_POINTS=2.0; MAX_STOP_POINTS=10.0
EMA_PROXIMITY_PCT=1.5; VOLUME_MULT=1.2; NEWS_BLOCK_MIN=15
AVOID_OPEN_MINUTES=15; AVOID_CLOSE_MINUTES=30
# Daily drawdown limit — stop trading if realized P&L < -$150 today
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "150"))
# Chop filter — skip entries when ATR is below this (sideways/low-vol market)
MIN_ATR = float(os.getenv("MIN_ATR", "3.0"))
# Trailing stop — trail by this many points once trade is in profit (0 = disabled)
TRAIL_POINTS = float(os.getenv("TRAIL_POINTS", "3.0"))
NEWS_TIMES_ET=[(8,30),(10,0),(14,0),(14,30)]

# Auth state machine
auth = {
    "step": "idle",         # idle | device_code | otp_code | done
    "session_token": None,
    "challenge_token": None,
    "challenge_headers": None,
    "input": None,
    "ready": threading.Event(),
    "got_input": threading.Event(),
}

class Bar:
    def __init__(self,date,open_,high,low,close,volume):
        self.date=date;self.open=open_;self.high=high;self.low=low;self.close=close;self.volume=volume

_bar_cache={"5m":{"bars":[],"ts":0},"15m":{"bars":[],"ts":0}}

# ── SMS alerts ─────────────────────────────────────────────────────────────────
def sms(msg):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, ALERT_TO]): return
    try:
        requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"From": TWILIO_FROM, "To": ALERT_TO, "Body": msg},
            timeout=10)
    except Exception as e:
        print(f"⚠️ SMS failed: {e}")

TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_API_KEY", "")

def fetch_bars_twelvedata(interval="5min", outputsize=100):
    """Fetch OHLCV bars from Twelve Data API — free, no Railway rate limits."""
    if not TWELVE_DATA_KEY: return []
    try:
        r=requests.get("https://api.twelvedata.com/time_series",
            params={"symbol":"MES","exchange":"CME","interval":interval,
                    "outputsize":outputsize,"apikey":TWELVE_DATA_KEY,"order":"ASC"})
        if r.status_code!=200: print(f"⚠️ TwelveData:{r.status_code}"); return []
        data=r.json()
        if data.get("status")=="error": print(f"⚠️ TwelveData:{data.get('message')}"); return []
        bars=[Bar(i["datetime"],float(i["open"]),float(i["high"]),float(i["low"]),
                  float(i["close"]),int(i.get("volume",0))) for i in data.get("values",[])]
        print(f"  📊 TwelveData: {len(bars)} bars"); return bars
    except Exception as e: print(f"⚠️ TwelveData:{e}"); return []

def fetch_bars(sym=None, interval="5m", period="2d"):
    global _bar_cache
    key = "15m" if interval=="15m" else "5m"
    ttl = 270 if key=="5m" else 600   # 15m bars stale after 10 min
    cache = _bar_cache[key]
    if cache["bars"] and time.time()-cache["ts"]<ttl:
        return cache["bars"]
    # Try Twelve Data first, fall back to yfinance
    td_interval="15min" if interval=="15m" else "5min"
    td_size=150 if interval=="15m" else 100
    bars=fetch_bars_twelvedata(td_interval, td_size)
    if not bars:
        try:
            df=yf.download("MES=F",period=period,interval=interval,progress=False,auto_adjust=True)
            if df.empty: df=yf.download("ES=F",period=period,interval=interval,progress=False,auto_adjust=True)
            if not df.empty:
                for ts,row in df.iterrows():
                    def _v(col):
                        v=row[col]; return float(v.iloc[0] if hasattr(v,"iloc") else v)
                    bars.append(Bar(str(ts),_v("Open"),_v("High"),_v("Low"),_v("Close"),int(_v("Volume"))))
        except Exception as e:
            print(f"⚠️ yfinance:{e}")
    if bars: _bar_cache[key]={"bars":bars,"ts":time.time()}
    elif cache["bars"]: print("  ↩️ Using cached bars"); return cache["bars"]
    return bars

# ── Web server ─────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass

    def do_GET(self):
        if self.path=="/token":
            tok=auth.get("session_token") or ""
            self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers()
            self.wfile.write(tok.encode()); return

        step=auth["step"]
        if step=="device_code":
            msg="<h2>Step 1 of 2: Device Verification</h2><p>Enter the SMS code Tastytrade sent to your phone:</p>"
        elif step=="otp_code":
            msg="<h2>Step 2 of 2: Two-Factor Authentication</h2><p>Enter the 2FA SMS code Tastytrade just sent:</p>"
        elif step=="done":
            msg="<h2>✅ Authorized — Bot is Trading</h2><p>MES futures — Thomas Wade strategy active.</p>"
        else:
            msg="<h2>Tastytrade Bot</h2><p>Starting up...</p>"

        form = f"""<form method='POST' action='/code'>
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
        if code:
            auth["input"]=code; auth["got_input"].set()
        self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers()
        self.wfile.write(b"<html><body style='font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center'><h2>Submitted! Processing...</h2><p><a href='/'>Check status</a></p></body></html>")

def start_web_server():
    s=HTTPServer(("0.0.0.0",PORT),Handler)
    threading.Thread(target=s.serve_forever,daemon=True).start()
    print(f"🌐 Web server on port {PORT}")

def start_session_refresh():
    """Proactively refresh TT session every 20h — before the ~24h expiry."""
    def _loop():
        while True:
            time.sleep(20 * 3600)
            if auth["step"] != "done": continue
            print("🔄 Proactive session refresh...")
            token = try_auto_login()
            if token:
                auth["session_token"] = token
                print("✅ Session refreshed proactively")
            else:
                print("⚠️ Proactive refresh failed — will retry on next cycle")
    threading.Thread(target=_loop, daemon=True).start()

# ── Auth flow ──────────────────────────────────────────────────────────────────
def save_session(token):
    with open(SESSION_FILE,"w") as f: json.dump({"session_token":token},f)

def load_session():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f: return json.load(f).get("session_token")
    return None

def try_auto_login():
    """Attempt silent re-login with credentials. Returns token on success, None on failure."""
    if not TT_USERNAME or not TT_PASSWORD: return None
    try:
        r=requests.post(f"{TT_BASE_URL}/sessions",
            json={"login":TT_USERNAME,"password":TT_PASSWORD,"remember-me":True},
            headers={"Content-Type":"application/json"}, timeout=15)
        if r.status_code in (200,201):
            token=r.json()["data"]["session-token"]
            save_session(token)
            print("✅ Auto re-login successful")
            return token
    except Exception as e:
        print(f"⚠️ Auto re-login failed: {e}")
    return None

def do_login():
    headers={"Content-Type":"application/json"}
    r=requests.post(f"{TT_BASE_URL}/sessions",json={"login":TT_USERNAME,"password":TT_PASSWORD,"remember-me":True},headers=headers)

    if r.status_code in (200,201):
        token=r.json()["data"]["session-token"]
        save_session(token); auth["session_token"]=token; auth["step"]="done"
        print("✅ Logged in — no device challenge needed"); auth["ready"].set(); return

    if r.status_code!=403: raise Exception(f"Login failed: {r.status_code} {r.text}")
    err=r.json().get("error",{})
    if err.get("code")!="device_challenge_required": raise Exception(f"Login error:{r.text}")

    challenge_token=r.headers.get("X-Tastyworks-Challenge-Token","")
    challenge_headers={"Content-Type":"application/json","X-Tastyworks-Challenge-Token":challenge_token}
    auth["challenge_token"]=challenge_token; auth["challenge_headers"]=challenge_headers

    print("📱 Device challenge required — waiting for code via web UI...")
    auth["step"]="device_code"; auth["got_input"].clear()
    auth["got_input"].wait(timeout=600)
    device_code=auth["input"]; auth["input"]=None; auth["got_input"].clear()

    if not device_code: raise Exception("Timed out waiting for device code")

    r2=requests.post(f"{TT_BASE_URL}/device-challenge",json={"code":device_code},headers=challenge_headers)
    if r2.status_code not in (200,201): raise Exception(f"Device challenge failed:{r2.status_code} {r2.text}")
    print("✅ Device challenge accepted!")

    # Check if 2FA OTP required
    r2_data=r2.json().get("data",{})
    needs_otp="X-Tastyworks-OTP" in str(r2_data.get("redirect",{}).get("required-headers",[]))

    final_headers=dict(challenge_headers)
    if needs_otp:
        print("📱 2FA code required — waiting for second code via web UI...")
        auth["step"]="otp_code"; auth["got_input"].clear()
        auth["got_input"].wait(timeout=300)
        otp=auth["input"]; auth["input"]=None; auth["got_input"].clear()
        if not otp: raise Exception("Timed out waiting for OTP")
        final_headers["X-Tastyworks-OTP"]=otp

    r3=requests.post(f"{TT_BASE_URL}/sessions",json={"login":TT_USERNAME,"password":TT_PASSWORD,"remember-me":True},headers=final_headers)
    if r3.status_code not in (200,201): raise Exception(f"Final login failed:{r3.status_code} {r3.text}")

    token=r3.json()["data"]["session-token"]
    save_session(token); auth["session_token"]=token; auth["step"]="done"
    print("✅ Fully authorized!"); auth["ready"].set()

def get_headers():
    return {"Content-Type":"application/json","Authorization":auth["session_token"]}

# ── Tastytrade API ─────────────────────────────────────────────────────────────
def get_account():
    r=requests.get(f"{TT_BASE_URL}/customers/me/accounts",headers=get_headers()); r.raise_for_status()
    accounts=r.json()["data"]["items"]
    if not accounts: raise Exception("No accounts found — account may still be pending setup")
    for a in accounts:
        if a["account"].get("margin-or-cash")=="Margin": return a["account"]["account-number"]
    return accounts[0]["account"]["account-number"]

def get_mes_position(acct):
    r=requests.get(f"{TT_BASE_URL}/accounts/{acct}/positions",headers=get_headers()); r.raise_for_status()
    for p in r.json()["data"]["items"]:
        if "MES" in p.get("symbol",""):
            qty=int(p.get("quantity",0))
            return qty if p.get("quantity-direction","Long")=="Long" else -qty
    return 0

def get_mes_symbol():
    r=requests.get(f"{TT_BASE_URL}/instruments/futures",params={"product-code":"MES"},headers=get_headers()); r.raise_for_status()
    items=r.json()["data"]["items"]; today=str(datetime.now(timezone.utc).date())
    active=[i for i in items if i.get("expiration-date","9999")>=today]
    if not active: active=items
    active.sort(key=lambda x:x.get("expiration-date","9999"))
    sym=active[0]["symbol"]; print(f"  MES:{sym} exp:{active[0].get('expiration-date')}"); return sym

def place_order(acct,sym,side,qty,cur_pos):
    action=("Buy to Close" if side=="Buy" else "Sell to Close") if cur_pos!=0 else ("Buy to Open" if side=="Buy" else "Sell to Open")
    r=requests.post(f"{TT_BASE_URL}/accounts/{acct}/orders",json={"time-in-force":"Day","order-type":"Market","legs":[{"instrument-type":"Future","symbol":sym,"quantity":qty,"action":action}]},headers=get_headers())
    if r.status_code not in (200,201): raise Exception(f"Order failed:{r.status_code} {r.text}")
    oid=r.json()["data"].get("order",{}).get("id","?"); print(f"✅ {action} {qty} {sym} ID:{oid}"); return oid

# ── Indicators ─────────────────────────────────────────────────────────────────
def ema(c,p):
    if len(c)<p: return None
    m=2/(p+1); e=sum(c[:p])/p
    for x in c[p:]: e=x*m+e*(1-m)
    return e

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
    trs=[max(bars[i].high-bars[i].low,abs(bars[i].high-bars[i-1].close),abs(bars[i].low-bars[i-1].close)) for i in range(1,len(bars))]
    return sum(trs[-p:])/p

def session_vwap(bars):
    """VWAP anchored to today's 9:30 AM ET session open — resets each day."""
    now_et_date = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
    session_open = now_et_date + " 09:30"
    session_bars = [b for b in bars if str(b.date)[:16] >= session_open]
    if not session_bars: return None
    cv=cv2=0
    for b in session_bars: tp=(b.high+b.low+b.close)/3; cv+=tp*b.volume; cv2+=b.volume
    return cv/cv2 if cv2 else None

def avgvol(bars,p=20):
    v=[b.volume for b in bars[-p:] if b.volume>0]; return sum(v)/len(v) if v else 0

def bias15m(sym=None):
    try:
        bars=fetch_bars(sym,"15m","5d")
        if len(bars)<EMA_PERIOD: return None
        c=[b.close for b in bars]; e=ema(c,EMA_PERIOD)
        return "bull" if c[-1]>e else "bear"
    except: return None

def mkt(): now=datetime.now(timezone.utc);m=now.hour*60+now.minute;return(13*60+35)<=m<=(19*60+55)
def avoid(): now=datetime.now(timezone.utc);m=now.hour*60+now.minute;return m<(13*60+30)+AVOID_OPEN_MINUTES or m>(20*60)-AVOID_CLOSE_MINUTES
def news():
    now=datetime.now(timezone.utc);et=((now.hour-4)%24)*60+now.minute
    for h,m in NEWS_TIMES_ET:
        if abs(et-(h*60+m))<=NEWS_BLOCK_MIN: return True,f"{h:02d}:{m:02d}ET"
    return False,None

def load_log():
    if not os.path.exists(LOG_FILE): return {"trades":[]}
    with open(LOG_FILE) as f: return json.load(f)
def save_log(log):
    with open(LOG_FILE,"w") as f: json.dump(log,f,indent=2)
def todays_trades(log):
    t=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for x in log["trades"] if x["timestamp"].startswith(t) and x.get("order_placed"))
def todays_pnl(log):
    """Sum of realized P&L on closed trades today."""
    t=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(x.get("pnl_usd",0) for x in log["trades"] if x["timestamp"].startswith(t) and x.get("closed"))
def open_trade(log):
    for t in reversed(log["trades"]):
        if t.get("order_placed") and not t.get("closed"): return t
    return None

def on_bar(bars,acct,sym):
    print(f"\n{'='*55}\n  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*55}")
    if not mkt(): print("🕐 Outside market hours"); return
    if avoid(): print("⏳ Open/close buffer"); return
    nn,nl=news()
    if nn: print(f"📰 News:{nl}"); return
    if len(bars)<ATR_PERIOD+EMA_PERIOD: print(f"⏳ Not enough bars ({len(bars)})"); return

    cl=[b.close for b in bars]; price=cl[-1]
    e20=ema(cl,EMA_PERIOD); r3=rsi(cl,RSI_PERIOD); at=atr(bars,ATR_PERIOD)
    vw=session_vwap(bars); b15=bias15m(); av=avgvol(bars,20); lv=bars[-1].volume
    dp=abs((price-e20)/e20)*100 if e20 else 0
    sp=max(MIN_STOP_POINTS,min(MAX_STOP_POINTS,(at*ATR_STOP_MULT) if at else 4.0)); tp=sp*2

    print(f"  {price:.2f} EMA:{e20:.2f}({dp:.1f}%) RSI:{r3:.1f} ATR:{at:.2f}" if all([e20,r3,at]) else f"  Price:{price:.2f}")

    cur=get_mes_position(acct)
    if cur!=0:
        log=load_log(); ot=open_trade(log)
        ep=ot.get("price",price) if ot else price
        osp=ot.get("stop_pts",sp) if ot else sp; otp=ot.get("target_pts",tp) if ot else tp
        be=ot.get("breakeven_triggered",False) if ot else False
        trail_high=ot.get("trail_high",ep) if ot else ep   # best price seen since entry
        d=1 if cur>0 else -1; pts=(price-ep)*d; pnl=pts*abs(cur)*5
        print(f"  Open: {'L' if cur>0 else 'S'}{abs(cur)} @ {ep:.2f} P&L:${pnl:.2f}({pts:.2f}pts)")

        # Update trail high/low and move stop up (longs) or down (shorts)
        if TRAIL_POINTS > 0 and be:
            new_best = max(trail_high, price) if cur>0 else min(trail_high, price)
            if new_best != trail_high:
                if ot: ot["trail_high"]=new_best; save_log(log)
                trail_high=new_best
            trail_stop_pts = (trail_high - ep) * d - TRAIL_POINTS
            osp = max(0.0, trail_stop_pts) if trail_stop_pts > 0 else osp

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
                if ot: ot.update({"closed":True,"exit_price":price,"exit_reason":ex,"pnl_usd":pnl,"exit_timestamp":datetime.now(timezone.utc).isoformat()}); save_log(log)
                emoji="✅" if pnl>=0 else "🔴"
                sms(f"{emoji} MES CLOSED {ex} | {'L' if cur>0 else 'S'} @ {ep:.2f} → {price:.2f} | P&L: ${pnl:+.2f}")
            except Exception as e: print(f"❌ Close:{e}")
        else: print(f"  ✅ Hold — stop:{'BE' if be else f'-{osp}'} target:+{otp}")
        return

    log=load_log()
    if todays_trades(log)>=MAX_TRADES_PER_DAY: print("🚫 Max trades"); return

    # Daily drawdown circuit breaker
    dpnl=todays_pnl(log)
    if dpnl<=-MAX_DAILY_LOSS:
        print(f"🛑 Daily loss limit hit (${dpnl:.2f}) — no new trades today")
        sms(f"🛑 MES bot hit daily loss limit (${dpnl:.2f}). Done trading for today.")
        return

    if e20 is None or r3 is None: return

    # Chop filter — skip if market is too quiet
    if at and at < MIN_ATR:
        print(f"🚫 Chop filter: ATR {at:.2f} < {MIN_ATR} — market too quiet"); return

    bull=price>e20
    checks=([(f"Above EMA",price>e20),(f"Within {EMA_PROXIMITY_PCT}%",dp<EMA_PROXIMITY_PCT),("RSI<40",r3<40),("Above VWAP",vw is None or price>vw),("15m bull",b15 is None or b15=="bull"),(f"Vol>={VOLUME_MULT}x",av==0 or lv>=av*VOLUME_MULT)]
             if bull else [(f"Below EMA",price<e20),(f"Within {EMA_PROXIMITY_PCT}%",dp<EMA_PROXIMITY_PCT),("RSI>60",r3>60),("Below VWAP",vw is None or price<vw),("15m bear",b15 is None or b15=="bear"),(f"Vol>={VOLUME_MULT}x",av==0 or lv>=av*VOLUME_MULT)])
    print(f"\n  {'LONG' if bull else 'SHORT'} checks:")
    ok=True
    for lb,ps in checks: print(f"  {'✅' if ps else '🚫'} {lb}"); ok=ok and ps
    if not ok: print("🚫 BLOCKED"); return
    side="Buy" if bull else "Sell"
    print(f"✅ ENTRY {side} {CONTRACTS} MES @ ~{price:.2f}")
    try:
        oid=place_order(acct,sym,side,CONTRACTS,0)
        log["trades"].append({"timestamp":datetime.now(timezone.utc).isoformat(),"action":side,"symbol":sym,"qty":CONTRACTS,"price":price,"ema20":round(e20,2),"rsi3":round(r3,2),"atr":round(at,2) if at else None,"vwap":round(vw,2) if vw else None,"bias_15m":b15,"stop_pts":round(sp,2),"target_pts":round(tp,2),"breakeven_triggered":False,"closed":False,"order_id":oid,"order_placed":True})
        save_log(log)
        sms(f"{'📈' if side=='Buy' else '📉'} MES {'LONG' if side=='Buy' else 'SHORT'} @ {price:.2f} | Stop: -{sp:.1f}pts Target: +{tp:.1f}pts")
    except Exception as e: print(f"❌ Order:{e}")

def main():
    print("="*55)
    print(f"  Tastytrade MES Bot — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Thomas Wade 4-Channel + TJR ICT | ATR Stops")
    print(f"  Daily loss limit: ${MAX_DAILY_LOSS}")
    print("="*55)
    start_web_server()
    start_session_refresh()

    # Try saved session → env var → auto re-login → web UI
    saved=load_session() or TT_SESSION_TOKEN_ENV or None
    if saved:
        auth["session_token"]=saved; auth["step"]="done"; auth["ready"].set()
        print("✅ Using saved session token")
    else:
        # Try silent login first before falling back to web UI
        token=try_auto_login()
        if token:
            auth["session_token"]=token; auth["step"]="done"; auth["ready"].set()
        else:
            print(f"\n🔐 Visit: https://tastytrade-bot-production.up.railway.app")
            while True:
                try: do_login(); break
                except Exception as e:
                    print(f"❌ Login failed: {e}")
                    print("⏳ Waiting 5 min before retry...")
                    time.sleep(300)

    auth["ready"].wait()

    acct=None
    while not acct:
        try: acct=get_account(); print(f"✅ Account:{acct}")
        except Exception as e:
            print(f"❌ Account:{e} — retrying in 5 min (account may still be activating)")
            time.sleep(300)

    sym=None
    while not sym:
        try: sym=get_mes_symbol()
        except Exception as e:
            print(f"❌ MES symbol:{e} — retrying in 5 min")
            time.sleep(300)

    print(f"\n📡 Polling every 60s...\n")
    last=None

    while True:
        try:
            bars=fetch_bars(sym)
            if bars and len(bars)>=2:
                done=bars[:-1]; bt=done[-1].date
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
                    sms(f"⚠️ MES bot: session expired, auto re-login failed. Visit https://tastytrade-bot-production.up.railway.app to re-authorize.")
                    print("🔐 Auto re-login failed — visit web UI to re-authorize")
                    try: do_login()
                    except: time.sleep(300)
        time.sleep(60)

if __name__=="__main__":
    main()
